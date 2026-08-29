#!/usr/bin/env python3
"""入库入口：把 knowledge/ 目录（或指定文件）送进知识库。

用法：
    python ingest.py                  # 入库 knowledge/ 下所有支持的文件
    python ingest.py 路径/文件.md     # 只入库指定文件（可多个）
    python ingest.py --rebuild        # 清空向量库后全量重建
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.pipeline import ingest_files


def main() -> int:
    parser = argparse.ArgumentParser(description="个人知识库 RAG —— 文档入库")
    parser.add_argument("paths", nargs="*", help="要入库的文件路径（默认扫描整个 knowledge/ 目录）")
    parser.add_argument("--rebuild", action="store_true", help="先清空向量库，再全量重建")
    parser.add_argument("--yes", action="store_true", help="重建时跳过确认")
    args = parser.parse_args()

    if args.rebuild and not args.yes:
        answer = input("⚠️ 将清空向量库里的全部数据并重建，确认请输入 y：").strip().lower()
        if answer != "y":
            print("已取消。")
            return 1

    paths = [Path(p) for p in args.paths]
    summary = ingest_files(paths=paths or None, rebuild=args.rebuild)

    if summary.ok_files == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
