"""RunnerSpec：一次实验运行的可冻结定义。

只做三件事：读取并校验 JSON、计算规格哈希、把相对路径解析到源根。
不执行任何计算，也不改动源目录。
"""
import hashlib
import json
import os
from pathlib import Path

SCHEMA_VERSION = "0.1"


def _expand(value):
    """展开 ${VAR}，让 RunnerSpec 不写死本机绝对路径。"""
    return os.path.expandvars(value) if isinstance(value, str) else value


class SpecError(ValueError):
    pass


def load_spec(spec_path):
    """读取并校验 RunnerSpec；返回 dict，附带 _path 与 spec_sha256。"""
    path = Path(spec_path).resolve()
    with open(path, "r", encoding="utf-8-sig") as f:
        spec = json.load(f)
    _expand_sources(spec)
    validate(spec)
    spec["_path"] = str(path)
    spec["spec_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return spec


def _expand_sources(spec):
    project_root = Path(__file__).resolve().parents[2]
    for src in spec.get("sources", []):
        root = _expand(src.get("root"))
        if root and not os.path.isabs(root):
            root = str(project_root / root)
        src["root"] = root
        src["include"] = [_expand(item) for item in src.get("include", [])]
    if spec.get("validation"):
        spec["validation"]["file"] = _expand(spec["validation"].get("file", ""))


def validate(spec):
    for key in ("id", "entry", "sources"):
        if key not in spec:
            raise SpecError("RunnerSpec 缺少字段: %s" % key)
    entry = spec["entry"]
    if entry.get("kind") != "matlab":
        raise SpecError("entry.kind 目前只支持 matlab")
    if not entry.get("command") or not entry.get("workdir"):
        raise SpecError("entry 需要 command 与 workdir")
    if not spec["sources"]:
        raise SpecError("sources 不能为空")
    for src in spec["sources"]:
        if "root" not in src or "include" not in src:
            raise SpecError("sources 每项需要 root 与 include")
        root = Path(src["root"])
        if not root.is_dir():
            raise SpecError("源根不存在: %s" % root)
        for rel in src["include"]:
            target = root / rel
            if not target.exists():
                raise SpecError("依赖缺失: %s" % target)
        if src.get("root", "").startswith("\\\\"):
            raise SpecError("不支持网络路径源根: %s" % src["root"])


def outputs_absolute(spec, work_root):
    """把 expected_outputs 解析成绝对路径（相对入口工作目录）。"""
    workdir = Path(work_root) / spec["entry"]["workdir"]
    return [workdir / rel for rel in spec.get("expected_outputs", [])]


def default_data_root():
    env = os.environ.get("SCIENCETAB_DATA")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data"