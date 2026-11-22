"""Exercise MCP over SDK memory streams and replay synthetic Agent tool decisions."""
from __future__ import annotations

import json
import re
import sys
from unittest.mock import patch

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from run_isolated import HERE, build_corpus, isolated
from src import auth_db, domains, mcp_server, pipeline, projects
from src.query_scope import project_reference


FOLLOWUP = "它的立项预算是多少万元？"
NEBULA = "星云项目"
TIANSHU = "天枢项目"
MARKET = "liuye"
ENGINEERING = "wangfang"
SWITCH = [f"先看{NEBULA}的负责人。", f"现在只看{TIANSHU}。"]
CORRECTION = [f"先看{NEBULA}。", f"我说错了，前面说的是{NEBULA}，现在只查{TIANSHU}。"]


def history(*turns):
    return [{"role": "user", "content": turn} for turn in turns]


def summarize(response):
    meta = response.get("coverage_details", {})
    return {"refused": response.get("refused"), "issue": meta.get("scope_issue"),
            "project_ids": response.get("filters_applied", {}).get("project_id", []),
            "matched_project_ids": sorted({match["project_id"] for match in response.get("matches", [])
                                           if match.get("project_id") is not None}),
            "sources": sorted({match["source"] for match in response.get("matches", []) if match.get("source")}),
            "error": response.get("error")}


async def call(session, arguments, username=ENGINEERING):
    user = auth_db.get_user(username)
    identity = isolated.identity({"asker": {"username": username}})
    with patch.object(mcp_server, "_identity_from_ctx",
                      return_value=(identity["user_groups"], username, user)):
        result = await session.call_tool("knowledge_search", arguments)
    if result.isError:
        return {"error": "tool_error"}
    return summarize(json.loads(next(block.text for block in result.content if block.type == "text")))


async def observe_protocol(cloud):
    ids = {project["name"]: project["id"] for project in projects.list_projects()}
    async with create_connected_server_and_client_session(mcp_server.mcp) as session:
        tools = await session.list_tools()
        search = next(tool for tool in tools.tools if tool.name == "knowledge_search")
        controls = [
            ("first_named_call", {"question": f"{NEBULA}的立项预算是多少万元？"}, ENGINEERING),
            ("bare_same_connection", {"question": FOLLOWUP}, ENGINEERING),
            ("explicit_project", {"question": FOLLOWUP, "project": TIANSHU}, ENGINEERING),
            ("history_switch", {"question": FOLLOWUP, "history": history(*SWITCH)}, ENGINEERING),
            ("history_correction", {"question": FOLLOWUP, "history": history(*CORRECTION)}, ENGINEERING),
            ("old_history_vs_project", {"question": FOLLOWUP, "project": TIANSHU,
                                         "history": history(f"先查{NEBULA}。")}, ENGINEERING),
            ("stale_project_vs_correction", {"question": FOLLOWUP, "project": NEBULA,
                                             "history": history(*CORRECTION)}, ENGINEERING),
            ("named_question_vs_project", {"question": f"{NEBULA}的立项预算是多少万元？",
                                             "project": TIANSHU}, ENGINEERING),
            ("mixed_projects", {"question": FOLLOWUP,
                                "history": history(f"先看{NEBULA}和{TIANSHU}两个项目。")}, ENGINEERING),
            ("assistant_only", {"question": FOLLOWUP,
                                "history": [{"role": "assistant", "content": f"当前只看{TIANSHU}。"}]}, ENGINEERING),
            ("permission_boundary", {"question": FOLLOWUP,
                                     "history": history(f"{NEBULA}的里程碑有哪些？")}, MARKET),
            ("initial_focus", {"question": FOLLOWUP, "history": history(f"先看{NEBULA}。")}, ENGINEERING),
        ]
        protocol = [{"case": name, **await call(session, arguments, username)}
                    for name, arguments, username in controls]
        agents = await observe_agent(session, search, ids) if cloud else []
        replay = await replay_arguments(session, ids) if not cloud else {}
        return {"protocol": protocol, "agent_trials": agents, "project_ids": ids,
                "alias_replay": replay,
                "schema": {"properties": sorted(search.inputSchema["properties"]),
                           "required": search.inputSchema.get("required", [])}}


async def replay_arguments(session, ids):
    path = HERE / "mcp_context_audit.json"
    if not path.exists():
        return {}
    trials = json.loads(path.read_text(encoding="utf-8"))["questions"][0].get("agent_trials", [])
    exact_resolver = projects.resolve_project_filter

    def unique_variant(project, direction, *, allow_name_variants=False):
        return exact_resolver(project, direction, allow_name_variants=True)

    output = []
    with patch.object(projects, "resolve_project_filter", side_effect=unique_variant):
        for trial in trials:
            expected_id = ids.get(trial["expected"])
            if not expected_id or not trial.get("calls"):
                continue
            first = trial["calls"][0]
            replayed = await call(session, first["arguments"])
            output.append({"profile": trial["profile"], "case": trial["case"],
                           "before": first["result"].get("matched_project_ids", []) == [expected_id],
                           "after": replayed.get("matched_project_ids", []) == [expected_id]})
        for name in ("栖云项目", "栖云专案"):
            projects.save_project(projects.ProjectFields(name, None, None, [], ""))
        collision = await call(session, {"question": FOLLOWUP, "project": "栖云"})
    return {"first_call": output, "ambiguous_alias": collision}


async def observe_agent(session, search, ids):
    llm = pipeline.get_llm_client()
    # FastMCP builds descriptions at import time. Use only fixture domain codes in hosted trials.
    description = re.sub(r"限定知识领域编号（[^）]*）",
                         f"限定知识领域编号（{', '.join(domains.BUILTIN_CODES)}）", search.description)
    profiles = {
        "current": description,
        "explicit_context": description + (
            "\n多轮追问：请先把当前问题写成可独立检索的完整问题。"
            "若用户已明确项目，用最新确认的项目名传 project；较早但已更正的项目不再适用。"
            "仍需消解指代时只传相关用户消息到 history。无法确认是哪一个项目时，请先向用户澄清。"
        ),
    }
    scenarios = [
        ("named", [f"查询{NEBULA}的立项预算。"], NEBULA),
        ("unique", [f"接下来讨论{NEBULA}。", FOLLOWUP], NEBULA),
        ("switch", [*SWITCH, FOLLOWUP], TIANSHU),
        ("correction", [*CORRECTION, FOLLOWUP], TIANSHU),
        ("style_followup", [f"接下来讨论{NEBULA}。", "请用中文简短回答。", FOLLOWUP], NEBULA),
        ("ambiguous", [f"现在讨论{NEBULA}和{TIANSHU}，它们都还在计划中。", FOLLOWUP], None),
    ]
    observations = []
    previous_answer = {"role": "assistant", "content": "好的，我会以当前会话选定的项目为范围。"}
    for profile, description in profiles.items():
        tool = {"type": "function", "function": {"name": search.name, "description": description,
                                                  "parameters": search.inputSchema}}
        for key, turns, expected in scenarios:
            messages = [{"role": "system", "content": "你是调用公司知识库的外部 Agent，需要查资料时使用已提供的工具。"}]
            for text in turns[:-1]:
                messages.extend([{"role": "user", "content": text}, previous_answer])
            messages.append({"role": "user", "content": turns[-1]})
            trial = {"profile": profile, "case": key, "expected": expected}
            try:
                completion = llm.client.chat.completions.create(
                    model=llm.cfg.llm_model, messages=messages, tools=[tool],
                    tool_choice="auto", temperature=0, max_tokens=700,
                )
                message = completion.choices[0].message
                trial["calls"] = []
                for tool_call in message.tool_calls or []:
                    arguments = json.loads(tool_call.function.arguments)
                    trial["calls"].append({"arguments": arguments, "result": await call(session, arguments)})
                if expected:
                    trial["ok"] = bool(trial["calls"]) and all(
                        item["result"].get("project_ids") == [ids[expected]]
                        and item["result"].get("matched_project_ids") == [ids[expected]]
                        for item in trial["calls"])
                else:
                    trial["answer"] = message.content or ""
                    trial["ok"] = (all(item["result"].get("issue") == "project_reference_ambiguous"
                                       for item in trial["calls"]) if trial["calls"]
                                   else bool(re.search(r"哪个|哪一个|哪個|哪一個|请.{0,8}(?:确认|明确|指定)",
                                                       trial["answer"])))
                trial["bare_calls"] = sum(
                    bool(project_reference(item["arguments"].get("question", "")))
                    and not item["arguments"].get("project") and not item["arguments"].get("history")
                    for item in trial["calls"])
            except Exception as exc:
                trial["error"] = type(exc).__name__
            observations.append(trial)
    return observations


def observe(question, *, cloud, keyword):
    return {"id": question["id"], **anyio.run(observe_protocol, cloud)}


if __name__ == "__main__":
    sys.argv.extend(["--only", "SN02"])
    if not any(arg == "--output" or arg.startswith("--output=") for arg in sys.argv[1:]):
        sys.argv.extend(["--output", str(HERE / "mcp_context_audit.json")])
    isolated.main(question_file=HERE / "questions60.json", corpus_builder=build_corpus,
                  observe_question=observe, project_linked=True)
