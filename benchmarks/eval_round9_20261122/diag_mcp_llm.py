#!/usr/bin/env python3
"""R9 续跑前诊断：①解开 MCP 客户端 ExceptionGroup 的真实子异常；②探测 LLM 余额是否恢复。"""
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

print("=" * 30, "① LLM 余额探测", "=" * 30)
try:
    from src.config import get_config
    from openai import OpenAI
    cfg = get_config()
    print("base_url:", cfg.llm_base_url, "| model:", cfg.llm_model,
          "| key:", (cfg.llm_api_key or "")[:8] + "...")
    client = OpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, timeout=20)
    resp = client.chat.completions.create(
        model=cfg.llm_model,
        messages=[{"role": "user", "content": "回复一个字：好"}],
        max_tokens=8,
    )
    print("LLM OK →", resp.choices[0].message.content)
except Exception as exc:
    print(f"LLM FAIL → {type(exc).__name__}: {str(exc)[:300]}")

print("=" * 30, "② MCP 子异常解开", "=" * 30)
from src import auth_db  # noqa: E402

pat = auth_db.create_token(auth_db.get_user("liqiang")["id"], "R9诊断临时令牌")
try:
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _probe():
        async with streamablehttp_client(
                "http://127.0.0.1:8760/mcp",
                headers={"Authorization": f"Bearer {pat}"}) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                result = await sess.call_tool("knowledge_search",
                                              {"question": "凤凰项目里程碑", "top_k": 5})
                text = next(b.text for b in result.content if b.type == "text")
                data = json.loads(text)
                return [m.get("source") or "" for m in data.get("matches", [])]

    try:
        srcs = anyio.run(_probe)
        print("MCP OK →", srcs[:3])
    except BaseException as exc:
        print(f"MCP FAIL 顶层: {type(exc).__name__}: {exc}")
        traceback.print_exc()
finally:
    prefix = pat[:16]
    n = auth_db.revoke_token(prefix)
    print(f"诊断令牌已吊销 prefix={prefix} revoked={n}")
