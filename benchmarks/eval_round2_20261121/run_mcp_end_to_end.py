"""Run a hosted tool-calling Agent against isolated, authenticated MCP HTTP."""
from __future__ import annotations

import functools
import json
import re
import socket
import sys
import time
from contextlib import asynccontextmanager

import anyio
import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from run_isolated import HERE, build_corpus, isolated
from run_mcp_context_audit import ENGINEERING, FOLLOWUP, MARKET, NEBULA, TIANSHU, history
from src import auth_db, domains, mcp_server, pipeline, projects


SCOPED_AGENT = "--scoped-agent" in sys.argv
FRESH_EVIDENCE = "--fresh-evidence" in sys.argv
ONLY_CASES = {name for arg in sys.argv[1:] if arg.startswith("--cases=")
              for name in arg.split("=", 1)[1].split(",")}
HOST_CONTEXT_POLICY = (
    "单项目问题每次调用都要用 project 传用户最近明确选定的完整项目名称或 ID，"
    "同时把 question 改写成可独立检索的当前问题。用户更正后，使用最新项目，并且只回答当前问题。"
    "单数代词若可能指多个项目，先向用户澄清，再调用工具；用户明确要求对比多个项目时才分别检索。"
    "遇到项目 conflict 先用最新确认的项目修正 project 后重试。"
    "项目查询无匹配证据时说明无法确认当前项目，保持原项目范围。"
    "预算和引用都必须来自当前项目的本轮证据，其他项目提及的事实和旧轮次引用不能代用。"
)
FRESH_EVIDENCE_POLICY = (
    "仅声明接下来关注某项目时，确认切换即可。回答具体知识库问题时，本轮必须重新检索。"
    "查无当前项目的证据时，只说明当前可见资料无法确认，省略其他项目的数字和引用。"
)


@asynccontextmanager
async def local_server():
    app = mcp_server._TokenAuthMiddleware(mcp_server.mcp.streamable_http_app())
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(functools.partial(server.serve, sockets=[listener]))
            try:
                with anyio.fail_after(15):
                    while not server.started:
                        await anyio.sleep(0.02)
                yield f"http://127.0.0.1:{port}/mcp"
            finally:
                server.should_exit = True


@asynccontextmanager
async def authenticated_client(url, username):
    token = auth_db.create_token(auth_db.get_user(username)["id"], "synthetic-e2e")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False,
                                 timeout=httpx.Timeout(15, read=60)) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def latest_log(username):
    rows = auth_db.list_qa_logs(limit=1, username=username, entry="mcp_http")
    if not rows:
        return {}
    row = rows[0]
    return {"id": row["id"], "entry": row["entry"], "identity": row["username"],
            "context": row["mcp_context"], "matched_sources": [source["source"] for source in row["sources"]]}


async def agent_turn(session, llm, tool, messages, username, *, forced=None):
    calls = []
    citations = {}
    for _ in range(5):
        request = functools.partial(
            llm.client.chat.completions.create, model=llm.cfg.llm_model, messages=messages,
            tools=[tool], tool_choice="auto", temperature=0, max_tokens=800)
        response = await anyio.to_thread.run_sync(request)
        reply = response.choices[0].message
        outgoing = [item.model_dump(include={"id", "type", "function"}) for item in reply.tool_calls or []]
        message = {"role": "assistant", "content": reply.content or ""}
        # The hosted provider requires its own reasoning payload on a tool continuation.
        # Keep it in the provider transcript only; never include it in audit artifacts.
        if outgoing and (reasoning := getattr(reply, "reasoning_content", None)):
            message["reasoning_content"] = reasoning
        if outgoing and forced is not None:
            outgoing = outgoing[:1]
            outgoing[0]["function"]["arguments"] = json.dumps(forced, ensure_ascii=False)
            forced = None
        if outgoing:
            message["tool_calls"] = outgoing
        messages.append(message)
        if not outgoing:
            return {"answer": message["content"], "calls": calls, "citations": citations}
        for call in outgoing:
            arguments = json.loads(call["function"]["arguments"])
            previous = latest_log(username).get("id", 0)
            returned = await session.call_tool("knowledge_search", arguments)
            body = json.loads(next(item.text for item in returned.content if item.type == "text"))
            log = latest_log(username)
            if log.get("id", 0) <= previous:
                log = {}
            calls.append({
                "arguments": arguments, "log": log, "error": body.get("error"),
                "refused": body.get("refused"), "issue": (body.get("coverage_details") or {}).get("scope_issue"),
                "project_ids": (body.get("filters_applied") or {}).get("project_id", []),
                "matches": [{key: item.get(key) for key in ("source", "project_id", "version", "page")}
                            for item in body.get("matches", [])],
            })
            for item in body.get("matches", []):
                rank = len(citations) + 1
                item["rank"] = rank
                citations[rank] = {key: item.get(key) for key in ("source", "project_id", "text")}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(body, ensure_ascii=False)})
    return {"answer": "", "calls": calls, "citations": citations, "error": "tool_loop_limit"}


def grade(turn, expected, amount, project_ids):
    answer = turn["answer"]
    calls = turn["calls"]
    matches = [item for call in calls for item in call["matches"]]
    refs = {int(value) for value in re.findall(r"\[(\d+)\]", answer)}
    cited = [turn["citations"][number] for number in refs if number in turn["citations"]]
    checks = {"audit": all(call["log"] for call in calls)}
    if expected == "ambiguous":
        checks.update(clarified=bool(re.search(r"哪个|哪一个|哪個|哪一個|请(?:您)?(?:明确|确认|指定)|指的是", answer)),
                      did_not_pick_project=not matches,
                      no_budget_assertion=not re.search(r"\d+\s*万(?:元)?", answer))
    elif expected == "denied":
        checks.update(executed=bool(calls), no_matches=not matches,
                      attempted_expected_project=any(call["project_ids"] == [project_ids[NEBULA]]
                                                     and call["refused"] for call in calls),
                      no_budget_assertion=not re.search(r"\d+\s*万(?:元)?", answer),
                      disclosed_gap=bool(re.search(r"权限|可见|没有找到|未找到|无法确认|无法查|暂无|无法给出|没有可确认", answer)))
    else:
        pid = project_ids[expected]
        checks.update(amount=bool(re.search(rf"(?<!\d){amount}\s*万(?:元)?", answer)),
                      only_expected_project=bool(matches) and all(item["project_id"] == pid for item in matches),
                      supported_citation=bool(refs) and len(cited) == len(refs)
                      and all(item["project_id"] == pid for item in cited)
                      and any(re.search(rf"(?<!\d){amount}\s*万元", item["text"] or "") for item in cited))
    return checks


async def run_e2e():
    project_ids = {row["name"]: row["id"] for row in projects.list_projects()}
    llm = pipeline.get_llm_client()
    scenarios = [
        ("followup", ENGINEERING, [f"先查{TIANSHU}的负责人。", FOLLOWUP], TIANSHU, 200),
        ("switch", ENGINEERING, [f"查{NEBULA}的负责人。", f"现在只查{TIANSHU}的负责人。", FOLLOWUP], TIANSHU, 200),
        ("ambiguous", ENGINEERING, [f"这轮聊{NEBULA}和{TIANSHU}。", FOLLOWUP], "ambiguous", None),
        ("permission", MARKET, [f"接下来只看{NEBULA}。", FOLLOWUP], "denied", None),
        ("stale_argument_recovery", ENGINEERING, [f"先查{NEBULA}的负责人。", f"现在只查{TIANSHU}的负责人。", FOLLOWUP], TIANSHU, 200),
    ]
    cases = []
    async with local_server() as url:
        for key, username, turns, expected, amount in scenarios:
            if ONLY_CASES and key not in ONLY_CASES:
                continue
            started = time.monotonic()
            case = {"case": key, "identity": username, "expected_project": expected, "turns": []}
            try:
                async with authenticated_client(url, username) as session:
                    listing = await session.list_tools()
                    spec = next(tool for tool in listing.tools if tool.name == "knowledge_search")
                    description = re.sub(r"限定知识领域编号（[^）]*）",
                                         f"限定知识领域编号（{', '.join(domains.BUILTIN_CODES)}）", spec.description)
                    tool = {"type": "function", "function": {"name": spec.name, "description": description,
                                                              "parameters": spec.inputSchema}}
                    instruction = ("你是使用公司知识库的外部 Agent。查询资料时使用已提供的工具。"
                                   "基于本轮工具证据回答并标注返回的引用编号 [n]。缺少证据请如实说明。")
                    if SCOPED_AGENT:
                        instruction += "\n" + HOST_CONTEXT_POLICY
                    if FRESH_EVIDENCE:
                        instruction += "\n" + FRESH_EVIDENCE_POLICY
                    messages = [{"role": "system", "content": instruction}]
                    for turn_number, text in enumerate(turns, 1):
                        if FRESH_EVIDENCE:
                            messages = [message for message in messages if message["role"] in {"system", "user"}]
                        messages.append({"role": "user", "content": text})
                        forced = None
                        if key == "stale_argument_recovery" and turn_number == len(turns):
                            # Force the stale request once to exercise conflict recovery deterministically.
                            forced = {"question": text, "project": NEBULA, "history": history(*turns[:-1])}
                        record = await agent_turn(session, llm, tool, messages, username, forced=forced)
                        case["turns"].append({"question": text, **record})
                    case["checks"] = grade(case["turns"][-1], expected, amount, project_ids)
                    if key == "stale_argument_recovery":
                        calls = case["turns"][-1]["calls"]
                        case["checks"]["recovered_conflict"] = bool(len(calls) >= 2
                            and calls[0]["issue"] == "conflict" and not calls[0]["matches"])
                    case["passed"] = all(case["checks"].values())
            except Exception as exc:
                while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
                    exc = exc.exceptions[0]
                case["error"] = type(exc).__name__
                case["http_status"] = getattr(exc, "status_code", None)
                if case["http_status"] == 400:
                    case["provider_error"] = getattr(exc, "message", "")[:300]
            case["seconds"] = round(time.monotonic() - started, 2)
            cases.append(case)
            print(key, case.get("checks", case.get("error")), flush=True)
            if case.get("http_status") == 402:
                break
    return cases


def observe(question, *, cloud, keyword):
    if not cloud:
        raise ValueError("This end-to-end audit requires --cloud")
    return {"id": question["id"], "scoped_agent_policy": SCOPED_AGENT,
            "fresh_evidence": FRESH_EVIDENCE, "scenarios": anyio.run(run_e2e)}


if __name__ == "__main__":
    sys.argv = [arg for arg in sys.argv if arg not in {"--scoped-agent", "--fresh-evidence"}
                and not arg.startswith("--cases=")]
    sys.argv.extend(["--only", "SN02"])
    if not any(arg == "--output" or arg.startswith("--output=") for arg in sys.argv[1:]):
        sys.argv.extend(["--output", str(HERE / "mcp_end_to_end_results.json")])
    isolated.main(question_file=HERE / "questions60.json", corpus_builder=build_corpus,
                  observe_question=observe, project_linked=True)
