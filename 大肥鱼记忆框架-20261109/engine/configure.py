# -*- coding: utf-8 -*-
"""
configure.py —— 填一次路径，六个模块就都能跑。

用法：
    python configure.py
    python configure.py --bridge "<bridge>" --archive "<路径>\\归档"

会在本目录生成 paths.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "paths.json"

TEMPLATE = {
    "engine": ".",
    "bridge": "",
    "state": "",
    "archive": "",
    "mnemonDb": "",
    "apiBase": "https://api.deepseek.com/v1",
    "apiKey": "",
    "apiModel": "deepseek-flash",
}


def ask(label: str, default: str = "") -> str:
    hint = f"（默认 {default}）" if default else ""
    v = input(f"{label}{hint}: ").strip()
    return v or default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default="")
    ap.add_argument("--archive", default="")
    ap.add_argument("--mnemon", default="")
    ap.add_argument("--key", default="")
    a = ap.parse_args()

    cfg = dict(TEMPLATE)
    if a.bridge:
        cfg["bridge"] = a.bridge
        cfg["state"] = str(Path(a.bridge) / "state")
    else:
        print("\n=== 记忆框架配置 ===\n（直接回车用括号里的默认值）\n")
        cfg["bridge"] = ask("QQ 桥接目录", r"<bridge>")
        cfg["state"] = ask("状态目录（social-v2 / her-notes）", str(Path(cfg["bridge"]) / "state"))
    cfg["archive"] = a.archive or (ask("归档根目录（例如 <路径>\\归档）") if not a.bridge else a.archive)
    if not a.bridge:
        cfg["mnemonDb"] = ask("Mnemon 记忆库路径（没有就回车跳过）", "")
        cfg["apiKey"] = ask("模型 API Key", "")
    else:
        cfg["mnemonDb"] = a.mnemon
        cfg["apiKey"] = a.key

    # 建目录，缺的补上
    for k in ("archive",):
        Path(cfg[k]).mkdir(parents=True, exist_ok=True)
    for sub in ("QQ", "归档要目", "交接"):
        (Path(cfg["archive"]) / sub).mkdir(parents=True, exist_ok=True)
    (Path(cfg["state"]) / "her-notes").mkdir(parents=True, exist_ok=True)

    TARGET.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n✓ 已写 {TARGET}")
    print("  下一步：python check.py  （第一次会提示缺数据，属正常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
