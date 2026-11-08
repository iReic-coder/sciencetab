"""一次实验运行的编排与记录。

目录结构（data/runs/<run_id>/）：
  request.json     用户请求（规格、幂等键、超时）
  spec.json        冻结的 RunnerSpec 副本
  manifest.json    环境、源快照、复制文件哈希
  work/            隔离副本
  logs/            stdout/stderr（原始字节 + GBK 解码文本）
  outputs/         期望产物副本及其哈希
  completion.json  结束状态与判定依据
  summary.md       人读摘要
"""
import csv
import hashlib
import json
import os
import platform
import secrets
import shutil
import time
from pathlib import Path

from . import matlab, spec as spec_mod, storage, workspace

STATUSES = ("queued", "running", "succeeded", "failed", "incomplete",
            "validation_failed", "timed_out", "cancelled")


def _atomic_write(path, payload):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _run_id(spec):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return "%s_%s_%s" % (stamp, spec["id"], secrets.token_hex(2))


def _check_validation(spec, work_root):
    """按规格里的 validation 配置检查产物；没有配置时返回 None。"""
    conf = spec.get("validation")
    if not conf:
        return None
    path = Path(work_root) / conf["file"]
    if not path.exists():
        return {"file": conf["file"], "exists": False, "rows": 0,
                "failures": [], "all_pass": False}
    column = conf.get("pass_column", "Pass")
    failures = []
    rows = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows += 1
            value = (row.get(column) or "").strip().lower()
            if value not in ("1", "true", "pass", "yes"):
                failures.append({"row": rows, "value": row.get(column)})
    if rows == 0:
        failures.append({"row": 0, "value": "empty"})
    return {"file": conf["file"], "exists": True, "rows": rows,
            "failures": failures, "all_pass": not failures}


def _collect_outputs(spec, work_root, outputs_dir):
    collected = []
    for rel in spec.get("expected_outputs", []):
        source = Path(work_root) / spec["entry"]["workdir"] / rel
        record = {"path": rel, "exists": source.exists()}
        if source.exists():
            dest = Path(outputs_dir) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            record["bytes"] = dest.stat().st_size
            record["sha256"] = workspace.sha256_file(dest)
        collected.append(record)
    return collected


def start_run(spec, data_root, idempotency_key=None, timeout_s=None, emit=None):
    """执行一次运行并落盘；相同幂等键直接返回既有运行，不重复启动 MATLAB。"""
    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    con = storage.connect(data_root)
    storage.migrate_index_json(con, data_root)
    storage.reconcile(con, data_root)

    if idempotency_key:
        existing = storage.find_by_idempotency(con, idempotency_key)
        if existing:
            record = load_run(data_root, existing)
            con.close()
            return {"run_id": existing, "reused": True,
                    "completion": record.get("completion", {})}

    run_id = _run_id(spec)
    run_dir = data_root / "runs" / run_id
    work_root = run_dir / "work"
    log_dir = run_dir / "logs"
    outputs_dir = run_dir / "outputs"
    run_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    outputs_dir.mkdir(parents=True)

    request = {
        "run_id": run_id,
        "spec_id": spec["id"],
        "spec_path": spec["_path"],
        "spec_sha256": spec["spec_sha256"],
        "idempotency_key": idempotency_key,
        "timeout_s": timeout_s or spec.get("timeout_s", 900),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _atomic_write(run_dir / "request.json", json.dumps(request, ensure_ascii=False, indent=2))
    _atomic_write(run_dir / "spec.json", json.dumps(spec, ensure_ascii=False, indent=2))
    storage.start_run(con, run_id, spec["id"], spec["spec_sha256"], run_dir, idempotency_key)

    if emit:
        emit("复制依赖副本")
    copies = workspace.build_work_tree(spec, work_root)
    source_snapshot = workspace.snapshot_source(spec)

    matlab_info = matlab.probe_matlab_version(data_root / "env" / "matlab.json")
    manifest = {
        "run_id": run_id,
        "spec_id": spec["id"],
        "spec_sha256": spec["spec_sha256"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "matlab": matlab_info,
        "entry": spec["entry"],
        "copied_files": {"count": len(copies),
                         "bytes": sum(c["bytes"] for c in copies),
                         "files": copies},
        "source_snapshot": source_snapshot,
    }
    _atomic_write(run_dir / "manifest.json",
                  json.dumps(manifest, ensure_ascii=False, indent=2))

    if emit:
        emit("启动 MATLAB 批处理")
    result = matlab.run_matlab(
        command=spec["entry"]["command"],
        workdir=work_root / spec["entry"]["workdir"],
        log_dir=log_dir,
        timeout_s=request["timeout_s"],
        cancel_file=run_dir / "cancel.request",
    )

    if emit:
        emit("收集产物与验证记录")
    outputs = _collect_outputs(spec, work_root, outputs_dir)
    validation = _check_validation(spec, work_root)

    if result["cancelled"]:
        status = "cancelled"
    elif result["timed_out"]:
        status = "timed_out"
    elif result["exit_code"] != 0:
        status = "failed"
    elif any(not o["exists"] for o in outputs):
        status = "incomplete"
    elif validation is not None and not validation["all_pass"]:
        status = "validation_failed"
    else:
        status = "succeeded"

    completion = {
        "run_id": run_id,
        "status": status,
        "exit_code": result["exit_code"],
        "cancelled": result["cancelled"],
        "timed_out": result["timed_out"],
        "duration_s": result["duration_s"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "argv": result["argv"],
        "pid": result["pid"],
        "outputs": outputs,
        "validation": validation,
        "logs": {"stdout": str(log_dir / "stdout.txt"),
                 "stderr": str(log_dir / "stderr.txt")},
        "stdout_tail": result["stdout_tail"],
        "stderr_tail": result["stderr_tail"],
    }
    _atomic_write(run_dir / "completion.json",
                  json.dumps(completion, ensure_ascii=False, indent=2))

    storage.finish_run(con, run_id, status, result["exit_code"],
                       completion["duration_s"], outputs, validation)
    con.close()

    _write_summary(run_dir, request, manifest, completion)
    return {"run_id": run_id, "reused": False, "completion": completion}


def load_run(data_root, run_id):
    run_dir = Path(data_root) / "runs" / run_id
    record = {}
    for name in ("request", "completion", "manifest", "spec"):
        path = run_dir / (name + ".json")
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                record[name] = json.load(f)
    record["run_dir"] = str(run_dir)
    return record


def list_runs(data_root, limit=20):
    con = storage.connect(data_root)
    storage.migrate_index_json(con, data_root)
    storage.reconcile(con, data_root)
    rows = storage.list_runs(con, limit=limit)
    con.close()
    return rows


def request_cancel(data_root, run_id):
    run_dir = Path(data_root) / "runs" / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError("没有这个运行: %s" % run_id)
    marker = run_dir / "cancel.request"
    marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    return str(marker)


def compare_runs(data_root, run_a, run_b):
    """比较两次运行：产物哈希、验证结果、耗时与规格是否同源。

    只比较两次都记录的产物；不判断科学结论，也不合并不同条件的运行。
    """
    left = load_run(data_root, run_a)
    right = load_run(data_root, run_b)
    for side in (left, right):
        if not side.get("completion"):
            raise FileNotFoundError("运行缺少 completion.json: %s" % side["run_dir"])

    def by_path(record):
        return {item["path"]: item.get("sha256") for item in record["completion"]["outputs"]}

    a_map, b_map = by_path(left), by_path(right)
    shared = sorted(set(a_map) & set(b_map))
    only_a = sorted(set(a_map) - set(b_map))
    only_b = sorted(set(b_map) - set(a_map))
    identical, different = [], []
    for path in shared:
        (identical if a_map[path] == b_map[path] else different).append(path)

    same_spec = left["request"]["spec_sha256"] == right["request"]["spec_sha256"]
    comparable = set((left.get("spec") or {}).get("comparable_outputs", []))
    comparable_shared = [p for p in shared if p in comparable]
    comparable_identical = [p for p in comparable_shared if a_map[p] == b_map[p]]
    comparable_different = [p for p in comparable_shared if a_map[p] != b_map[p]]
    metadata_only = [p for p in different if p not in comparable]
    return {
        "a": {"run_id": run_a, "status": left["completion"]["status"],
              "duration_s": left["completion"]["duration_s"]},
        "b": {"run_id": run_b, "status": right["completion"]["status"],
              "duration_s": right["completion"]["duration_s"]},
        "same_spec": same_spec,
        "comparable_identical": comparable_identical,
        "comparable_different": comparable_different,
        "identical_all_outputs": identical,
        "different_non_comparable": metadata_only,
        "only_in_a": only_a,
        "only_in_b": only_b,
        "all_comparable_identical": (not comparable_different and not only_a
                                     and not only_b and bool(comparable_shared)),
        "validation_a": (left["completion"].get("validation") or {}).get("all_pass"),
        "validation_b": (right["completion"].get("validation") or {}).get("all_pass"),
    }


def verify_run(data_root, run_id):
    record = load_run(data_root, run_id)
    completion = record.get("completion")
    if not completion:
        return {"ok": False, "reason": "缺少 completion.json"}
    run_dir = Path(record["run_dir"])
    problems = []
    for item in completion["outputs"]:
        if not item["exists"]:
            problems.append({"path": item["path"], "reason": "missing"})
            continue
        path = run_dir / "outputs" / item["path"]
        if not path.exists():
            problems.append({"path": item["path"], "reason": "removed"})
            continue
        actual = workspace.sha256_file(path)
        if actual != item["sha256"]:
            problems.append({"path": item["path"], "reason": "hash_changed",
                             "expected": item["sha256"], "actual": actual})
    return {"ok": not problems, "run_id": run_id, "problems": problems}


def _write_summary(run_dir, request, manifest, completion):
    lines = [
        "# 运行 %s" % completion["run_id"],
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 实验 | `%s` |" % manifest["spec_id"],
        "| 状态 | **%s** |" % completion["status"],
        "| 退出码 | %s |" % completion["exit_code"],
        "| 耗时 | %s s |" % completion["duration_s"],
        "| MATLAB | %s（%s） |" % (manifest["matlab"]["version"], manifest["matlab"]["release"]),
        "| 副本文件 | %d 个 / %.2f MB |" % (
            manifest["copied_files"]["count"],
            manifest["copied_files"]["bytes"] / 1048576.0),
        "| 结束时间 | %s |" % completion["finished_at"],
        "",
        "## 产物",
        "",
    ]
    for item in completion["outputs"]:
        mark = "OK" if item["exists"] else "缺"
        lines.append("- `%s` %s %s" % (item["path"], mark,
                                       item.get("sha256", "")[:16]))
    if completion["validation"]:
        v = completion["validation"]
        lines += ["", "## 验证", "",
                  "- 文件 `%s`，%d 行，全通过：%s" % (v["file"], v["rows"], v["all_pass"])]
        for failure in v["failures"][:5]:
            lines.append("  - 失败行 %s：`%s`" % (failure["row"], failure["value"]))
    lines += ["", "## 日志尾部", "", "```", *(completion["stdout_tail"][-12:]), "```", ""]
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")