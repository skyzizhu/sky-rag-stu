#!/usr/bin/env python3
"""Independent, local-only ONNX CPU thread benchmark.

Pass a question at runtime. Retrieval is called once, without query understanding
or generation, and the fixed ten candidates are cached only in ignored storage.
This module intentionally does not import the application's reranker.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import hashlib
import io
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "storage" / "rerank_benchmark"
DOC_CHARS = 1200
PAIR_TOKENS = 2048
CANDIDATE_COUNT = 10


class BenchmarkError(Exception):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                   allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def validate_local_endpoint(url, label, port=None):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username or parsed.password
            or (port is not None and parsed.port != port)):
        raise BenchmarkError(f"{label} must be a credential-free loopback endpoint")


def prepare_output(path):
    output = path.resolve()
    if not output.is_relative_to(OUTPUT_ROOT.resolve()):
        raise BenchmarkError("Output must remain under storage/rerank_benchmark")
    if (ROOT / ".git").exists():
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "--quiet",
             str((output / "candidates.json").relative_to(ROOT))],
            capture_output=True, timeout=10, check=False)
        if ignored.returncode != 0:
            raise BenchmarkError("Candidate and report storage must be git-ignored")
    output.mkdir(parents=True, exist_ok=True)
    return output


def hardware_info():
    result = {"platform": platform.platform(), "logical_cpus": os.cpu_count(),
              "python": platform.python_version(), "processor": platform.processor()}
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                result["cpu_name"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
        try:
            command = ("Get-CimInstance Win32_Processor | "
                       "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | "
                       "ConvertTo-Json -Compress")
            probe = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                                   capture_output=True, text=True, timeout=15, check=False)
            if probe.returncode == 0:
                result["cpu_packages"] = json.loads(probe.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            result["physical_core_probe"] = "unavailable"
    return result


def process_snapshot():
    result = {"at": timestamp(), "self_pid": os.getpid(),
              "self_cpu_seconds": time.process_time()}
    if os.name != "nt":
        return result
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        result["thread_probe"] = "unavailable"
        return result
    processes = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            processes.append({"pid": int(entry.th32ProcessID), "name": entry.szExeFile,
                              "threads": int(entry.cntThreads)})
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    result["process_count"] = len(processes)
    result["total_thread_count"] = sum(p["threads"] for p in processes)
    result["relevant_processes"] = [
        p for p in processes if p["pid"] == os.getpid()
        or any(name in p["name"].lower() for name in ("python", "ollama", "qdrant"))]
    result["self_threads"] = next((p["threads"] for p in processes
                                  if p["pid"] == os.getpid()), None)
    return result


def candidates_for(question, cfg, output):
    cache = output / "candidates.json"
    question_hash = digest(question)
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if (data.get("question_sha256") != question_hash
                or data.get("collection") != cfg.qdrant_collection
                or data.get("filters") != {"status": "active"}
                or data.get("use_hybrid") is not True):
            raise BenchmarkError("Existing cache does not match this question and retrieval")
        print("Reusing existing fixed candidate cache; retrieval not repeated.", flush=True)
    else:
        validate_local_endpoint(cfg.qdrant_url, "Qdrant", port=6333)
        validate_local_endpoint(cfg.ollama_url, "Ollama")
        # No application pipeline, QU client, LLM client, or reranker is imported.
        from src.embedding import EmbeddingClient
        from src.retriever import Retriever
        from src.vector_store import VectorStore
        local_cfg = replace(cfg, qdrant_url="http://127.0.0.1:6333", debug=False)
        store = VectorStore(config=local_cfg)
        embedding = EmbeddingClient(config=local_cfg)
        started = time.perf_counter()
        try:
            if not store.collection_exists():
                raise BenchmarkError("Configured local Qdrant collection is unavailable")
            # Existing hybrid retrieval's BM25 factory uses get_config, whose URLs
            # were validated above. All store operations here are searches/counts/scrolls.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                items = Retriever(embedding_client=embedding, store=store,
                                  config=local_cfg).retrieve(
                    question, top_k=CANDIDATE_COUNT, filters={"status": "active"},
                    use_hybrid=True)
        except BenchmarkError:
            raise
        except Exception as exc:
            raise BenchmarkError("Cannot benchmark: local Qdrant/Ollama retrieval failed "
                                 f"({type(exc).__name__}); no candidates fabricated") from None
        finally:
            store.client.close()
            embedding._session.close()
        data = {"created_at": timestamp(), "question": question,
                "question_sha256": question_hash, "collection": cfg.qdrant_collection,
                "filters": {"status": "active"}, "use_hybrid": True,
                "top_k": CANDIDATE_COUNT, "retrieval_seconds": time.perf_counter() - started,
                "items": [asdict(item) for item in items]}
        if len(data["items"]) != CANDIDATE_COUNT:
            raise BenchmarkError(f"Cannot benchmark: expected ten candidates, got {len(data['items'])}")
        data["candidate_sha256"] = digest(data["items"])
        save_json(cache, data)
        print(f"Cached ten fixed candidates; retrieval {data['retrieval_seconds']:.3f}s.", flush=True)
    if len(data.get("items", [])) != CANDIDATE_COUNT:
        raise BenchmarkError("Cache must contain exactly ten candidates")
    if digest(data["items"]) != data.get("candidate_sha256"):
        raise BenchmarkError("Candidate cache checksum mismatch")
    for item in data["items"]:
        if not isinstance(item.get("text"), str) or not item["text"]:
            raise BenchmarkError("Candidate text missing")
        if item.get("metadata", {}).get("status") != "active":
            raise BenchmarkError("Candidate is not active")
    return data


def rank_orders(pairs):
    # Application ranking uses rounded sigmoid * 10; raw logit order is also useful.
    return {"raw": [p["id"] for p in sorted(pairs, key=lambda p: (-p["logit"], p["index"]))],
            "application_rounded": [p["id"] for p in sorted(
                pairs, key=lambda p: (-p["score_0_10_rounded"], p["index"]))]}


def encode_pair(tokenizer, np, query, text, input_names):
    started = time.perf_counter()
    encoding = tokenizer.encode(query, text[:DOC_CHARS])
    tokenize_seconds = time.perf_counter() - started
    started = time.perf_counter()
    feed = {"input_ids": np.array([encoding.ids], dtype=np.int64),
            "attention_mask": np.array([encoding.attention_mask], dtype=np.int64)}
    if "token_type_ids" in input_names:
        feed["token_type_ids"] = np.zeros((1, len(encoding.ids)), dtype=np.int64)
    return feed, len(encoding.ids), tokenize_seconds, time.perf_counter() - started


def benchmark_variant(threads, args, data, model_dir, report, checkpoint):
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    variant = {"intra_op_threads": threads, "inter_op_threads": 1,
               "status": "running", "snapshots": {"before_load": process_snapshot()},
               "runs": []}
    report["variants"].append(variant)
    checkpoint()
    tokenizer = session = None
    try:
        started = time.perf_counter()
        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=PAIR_TOKENS)
        variant["tokenizer_load_seconds"] = time.perf_counter() - started
        options = ort.SessionOptions()
        # Preserve defaults for intra-op 0 and sequential execution. Inter-op is
        # explicit 1 for every variant and has no parallel executor to drive.
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        if options.execution_mode != ort.ExecutionMode.ORT_SEQUENTIAL:
            raise BenchmarkError("Unexpected ONNX executor default")
        started = time.perf_counter()
        session = ort.InferenceSession(str(model_dir / "model_quantized.onnx"),
                                       sess_options=options, providers=["CPUExecutionProvider"])
        variant["session_load_seconds"] = time.perf_counter() - started
        variant["load_total_seconds"] = (variant["tokenizer_load_seconds"]
                                         + variant["session_load_seconds"])
        variant["execution_mode"] = str(session.get_session_options().execution_mode)
        variant["providers"] = session.get_providers()
        inputs = session.get_inputs()
        variant["model_inputs"] = [{"name": value.name, "type": value.type,
                                    "shape": value.shape} for value in inputs]
        input_names = {value.name for value in inputs}
        if (not {"input_ids", "attention_mask"}.issubset(input_names)
                or input_names - {"input_ids", "attention_mask", "token_type_ids"}
                or any(value.type != "tensor(int64)" or len(value.shape) != 2 for value in inputs)):
            raise BenchmarkError("Unsupported model attention/input interface")
        variant["model_outputs"] = [{"name": value.name, "type": value.type,
                                     "shape": value.shape} for value in session.get_outputs()]
        variant["snapshots"]["after_load"] = process_snapshot()
        print(f"threads={threads}: model load {variant['session_load_seconds']:.3f}s; "
              f"self threads {variant['snapshots']['after_load'].get('self_threads')}", flush=True)
        # One representative pair initializes kernels; hot passes still score all ten.
        warmup_start = time.perf_counter()
        warm_feed, _, _, _ = encode_pair(tokenizer, np, data["question"],
                                         data["items"][0]["text"], input_names)
        session.run(None, warm_feed)
        del warm_feed
        variant["warmup_seconds"] = time.perf_counter() - warmup_start
        variant["warmup_pairs"] = 1
        variant["snapshots"]["after_warmup"] = process_snapshot()
        checkpoint()
        target = args.repeats
        repeat = 0
        while repeat < target:
            repeat += 1
            cpu_start = time.process_time()
            start = time.perf_counter()
            pairs = []
            for index, item in enumerate(data["items"], 1):
                feed, tokens, tokenize_seconds, feed_seconds = encode_pair(
                    tokenizer, np, data["question"], item["text"], input_names)
                infer_start = time.perf_counter()
                logits = session.run(None, feed)[0]
                inference_seconds = time.perf_counter() - infer_start
                logit = float(logits.reshape(-1)[0])
                if not math.isfinite(logit):
                    raise BenchmarkError("Nonfinite model logit")
                score = sigmoid(logit)
                pairs.append({"id": f"C{index}", "index": index,
                              "original_chars": len(item["text"]),
                              "scored_chars": min(len(item["text"]), DOC_CHARS),
                              "tokens": tokens, "tokenize_seconds": tokenize_seconds,
                              "feed_seconds": feed_seconds, "inference_seconds": inference_seconds,
                              "pair_seconds": tokenize_seconds + feed_seconds + inference_seconds,
                              "logit": logit, "sigmoid": score,
                              "score_0_10_rounded": round(score * 10.0, 2)})
                del feed, logits
                print(f"threads={threads} pass={repeat} C{index}: tokens={tokens}, "
                      f"inference={inference_seconds:.3f}s", flush=True)
            elapsed = time.perf_counter() - start
            run = {"repeat": repeat, "wall_seconds": elapsed,
                   "process_cpu_seconds": time.process_time() - cpu_start,
                   "tokenize_total_seconds": sum(p["tokenize_seconds"] for p in pairs),
                   "feed_total_seconds": sum(p["feed_seconds"] for p in pairs),
                   "inference_total_seconds": sum(p["inference_seconds"] for p in pairs),
                   "rank_orders": rank_orders(pairs), "pairs": pairs}
            variant["runs"].append(run)
            if (threads == 0 and repeat == 1 and elapsed > args.slow_default_seconds
                    and target > 2):
                target = 2
                variant["repeat_limit_reason"] = "First default-thread pass exceeded slow threshold"
            variant["effective_repeats"] = target
            checkpoint()
            print(f"threads={threads} pass={repeat}: tokenize={run['tokenize_total_seconds']:.4f}s "
                  f"inference={run['inference_total_seconds']:.3f}s wall={elapsed:.3f}s", flush=True)
        variant["median_hot_inference_seconds"] = statistics.median(
            r["inference_total_seconds"] for r in variant["runs"])
        variant["median_hot_wall_seconds"] = statistics.median(r["wall_seconds"] for r in variant["runs"])
        variant["median_tokenize_seconds"] = statistics.median(
            r["tokenize_total_seconds"] for r in variant["runs"])
        variant["snapshots"]["after_measurements"] = process_snapshot()
        variant["status"] = "completed"
    finally:
        del session, tokenizer
        gc.collect()
        variant["snapshots"]["after_unload"] = process_snapshot()
        checkpoint()


def finish_report(report):
    variants = [v for v in report["variants"] if v["status"] == "completed"]
    if not variants:
        return
    reference = next((v for v in variants if v["intra_op_threads"] == 0), variants[0])
    ref_run = reference["runs"][0]
    ref_logits = [p["logit"] for p in ref_run["pairs"]]
    ref_sigmoids = [p["sigmoid"] for p in ref_run["pairs"]]
    for variant in variants:
        variant["rank_consistency"] = {
            "raw_order_matches_reference_all_runs": all(
                r["rank_orders"]["raw"] == ref_run["rank_orders"]["raw"] for r in variant["runs"]),
            "rounded_order_matches_reference_all_runs": all(
                r["rank_orders"]["application_rounded"] == ref_run["rank_orders"]["application_rounded"]
                for r in variant["runs"]),
            "max_abs_logit_delta": max(abs(p["logit"] - ref_logits[i])
                                       for r in variant["runs"] for i, p in enumerate(r["pairs"])),
            "max_abs_sigmoid_delta": max(abs(p["sigmoid"] - ref_sigmoids[i])
                                         for r in variant["runs"] for i, p in enumerate(r["pairs"]))}
    consistent = [v for v in variants if v["rank_consistency"]["rounded_order_matches_reference_all_runs"]]
    if consistent:
        best = min(consistent, key=lambda v: v["median_hot_inference_seconds"])
        report["recommendation"] = {
            "measured_intra_op_threads": best["intra_op_threads"],
            "median_hot_inference_seconds": best["median_hot_inference_seconds"],
            "speedup_vs_default": reference["median_hot_inference_seconds"]
                                  / best["median_hot_inference_seconds"],
            "basis": "Lowest measured hot median among variants with preserved rounded ranking",
            "scope": "This CPU, model, fixed candidate set, and live-server workload only"}


def markdown_report(report):
    lines = ["# Independent local ONNX reranker thread benchmark", "",
             f"Status: {report['status']}",
             f"Candidate checksum: `{report.get('candidate_sha256', 'unavailable')}`", "",
             "Fixed ten pairs; document cap 1200 characters, pair cap 2048 tokens; int64 feeds.",
             "CPUExecutionProvider; default sequential executor; inter-op 1; one-pair warm-up.",
             "No QU, generation, cloud model, application reranker, or corpus writes.",
             "Threads 0 means ORT automatic/default intra-op selection, not zero workers.",
             "Sessions are created sequentially and deleted with garbage collection between variants.", "",
             "## Hardware", "", "```json", json.dumps(report.get("hardware", {}), indent=2), "```", "",
             "## Candidate lengths", "", "| Candidate | Original chars | Scored chars | Pair tokens |",
             "|---|---:|---:|---:|"]
    complete = [v for v in report.get("variants", []) if v["status"] == "completed"]
    first_run = complete[0]["runs"][0] if complete else None
    token_counts = {p["id"]: p["tokens"] for p in first_run["pairs"]} if first_run else {}
    for p in report.get("candidate_lengths", []):
        lines.append(f"| {p['id']} | {p['original_chars']} | {p['scored_chars']} | {token_counts.get(p['id'], '-')} |")
    lines += ["", "## Timings", "",
              "| Intra-op | Hot passes | Model load s | Warm-up s | Tokenize median s | Inference median s | Wall median s |",
              "|---:|---:|---:|---:|---:|---:|---:|"]
    for v in complete:
        lines.append(f"| {v['intra_op_threads']} | {len(v['runs'])} | {v['session_load_seconds']:.3f} | "
                     f"{v['warmup_seconds']:.3f} | {v['median_tokenize_seconds']:.5f} | "
                     f"{v['median_hot_inference_seconds']:.3f} | {v['median_hot_wall_seconds']:.3f} |")
    if report.get("recommendation"):
        r = report["recommendation"]
        lines += ["", f"Measured recommendation: intra-op **{r['measured_intra_op_threads']}**, "
                  f"**{r['speedup_vs_default']:.2f}x** hot inference speedup versus automatic/default.",
                  "This is a local measurement, not a latency promise for other CPUs or workloads."]
    for v in complete:
        lines += ["", f"## Intra-op {v['intra_op_threads']}", "",
                  "Rank consistency: " + json.dumps(v.get("rank_consistency", {}), sort_keys=True),
                  "", "| Pass | Candidate | Tokens | Tokenize ms | Inference s | Logit | Sigmoid | Rounded score /10 |",
                  "|---:|---|---:|---:|---:|---:|---:|---:|"]
        for run in v["runs"]:
            for p in run["pairs"]:
                lines.append(f"| {run['repeat']} | {p['id']} | {p['tokens']} | "
                             f"{p['tokenize_seconds'] * 1000:.3f} | {p['inference_seconds']:.4f} | "
                             f"{p['logit']:.8f} | {p['sigmoid']:.8f} | {p['score_0_10_rounded']:.2f} |")
        lines += ["", "Raw rank order: " + ", ".join(v["runs"][0]["rank_orders"]["raw"]),
                  "", "Rounded application order: "
                  + ", ".join(v["runs"][0]["rank_orders"]["application_rounded"])]
    lines += ["", "Process/thread snapshots and all run timings are in summary.json.",
              "Thread counts alone do not establish CPU contention; no such conclusion is claimed."]
    if report.get("error"):
        lines += ["", "Error: " + report["error"]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True, help="Local runtime query; never embedded in this script")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--threads", default="0,1,2,4", help="Sequential intra-op settings")
    parser.add_argument("--repeats", type=int, default=3, help="Hot passes of the same ten pairs")
    parser.add_argument("--slow-default-seconds", type=float, default=60.0,
                        help="Limit automatic/default to two passes if its first pass exceeds this")
    parser.add_argument("--fetch-only", action="store_true", help="Cache/reuse candidates without loading ONNX")
    parser.add_argument("--resume", action="store_true", help="Skip completed variants in an existing report")
    args = parser.parse_args()
    if not args.question.strip() or args.repeats < 1 or args.slow_default_seconds <= 0:
        parser.error("Question, repeat count, and slow threshold must be positive/nonempty")
    try:
        args.thread_values = [int(value) for value in args.threads.split(",")]
    except ValueError:
        parser.error("Threads must be comma-separated integers")
    if (not args.thread_values or any(t < 0 for t in args.thread_values)
            or len(set(args.thread_values)) != len(args.thread_values)):
        parser.error("Threads must be distinct nonnegative integers")
    output = prepare_output(args.output_dir)
    summary_path = output / "summary.json"
    report = {"created_at": timestamp(), "status": "running", "variants": [],
              "method": {"doc_chars": DOC_CHARS, "max_pair_tokens": PAIR_TOKENS,
                         "requested_repeats": args.repeats, "slow_default_seconds": args.slow_default_seconds,
                         "inter_op_threads": 1, "executor": "ORT_SEQUENTIAL", "warmup_pairs": 1}}

    def checkpoint():
        report["updated_at"] = timestamp()
        save_json(summary_path, report)
        (output / "summary.md").write_text(markdown_report(report), encoding="utf-8")

    try:
        sys.path.insert(0, str(ROOT))
        from src.config import get_config
        cfg = get_config()  # Do not print config or expose credentials in exception text.
        if summary_path.exists() and not args.resume and not args.fetch_only:
            raise BenchmarkError("Report already exists; use --resume to avoid duplicate measurements")
        data = candidates_for(args.question, cfg, output)
        lengths = [{"id": f"C{i}", "original_chars": len(item["text"]),
                    "scored_chars": min(len(item["text"]), DOC_CHARS)}
                   for i, item in enumerate(data["items"], 1)]
        print("Candidate character lengths: " + json.dumps(lengths), flush=True)
        if args.fetch_only:
            return 0
        model_dir = cfg.rerank_model_dir
        model_files = {name: {"bytes": (model_dir / name).stat().st_size,
                              "mtime_ns": (model_dir / name).stat().st_mtime_ns}
                       for name in ("model_quantized.onnx", "tokenizer.json")}
        if args.resume and summary_path.exists():
            previous = json.loads(summary_path.read_text(encoding="utf-8"))
            if (previous.get("candidate_sha256") != data["candidate_sha256"]
                    or previous.get("model_files") != model_files
                    or previous.get("method") != report["method"]):
                raise BenchmarkError("Existing report does not match candidates/model/method")
            report = previous
            if any(v["status"] != "completed" for v in report["variants"]):
                raise BenchmarkError("Report has an incomplete variant; inspect before rerunning")
            report["status"] = "running"
        else:
            from importlib.metadata import version
            report.update({"question_sha256": data["question_sha256"],
                           "candidate_sha256": data["candidate_sha256"],
                           "retrieval_seconds": data["retrieval_seconds"],
                           "candidate_lengths": lengths, "model_files": model_files,
                           "hardware": hardware_info(),
                           "libraries": {name: version(name) for name in ("onnxruntime", "tokenizers", "numpy")}})
        checkpoint()
        done = {v["intra_op_threads"] for v in report["variants"]}
        for threads in args.thread_values:
            if threads in done:
                print(f"threads={threads}: already completed, not rerunning.", flush=True)
                continue
            benchmark_variant(threads, args, data, model_dir, report, checkpoint)
            finish_report(report)
            checkpoint()
        report["status"] = "completed"
        finish_report(report)
        checkpoint()
        print(json.dumps(report.get("recommendation", {}), indent=2), flush=True)
        return 0
    except Exception as exc:
        # Error strings from network/model libraries may contain private text or URLs.
        message = str(exc) if isinstance(exc, BenchmarkError) else f"Benchmark failed ({type(exc).__name__})"
        if summary_path.exists() and "candidate_sha256" not in report:
            print(message, file=sys.stderr)
            return 1  # Never overwrite a previous report on an invalid duplicate/resume.
        report.update({"status": "cannot_benchmark", "error": message})
        checkpoint()
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
