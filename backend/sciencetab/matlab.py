"""MATLAB 批处理适配器。

约束来自 M0 实测：用 argv 数组启动（不经 shell 拼接）；输出按 GBK 解码；
取消按 Windows 进程树终止；退出码原样记录，但 cancelled/timed_out 由本模块判定，
不依赖退出码——被终止的进程同样返回 1，与运行期报错无法区分。
"""
import json
import os
import subprocess
import time
from pathlib import Path

MATLAB_EXE = os.environ.get("SCIENCETAB_MATLAB", r"D:\Matlab\bin\matlab.exe")
OUTPUT_ENCODING = "gbk"
TAIL_LINES = 40


def _taskkill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True, encoding=OUTPUT_ENCODING, errors="replace")


def run_matlab(command, workdir, log_dir, timeout_s=900, cancel_file=None, poll_s=1.0):
    """执行一次 matlab -batch，返回状态字典；日志原始字节写到 log_dir。"""
    workdir = Path(workdir)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    raw_out = log_dir / "stdout.raw"
    raw_err = log_dir / "stderr.raw"

    argv = [MATLAB_EXE, "-batch", command, "-sd", str(workdir)]
    started = time.time()
    cancelled = False
    timed_out = False
    # 后台 Worker 场景下不给 MATLAB 分配可见控制台，避免控制台拆除时
    # 让进程以 0xC000013A（STATUS_CONTROL_C_EXIT）结束，掩盖真实计算结果。
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

    with open(raw_out, "wb") as fo, open(raw_err, "wb") as fe:
        proc = subprocess.Popen(argv, cwd=str(workdir), stdout=fo, stderr=fe,
                                creationflags=flags)
        while True:
            code = proc.poll()
            if code is not None:
                break
            if cancel_file is not None and Path(cancel_file).exists() and not cancelled:
                cancelled = True
                _taskkill_tree(proc.pid)
            if not timed_out and (time.time() - started) > timeout_s:
                timed_out = True
                _taskkill_tree(proc.pid)
            time.sleep(poll_s)
        exit_code = proc.returncode

    duration = round(time.time() - started, 2)
    stdout_text, stderr_text = decode_logs(raw_out, raw_err)
    return {
        "argv": argv,
        "pid": proc.pid,
        "exit_code": exit_code,
        "cancelled": cancelled,
        "timed_out": timed_out,
        "duration_s": duration,
        "stdout_path": str(raw_out),
        "stderr_path": str(raw_err),
        "stdout_tail": stdout_text.splitlines()[-TAIL_LINES:],
        "stderr_tail": stderr_text.splitlines()[-TAIL_LINES:],
    }


def decode_logs(raw_out, raw_err):
    """把 GBK 原始字节另存为可读文本；原始字节始终保留。"""
    texts = []
    for raw in (raw_out, raw_err):
        raw = Path(raw)
        data = raw.read_bytes() if raw.exists() else b""
        try:
            text = data.decode(OUTPUT_ENCODING, "replace")
        except LookupError:
            text = data.decode("utf-8", "replace")
        Path(str(raw).replace(".raw", ".txt")).write_text(text, encoding="utf-8")
        texts.append(text)
    return texts[0], texts[1]


def probe_matlab_version(cache_file, max_age_s=86400):
    """探测 MATLAB 版本；结果缓存到 data/env，避免每次都付启动开销。"""
    cache_file = Path(cache_file)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - cached.get("probed_at_epoch", 0) < max_age_s:
                return cached
        except (ValueError, OSError):
            pass

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    proc = subprocess.run(
        [MATLAB_EXE, "-batch", "fprintf('%s|%s|%s', version, version('-release'), computer)"],
        capture_output=True, cwd=str(cache_file.parent), timeout=300)
    text = proc.stdout.decode(OUTPUT_ENCODING, "replace").strip()
    version, release, comp = (text.split("|") + ["", "", ""])[:3]
    info = {
        "matlab_exe": MATLAB_EXE,
        "version": version,
        "release": release,
        "computer": comp,
        "exit_code": proc.returncode,
        "probe_duration_s": round(time.time() - started, 2),
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "probed_at_epoch": int(time.time()),
        "output_encoding": OUTPUT_ENCODING,
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info