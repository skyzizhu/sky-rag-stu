#!/usr/bin/env python3
"""V3.7 存量迁移：给全部已有知识卡片补 allowed_groups 字段。

背景：V3.6 之前入库的卡片 payload 没有 allowed_groups 字段，而 Qdrant 的
MatchAny 过滤对"字段缺失"的点是"不匹配"——不迁移的话，开启权限过滤后
旧卡片会全部消失。

做法：
  1. 为 allowed_groups 建 KEYWORD 索引（幂等）；
  2. 用 must_not 过滤找出"没有该字段"的卡片，分批 set_payload ["all"]（全员可见）。

用法：python scripts/migrate_groups.py [--dry-run]
幂等：已迁移过的库再跑一遍，找不到缺失卡片，直接结束。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdrant_client import models

from src.config import get_config
from src.vector_store import get_vector_store


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐存量卡片的 allowed_groups 字段")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = parser.parse_args()

    cfg = get_config()
    store = get_vector_store()
    if not store.collection_exists():
        print("集合不存在（还没入过库），无需迁移。")
        return 0

    # 1. 建索引（create_payload_index 幂等）
    print(f"为 allowed_groups 建立 KEYWORD 索引（{cfg.qdrant_collection}）…")
    store.client.create_payload_index(
        collection_name=cfg.qdrant_collection,
        field_name="allowed_groups",
        field_schema=models.PayloadSchemaType.KEYWORD,
        wait=True,
    )

    # 2. 扫描全部卡片，找出没有 allowed_groups 字段的点，分批补 ["all"]
    #    （字段缺失检查在内存里做：payload.get("allowed_groups") 为空即缺失）
    total = store.count()
    print(f"集合共 {total} 张卡片，开始扫描缺失字段…")

    fixed, scanned, offset = 0, 0, None
    while True:
        points, offset = store.client.scroll(
            collection_name=cfg.qdrant_collection,
            limit=256,
            offset=offset,
            with_payload=["allowed_groups"],
        )
        batch_ids = []
        for point in points:
            scanned += 1
            payload = point.payload or {}
            if not payload.get("allowed_groups"):
                batch_ids.append(point.id)
        if batch_ids and not args.dry_run:
            store.client.set_payload(
                collection_name=cfg.qdrant_collection,
                payload={"allowed_groups": ["all"]},
                points=batch_ids,
                wait=True,
            )
        fixed += len(batch_ids)
        print(f"  已扫描 {scanned}/{total}，本批补齐 {len(batch_ids)} 张")
        if offset is None:
            break

    action = "需要补齐" if args.dry_run else "已补齐"
    print(f"\n完成：扫描 {scanned} 张，{action} {fixed} 张（allowed_groups=['all']）。")
    if args.dry_run and fixed:
        print("去掉 --dry-run 重新运行即可执行迁移。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
