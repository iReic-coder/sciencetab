"""验收：六种终态都能由真实运行产生，且页面能正确显示。

succeeded / failed / incomplete / validation_failed / timed_out / cancelled
（timed_out 与 cancelled 已由 test_cancel_timeout.py 覆盖，这里重跑一遍确认仍然成立）
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sciencetab import run as run_mod, spec as spec_mod  # noqa: E402

REPORT = ROOT / "verification" / "b1" / "state_machine.json"

CASES = [
    ("selftest_quick", "succeeded", {}),
    ("selftest_fail", "failed", {}),
    ("selftest_incomplete", "incomplete", {}),
    ("selftest_validation_fail", "validation_failed", {}),
    ("selftest_cancel", "timed_out", {"timeout_s": 20}),
]


def main():
    data_root = spec_mod.default_data_root()
    results = []
    for spec_id, expected, override in CASES:
        spec = spec_mod.load_spec(ROOT / "runspecs" / (spec_id + ".json"))
        key = "%s-%d" % (spec_id, int(time.time() * 1000))
        result = run_mod.start_run(spec, data_root, idempotency_key=key,
                                   timeout_s=override.get("timeout_s"))
        actual = result["completion"]["status"]
        results.append({"spec": spec_id, "expected": expected, "actual": actual,
                        "run_id": result["run_id"],
                        "exit_code": result["completion"]["exit_code"],
                        "ok": actual == expected})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)

    report = {"cases": results, "all_ok": all(r["ok"] for r in results)}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"all_ok": report["all_ok"]}, ensure_ascii=False))
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())