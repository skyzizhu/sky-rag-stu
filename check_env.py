#!/usr/bin/env python3
"""环境自检：搭建完环境后先跑这个，哪里不对马上告诉你怎么修。

用法：
    python check_env.py               # 检查基础环境（不花 API 钱）
    python check_env.py --with-llm    # 顺带真实调用一次大模型（验证 Key）
"""

from __future__ import annotations

import argparse
import sys

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
results: list[tuple[str, str, str]] = []  # (状态, 项目, 说明)


def record(status: str, name: str, note: str = "") -> bool:
    results.append((status, name, note))
    return status != FAIL


def main() -> int:
    parser = argparse.ArgumentParser(description="个人知识库 RAG —— 环境自检")
    parser.add_argument("--with-llm", action="store_true", help="真实调用一次大模型验证 API Key")
    args = parser.parse_args()

    print("=" * 62)
    print("个人知识库 RAG —— 环境自检")
    print("=" * 62)

    # 1. Python 版本
    version = sys.version_info
    record(
        OK if version >= (3, 10) else FAIL,
        f"Python 版本 {version.major}.{version.minor}",
        "需要 3.10 及以上" if version < (3, 10) else "",
    )

    # 2. 依赖包
    missing = []
    for module in ("streamlit", "openai", "qdrant_client", "pypdf", "docx", "bs4",
                   "striprtf", "dotenv", "requests"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    record(OK if not missing else FAIL, "依赖包安装", f"缺少：{', '.join(missing)}（请运行 pip install -r requirements.txt）" if missing else "")

    # 3. .env
    from pathlib import Path

    env_path = Path(__file__).parent / ".env"
    record(OK if env_path.exists() else FAIL, ".env 配置文件",
           "不存在（请运行 cp .env.example .env）" if not env_path.exists() else "")

    # 4. 知识目录
    from src.config import get_config

    cfg = get_config()
    knowledge_files = (
        [p for p in cfg.knowledge_dir.rglob("*") if p.is_file() and not p.name.startswith(".")
         and p.suffix.lower() in {".txt", ".md", ".rtf", ".html", ".htm", ".docx", ".pdf"}]
        if cfg.knowledge_dir.exists() else []
    )
    record(
        OK if (cfg.knowledge_dir.exists() and knowledge_files) else FAIL,
        f"知识目录 {cfg.knowledge_dir.name}/",
        f"{len(knowledge_files)} 个待入库文件" if knowledge_files else "目录不存在或没有可入库的文件",
    )

    # 5. Ollama + 向量化模型
    try:
        from src.embedding import get_embedding_client

        client = get_embedding_client()
        if client.model_available():
            dim = client.dimension()
            record(OK, f"Ollama 向量化模型 {cfg.embedding_model}", f"向量维度 {dim}")
        else:
            record(FAIL, "Ollama 向量化模型", f"已连上 Ollama，但没有 {cfg.embedding_model}（运行 ollama pull {cfg.embedding_model}）")
    except Exception as exc:
        record(FAIL, "Ollama 服务", str(exc))

    # 6. Qdrant
    try:
        import requests as _requests

        resp = _requests.get(f"{cfg.qdrant_url}/", timeout=5)
        version = resp.json().get("version", "?")
        from src.vector_store import get_vector_store

        store = get_vector_store()
        count = store.count()
        record(OK, f"Qdrant 向量数据库（v{version}）",
               f"已存 {count} 张知识卡片" if count else "运行正常，还没有数据（先入库）")
    except Exception as exc:
        record(FAIL, "Qdrant 向量数据库", f"{exc}（先启动 Qdrant：bash scripts/start_qdrant.sh）")

    # 7. LLM 配置
    if cfg.llm_api_key and cfg.llm_base_url and cfg.llm_model:
        record(OK, f"LLM 配置（{cfg.llm_model}）")
        if args.with_llm:
            try:
                from src.llm import get_llm_client
                from src.prompt import SYSTEM_PROMPT

                answer, elapsed = get_llm_client().chat(
                    [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": "请只回复两个字：正常"}]
                )
                record(OK if "正常" in answer else WARN, "LLM 真实调用测试", f"回复：{answer[:50]}，耗时 {elapsed:.1f} 秒")
            except Exception as exc:
                record(FAIL, "LLM 真实调用测试", str(exc))
    else:
        record(WARN, "LLM 配置", "还没配置。在 .env 里填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 三项（填好前只能检索不能生成答案）")

    # 汇总
    print()
    for status, name, note in results:
        line = f"{status} {name}"
        if note:
            line += f" —— {note}"
        print(line)
    print()

    if any(status == FAIL for status, _, _ in results):
        print("结论：❌ 有未通过的项，按上面的提示修复后重新运行本命令。")
        return 1
    if any(status == WARN for status, _, _ in results):
        print("结论：⚠️ 环境基本可用，但有需要注意的项（见上）。")
        return 0
    print("结论：✅ 环境全部就绪！可以运行 python ingest.py 入库，然后 streamlit run app.py 开始使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
