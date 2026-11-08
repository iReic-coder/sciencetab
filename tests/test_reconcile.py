"""自检：状态对账把"进程消失但状态仍为 running"的记录标成 interrupted。"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sciencetab import storage  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as tmp:
        data_root = Path(tmp) / "data"
        con = storage.connect(data_root)
        run_dir = data_root / "runs" / "fake_running"
        run_dir.mkdir(parents=True)
        (run_dir / "request.json").write_text("{}", encoding="utf-8")
        storage.start_run(con, "fake_running", "selftest_quick", "0" * 64, run_dir)

        fresh = storage.get_run(con, "fake_running")["status"]
        # 第一次：刚创建，未超时，不应改动
        changed_now = storage.reconcile(con, data_root, stale_after_s=3600)
        status_now = storage.get_run(con, "fake_running")["status"]
        # 第二次：把阈值设为 0，模拟长时间无更新的残留运行
        time.sleep(1.1)
        changed_stale = storage.reconcile(con, data_root, stale_after_s=0)
        status_stale = storage.get_run(con, "fake_running")["status"]

        # 有 completion.json 的不应被标记
        storage.start_run(con, "fake_done", "selftest_quick", "0" * 64, run_dir)
        (run_dir / "completion.json").write_text(json.dumps({"run_id": "fake_done", "status": "succeeded"}),
                                                 encoding="utf-8")
        storage.reconcile(con, data_root, stale_after_s=0)
        with_file = storage.get_run(con, "fake_done")["status"]
        con.close()

    report = {
        "initial_status": fresh,
        "changed_when_fresh": changed_now,
        "status_when_fresh": status_now,
        "changed_when_stale": changed_stale,
        "status_when_stale": status_stale,
        "status_with_completion_file": with_file,
    }
    out = ROOT / "verification" / "b1" / "reconcile.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (fresh == "running" and changed_now == 0 and status_now == "running"
          and status_stale == "interrupted" and with_file == "running")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())