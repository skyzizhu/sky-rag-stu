"""Run the core regression suite with performance and concurrency probes excluded."""
from collections import Counter
import json
from pathlib import Path
import unittest

EXCLUDED_MODULES = {"test_reranker_timing", "test_stage_timing"}
EXCLUDED_METHODS = {
    "test_concurrent_tabs_do_not_silently_replace_each_others_turn",
    "test_run_evaluation_payload_carries_timing",
    "test_run_evaluation_timing_survives_judge_failure",
}


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


class Result(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.totals = Counter()
        self.successes = Counter()

    def startTest(self, test):
        self.totals[test.id().split(".")[1]] += 1
        super().startTest(test)

    def addSuccess(self, test):
        self.successes[test.id().split(".")[1]] += 1
        super().addSuccess(test)

    def printErrors(self):
        self.stream.writeln("")
        for label, group in (("FAIL", self.failures), ("ERROR", self.errors)):
            for test, trace in group:
                lines = trace.splitlines()
                self.stream.writeln(f"{label} {test.id()} | {lines[-1] if lines else ''}")


def main():
    all_tests = list(cases(unittest.defaultTestLoader.discover("tests", top_level_dir=".")))
    chosen, excluded = [], []
    for item in all_tests:
        test_id = item.id()
        module, method = test_id.split(".")[1], test_id.rsplit(".", 1)[-1]
        if module in EXCLUDED_MODULES or method in EXCLUDED_METHODS:
            excluded.append(test_id)
        else:
            chosen.append(item)
    result = unittest.TextTestRunner(verbosity=1, resultclass=Result).run(unittest.TestSuite(chosen))
    issues = []
    for label, group in (("failure", result.failures), ("error", result.errors)):
        for test, trace in group:
            category = ("windows_app_loopback" if "_fallback_socketpair" in trace and "TestIsolationError" in trace
                        else "assertion" if label == "failure" else "runtime")
            issues.append({"test": test.id(), "category": category, "traceback": trace})
    data = {
        "discovered": len(all_tests), "run": result.testsRun, "passed": sum(result.successes.values()),
        "skipped": [{"test": case.id(), "reason": reason} for case, reason in result.skipped],
        "excluded": excluded, "issues": issues,
        "modules": {module: {"run": count, "passed": result.successes[module]}
                    for module, count in sorted(result.totals.items())},
    }
    Path(__file__).with_name("regression.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
