# -*- coding: utf-8 -*-
"""去掉 runspecs 下的 UTF-8 BOM，并校验 JSON 可解析。"""
import json
from pathlib import Path

SPEC_DIR = Path(r"D:\ScienceTab\runspecs")
for path in sorted(SPEC_DIR.glob("*.json")):
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        path.write_bytes(raw)
        print("BOM removed: %s" % path.name)
    json.loads(raw.decode("utf-8"))
print("all specs parse as UTF-8 JSON")