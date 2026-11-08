"""验收：本地服务页面、从页面触发运行、取消、产物下载、对比页。

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


def post(path, data, token=None):
    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def csrf_token():
    _status, body = get("/")
    match = re.search(r"name='token' value='([^']+)'", body)
    return match.group(1) if match else None


def wait_status(run_id, terminal=("succeeded", "failed", "incomplete",
                                  "validation_failed", "timed_out", "cancelled"), timeout_s=240):
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


def newest_run_id():
    _status, body = get("/api/runs")
    runs = json.loads(body)["runs"]
    return runs[0]["run_id"] if runs else None


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (" | " + detail if detail else ""))

    try:
        get("/healthz")
    except Exception as exc:  # noqa: BLE001
        print("跳过：服务未启动（%s）。先运行 python -m sciencetab serve" % exc)
        return 0

    known = run_ids()
    if RUN_A not in known or RUN_B not in known:
        print("跳过：需要两次已完成的真实运行（当前库中没有 %s / %s）" % (RUN_A[:18], RUN_B[:18]))
        return 0

    status, body = get("/")
    check("首页 200 且含运行列表", status == 200 and "运行列表" in body, str(status))
    check("首页含两次真实运行", RUN_A in body and RUN_B in body)

    status, body = get("/specs")
    check("入口页列出登记入口", status == 200 and "rr_thz_rate_power" in body)

    status, body = get("/runs/" + RUN_B)
    check("详情页含产物与验证", status == 200 and "rate_power.csv" in body and "root_residual_max" in body)

    status, body = get("/runs/%s/artifact?path=%s" % (RUN_B, urllib.parse.quote("results/rate_power.csv")))
    check("产物可下载", status == 200 and len(body) > 1000, "%d bytes" % len(body))

    status, body = get("/compare?a=%s&b=%s" % (RUN_A, RUN_B))
    check("对比页显示可比产物一致", status == 200 and "可比产物全部一致" in body)

    status, body = get("/specs/rr_thz_rate_power")
    check("入口详情页含依赖与产物", status == 200 and "依赖（只读挂载）" in body
          and "期望产物" in body and "该入口的运行" in body)

    status, body = get("/runs/%s/export?format=json" % RUN_B)
    try:
        bundle = json.loads(body)
    except ValueError:
        bundle = {}
    check("来源导出可用", status == 200
          and bundle.get("schema") == "sciencetab.provenance/0.1"
          and (bundle.get("dependencies") or {}).get("count") == 111
          and len(bundle.get("artifacts") or []) >= 5,
          "%s deps" % (bundle.get("dependencies") or {}).get("count"))

    token = csrf_token()
    check("取得表单令牌", bool(token))
    status, _body = post("/start", {"spec": "selftest_quick"})
    check("无令牌提交被拒绝", status == 403, str(status))

    runs_before = run_ids()
    status, _body = post("/start", {"spec": "selftest_quick", "token": token})
    check("有令牌提交被接受", status in (200, 303), str(status))
    run_id = wait_new_run(before=runs_before, spec_id="selftest_quick")
    check("页面触发产生了新运行", bool(run_id), str(run_id))
    status = wait_status(run_id, timeout_s=180)
    check("页面触发的运行完成", status == "succeeded", "%s -> %s" % (run_id, status))
    status, body = get("/runs/" + run_id)
    check("新运行详情含产物", status == 200 and "quick.csv" in body)

    before_cancel = run_ids()
    status, _body = post("/start", {"spec": "selftest_cancel", "token": token})
    cancel_id = wait_new_run(before_cancel, "selftest_cancel")
    check("取消用例已启动", bool(cancel_id), str(cancel_id))
    time.sleep(6)
    status, _body = post("/runs/%s/cancel" % cancel_id, {"token": token})
    check("取消请求被接受", status in (200, 303), str(status))
    final = wait_status(cancel_id, timeout_s=180)
    check("页面取消生效", final == "cancelled", "%s -> %s" % (cancel_id, final))

    summary = {"base": BASE, "checks": checks, "passed": sum(1 for c in checks if c["ok"]),
               "total": len(checks), "cancel_run": cancel_id}
    import os
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": summary["passed"], "total": summary["total"]}, ensure_ascii=False))
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())