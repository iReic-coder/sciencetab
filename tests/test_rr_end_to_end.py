"""验收：同一个 RunnerSpec 跑两次，核对结果一致、原工程未被改写。

判据来自 M0：四个主 CSV 与历史结果逐字节一致；validation.csv 5 项全 Pass。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sciencetab import run as run_mod, spec as spec_mod, workspace  # noqa: E402

SPEC = ROOT / "runspecs" / "rr_thz_rate_power.json"
REPORT = ROOT / "verification" / "a1" / "rr_two_runs.json"


def source_snapshot(spec):
    items = []
    for src in spec["sources"]:
        root = Path(src["root"])
        for rel in src["include"]:
            target = root / rel
            files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
            for f in files:
                st = f.stat()
                items.append([str(f), st.st_size, st.st_mtime_ns])
    return sorted(items)


def main():
    if not SPEC.exists():
        print("跳过：需要本地的 %s（示例见 runspecs/example_rate_power.json）" % SPEC.name)
        return 0
    spec = spec_mod.load_spec(SPEC)
    data_root = spec_mod.default_data_root()
    before = source_snapshot(spec)

    runs = []
    stamp = int(time.time())
    for label in ("a", "b"):
        print("[test] 第 %s 次运行" % label, flush=True)
        result = run_mod.start_run(spec, data_root,
                                   idempotency_key="rr-two-runs-%s-%d" % (label, stamp))
        assert not result["reused"], "应当是一次新的运行，而不是复用旧记录"
        runs.append(result)
        print("[test] %s -> %s (%ss)" % (result["run_id"], result["completion"]["status"],
                                         result["completion"]["duration_s"]), flush=True)

    after = source_snapshot(spec)

    hashes = []
    for result in runs:
        run_dir = data_root / "runs" / result["run_id"]
        entry = {"run_id": result["run_id"],
                 "status": result["completion"]["status"],
                 "exit_code": result["completion"]["exit_code"],
                 "duration_s": result["completion"]["duration_s"],
                 "validation_all_pass": (result["completion"].get("validation") or {}).get("all_pass"),
                 "outputs": {}}
        for rel in spec["comparable_outputs"]:
            path = run_dir / "outputs" / rel
            entry["outputs"][rel] = workspace.sha256_file(path) if path.exists() else None
        hashes.append(entry)

    identical = {}
    for rel in spec["comparable_outputs"]:
        values = [item["outputs"][rel] for item in hashes]
        identical[rel] = len(set(values)) == 1 and values[0] is not None

    verify = [run_mod.verify_run(data_root, r["run_id"]) for r in runs]

    report = {
        "spec_id": spec["id"],
        "spec_sha256": spec["spec_sha256"],
        "runs": hashes,
        "comparable_identical": identical,
        "all_comparable_identical": all(identical.values()),
        "all_succeeded": all(h["status"] == "succeeded" for h in hashes),
        "validation_all_pass": all(h["validation_all_pass"] for h in hashes),
        "verify_ok": all(v["ok"] for v in verify),
        "origin_unchanged": before == after,
        "origin_files": len(before),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "all_succeeded", "validation_all_pass", "all_comparable_identical",
        "verify_ok", "origin_unchanged", "origin_files")}, ensure_ascii=False, indent=2))
    ok = (report["all_succeeded"] and report["validation_all_pass"]
          and report["all_comparable_identical"] and report["verify_ok"]
          and report["origin_unchanged"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())