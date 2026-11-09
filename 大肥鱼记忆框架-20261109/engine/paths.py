# -*- coding: utf-8 -*-
"""
paths.py —— 这个框架唯一的「路径来源」

设计意图：代码里不出现任何绝对路径。所有目录在这里读 paths.json，
改机器、改目录只改那一个 json，不用翻六个模块。

paths.json 不存在时会在导入处直接报错，并告诉你跑 configure.py。
"""
from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CFG = _HERE / "paths.json"

DEFAULTS = {
    # 记忆引擎自己的目录（本文件所在目录，一般不用改）
    "engine": ".",
    # 宿主：QQ 桥接的根目录
    "bridge": "<QQ桥接目录>",
    # 桥接状态：social-v2.json、her-notes、her-core.md 都在这里
    "state": "<QQ桥接目录>/state",
    # 归档：原文 / 要目 / 交接
    "archive": "<归档根目录>",
    # Mnemon 记忆库（主账本），没有就留空
    "mnemonDb": "",
    # 模型接口（要目 / 交接 / 落账三处压缩用）
    "apiBase": "https://api.deepseek.com/v1",
    "apiKey": "",
    "apiModel": "deepseek-flash",
}


def _load() -> dict:
    cfg = dict(DEFAULTS)
    if _CFG.exists():
        try:
            cfg.update(json.loads(_CFG.read_text("utf-8")))
        except Exception as e:
            raise RuntimeError(f"paths.json 读不了：{e}") from e
    missing = [k for k, v in cfg.items() if isinstance(v, str) and v.startswith("<")]
    if missing:
        raise RuntimeError(
            "paths.json 还没填好：" + "、".join(missing) +
            "\n→ 先跑 python configure.py"
        )
    return cfg


CFG = _load()

ENGINE = (_HERE / CFG["engine"]).resolve() if CFG["engine"] not in (".", "") else _HERE
BRIDGE = Path(CFG["bridge"])
STATE = Path(CFG["state"])
ARCHIVE = Path(CFG["archive"])
MNEMON_DB = Path(CFG["mnemonDb"]) if CFG.get("mnemonDb") else None

# 派生路径（模块里常用）
OUT = ENGINE / "out"
LOGS = ENGINE / "logs"
SOCIAL = STATE / "social-v2.json"
NOTES = STATE / "her-notes"
DESK = STATE / "her-desk.md"
CORE = STATE / "her-core.md"
DIARY = BRIDGE / "diary"

ARCHIVE_QQ = ARCHIVE / "QQ"
ARCHIVE_DIGEST = ARCHIVE / "归档要目"
ARCHIVE_HANDOFF = ARCHIVE / "交接"
ARCHIVE_SNAP = ARCHIVE / "快照"
