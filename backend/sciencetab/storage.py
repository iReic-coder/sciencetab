"""SQLite 索引：运行、产物、验证记录的查询视图。

JSON 文件仍是权威证据（request/spec/manifest/completion），数据库只做索引与查询；
两者不一致时以 run 目录内的文件为准，可用 `sciencetab reindex` 重建。
"""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  spec_id TEXT NOT NULL,
  spec_sha256 TEXT,
  status TEXT NOT NULL,
  exit_code INTEGER,
  duration_s REAL,
  started_at TEXT,
  finished_at TEXT,
  run_dir TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_spec ON runs(spec_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS artifacts (
  run_id TEXT NOT NULL,
  path TEXT NOT NULL,
  bytes INTEGER,
  sha256 TEXT,
  present INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (run_id, path)
);
CREATE TABLE IF NOT EXISTS validations (
  run_id TEXT PRIMARY KEY,
  file TEXT,
  rows INTEGER,
  all_pass INTEGER
);
"""

STATUS_TERMINAL = ("succeeded", "failed", "incomplete", "validation_failed",
                   "timed_out", "cancelled", "interrupted")


def connect(data_root):
    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(data_root / "metadata.sqlite"), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    init(con)
    return con


def init(con):
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')")
    con.commit()


def migrate_index_json(con, data_root):
    """首次使用时把旧的 data/index.json 迁进数据库；保留原文件供核对。"""
    path = Path(data_root) / "index.json"
    if not path.exists():
        return 0
    try:
        legacy = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0
    moved = 0
    for item in legacy.get("runs", []):
        run_dir = Path(data_root) / "runs" / item["run_id"]
        completion = {}
        if (run_dir / "completion.json").exists():
            try:
                completion = json.loads((run_dir / "completion.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                completion = {}
        key = None
        for idem, rid in (legacy.get("idempotency") or {}).items():
            if rid == item["run_id"]:
                key = idem
        con.execute(
            "INSERT OR IGNORE INTO runs(run_id, spec_id, status, exit_code, duration_s,"
            " finished_at, run_dir, idempotency_key, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (item["run_id"], item.get("spec_id", ""), item.get("status", "unknown"),
             completion.get("exit_code"), item.get("duration_s", completion.get("duration_s")),
             item.get("finished_at", completion.get("finished_at")), str(run_dir), key,
             item.get("finished_at") or time.strftime("%Y-%m-%dT%H:%M:%S")))
        if completion:
            _write_artifacts(con, item["run_id"], completion.get("outputs", []))
            _write_validation(con, item["run_id"], completion.get("validation"))
        moved += 1
    con.commit()
    if moved:
        path.replace(Path(str(path) + ".migrated"))
    return moved


def reindex(con, data_root):
    """以 run 目录内的 completion.json 为准重建索引。"""
    con.execute("DELETE FROM runs")
    con.execute("DELETE FROM artifacts")
    con.execute("DELETE FROM validations")
    count = 0
    for run_dir in sorted((Path(data_root) / "runs").glob("*")):
        completion_path = run_dir / "completion.json"
        if not completion_path.exists():
            continue
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        request = {}
        if (run_dir / "request.json").exists():
            request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        con.execute(
            "INSERT OR REPLACE INTO runs(run_id, spec_id, spec_sha256, status, exit_code,"
            " duration_s, finished_at, run_dir, idempotency_key, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (completion["run_id"], request.get("spec_id", ""), request.get("spec_sha256"),
             completion.get("status"), completion.get("exit_code"), completion.get("duration_s"),
             completion.get("finished_at"), str(run_dir), request.get("idempotency_key"),
             request.get("requested_at") or time.strftime("%Y-%m-%dT%H:%M:%S")))
        _write_artifacts(con, completion["run_id"], completion.get("outputs", []))
        _write_validation(con, completion["run_id"], completion.get("validation"))
        count += 1
    con.commit()
    return count


def _write_artifacts(con, run_id, outputs):
    for item in outputs:
        con.execute(
            "INSERT OR REPLACE INTO artifacts(run_id, path, bytes, sha256, present)"
            " VALUES(?,?,?,?,?)",
            (run_id, item["path"], item.get("bytes"), item.get("sha256"),
             1 if item.get("exists") else 0))


def _write_validation(con, run_id, validation):
    if not validation:
        return
    con.execute(
        "INSERT OR REPLACE INTO validations(run_id, file, rows, all_pass) VALUES(?,?,?,?)",
        (run_id, validation.get("file"), validation.get("rows"),
         1 if validation.get("all_pass") else 0))


def start_run(con, run_id, spec_id, spec_sha256, run_dir, idempotency_key=None):
    con.execute(
        "INSERT INTO runs(run_id, spec_id, spec_sha256, status, run_dir, idempotency_key,"
        " started_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, spec_id, spec_sha256, "running", str(run_dir), idempotency_key,
         time.strftime("%Y-%m-%dT%H:%M:%S"), time.strftime("%Y-%m-%dT%H:%M:%S")))
    con.commit()


def finish_run(con, run_id, status, exit_code, duration_s, outputs, validation):
    con.execute(
        "UPDATE runs SET status=?, exit_code=?, duration_s=?, finished_at=? WHERE run_id=?",
        (status, exit_code, duration_s, time.strftime("%Y-%m-%dT%H:%M:%S"), run_id))
    _write_artifacts(con, run_id, outputs)
    _write_validation(con, run_id, validation)
    con.commit()


def find_by_idempotency(con, key):
    row = con.execute("SELECT run_id FROM runs WHERE idempotency_key=?", (key,)).fetchone()
    return row["run_id"] if row else None


def list_runs(con, limit=50, spec_id=None, status=None):
    sql = "SELECT * FROM runs"
    where, args = [], []
    if spec_id:
        where.append("spec_id=?")
        args.append(spec_id)
    if status:
        where.append("status=?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
    args.append(limit)
    return [dict(row) for row in con.execute(sql, args)]


def get_run(con, run_id):
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    record = dict(row)
    record["artifacts"] = [dict(r) for r in con.execute(
        "SELECT path, bytes, sha256, present FROM artifacts WHERE run_id=? ORDER BY path",
        (run_id,))]
    record["validation"] = con.execute(
        "SELECT file, rows, all_pass FROM validations WHERE run_id=?", (run_id,)).fetchone()
    record["validation"] = dict(record["validation"]) if record["validation"] else None
    return record


def spec_summary(con):
    rows = con.execute(
        "SELECT spec_id, COUNT(*) AS runs,"
        " SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,"
        " MAX(finished_at) AS last_finished"
        " FROM runs GROUP BY spec_id ORDER BY last_finished DESC").fetchall()
    return [dict(r) for r in rows]


def reconcile(con, data_root, stale_after_s=3600):
    """把进程已消失但状态仍是 running 的记录标成 interrupted；不猜结果。"""
    now = time.time()
    fixed = 0
    rows = con.execute("SELECT run_id, run_dir FROM runs WHERE status='running'").fetchall()
    for row in rows:
        run_dir = Path(row["run_dir"])
        completion = run_dir / "completion.json"
        if completion.exists():
            continue
        latest = max([p.stat().st_mtime for p in run_dir.rglob("*") if p.is_file()] or [run_dir.stat().st_mtime])
        if now - latest > stale_after_s:
            con.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE run_id=?",
                        (time.strftime("%Y-%m-%dT%H:%M:%S"), row["run_id"]))
            fixed += 1
    if fixed:
        con.commit()
    return fixed