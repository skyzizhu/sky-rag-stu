#!/usr/bin/env python3
"""下载本地精排模型（bge-reranker-v2-m3 ONNX 量化版）到 models/ 目录。

用法：
    python scripts/download_rerank_model.py

- 从 HF-Mirror（国内可达）下载 onnx-community/bge-reranker-v2-m3-ONNX 的
  model_quantized.onnx（约 570MB）与全套分词器文件；
- 已存在且大小一致时跳过（可重复运行）；
- 下载完成后在 .env 设 RERANK_BACKEND=local_onnx 即启用本地精排。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_ROOT / "models" / "bge-reranker-v2-m3-onnx"
MIRROR_BASE = "https://hf-mirror.com/onnx-community/bge-reranker-v2-m3-ONNX/resolve/main"

FILES = [
    ("onnx/model_quantized.onnx", "model_quantized.onnx"),
    ("tokenizer.json", "tokenizer.json"),
    ("tokenizer_config.json", "tokenizer_config.json"),
    ("special_tokens_map.json", "special_tokens_map.json"),
    ("config.json", "config.json"),
]


def main() -> int:
    import requests

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"目标目录：{MODEL_DIR}")
    failed: list[str] = []
    for remote, local_name in FILES:
        local = MODEL_DIR / local_name
        if local.exists() and local.stat().st_size > 0:
            print(f"  ✓ 已存在，跳过：{local_name}")
            continue
        print(f"  ↓ 下载 {local_name} …")
        try:
            resp = requests.get(f"{MIRROR_BASE}/{remote}", timeout=(15, 300), stream=True)
            resp.raise_for_status()
            with open(local, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            print(f"  ✓ 完成：{local.stat().st_size} 字节")
        except Exception as exc:
            failed.append(local_name)
            print(f"  ✗ 失败：{exc}")

    if failed:
        print(f"\n❌ 以下文件下载失败，请重跑本脚本：{', '.join(failed)}")
        return 1
    print("\n✅ 本地精排模型就绪。在 .env 设 RERANK_BACKEND=local_onnx 即启用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
