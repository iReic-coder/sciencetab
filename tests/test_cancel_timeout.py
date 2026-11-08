"""自检：取消与超时通路（不涉及科研计算）。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=str(ROOT / "backend"))


def newest_run():
    runs = sorted((DATA / "runs").glob("*"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def wait_for(run_dir, name, timeout_s=180):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        target = run_dir / name
        if target.exists() and target.stat().st_size > 0:
            return True
        time.sleep(1)
    return False


def launch(spec, extra=()):
    return subprocess.Popen(
        [PY, "-X", "utf8", "-m", "sciencetab", "run", "--spec", spec, *extra],
        cwd=str(ROOT), env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def case_cancel():
    before = {p.name for p in (DATA / "runs").glob("*")} if (DATA / "runs").exists() else set()
    proc = launch("selftest_cancel")
    run_dir = None
    deadline = time.time() + 120
    while time.time() < deadline:
        fresh = [p for p in (DATA / "runs").glob("*") if p.name not in before]
        if fresh:
            run_dir = sorted(fresh, key=lambda p: p.stat().st_mtime)[-1]
            break
        time.sleep(1)
    assert run_dir is not None, "没有创建运行目录"
    started = wait_for(run_dir, Path("logs") / "stdout.raw", timeout_s=120)
    time.sleep(8)
    cancel_at = time.time()
    subprocess.run([PY, "-X", "utf8", "-m", "sciencetab", "cancel", run_dir.name],
                   cwd=str(ROOT), env=ENV, capture_output=True, text=True, encoding="utf-8")
    out, err = proc.communicate(timeout=180)
    completion = json.loads((run_dir / "completion.json").read_text(encoding="utf-8"))
    return {
        "case": "cancel",
        "run_id": run_dir.name,
        "saw_output": started,
        "status": completion["status"],
        "cancelled": completion["cancelled"],
        "exit_code": completion["exit_code"],
        "duration_s": completion["duration_s"],
        "kill_to_exit_s": round(time.time() - cancel_at, 2),
        "cli_exit": proc.returncode,
        "stdout_tail": out.decode("utf-8", "replace").strip().splitlines()[-4:],
        "log_has_ticks": "TICK" in (run_dir / "logs" / "stdout.txt").read_text(encoding="utf-8"),
    }


def case_timeout():
    before = {p.name for p in (DATA / "runs").glob("*")}
    proc = launch("selftest_cancel", extra=("--timeout", "25"))
    out, err = proc.communicate(timeout=240)
    fresh = [p for p in (DATA / "runs").glob("*") if p.name not in before]
    run_dir = sorted(fresh, key=lambda p: p.stat().st_mtime)[-1]
    completion = json.loads((run_dir / "completion.json").read_text(encoding="utf-8"))
    return {
        "case": "timeout",
        "run_id": run_dir.name,
        "status": completion["status"],
        "timed_out": completion["timed_out"],
        "exit_code": completion["exit_code"],
        "duration_s": completion["duration_s"],
        "cli_exit": proc.returncode,
    }


def main():
    report = {"cancel": case_cancel(), "timeout": case_timeout()}
    out = ROOT / "verification" / "a1" / "selftest_cancel_timeout.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (report["cancel"]["status"] == "cancelled"
          and report["cancel"]["cancelled"]
          and report["timeout"]["status"] == "timed_out"
          and report["timeout"]["timed_out"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())