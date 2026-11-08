"""ScienceTab 命令行入口（A 档：无界面，跑实验、留记录、可核对）。"""
import argparse
import json
import sys
from pathlib import Path

from . import run as run_mod, spec as spec_mod

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = PROJECT_ROOT / "runspecs"


def _resolve_spec(value):
    path = Path(value)
    if path.exists():
        return spec_mod.load_spec(path)
    candidate = SPEC_DIR / (value if value.endswith(".json") else value + ".json")
    if candidate.exists():
        return spec_mod.load_spec(candidate)
    available = sorted(p.stem for p in SPEC_DIR.glob("*.json")) if SPEC_DIR.is_dir() else []
    raise SystemExit("找不到 RunnerSpec: %s（可用: %s）" % (value, ", ".join(available) or "无"))


def cmd_run(args):
    spec = _resolve_spec(args.spec)
    data_root = spec_mod.default_data_root()

    def emit(message):
        print("[run] %s" % message, file=sys.stderr, flush=True)

    result = run_mod.start_run(spec, data_root, idempotency_key=args.idempotency_key,
                               timeout_s=args.timeout, emit=emit)
    completion = result["completion"]
    print(json.dumps({
        "run_id": result["run_id"],
        "reused": result["reused"],
        "status": completion.get("status"),
        "exit_code": completion.get("exit_code"),
        "duration_s": completion.get("duration_s"),
        "run_dir": str(data_root / "runs" / result["run_id"]),
    }, ensure_ascii=False, indent=2))
    return 0 if completion.get("status") == "succeeded" else 2


def cmd_list(args):
    for item in run_mod.list_runs(spec_mod.default_data_root(), limit=args.limit):
        print("%-28s %-9s %8ss  %s" % (item["run_id"], item["status"],
                                       item["duration_s"], item["spec_id"]))
    return 0


def cmd_show(args):
    record = run_mod.load_run(spec_mod.default_data_root(), args.run_id)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(args):
    marker = run_mod.request_cancel(spec_mod.default_data_root(), args.run_id)
    print("已写入取消请求：%s" % marker)
    return 0


def cmd_verify(args):
    result = run_mod.verify_run(spec_mod.default_data_root(), args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def cmd_compare(args):
    result = run_mod.compare_runs(spec_mod.default_data_root(), args.run_a, args.run_b)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_comparable_identical"] else 2


def cmd_check_spec(args):
    spec = _resolve_spec(args.spec)
    print(json.dumps({
        "id": spec["id"],
        "spec_sha256": spec["spec_sha256"],
        "sources": spec["sources"],
        "expected_outputs": spec.get("expected_outputs", []),
        "timeout_s": spec.get("timeout_s"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args):
    from . import server
    server.serve(spec_mod.default_data_root(), host=args.host, port=args.port)
    return 0


def cmd_reindex(args):
    from . import storage
    data_root = spec_mod.default_data_root()
    con = storage.connect(data_root)
    count = storage.reindex(con, data_root)
    con.close()
    print(json.dumps({"reindexed": count, "data_root": str(data_root)}, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sciencetab", description="本地实验运行与记录")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="运行一个登记的入口")
    p_run.add_argument("--spec", required=True, help="RunnerSpec 路径或 id")
    p_run.add_argument("--idempotency-key", default=None)
    p_run.add_argument("--timeout", type=int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list", help="列出运行")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看一次运行的记录")
    p_show.add_argument("run_id")
    p_show.set_defaults(func=cmd_show)

    p_cancel = sub.add_parser("cancel", help="请求取消正在运行的实验")
    p_cancel.add_argument("run_id")
    p_cancel.set_defaults(func=cmd_cancel)

    p_verify = sub.add_parser("verify", help="重算产物哈希并核对记录")
    p_verify.add_argument("run_id")
    p_verify.set_defaults(func=cmd_verify)

    p_compare = sub.add_parser("compare", help="比较两次运行的产物与验证结果")
    p_compare.add_argument("run_a")
    p_compare.add_argument("run_b")
    p_compare.set_defaults(func=cmd_compare)

    p_check = sub.add_parser("check-spec", help="校验 RunnerSpec 与依赖")
    p_check.add_argument("--spec", required=True)
    p_check.set_defaults(func=cmd_check_spec)

    p_serve = sub.add_parser("serve", help="启动本地只读视图与触发入口")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.set_defaults(func=cmd_serve)

    p_reindex = sub.add_parser("reindex", help="按 run 目录重建 SQLite 索引")
    p_reindex.set_defaults(func=cmd_reindex)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())