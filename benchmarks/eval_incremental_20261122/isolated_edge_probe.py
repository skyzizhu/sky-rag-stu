"""Audit same-document revision boundaries without touching the real knowledge base."""
from __future__ import annotations

import json
import sys
import threading
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import index_state, manage, pipeline
from src.permissions import scope_filters
from src.upload_plan import execute_uploads, plan_uploads
from src.web_fetcher import save_web_snapshot
from tests._helpers import IngestHarness


def revision_cards(case, path):
    relative = path.relative_to(case.knowledge).as_posix()
    return [row["payload"] for row in case.store.get_all_cards({"path": relative})]


def state(case, path):
    relative = path.relative_to(case.knowledge).as_posix()
    entry = index_state.load_state()[relative]
    return {"version": entry["version"], "hash_matches_disk": entry["hash"] == index_state.file_hash(path),
            "ledger_chunks": entry["chunks"],
            "cards": dict(Counter(f"{row['version']}/{row['status']}" for row in revision_cards(case, path)))}


def text_sections():
    return "\n\n".join(f"## Section {number}\nOld evidence {number}. " + f"Topic{number} explanation. " * 220
                       for number in range(6))


def failed_publication(case):
    path = case.write_note("Current approved first edition.")
    case.ingest([path])
    path.write_text(text_sections(), encoding="utf-8")
    before = state(case, path)
    upsert = case.store.upsert_chunks

    def fail_before_write(_chunks, _vectors):
        raise RuntimeError("simulated database failure")

    def fail_after_one(chunks, vectors):
        upsert(chunks[:1], vectors[:1])
        raise RuntimeError("simulated failure after first batch")

    snapshots = {}
    for name, failure in (("before_any_write", fail_before_write), ("after_one_card", fail_after_one)):
        with patch.object(case.store, "upsert_chunks", side_effect=failure):
            try:
                case.ingest([path])
            except RuntimeError:
                snapshots[name] = state(case, path)
    case.ingest([path], skip_unchanged=True)
    return {"before": before, **snapshots, "after_retry": state(case, path)}


def same_revision_shrinks(case):
    path = case.write_note(text_sections(), "manual_v2.0.md")
    case.ingest([path])
    before = state(case, path)
    path.write_text("Current short approved text.", encoding="utf-8")
    case.ingest([path])
    cards = revision_cards(case, path)
    return {"before": before, "after": state(case, path), "distinct_payload_hashes": len({
        row["version_content_hash"] for row in cards}),
        "expired_old_text_cards": sum(row["status"] == "expired" and "Old evidence" in row["text"]
                                      for row in cards)}


def empty_replacement(case):
    path = case.write_note("Existing indexed content.")
    second = case.write_note("Existing second document.", "second.md")
    case.ingest([path, second])
    path.write_text("", encoding="utf-8")
    alone = case.ingest([path], skip_unchanged=True)
    single = {"ledger": state(case, path), "ok_files": alone.ok_files, "chunks": alone.total_chunks}
    second.write_text("Second document updated.", encoding="utf-8")
    batch = case.ingest([path, second], skip_unchanged=True)
    queued = case.ingest([path], skip_unchanged=True)
    return {"alone": single, "mixed": {"ledger": state(case, path), "ok_files": batch.ok_files,
                                      "row_chunks": [row["chunks"] for row in batch.file_rows]},
            "next_incremental_skip": bool(queued.skipped_files)}


def old_revision_label_reused(case):
    path = case.write_note("Original material.")
    case.ingest([path])
    path.write_text("---\nversion: '2.0'\n---\nSecond approved material.", encoding="utf-8")
    case.ingest([path])
    before = state(case, path)
    path.write_text("---\nversion: '1.0'\n---\nDifferent content with the old declared revision.", encoding="utf-8")
    case.ingest([path])
    cards = revision_cards(case, path)
    return {"before": before, "after": state(case, path),
            "original_text_preserved": any("Original material" in row["text"] for row in cards)}


def manually_edited_revision(case):
    path = case.write_note("Original material.")
    case.ingest([path])
    rel = path.relative_to(case.knowledge).as_posix()
    manage.update_metadata(rel, {"version": "7.0"})
    before = state(case, path)
    path.write_text("Updated material after manual correction.", encoding="utf-8")
    case.ingest([path])
    return {"before": before, "after": state(case, path)}


def bytes_change_after_hash(case):
    path = case.write_note("Original material.")
    case.ingest([path])
    path.write_text("Second material used for fingerprint.", encoding="utf-8")
    parse = pipeline.parse_file

    def changed_before_parse(path):
        path.write_text("Third material actually parsed.", encoding="utf-8")
        return parse(path)

    with patch.object(pipeline, "parse_file", side_effect=changed_before_parse):
        case.ingest([path], skip_unchanged=True)
    after_race = state(case, path)
    case.ingest([path], skip_unchanged=True)
    return {"after_race": after_race, "after_plain_retry": state(case, path),
            "copies_of_third_material": sum("Third material" in row["text"] for row in revision_cards(case, path))}


def upload_decisions(case):
    a = case.write_note("Data A", "a.md")
    b = case.write_note("Data B", "b.md")
    case.ingest([a, b])
    ledger = index_state.load_state()
    target = case.knowledge / "reference"
    duplicates = plan_uploads([("b.md", a.read_bytes())], "learning/general", ledger, case.knowledge)
    multiple = [("b.md", b"First upload to the existing version"),
                ("b.md", b"Second upload to the existing version")]
    decisions = plan_uploads(multiple, target.name, ledger, case.knowledge)
    write_paths, _ = execute_uploads(decisions, multiple, case.knowledge)
    summary = case.ingest(write_paths, skip_unchanged=True)
    return {"same_bytes_as_another_document": duplicates[0].options,
            "same_name_across_folders": [decision.action for decision in decisions],
            "distinct_write_targets": len(set(write_paths)), "submitted_files": len(multiple),
            "reported_card_count": summary.total_chunks, "published_state": state(case, b),
            "first_submitted_content_survived": any("First upload" in row["text"] for row in revision_cards(case, b))}


def failed_ledger_commit(case):
    path = case.write_note("First approved material.")
    original_bytes = path.read_bytes()
    case.ingest([path])
    path.write_text("Second approved material.", encoding="utf-8")
    with patch.object(index_state, "save_state", side_effect=OSError("simulated ledger commit failure")):
        try:
            case.ingest([path], skip_unchanged=True)
        except OSError:
            after_failure = state(case, path)
    path.write_bytes(original_bytes)
    retry = case.ingest([path], skip_unchanged=True)
    return {"after_failure": after_failure, "after_restore_and_retry": state(case, path),
            "retry_skipped": bool(retry.skipped_files)}


def stale_ledger_snapshot(case):
    a = case.write_note("A before.", "a.md")
    b = case.write_note("B before.", "b.md")
    case.ingest([a, b])
    before_both_updates = deepcopy(index_state.load_state())
    a.write_text("A updated by the first writer.", encoding="utf-8")
    case.ingest([a], skip_unchanged=True)
    first_commit = state(case, a)
    b.write_text("B updated by the second writer.", encoding="utf-8")
    # Model a second process that read the same starting ledger before the first committed.
    with patch.object(index_state, "load_state", return_value=before_both_updates):
        case.ingest([b], skip_unchanged=True)
    return {"a_after_first_commit": first_commit, "a_after_stale_second_commit": state(case, a),
            "b_after_second_commit": state(case, b)}


def concurrent_permission_narrowing(case):
    path = case.write_note("Original material.")
    case.ingest([path], default_groups=["all"])
    relative = path.relative_to(case.knowledge).as_posix()
    path.write_text("Sensitive new revision.", encoding="utf-8")
    embed = case.embedding.embed_texts
    public_view = scope_filters({"path": relative, "status": "active"}, [], "ordinary-reader")
    intervening = {}

    def change_visibility_during_embedding(texts):
        # A different process uses a different in-process lock, but the same DB and index.
        with patch.object(pipeline, "_ingest_lock", threading.Lock()):
            manage.update_metadata(relative, {"allowed_groups": ["finance"], "allowed_users": []})
        intervening["public_matches_after_edit"] = len(case.store.get_all_cards(public_view))
        return embed(texts)

    with patch.object(case.embedding, "embed_texts", side_effect=change_visibility_during_embedding):
        case.ingest([path], skip_unchanged=True)
    active = case.store.get_all_cards(public_view)
    return {**intervening, "public_matches_after_ingest": len(active),
            "new_revision_visible": any("Sensitive new revision" in row["payload"]["text"] for row in active),
            "ledger_acl": index_state.load_state()[relative]["visibility_info"]["value"]}


def unchanged_with_missing_vectors(case):
    path = case.write_note("Source and ledger are intact, but the vector snapshot is missing.")
    case.ingest([path])
    relative = path.relative_to(case.knowledge).as_posix()
    case.store.delete_documents([index_state.load_state()[relative]["document_id"]])
    retry = case.ingest([path], skip_unchanged=True)
    return {"retry_skipped": bool(retry.skipped_files), "published_state": state(case, path)}


def web_snapshot_revision(case):
    url = "https://example.invalid/revisions"
    saved = save_web_snapshot(url, "First snapshot.", "Synthetic project note", knowledge_dir=case.knowledge)
    case.ingest([saved], skip_unchanged=True)
    same = save_web_snapshot(url, "First snapshot.", "Synthetic project note", knowledge_dir=case.knowledge)
    skipped = case.ingest([same], skip_unchanged=True)
    changed = save_web_snapshot(url, "Updated snapshot.", "Synthetic project note", knowledge_dir=case.knowledge)
    case.ingest([changed], skip_unchanged=True)
    return {"same_path": saved == same == changed, "unchanged_skipped": bool(skipped.skipped_files),
            "changed": state(case, changed)}


PROBES = (failed_publication, same_revision_shrinks, empty_replacement, old_revision_label_reused,
          manually_edited_revision, bytes_change_after_hash, upload_decisions, failed_ledger_commit,
          stale_ledger_snapshot, concurrent_permission_narrowing, unchanged_with_missing_vectors,
          web_snapshot_revision)


if __name__ == "__main__":
    rows = {}
    for probe in PROBES:
        case = IngestHarness()
        case.setUp()
        try:
            case.use_fakes()
            rows[probe.__name__] = probe(case)
        finally:
            case.store.client.close()
            case.tearDown()
            case.doCleanups()
    destination = Path(__file__).parent / "results" / "isolated_edge_probe_results.json"
    destination.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False))
