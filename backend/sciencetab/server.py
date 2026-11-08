"""本地只读视图 + 触发运行的最小 Web 服务。

只用标准库：绑定 127.0.0.1，服务端渲染 HTML，不引入 Node/React。
页面：运行列表、运行详情、两次运行对比、登记入口。POST 需要同源表单令牌。
"""
import html
import json
import os
import secrets
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import run as run_mod, spec as spec_mod, storage, workspace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = PROJECT_ROOT / "runspecs"
DRY_RUN_WINDOW_S = 3600

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:24px 32px 64px; background:#131417; color:#e6e7ea;
       font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
a { color:#7fb2ff; text-decoration:none; }
a:hover { text-decoration:underline; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:16px; margin:28px 0 8px; color:#c9cbd1; font-weight:600; }
.muted { color:#8b8f99; font-size:13px; }
table { border-collapse:collapse; width:100%; margin:8px 0 4px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid #262930; vertical-align:top; }
th { color:#9aa0ab; font-weight:600; font-size:13px; }
tr:hover td { background:#191b20; }
code, pre, .mono { font-family:Consolas,"Cascadia Mono",monospace; font-size:13px; }
pre { background:#0d0e11; border:1px solid #23262d; border-radius:6px; padding:12px;
      overflow:auto; max-height:420px; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; }
.s-succeeded { background:#173a26; color:#7ee2a8; }
.s-failed, .s-validation_failed { background:#3d1a1d; color:#ff9b9b; }
.s-cancelled, .s-timed_out, .s-interrupted { background:#3a3317; color:#e8cf7a; }
.s-running { background:#18283d; color:#8fc4ff; }
.s-incomplete { background:#3a2a17; color:#f0b177; }
.card { background:#17191e; border:1px solid #23262d; border-radius:8px; padding:14px 16px; margin:10px 0; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
.kv .k { color:#8b8f99; font-size:12px; }
.bar { display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; }
button, .btn { background:#22304a; color:#dce8ff; border:1px solid #33456b; border-radius:6px;
               padding:5px 12px; font-size:13px; cursor:pointer; }
button:hover { background:#2b3d5e; }
input[type=text] { background:#0d0e11; color:#e6e7ea; border:1px solid #2b2f38;
                   border-radius:5px; padding:5px 8px; min-width:280px; }
"""


def esc(value):
    return html.escape("" if value is None else str(value))


def _read_json_helper(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def badge(status):
    return '<span class="badge s-%s">%s</span>' % (esc(status), esc(status))


def fmt_bytes(n):
    if n is None:
        return "—"
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def page(title, body, token=None):
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · ScienceTab</title><style>%s</style></head>
<body>
<div class="bar"><a href="/">ScienceTab</a><span class="muted">本地实验工作台</span></div>
<h1>%s</h1>
%s
</body></html>""" % (esc(title), CSS, esc(title), body)


class Handler(BaseHTTPRequestHandler):
    server_version = "ScienceTab/0.2"

    # --- 基础设施 -------------------------------------------------------
    def log_message(self, fmt, *args):  # 安静一些，避免刷屏
        sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, body, status=200, content_type="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _data_root(self):
        return Path(self.server.data_root)

    def _db(self):
        con = storage.connect(self._data_root())
        storage.reconcile(con, self._data_root())
        return con

    @staticmethod
    def _read_json(path):
        return _read_json_helper(path)

    # --- 路由 -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                return self._runs_page(query)
            if path == "/specs":
                return self._specs_page()
            if path.startswith("/specs/"):
                return self._spec_page(path.split("/")[2])
            if path.startswith("/runs/"):
                parts = path.split("/")
                if len(parts) == 3:
                    return self._run_page(parts[2])
                if len(parts) == 4 and parts[3] == "artifact":
                    return self._artifact(parts[2], query)
                if len(parts) == 4 and parts[3] == "log":
                    return self._log(parts[2], query)
                if len(parts) == 4 and parts[3] == "raw":
                    return self._raw(parts[2], query)
                if len(parts) == 4 and parts[3] == "export":
                    return self._export(parts[2], query)
            if path == "/compare":
                return self._compare_page(query)
            if path == "/api/runs":
                return self._api_runs()
            if path.startswith("/api/runs/"):
                return self._api_run(path.split("/")[3])
            if path == "/api/specs":
                return self._api_specs()
            if path == "/healthz":
                return self._send(json.dumps({"ok": True}), content_type="application/json")
            return self._send(page("未找到", "<p class='muted'>没有这个页面。</p>"), status=404)
        except FileNotFoundError as exc:
            return self._send(page("未找到", "<p class='muted'>%s</p>" % esc(exc)), status=404)
        except Exception as exc:  # noqa: BLE001 - 页面出错也要可读
            return self._send(page("出错了", "<pre>%s: %s</pre>" % (esc(type(exc).__name__), esc(exc))),
                              status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = parse_qs(raw)
        token = (form.get("token") or [""])[0]
        if token != self.server.csrf_token:
            return self._send(page("拒绝", "<p class='muted'>表单令牌无效，请从页面重新操作。</p>"),
                              status=403)
        try:
            if parsed.path == "/start":
                return self._start_run(form)
            if parsed.path.startswith("/runs/") and parsed.path.endswith("/cancel"):
                run_id = parsed.path.split("/")[2]
                run_mod.request_cancel(self._data_root(), run_id)
                return self._redirect("/")
        except Exception as exc:  # noqa: BLE001
            return self._send(page("出错了", "<pre>%s</pre>" % esc(exc)), status=500)
        return self._send(page("未找到", "<p class='muted'>没有这个动作。</p>"), status=404)

    def _redirect(self, location, status=303):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- 列表 -----------------------------------------------------------
    def _spec_page(self, spec_id):
        spec_path = SPEC_DIR / (spec_id + ".json")
        if not spec_path.exists():
            raise FileNotFoundError("没有这个入口: %s" % spec_id)
        spec = spec_mod.load_spec(spec_path)
        con = self._db()
        runs = storage.list_runs(con, limit=50, spec_id=spec_id)
        con.close()
        rows = "".join(
            "<tr><td><a class='mono' href='/runs/%s'>%s</a></td><td>%s</td><td>%s</td><td class='mono'>%s</td></tr>" % (
                esc(r["run_id"]), esc(r["run_id"]), badge(r["status"]),
                "%ss" % r["duration_s"] if r["duration_s"] is not None else "—",
                esc((r["finished_at"] or "")[:19]))
            for r in runs) or "<tr><td colspan='4' class='muted'>还没有运行</td></tr>"
        sources = "".join("<li><code>%s</code><br><span class='muted'>%s</span></li>" % (
            esc(s.get("root")), esc(", ".join(s.get("include", [])))) for s in spec["sources"])
        outputs = "".join("<li class='mono'>%s</li>" % esc(p) for p in spec.get("expected_outputs", []))
        body = """
<div class="card">
  <h1>%s</h1>
  <p class="muted">%s</p>
  <div class="grid">
    <div class="kv"><div class="k">入口 id</div><span class="mono">%s</span></div>
    <div class="kv"><div class="k">工作目录</div><span class="mono">%s</span></div>
    <div class="kv"><div class="k">命令</div><span class="mono">%s</span></div>
    <div class="kv"><div class="k">规格哈希</div><span class="mono">%s</span></div>
    <div class="kv"><div class="k">超时</div>%s s</div>
  </div>
  <form method="post" action="/start" style="margin-top:12px">
    <input type="hidden" name="spec" value="%s">
    <input type="hidden" name="token" value="%s">
    <button type="submit">运行这个入口</button>
  </form>
</div>
<h2>依赖（只读挂载）</h2><ul class="muted">%s</ul>
<h2>期望产物</h2><ul class="muted">%s</ul>
<h2>该入口的运行</h2>
<table><tr><th>run_id</th><th>状态</th><th>耗时</th><th>结束时间</th></tr>%s</table>
""" % (esc(spec.get("name", spec_id)), esc(spec.get("description", "")), esc(spec["id"]),
       esc(spec["entry"]["workdir"]), esc(spec["entry"]["command"]),
       esc(spec["spec_sha256"][:16]), esc(spec.get("timeout_s")),
       esc(spec["id"]), esc(self.server.csrf_token), sources, outputs, rows)
        return self._send(page(spec.get("name", spec_id), body))

    def _runs_page(self, query):
        con = self._db()
        spec_filter = (query.get("spec") or [None])[0]
        runs = storage.list_runs(con, limit=100, spec_id=spec_filter)
        specs = storage.spec_summary(con)
        con.close()

        rows = []
        for item in runs:
            rows.append(
                "<tr><td><a class='mono' href='/runs/%s'>%s</a></td><td>%s</td>"
                "<td>%s</td><td>%s</td><td class='mono'>%s</td></tr>" % (
                    esc(item["run_id"]), esc(item["run_id"]), badge(item["status"]),
                    esc(item["spec_id"]),
                    "%ss" % (item["duration_s"],) if item["duration_s"] is not None else "—",
                    esc((item["finished_at"] or item["created_at"] or "")[:19])))

        spec_rows = []
        for item in specs:
            spec_rows.append(
                "<tr><td><a href='/?spec=%s'>%s</a></td><td>%s</td><td>%s</td><td class='mono'>%s</td>"
                "<td><form method='post' action='/start' style='margin:0'>"
                "<input type='hidden' name='spec' value='%s'>"
                "<input type='hidden' name='token' value='%s'>"
                "<button type='submit'>运行</button></form></td></tr>" % (
                    quote(item["spec_id"]), esc(item["spec_id"]), item["runs"],
                    item["succeeded"] or 0, esc(item["last_finished"] or "—"),
                    esc(item["spec_id"]), esc(self.server.csrf_token)))

        body = """
<p class="muted">共 {n} 次运行{scope}。页面只读；运行由登记入口触发，取消在详情页。</p>
<h2>登记入口</h2>
<table><tr><th>入口</th><th>运行数</th><th>成功</th><th>最近结束</th><th></th></tr>{specs}</table>
<h2>运行列表</h2>
<table><tr><th>run_id</th><th>状态</th><th>入口</th><th>耗时</th><th>结束时间</th></tr>{rows}</table>
""".format(n=len(runs),
           scope="（筛选：%s）" % esc(spec_filter) if spec_filter else "",
           specs="".join(spec_rows) or "<tr><td colspan='5' class='muted'>还没有登记入口</td></tr>",
           rows="".join(rows) or "<tr><td colspan='5' class='muted'>还没有运行</td></tr>")
        return self._send(page("运行列表", body))

    # --- 详情 -----------------------------------------------------------
    def _run_page(self, run_id):
        con = self._db()
        record = storage.get_run(con, run_id)
        con.close()
        if record is None:
            raise FileNotFoundError("没有这个运行: %s" % run_id)
        run_dir = Path(record["run_dir"])
        request = self._read_json(run_dir / "request.json") or {}
        completion = self._read_json(run_dir / "completion.json") or {}
        manifest = self._read_json(run_dir / "manifest.json") or {}

        artifacts = "".join(
            "<tr><td class='mono'>%s</td><td>%s</td><td class='mono'>%s</td>"
            "<td><a href='/runs/%s/artifact?path=%s'>下载</a></td></tr>" % (
                esc(a["path"]), fmt_bytes(a["bytes"]), esc((a["sha256"] or "")[:16]),
                esc(run_id), quote(a["path"], safe=""))
            for a in record["artifacts"]) or "<tr><td colspan='4' class='muted'>没有产物</td></tr>"

        validation = record["validation"]
        if validation:
            detail = completion.get("validation") or {}
            failures = detail.get("failures") or []
            vtext = ("<table><tr><th>文件</th><th>行数</th><th>全部通过</th></tr>"
                     "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td></tr></table>" % (
                         esc(validation["file"]), validation["rows"],
                         "是" if validation["all_pass"] else "否"))
            if failures:
                vtext += "<pre>" + esc(json.dumps(failures[:10], ensure_ascii=False, indent=2)) + "</pre>"
        else:
            vtext = "<p class='muted'>这次运行没有登记验证判据。</p>"

        spec_doc = self._read_json(run_dir / "spec.json") or {}
        sources = "".join(
            "<li><code>%s</code><br><span class='muted'>%s</span></li>" % (
                esc(src.get("root")), esc(", ".join(src.get("include", []))))
            for src in spec_doc.get("sources", []))

        tail = "\n".join((completion.get("stdout_tail") or [])[-40:])
        actions = ""
        if record["status"] == "running":
            actions = ("<form method='post' action='/runs/%s/cancel' style='display:inline'>"
                       "<input type='hidden' name='token' value='%s'>"
                       "<button type='submit'>取消运行</button></form> " % (
                           esc(run_id), esc(self.server.csrf_token)))

        body = """
<div class="card">
  <div class="bar"><h1 class="mono" style="font-size:18px">%s</h1>%s</div>
  <div class="grid" style="margin-top:10px">
    <div class="kv"><div class="k">入口</div>%s</div>
    <div class="kv"><div class="k">耗时</div>%s s</div>
    <div class="kv"><div class="k">退出码</div>%s</div>
    <div class="kv"><div class="k">MATLAB</div>%s</div>
    <div class="kv"><div class="k">结束时间</div>%s</div>
    <div class="kv"><div class="k">规格哈希</div><span class="mono">%s</span></div>
  </div>
  <p style="margin:12px 0 0">%s<a href="/runs/%s/log?stream=stdout">stdout</a> ·
  <a href="/runs/%s/log?stream=stderr">stderr</a> ·
  <a href="/runs/%s/raw?name=manifest.json">manifest</a> ·
  <a href="/runs/%s/raw?name=completion.json">completion</a>
  <a href="/runs/%s/export?format=json">导出来源</a>
  <a href="/compare?a=%s&b=" style="margin-left:12px">与另一次运行对比</a></p>
</div>
<h2>产物</h2>
<table><tr><th>路径</th><th>大小</th><th>SHA-256</th><th></th></tr>%s</table>
<h2>验证</h2>%s
<h2>入口与依赖</h2>
<div class="card"><div class="k muted">工作目录</div><code>%s</code>
<div class="k muted" style="margin-top:8px">命令</div><code>%s</code>
<div class="k muted" style="margin-top:8px">副本</div>%s 个文件 / %s
<ul class="muted" style="margin:8px 0 0">%s</ul></div>
<h2>stdout 尾部</h2><pre>%s</pre>
""" % (esc(run_id), badge(record["status"]), esc(record["spec_id"]),
       record["duration_s"] if record["duration_s"] is not None else "—",
       esc(completion.get("exit_code")), esc((manifest.get("matlab") or {}).get("version", "—")),
       esc((record["finished_at"] or "")[:19]), esc((record["spec_sha256"] or "")[:16]),
       actions, esc(run_id), esc(run_id), esc(run_id), esc(run_id),
       esc(run_id), esc(run_id), artifacts, vtext,
       esc((manifest.get("entry") or {}).get("workdir", "")),
       esc((manifest.get("entry") or {}).get("command", "")),
       esc((manifest.get("copied_files") or {}).get("count")),
       fmt_bytes((manifest.get("copied_files") or {}).get("bytes")), sources,
       esc(tail))
        return self._send(page(run_id, body))

    # --- 对比 -----------------------------------------------------------
    def _compare_page(self, query):
        a = (query.get("a") or [""])[0]
        b = (query.get("b") or [""])[0]
        con = self._db()
        runs = storage.list_runs(con, limit=100)
        con.close()
        options = "".join("<option value='%s'%s>%s · %s</option>" % (
            esc(r["run_id"]), " selected" if r["run_id"] == b else "",
            esc(r["run_id"]), esc(r["status"])) for r in runs)
        form = ("<form method='get' action='/compare' class='bar'>"
                "<input type='text' name='a' value='%s' placeholder='run A'>"
                "<select name='b' style='background:#0d0e11;color:#e6e7ea;border:1px solid #2b2f38;"
                "border-radius:5px;padding:5px'>%s</select>"
                "<button type='submit'>对比</button></form>" % (esc(a), options))

        body = "<p class='muted'>只比较同一规格记录的产物哈希，不合并不同实验条件的运行。</p>" + form
        if a and b:
            result = run_mod.compare_runs(self._data_root(), a, b)
            rows = []
            for path in result["comparable_identical"]:
                rows.append("<tr><td class='mono'>%s</td><td>一致</td></tr>" % esc(path))
            for path in result["comparable_different"]:
                rows.append("<tr><td class='mono'>%s</td><td><b>不同</b></td></tr>" % esc(path))
            for path in result["only_in_a"]:
                rows.append("<tr><td class='mono'>%s</td><td>只在 A</td></tr>" % esc(path))
            for path in result["only_in_b"]:
                rows.append("<tr><td class='mono'>%s</td><td>只在 B</td></tr>" % esc(path))
            body += """
<div class="card"><div class="bar">%s → %s</div>
<div class="grid" style="margin-top:8px">
<div class="kv"><div class="k">同一规格</div>%s</div>
<div class="kv"><div class="k">可比产物全部一致</div>%s</div>
<div class="kv"><div class="k">A 验证</div>%s</div>
<div class="kv"><div class="k">B 验证</div>%s</div></div></div>
<table><tr><th>可比产物</th><th>结果</th></tr>%s</table>
<p class="muted">图件与 run_log.txt 含生成时间，通常不同，不计入可比产物。</p>
""" % (badge(result["a"]["status"]), badge(result["b"]["status"]),
       "是" if result["same_spec"] else "否",
       "是" if result["all_comparable_identical"] else "否",
       "通过" if result["validation_a"] else "未通过/未登记",
       "通过" if result["validation_b"] else "未通过/未登记",
       "".join(rows) or "<tr><td colspan='2' class='muted'>没有可比的共同产物</td></tr>")
        return self._send(page("运行对比", body))

    # --- 原始文件 -------------------------------------------------------
    def _artifact(self, run_id, query):
        rel = (query.get("path") or [""])[0]
        base = (self._data_root() / "runs" / run_id / "outputs").resolve()
        target = (base / rel).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            raise FileNotFoundError("产物不存在或越界: %s" % rel)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % quote(target.name))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _log(self, run_id, query):
        stream = (query.get("stream") or ["stdout"])[0]
        if stream not in ("stdout", "stderr"):
            raise FileNotFoundError("未知日志流: %s" % stream)
        path = self._data_root() / "runs" / run_id / "logs" / (stream + ".txt")
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        body = "<p><a href='/runs/%s'>返回运行</a> · %s（按 GBK 解码）</p><pre>%s</pre>" % (
            esc(run_id), esc(stream), esc(text))
        return self._send(page("%s · %s" % (run_id, stream), body))

    def _raw(self, run_id, query):
        name = (query.get("name") or [""])[0]
        allowed = {"manifest.json", "completion.json", "request.json", "spec.json", "summary.md"}
        if name not in allowed:
            raise FileNotFoundError("不允许读取: %s" % name)
        path = self._data_root() / "runs" / run_id / name
        if not path.exists():
            raise FileNotFoundError(name)
        text = path.read_text(encoding="utf-8", errors="replace")
        if name == "summary.md":
            return self._send(page("%s · summary" % run_id, "<pre>%s</pre>" % esc(text)))
        return self._send(text, content_type="application/json; charset=utf-8")

    def _export(self, run_id, query):
        """导出来源清单：一次运行用到的配置、依赖哈希、产物与验证记录。"""
        run_dir = self._data_root() / "runs" / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(run_id)
        request = self._read_json(run_dir / "request.json") or {}
        spec = self._read_json(run_dir / "spec.json") or {}
        manifest = self._read_json(run_dir / "manifest.json") or {}
        completion = self._read_json(run_dir / "completion.json") or {}
        copied = (manifest.get("copied_files") or {})
        bundle = {
            "schema": "sciencetab.provenance/0.1",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run_id": run_id,
            "request": request,
            "spec": {k: spec.get(k) for k in
                     ("id", "name", "entry", "sources", "expected_outputs",
                      "comparable_outputs", "timeout_s", "spec_sha256")},
            "environment": {
                "matlab": manifest.get("matlab"),
                "platform": manifest.get("platform"),
                "python": manifest.get("python"),
                "host": manifest.get("host"),
            },
            "dependencies": {
                "count": copied.get("count"),
                "bytes": copied.get("bytes"),
                "files": [{k: f.get(k) for k in ("path", "bytes", "sha256", "role")}
                          for f in copied.get("files", [])],
            },
            "completion": completion,
            "artifacts": completion.get("outputs", []),
            "validation": completion.get("validation"),
        }
        data = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"provenance-%s.json\"" % run_id)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- JSON API -------------------------------------------------------
    def _api_runs(self):
        query = parse_qs(urlparse(self.path).query)
        spec_filter = (query.get("spec") or [None])[0]
        con = self._db()
        runs = storage.list_runs(con, limit=100, spec_id=spec_filter)
        specs = storage.spec_summary(con)
        con.close()
        return self._send(json.dumps({"runs": runs, "specs": specs}, ensure_ascii=False, indent=2),
                          content_type="application/json; charset=utf-8")

    def _api_run(self, run_id):
        con = self._db()
        record = storage.get_run(con, run_id)
        con.close()
        if record is None:
            raise FileNotFoundError(run_id)
        return self._send(json.dumps(record, ensure_ascii=False, indent=2),
                          content_type="application/json; charset=utf-8")

    def _api_specs(self):
        return self._send(json.dumps(self._spec_list(), ensure_ascii=False, indent=2),
                          content_type="application/json; charset=utf-8")

    # --- 触发运行 -------------------------------------------------------
    def _start_run(self, form):
        spec_id = (form.get("spec") or [""])[0]
        spec_path = SPEC_DIR / (spec_id + ".json")
        if not spec_path.exists():
            raise FileNotFoundError("没有这个入口: %s" % spec_id)
        spec_mod.load_spec(spec_path)  # 依赖缺失时在提交前暴露

        log_dir = self._data_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        log_path = log_dir / ("submit-%s-%s.log" % (stamp, spec_id))
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "backend"))
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with open(log_path, "wb") as log:
            subprocess.Popen(
                [sys.executable, "-X", "utf8", "-m", "sciencetab", "run", "--spec", spec_id],
                cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=log,
                stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        return self._redirect("/")

    def _specs_page(self):
        rows = "".join(
            "<tr><td class='mono'>%s</td><td>%s</td><td class='mono'>%s</td></tr>" % (
                esc(item["id"]), esc(item.get("name", "")),
                esc((item.get("entry") or {}).get("command", "")))
            for item in self._spec_list())
        body = ("<p class='muted'>登记入口来自 <code>runspecs/*.json</code>；依赖缺失时不会出现在这里。</p>"
                "<table><tr><th>id</th><th>名称</th><th>命令</th></tr>%s</table>" % rows)
        return self._send(page("登记入口", body))

    def _spec_list(self):
        items = []
        for path in sorted(SPEC_DIR.glob("*.json")):
            try:
                spec = spec_mod.load_spec(path)
            except Exception:  # noqa: BLE001 - 依赖缺失时跳过，不让整页失败
                continue
            items.append({"id": spec["id"], "name": spec.get("name", ""),
                          "entry": spec.get("entry", {})})
        return items


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_root):
        self.data_root = str(data_root)
        self.csrf_token = secrets.token_urlsafe(18)
        super().__init__(address, Handler)


def serve(data_root, host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("只允许绑定本机回环地址")
    con = storage.connect(data_root)
    storage.migrate_index_json(con, data_root)
    fixed = storage.reconcile(con, data_root)
    con.close()
    if fixed:
        print("[serve] 标为 interrupted 的运行: %d" % fixed)
    httpd = Server((host, port), data_root)
    print("ScienceTab 本地服务: http://%s:%d/  (数据目录 %s)" % (host, port, data_root))
    print("Ctrl+C 停止；关闭页面不会中断正在运行的计算。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()