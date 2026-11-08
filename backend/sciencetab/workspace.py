"""隔离副本构建：把登记过的依赖按原相对布局复制到 run 的 work/ 下。

规则：只复制 RunnerSpec 明确列出的目录或文件；入口目录的 results/ 由平台创建为空目录，
避免上一次运行的历史产物混进本次结果。源目录只读，本模块不写源目录。
"""
import hashlib
import os
import shutil
from pathlib import Path


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def build_work_tree(spec, work_root):
    """复制依赖并返回清单：[{path, bytes, sha256, role, source}]。"""
    work_root = Path(work_root)
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)

    entries = []
    for src in spec["sources"]:
        root = Path(src["root"])
        role = src.get("role", "dependency")
        for rel in src["include"]:
            source = root / rel
            dest = work_root / rel
            if source.is_dir():
                shutil.copytree(source, dest)
                for dirpath, _dirnames, filenames in os.walk(dest):
                    for name in sorted(filenames):
                        full = Path(dirpath) / name
                        entries.append({
                            "path": full.relative_to(work_root).as_posix(),
                            "bytes": full.stat().st_size,
                            "sha256": sha256_file(full),
                            "role": role,
                            "source": str(source),
                        })
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                entries.append({
                    "path": dest.relative_to(work_root).as_posix(),
                    "bytes": dest.stat().st_size,
                    "sha256": sha256_file(dest),
                    "role": role,
                    "source": str(source),
                })

    for rel in spec.get("empty_dirs", []):
        (work_root / rel).mkdir(parents=True, exist_ok=True)

    entries.sort(key=lambda item: item["path"])
    return entries


def snapshot_source(spec):
    """记录源目录中实际被引用的文件（路径/大小/mtime），用于事后核对未被改写。"""
    snap = []
    for src in spec["sources"]:
        root = Path(src["root"])
        for rel in src["include"]:
            source = root / rel
            files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
            for f in files:
                st = f.stat()
                snap.append({
                    "path": str(f),
                    "bytes": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                })
    snap.sort(key=lambda item: item["path"])
    return snap