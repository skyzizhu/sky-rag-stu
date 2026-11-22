"""Replay the second-round exam on freshly generated synthetic material."""
from __future__ import annotations

import ast
import re
import os
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "full_eval_recheck_20261121"))

import run as isolated
from src import auth_db, business_directions, manage, pipeline, projects


def fixture_values(filename, *names):
    """Read the audited fixture declarations without invoking their service setup."""
    path = HERE / filename
    selected = [node for node in ast.parse(path.read_text(encoding="utf-8")).body
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names]
    values = {}

    def read(node):
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Subscript):
            return read(node.value)[read(node.slice)]
        if isinstance(node, ast.List):
            return [read(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(read(item) for item in node.elts)
        return ast.literal_eval(node)

    for statement in selected:
        values[statement.targets[0].id] = read(statement.value)
    return [values[name] for name in names]


def build_corpus(base, cfg, store, embedding):
    charter, direction_spec, project_plan = fixture_values(
        "setup_projects.py", "CHARTER_V11", "DIRECTION", "PROJECTS")
    original, indexed, ingest, missing = isolated.build_corpus(
        base, cfg, store, embedding, latest_charter=charter)
    generator = isolated.load_module("round2_generator", HERE / "gen_corpus2.py")
    generator.ROOT, generator.K = base, cfg.knowledge_dir
    generator.main()
    departments = {d["name"]: d for d in auth_db.list_departments()}
    direction_id = business_directions.create_direction(*direction_spec)["id"]
    project_ids = {
        name: projects.save_project(projects.ProjectFields(
            name, direction_id, departments["研发部"]["id"],
            [departments[department]["id"] for department in participants], ""
        ))["id"]
        for name, participants in project_plan
    }
    numbers, plan = fixture_values("ingest2.py", "PID", "PLAN", "ALL")[:2]
    ids = {number: project_ids[f"{short}项目"] for short, number in numbers.items()}
    original_paths = {row[0] for row in original}
    for path, groups, users, department, owner, number in plan:
        project_id = ids.get(number)
        if path in original_paths:
            manage.update_metadata(path, {"project_id": project_id})
            continue
        os.utime(cfg.knowledge_dir / path, (1795132800, 1795132800))
        summary = pipeline.ingest_files(
            paths=[cfg.knowledge_dir / path], config=cfg, store=store, embedding_client=embedding,
            default_groups=[departments[group]["permission_key"] if group in departments else group
                            for group in groups],
            default_allowed_users=users, default_department=department,
            default_project_id=project_id, owner=owner,
        )
        indexed.append({"path": path, "rows": summary.file_rows, "errors": summary.file_errors})
    return original, indexed, ingest, missing


def observe(question, *, cloud=False, keyword=False):
    def grade(q, result):
        return regrade.grade(q, {"answer": result.answer, "sources": result.sources})

    if question["id"] not in {"SN06", "MD20"}:
        return isolated.observe(question, cloud=cloud, keyword=keyword, grader=grade)

    checks = []
    original = pipeline.assess_evidence

    def capture(text, passages, scope_terms=()):
        decision = original(text, passages, scope_terms)
        checks.append({"decision": decision, "passages": passages[:5]})
        return decision

    with patch.object(pipeline, "assess_evidence", side_effect=capture):
        row = isolated.observe(question, cloud=cloud, keyword=keyword, grader=grade)
    with patch.object(pipeline, "assess_evidence", return_value={}):
        recovered = isolated.observe(question, cloud=False, keyword=keyword, grader=grade)
    row["support_probe"] = {"checks": checks, "evidence_only_sources_without_gate": recovered["sources"]}
    return row


def control(question, *, cloud=False, keyword=False):
    """Compare one changed condition at a time without changing application code."""
    qid, text = question["id"], question["question"]

    def evidence(query, **kwargs):
        result, meta = pipeline.query_evidence(
            query, use_llm=False, use_rerank=False,
            **isolated.identity(question), **kwargs,
        )
        return {"answer": result.answer, "route": meta.get("route"), "issue": meta.get("scope_issue"),
                "project_id": result.filters.get("project_id"),
                "sources": list(dict.fromkeys(s["path"] for s in result.sources))}

    if qid in {"CX01", "CX03", "CX07"}:
        variant = re.sub(r"[两三]个项目的", "项目的", text, count=1)
        return {"id": qid, "original": evidence(text), "variant": variant,
                "without_count_phrase": evidence(variant)}
    if qid == "MD07":
        def predicted_source(source):
            qu = isolated.QueryUnderstanding(
                original=text, vector_query=text, enumeration=True, enumeration_scope="document",
                filters={"source": source} if source else {}, ok=True,
            )
            with patch("src.pipeline.understand_query", return_value=qu), \
                 patch("src.pipeline.get_llm_client", return_value=None):
                result, meta = pipeline.query_evidence(
                    text, use_rerank=False, **isolated.identity(question),
                )
            return {"route": meta["route"], "filters": result.filters,
                    "sources": list(dict.fromkeys(s["path"] for s in result.sources))}

        return {"id": qid, "inferred_source": predicted_source("在册供应商名录"),
                "no_inferred_source": predicted_source(None)}
    if qid == "SN02":
        history = [{"role": "user", "content": "星云项目的里程碑有哪些？"}]
        return {"id": "project_anaphora",
                "pronoun_only": evidence("它的立项预算是多少万元？", history=history),
                "explicit_project_reference": evidence("这个项目的立项预算是多少万元？", history=history)}
    raise ValueError(f"No control configured for {qid}")


if __name__ == "__main__":
    regrade = isolated.load_module("round2_grader", HERE / "regrade.py")
    controls = "--controls" in sys.argv
    if controls:
        sys.argv.remove("--controls")
        sys.argv.extend(["--keyword", "--only", "SN02,MD07,CX01,CX03,CX07"])
    if not any(arg == "--output" or arg.startswith("--output=") for arg in sys.argv[1:]):
        sys.argv.extend(["--output", str(HERE / ("isolated_controls.json" if controls else "isolated_retest.json"))])
    isolated.main(question_file=HERE / "questions60.json", corpus_builder=build_corpus,
                  observe_question=control if controls else observe, project_linked=True)
