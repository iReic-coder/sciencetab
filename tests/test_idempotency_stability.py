"""自检：连续短任务稳定性 + 幂等键复用（不涉及科研计算）。"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sciencetab import run as run_mod, spec as spec_mod  # noqa: E402

SPEC = ROOT / "runspecs" / "selftest_quick.json"
REPORT = ROOT / "verification" / "b1" / "stability_idempotency.json"


def main():
    spec = spec_mod.load_spec(SPEC)
    data_root = spec_mod.default_data_root()
    runs = []
    for i in range(3):
        started = time.time()
        result = run_mod.start_run(spec, data_root, idempotency_key="stability-%d-%d" % (int(started), i))
        runs.append({
            "run_id": result["run_id"],
            "status": result["completion"]["status"],
            "exit_code": result["completion"]["exit_code"],
            "duration_s": result["completion"]["duration_s"],
        })
        print(json.dumps(runs[-1], ensure_ascii=False), flush=True)

    key = "idem-%d" % int(time.time())
    first = run_mod.start_run(spec, data_root, idempotency_key=key)
    second = run_mod.start_run(spec, data_root, idempotency_key=key)
    idem = {
        "key": key,
        "first_run": first["run_id"],
        "first_reused": first["reused"],
        "second_run": second["run_id"],
        "second_reused": second["reused"],
        "same_run_id": first["run_id"] == second["run_id"],
    }
    report = {
        "stability": runs,
        "all_succeeded": all(r["status"] == "succeeded" and r["exit_code"] == 0 for r in runs),
        "idempotency": idem,
        "idempotency_ok": idem["second_reused"] and idem["same_run_id"] and not idem["first_reused"],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"all_succeeded": report["all_succeeded"],
                      "idempotency_ok": report["idempotency_ok"]}, ensure_ascii=False, indent=2))
    return 0 if report["all_succeeded"] and report["idempotency_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())