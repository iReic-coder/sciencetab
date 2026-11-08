"""本地工作台 Web 服务：服务端渲染首屏 + JSON 接口 + 少量前端脚本做实时交互。

只用标准库。绑定 127.0.0.1；POST 需要表单令牌（表单用 hidden 字段，脚本用 X-CSRF-Token）。
"""
import json
import os
import secrets
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import run as run_mod, spec as spec_mod, storage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = PROJECT_ROOT / "runspecs"
WEB_DIR = Path(__file__).resolve().parent / "web"

STATIC_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


def esc(value):
    import html as _html
    return _html.escape("" if value is None else str(value))


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


def fmt_time(value):
    return esc((value or "")[:19].replace("T", " ") or "—")


def layout(page_id, title, body, csrf, extra_head=""):
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="%s">
<title>%s · ScienceTab</title>
<link rel="stylesheet" href="/static/app.css">
%s</head>
<body data-page="%s">
<header class="top">
  <span class="brand">ScienceTab</span>
  <nav><a href="/">运行列表</a><a href="/specs">登记入口</a><a href="/compare">运行对比</a></nav>
  <span class="spacer"></span>
  <span class="hint" data-active-count>—</span>
</header>
<main>
%s
</main>
<div class="toast"></div>
<script src="/static/app.js" defer></script>
</body></html>""" % (esc(csrf), esc(title), extra_head, esc(page_id), body)


def log_text(run_dir, stream):
    """运行中读原始字节实时解码，结束后优先用已解码副本。"""
    run_dir = Path(run_dir)
    decoded = run_dir / "logs" / (stream + ".txt")
    if decoded.exists():
        return decoded.read_text(encoding="utf-8", errors="replace")
    raw = run_dir / "logs" / (stream + ".raw")
    if raw.exists():
        return raw.read_bytes().decode("gbk", "replace")
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "ScienceTab/0.3"

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))

    # ---------- 基础 ----------
    def _send(self, body, status=200, content_type="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload, status=200):
        return self._send(json.dumps(payload, ensure_ascii=False, indent=2), status=status,
                          content_type="application/json; charset=utf-8")

    def _data_root(self):
        return Path(self.server.data_root)

    def _db(self):
        con = storage.connect(self._data_root())
        storage.reconcile(con, self._data_root())
        return con

    @staticmethod
    def _read_json(path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _redirect(self, location, status=303):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 路由 ----------
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
            if path.startswith("/static/"):
                return self._static(path.split("/")[2])
            if path.startswith("/runs/"):
                parts = path.split("/")
                if len(parts) == 3:
                    return self._run_page(parts[2])
                if len(parts) == 4 and parts[3] == "artifact":
                    return self._artifact(parts[2], query)
                if len(parts) == 4 and parts[3] == "export":
                    return self._export(parts[2])
            if path == "/compare":
                return self._compare_page(query)
            if path == "/api/runs":
                return self._api_runs(query)
            if path.startswith("/api/runs/"):
                parts = path.split("/")
                if len(parts) == 4:
                    return self._api_run(parts[3])
                if len(parts) == 5 and parts[4] == "log":
                    return self._api_log(parts[3], query)
            if path == "/api/specs":
                return self._json(self._spec_list())
            if path == "/api/compare":
                return self._api_compare(query)
            if path == "/healthz":
                return self._json({"ok": True})
            return self._not_found("没有这个页面")
        except FileNotFoundError as exc:
            return self._not_found(str(exc))
        except run_mod.BusyError as exc:
            return self._json({"error": "busy", "running": exc.run_id,
                               "message": "已有运行在进行"}, status=409)
        except Exception as exc:  # noqa: BLE001
            return self._error_page(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        is_json = "application/json" in (self.headers.get("Content-Type") or "")
        token = self.headers.get("X-CSRF-Token") or ""
        payload = {}
        if is_json:
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                return self._json({"error": "bad_json"}, status=400)
        else:
            form = parse_qs(raw)
            token = token or (form.get("token") or [""])[0]
            payload = {k: v[0] for k, v in form.items()}
        if token != self.server.csrf_token:
            if is_json:
                return self._json({"error": "csrf"}, status=403)
            return self._send(layout("error", "拒绝", "<p class='muted'>表单令牌无效。</p>",
                                     self.server.csrf_token), status=403)
        try:
            if parsed.path == "/api/start":
                return self._api_start(payload)
            if parsed.path == "/api/cancel":
                return self._api_cancel(payload)
            if parsed.path == "/start":
                run_id = self._spawn(payload.get("spec"))
                return self._redirect("/runs/%s" % run_id if run_id else "/")
            if parsed.path.startswith("/runs/") and parsed.path.endswith("/cancel"):
                run_mod.request_cancel(self._data_root(), parsed.path.split("/")[2])
                return self._redirect("/")
        except run_mod.BusyError as exc:
            return self._json({"error": "busy", "running": exc.run_id,
                               "message": "已有运行在进行"}, status=409)
        except FileNotFoundError as exc:
            return self._not_found(str(exc))
        return self._not_found("没有这个动作")

    # ---------- 错误 ----------
    def _not_found(self, message):
        if (self.headers.get("Accept") or "").find("application/json") >= 0:
            return self._json({"error": "not_found", "message": message}, status=404)
        return self._send(layout("error", "未找到",
                                 "<h1>未找到</h1><p class='muted'>%s</p><p><a href='/'>返回运行列表</a></p>" % esc(message),
                                 self.server.csrf_token), status=404)

    def _error_page(self, exc):
        body = ("<h1>出错了</h1><pre class='block'>%s: %s</pre><p><a href='/'>返回运行列表</a></p>"
                % (esc(type(exc).__name__), esc(exc)))
        return self._send(layout("error", "出错了", body, self.server.csrf_token), status=500)

    # ---------- 静态资源 ----------
    def _static(self, name):
        if name not in STATIC_FILES:
            raise FileNotFoundError("没有这个静态文件: %s" % name)
        path = WEB_DIR / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return self._send(path.read_bytes(), content_type=STATIC_FILES[name])

    # ---------- 列表 ----------
    def _runs_page(self, query):
        con = self._db()
        spec_filter = (query.get("spec") or [None])[0]
        runs = storage.list_runs(con, limit=200, spec_id=spec_filter)
        specs = storage.spec_summary(con)
        con.close()
        busy = run_mod.active_run(self._data_root())
        rows = self._runs_rows(runs)
        spec_rows = "".join(
            "<tr><td><a href='/specs/%s'>%s</a></td><td class='num'>%s</td>"
            "<td class='num'>%s</td><td class='mono'>%s</td>"
            "<td><button data-start-spec='%s'>运行</button></td></tr>" % (
                esc(item["spec_id"]), esc(item["spec_id"]), item["runs"],
                item["succeeded"] or 0, fmt_time(item["last_finished"]), esc(item["spec_id"]))
            for item in specs) or "<tr><td colspan='5' class='muted'>还没有登记入口</td></tr>"
        spec_options = "".join(
            "<option value='%s'%s>%s</option>" % (
                esc(item["spec_id"]),
                " selected" if spec_filter and item["spec_id"] == spec_filter else "",
                esc(item["spec_id"]))
            for item in specs)
        banner = ""
        if busy:
            banner = ("<div class='card'><b>已有运行在进行</b>：<a class='mono' href='/runs/%s'>%s</a>。"
                      "同时启动两个 MATLAB 会让在线许可排队，新的提交会被拒绝。</div>" % (esc(busy), esc(busy)))
        body = """
%s
<div class="toolbar">
  <input type="search" placeholder="按 run_id / 入口 / 状态搜索（按 / 聚焦）" data-filter-text>
  <select data-filter-status>
    <option value="">全部状态</option>
    <option value="active">只看运行中</option>
    <option value="succeeded">succeeded</option>
    <option value="failed">failed</option>
    <option value="incomplete">incomplete</option>
    <option value="validation_failed">validation_failed</option>
    <option value="timed_out">timed_out</option>
    <option value="cancelled">cancelled</option>
    <option value="interrupted">interrupted</option>
  </select>
  <select data-filter-spec><option value="">全部入口</option>%s</select>
  <span class="muted">列表每 2.5 秒自动刷新，无需手动刷新页面。</span>
</div>
<h2>登记入口</h2>
<table><thead><tr><th>入口</th><th class="num">运行数</th><th class="num">成功</th><th>最近结束</th><th></th></tr></thead>
<tbody>%s</tbody></table>
<h2>运行列表</h2>
<table data-runs-table><thead><tr><th>run_id</th><th>入口</th><th>状态</th><th class="num">耗时</th><th>结束时间</th><th></th></tr></thead>
<tbody>%s</tbody></table>
""" % (banner, spec_options, spec_rows, rows)
        return self._send(layout("runs", "运行列表", body, self.server.csrf_token))

    def _runs_rows(self, runs):
        if not runs:
            return "<tr><td colspan='6' class='muted'>还没有运行</td></tr>"
        rows = []
        for item in runs:
            action = ("<button class='ghost' data-cancel='%s'>取消</button>" % esc(item["run_id"])
                      if item["status"] == "running"
                      else "<a class='muted' href='/compare?a=%s&b='>对比</a>" % esc(item["run_id"]))
            rows.append(
                "<tr data-run-id='%s'><td><a class='mono' href='/runs/%s'>%s</a></td>"
                "<td>%s</td><td>%s</td><td class='num'>%s</td><td class='mono'>%s</td><td>%s</td></tr>" % (
                    esc(item["run_id"]), esc(item["run_id"]), esc(item["run_id"]),
                    esc(item["spec_id"]), badge(item["status"]),
                    "—" if item["duration_s"] is None else "%ss" % item["duration_s"],
                    fmt_time(item["finished_at"] or item["created_at"]), action))
        return "".join(rows)

    # ---------- 入口 ----------
    def _specs_page(self):
        rows = "".join(
            "<tr><td><a href='/specs/%s'>%s</a></td><td>%s</td><td class='mono'>%s</td>"
            "<td><button data-start-spec='%s'>运行</button></td></tr>" % (
                esc(item["id"]), esc(item["id"]), esc(item.get("name", "")),
                esc((item.get("entry") or {}).get("command", "")), esc(item["id"]))
            for item in self._spec_list())
        body = ("<h1>登记入口</h1><p class='muted'>来自 <code>runspecs/*.json</code>；"
                "依赖缺失的入口不会出现在这里。</p>"
                "<table><thead><tr><th>id</th><th>名称</th><th>命令</th><th></th></tr></thead>"
                "<tbody>%s</tbody></table>" % (rows or "<tr><td colspan='4' class='muted'>还没有登记入口</td></tr>"))
        return self._send(layout("runs", "登记入口", body, self.server.csrf_token))

    def _spec_page(self, spec_id):
        path = SPEC_DIR / (spec_id + ".json")
        if not path.exists():
            raise FileNotFoundError("没有这个入口: %s" % spec_id)
        spec = spec_mod.load_spec(path)
        con = self._db()
        runs = storage.list_runs(con, limit=50, spec_id=spec_id)
        con.close()
        sources = "".join(
            "<li><code>%s</code><br><span class='muted'>%s</span></li>" % (
                esc(s.get("root")), esc(", ".join(s.get("include", []))))
            for s in spec["sources"])
        outputs = "".join("<li class='mono'>%s</li>" % esc(p) for p in spec.get("expected_outputs", []))
        body = """
<div class="card">
  <h1>%s</h1>
  <p class="muted">%s</p>
  <div class="grid">
    <div class="kv"><div class="k">入口 id</div><div class="v mono">%s</div></div>
    <div class="kv"><div class="k">工作目录</div><div class="v mono">%s</div></div>
    <div class="kv"><div class="k">命令</div><div class="v mono">%s</div></div>
    <div class="kv"><div class="k">规格哈希</div><div class="v mono">%s</div></div>
    <div class="kv"><div class="k">超时</div><div class="v">%s s</div></div>
  </div>
  <p style="margin-top:12px"><button data-start-spec="%s">运行这个入口</button></p>
</div>
<h2>依赖（只读挂载）</h2><ul class="muted">%s</ul>
<h2>期望产物</h2><ul class="muted">%s</ul>
<h2>该入口的运行</h2>
<table data-runs-table data-spec="%s"><thead><tr><th>run_id</th><th>入口</th><th>状态</th>
<th class="num">耗时</th><th>结束时间</th><th></th></tr></thead>
<tbody>%s</tbody></table>
""" % (esc(spec.get("name", spec_id)), esc(spec.get("description", "")), esc(spec["id"]),
       esc(spec["entry"]["workdir"]), esc(spec["entry"]["command"]),
       esc(spec["spec_sha256"][:16]), esc(spec.get("timeout_s")), esc(spec["id"]),
       sources, outputs, esc(spec["id"]), self._runs_rows(runs))
        return self._send(layout("spec", spec.get("name", spec_id), body, self.server.csrf_token))

    # ---------- 运行详情 ----------
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
        spec_doc = self._read_json(run_dir / "spec.json") or {}

        artifacts = "".join(
            "<tr><td class='mono'>%s</td><td class='num'>%s</td><td class='mono'>%s</td>"
            "<td><a href='/runs/%s/artifact?path=%s'>下载</a></td></tr>" % (
                esc(a["path"]), fmt_bytes(a["bytes"]), esc((a["sha256"] or "")[:16]),
                esc(run_id), quote(a["path"], safe=""))
            for a in record["artifacts"]) or "<tr><td colspan='4' class='muted'>没有产物</td></tr>"

        validation = record["validation"]
        if validation:
            detail = completion.get("validation") or {}
            failures = detail.get("failures") or []
            vtext = ("<table><thead><tr><th>文件</th><th class='num'>行数</th><th>全部通过</th></tr></thead>"
                     "<tbody><tr><td class='mono'>%s</td><td class='num'>%s</td><td>%s</td></tr></tbody></table>" % (
                         esc(validation["file"]), validation["rows"],
                         "是" if validation["all_pass"] else "否"))
            if failures:
                vtext += "<pre class='block'>" + esc(json.dumps(failures[:10], ensure_ascii=False, indent=2)) + "</pre>"
        else:
            vtext = "<p class='muted'>这次运行没有登记验证判据。</p>"

        sources = "".join(
            "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(src.get("root")), esc(", ".join(src.get("include", []))), esc(src.get("role", "")))
            for src in spec_doc.get("sources", []))

        log_text_value = log_text(run_dir, "stdout")
        cancel = ("<button class='danger' data-cancel-current>取消运行</button>"
                  if record["status"] == "running" else "")

        body = """
<div class="card" data-run-id="%s">
  <div class="toolbar" style="margin:0">
    <h1 class="mono" style="font-size:17px;margin:0">%s</h1>
    <span data-status>%s</span>
    <span class="spacer"></span>%s
  </div>
  <div class="grid meta-grid" style="margin-top:12px">
    <div class="kv"><div class="k">入口</div><div class="v"><a href="/specs/%s">%s</a></div></div>
    <div class="kv"><div class="k">耗时</div><div class="v" data-duration>%s</div></div>
    <div class="kv"><div class="k">退出码</div><div class="v" data-exit>%s</div></div>
    <div class="kv"><div class="k">MATLAB</div><div class="v">%s</div></div>
    <div class="kv"><div class="k">结束时间</div><div class="v" data-finished>%s</div></div>
    <div class="kv"><div class="k">规格哈希</div><div class="v mono">%s</div></div>
  </div>
  <p class="muted" style="margin:12px 0 0">
    <a href="/runs/%s/export?format=json">导出来源清单</a> ·
    <a href="/compare?a=%s&b=">与另一次运行对比</a> ·
    <a href="/api/runs/%s" target="_blank">查看 JSON</a>
  </p>
</div>

<div class="tabs" role="tablist">
  <button aria-selected="true" data-tab="tab-artifacts">产物</button>
  <button aria-selected="false" data-tab="tab-validation">验证</button>
  <button aria-selected="false" data-tab="tab-log">日志</button>
  <button aria-selected="false" data-tab="tab-sources">来源</button>
</div>

<section class="tabpanel" id="tab-artifacts">
  <table><thead><tr><th>路径</th><th class="num">大小</th><th>SHA-256</th><th></th></tr></thead>
  <tbody>%s</tbody></table>
</section>

<section class="tabpanel" id="tab-validation" hidden>%s</section>

<section class="tabpanel" id="tab-log" hidden>
  <div class="toolbar">
    <select data-log-stream><option value="stdout">stdout</option><option value="stderr">stderr</option></select>
    <label class="muted"><input type="checkbox" data-autoscroll checked> 自动滚动</label>
    <span class="muted">运行中会持续追加新输出</span>
  </div>
  <pre class="log" data-log data-log-offset="%d">%s</pre>
</section>

<section class="tabpanel" id="tab-sources" hidden>
  <h2>入口与依赖</h2>
  <div class="card">
    <div class="kv"><div class="k">工作目录</div><div class="v mono">%s</div></div>
    <div class="kv" style="margin-top:8px"><div class="k">命令</div><div class="v mono">%s</div></div>
    <div class="kv" style="margin-top:8px"><div class="k">副本</div>
      <div class="v">%s 个文件 / %s</div></div>
    <table style="margin-top:10px"><thead><tr><th>源根</th><th>包含</th><th>角色</th></tr></thead>
    <tbody>%s</tbody></table>
  </div>
  <h2>记录文件</h2>
  <p class="muted"><a href="/api/runs/%s">运行记录 JSON</a> ·
  <a href="/runs/%s/export?format=json">来源清单（含全部依赖哈希）</a></p>
</section>
""" % (esc(run_id), esc(run_id), badge(record["status"]), cancel,
       esc(record["spec_id"]), esc(record["spec_id"]),
       "—" if record["duration_s"] is None else "%ss" % record["duration_s"],
       esc(completion.get("exit_code")), esc((manifest.get("matlab") or {}).get("version", "—")),
       fmt_time(record["finished_at"]), esc((record["spec_sha256"] or "")[:16]),
       esc(run_id), esc(run_id), esc(run_id),
       artifacts, vtext, len(log_text_value), esc(log_text_value),
       esc((manifest.get("entry") or {}).get("workdir", "")),
       esc((manifest.get("entry") or {}).get("command", "")),
       esc((manifest.get("copied_files") or {}).get("count")),
       fmt_bytes((manifest.get("copied_files") or {}).get("bytes")), sources,
       esc(run_id), esc(run_id))
        return self._send(layout("run", run_id, body, self.server.csrf_token))

    # ---------- 对比 ----------
    def _compare_page(self, query):
        a = (query.get("a") or [""])[0]
        b = (query.get("b") or [""])[0]
        result_block = "<p class='muted'>选择两次运行后自动比较。</p>"
        if a and b:
            result_block = self._compare_html(run_mod.compare_runs(self._data_root(), a, b))
        body = """
<h1>运行对比</h1>
<form class="toolbar" data-compare-form>
  <select name="a" data-compare-a><option value="%s">%s</option></select>
  <select name="b" data-compare-b><option value="%s">%s</option></select>
  <button type="submit">比较</button>
  <span class="muted">只比较同一规格的可比产物哈希；图件等含时间戳的输出不计入。</span>
</form>
<div data-compare-output>%s</div>
""" % (esc(a), esc(a or "选择一次运行…"), esc(b), esc(b or "选择一次运行…"), result_block)
        return self._send(layout("compare", "运行对比", body, self.server.csrf_token))

    def _compare_html(self, result):
        rows = []
        for path in result["comparable_identical"]:
            rows.append("<tr><td class='mono'>%s</td><td class='result-same'>一致</td></tr>" % esc(path))
        for path in result["comparable_different"]:
            rows.append("<tr><td class='mono'>%s</td><td class='result-diff'>不同</td></tr>" % esc(path))
        for path in result["only_in_a"]:
            rows.append("<tr><td class='mono'>%s</td><td class='result-diff'>只在 A</td></tr>" % esc(path))
        for path in result["only_in_b"]:
            rows.append("<tr><td class='mono'>%s</td><td class='result-diff'>只在 B</td></tr>" % esc(path))
        return """
<div class="card"><div class="grid">
  <div class="kv"><div class="k">同一规格</div><div class="v">%s</div></div>
  <div class="kv"><div class="k">可比产物全部一致</div><div class="v %s">%s</div></div>
  <div class="kv"><div class="k">A 验证</div><div class="v">%s</div></div>
  <div class="kv"><div class="k">B 验证</div><div class="v">%s</div></div>
</div></div>
<table><thead><tr><th>产物</th><th>结果</th></tr></thead><tbody>%s</tbody></table>
""" % ("是" if result["same_spec"] else "否",
       "result-same" if result["all_comparable_identical"] else "result-diff",
       "是" if result["all_comparable_identical"] else "否",
       "通过" if result["validation_a"] else "未通过/未登记",
       "通过" if result["validation_b"] else "未通过/未登记",
       "".join(rows) or "<tr><td colspan='2' class='muted'>没有可比的共同产物</td></tr>")

    # ---------- 原始文件 ----------
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

    def _export(self, run_id):
        run_dir = self._data_root() / "runs" / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(run_id)
        request = self._read_json(run_dir / "request.json") or {}
        spec = self._read_json(run_dir / "spec.json") or {}
        manifest = self._read_json(run_dir / "manifest.json") or {}
        completion = self._read_json(run_dir / "completion.json") or {}
        copied = manifest.get("copied_files") or {}
        bundle = {
            "schema": "sciencetab.provenance/0.1",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run_id": run_id,
            "request": request,
            "spec": {k: spec.get(k) for k in ("id", "name", "entry", "sources", "expected_outputs",
                                              "comparable_outputs", "timeout_s", "spec_sha256")},
            "environment": {"matlab": manifest.get("matlab"), "platform": manifest.get("platform"),
                            "python": manifest.get("python"), "host": manifest.get("host")},
            "dependencies": {"count": copied.get("count"), "bytes": copied.get("bytes"),
                             "files": [{k: f.get(k) for k in ("path", "bytes", "sha256", "role")}
                                       for f in copied.get("files", [])]},
            "completion": completion,
            "artifacts": completion.get("outputs", []),
            "validation": completion.get("validation"),
        }
        data = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="provenance-%s.json"' % run_id)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- API ----------
    def _api_runs(self, query):
        spec_filter = (query.get("spec") or [None])[0]
        status_filter = (query.get("status") or [None])[0]
        con = self._db()
        runs = storage.list_runs(con, limit=200, spec_id=spec_filter, status=status_filter)
        specs = storage.spec_summary(con)
        con.close()
        return self._json({"runs": runs, "specs": specs,
                           "running": run_mod.active_run(self._data_root())})

    def _api_run(self, run_id):
        con = self._db()
        record = storage.get_run(con, run_id)
        con.close()
        if record is None:
            raise FileNotFoundError(run_id)
        return self._json(record)

    def _api_log(self, run_id, query):
        stream = (query.get("stream") or ["stdout"])[0]
        if stream not in ("stdout", "stderr"):
            return self._json({"error": "bad_stream"}, status=400)
        try:
            offset = int((query.get("offset") or ["0"])[0])
        except ValueError:
            offset = 0
        path = self._data_root() / "runs" / run_id / "logs"
        text = log_text(self._data_root() / "runs" / run_id, stream)
        if offset > len(text):
            offset = 0
        chunk = text[offset:]
        return self._json({"stream": stream, "offset": len(text), "chunk": chunk,
                           "eof": not chunk})

    def _api_compare(self, query):
        a = (query.get("a") or [""])[0]
        b = (query.get("b") or [""])[0]
        if not a or not b:
            return self._json({"error": "need_two_runs"}, status=400)
        return self._json(run_mod.compare_runs(self._data_root(), a, b))

    def _api_start(self, payload):
        spec_id = payload.get("spec")
        if not spec_id:
            return self._json({"error": "need_spec"}, status=400)
        run_id = self._spawn(spec_id)
        return self._json({"submitted": True, "run_id": run_id})

    def _api_cancel(self, payload):
        run_id = payload.get("run_id")
        if not run_id:
            return self._json({"error": "need_run_id"}, status=400)
        marker = run_mod.request_cancel(self._data_root(), run_id)
        return self._json({"ok": True, "marker": marker})

    def _spawn(self, spec_id):
        """提交一次运行：先做忙碌检查，再以独立进程启动，避免阻塞请求。"""
        if not spec_id:
            raise FileNotFoundError("缺少 spec")
        spec_path = SPEC_DIR / (spec_id + ".json")
        if not spec_path.exists():
            raise FileNotFoundError("没有这个入口: %s" % spec_id)
        spec_mod.load_spec(spec_path)  # 依赖缺失在提交前暴露
        running = run_mod.active_run(self._data_root())
        if running:
            raise run_mod.BusyError(running)

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
        # 给子进程一点时间把 running 记录写进索引，便于前端立即看到
        for _ in range(20):
            time.sleep(0.1)
            con = self._db()
            rows = storage.list_runs(con, limit=1, status="running")
            con.close()
            if rows:
                return rows[0]["run_id"]
        return None

    # ---------- 入口清单 ----------
    def _spec_list(self):
        items = []
        for path in sorted(SPEC_DIR.glob("*.json")):
            try:
                spec = spec_mod.load_spec(path)
            except Exception:  # noqa: BLE001
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