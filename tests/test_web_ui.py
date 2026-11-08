"""验收：页面、静态资源、JSON 接口、免刷新提交/取消、单槽保护、日志增量。

对已启动的服务（默认 http://127.0.0.1:8765）做黑盒 HTTP 检查。
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8765"
REPORT = r"D:\ScienceTab\verification\b1\web_ui.json"
RUN_A = "20261108T221638_rr_thz_rate_power_1801"
RUN_B = "20261108T221907_rr_thz_rate_power_34da"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def request_json(path, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-CSRF-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body or "{}")
        except ValueError:
            return exc.code, {"raw": body}


def csrf_token():
    _status, body = get("/")
    match = re.search(r'name="csrf-token" content="([^"]+)"', body)
    return match.group(1) if match else None


def run_ids():
    _status, body = get("/api/runs")
    return {r["run_id"] for r in json.loads(body)["runs"]}


def wait_new_run(before, spec_id, timeout_s=120):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _status, body = get("/api/runs?spec=" + spec_id)
        for item in json.loads(body)["runs"]:
            if item["run_id"] not in before:
                return item["run_id"]
        time.sleep(1)
    return None


def wait_status(run_id, timeout_s=240):
    terminal = ("succeeded", "failed", "incomplete", "validation_failed",
                "timed_out", "cancelled", "interrupted")
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        status, body = get("/api/runs/" + run_id)
        if status == 200:
            last = json.loads(body).get("status")
            if last in terminal:
                return last
        time.sleep(2)
    return last


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (" | " + detail if detail else ""))

    try:
        get("/healthz")
    except Exception as exc:  # noqa: BLE001
        print("跳过：服务未启动（%s）" % exc)
        return 0

    known = run_ids()
    if RUN_A not in known or RUN_B not in known:
        print("跳过：需要两次已完成的真实运行")
        return 0

    status, body = get("/")
    check("首页含运行表与筛选控件", status == 200 and "data-runs-table" in body
          and "data-filter-text" in body and "data-filter-status" in body)
    check("首页含两次真实运行", RUN_A in body and RUN_B in body)

    status, body = get("/static/app.css")
    check("样式表可访问", status == 200 and "--panel" in body)
    status, body = get("/static/app.js")
    check("前端脚本可访问且含轮询", status == 200 and "initRunsTable" in body and "streamLog" in body)

    status, body = get("/specs/rr_thz_rate_power")
    check("入口详情页", status == 200 and "依赖（只读挂载）" in body and "data-start-spec" in body)

    status, body = get("/runs/" + RUN_B)
    check("详情页含标签页与产物", status == 200 and 'data-tab="tab-log"' in body
          and "rate_power.csv" in body)

    status, body = get("/runs/%s/artifact?path=%s" % (
        RUN_B, urllib.parse.quote("results/rate_power.csv")))
    check("产物可下载", status == 200 and len(body) > 1000, "%d bytes" % len(body))

    status, body = get("/api/runs/%s/log?stream=stdout&offset=0" % RUN_B)
    log = json.loads(body)
    check("日志接口返回内容与偏移", status == 200 and log["offset"] > 100 and "root_residual_max" in log["chunk"],
          "offset=%d" % log["offset"])
    status, body = get("/api/runs/%s/log?stream=stdout&offset=%d" % (RUN_B, log["offset"]))
    tail = json.loads(body)
    check("日志增量接口不重复", status == 200 and tail["chunk"] == "" and tail["offset"] == log["offset"])

    status, body = get("/api/compare?a=%s&b=%s" % (RUN_A, RUN_B))
    compare = json.loads(body)
    check("对比接口可比产物一致", status == 200 and compare["all_comparable_identical"] is True)

    status, body = get("/compare?a=%s&b=%s" % (RUN_A, RUN_B))
    check("对比页服务端渲染结论", status == 200 and "可比产物全部一致" in body)

    status, body = get("/runs/%s/export?format=json" % RUN_B)
    bundle = json.loads(body)
    check("来源导出可用", status == 200 and bundle.get("schema") == "sciencetab.provenance/0.1"
          and (bundle.get("dependencies") or {}).get("count") == 111)

    token = csrf_token()
    check("页面含 CSRF 令牌", bool(token))

    status, _ = request_json("/api/start", {"spec": "selftest_quick"})
    check("无令牌启动被拒绝", status == 403, str(status))

    before = run_ids()
    status, payload = request_json("/api/start", {"spec": "selftest_quick"}, token)
    check("有令牌启动被接受", status == 200 and payload.get("run_id"), str(status))
    run_id = wait_new_run(before, "selftest_quick")
    final = wait_status(run_id) if run_id else None
    check("启动的运行完成", final == "succeeded", "%s -> %s" % (run_id, final))

    before = run_ids()
    status, payload = request_json("/api/start", {"spec": "selftest_cancel"}, token)
    long_run = payload.get("run_id")
    check("长任务已启动", status == 200 and bool(long_run), str(long_run))
    status, payload = request_json("/api/start", {"spec": "selftest_quick"}, token)
    check("并发启动被拒绝（单槽）", status == 409 and payload.get("running") == long_run, str(status))

    deadline = time.time() + 60
    partial = {"offset": 0, "chunk": ""}
    while time.time() < deadline:
        status, body = get("/api/runs/%s/log?stream=stdout&offset=0" % long_run)
        partial = json.loads(body)
        if partial["offset"] > 0:
            break
        time.sleep(2)
    time.sleep(3)
    status2, body2 = get("/api/runs/%s/log?stream=stdout&offset=%d" % (long_run, partial["offset"]))
    incremental = json.loads(body2)
    check("运行中日志可增量读取",
          status == 200 and status2 == 200 and partial["offset"] > 0
          and incremental["offset"] >= partial["offset"],
          "offset %d -> %d" % (partial["offset"], incremental["offset"]))

    status, payload = request_json("/api/cancel", {"run_id": long_run}, token)
    check("取消接口接受请求", status == 200 and payload.get("ok") is True)
    final = wait_status(long_run)
    check("取消生效", final == "cancelled", str(final))
    check("取消后不再阻塞", not json.loads(get("/api/runs")[1]).get("running"))

    summary = {"base": BASE, "checks": checks, "passed": sum(1 for c in checks if c["ok"]),
               "total": len(checks), "last_run": long_run}
    import os
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": summary["passed"], "total": summary["total"]}, ensure_ascii=False))
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())