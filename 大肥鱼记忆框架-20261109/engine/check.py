# -*- coding: utf-8 -*-
"""
check.py —— 验收脚本（模板第七节那套）

一次看清四件事：
  1. 服务在不在（记忆注入 / 桥接 / QQ 网关 / 机器人桥）
  2. 归档今天有没有在长（行数、会话、时间跨度、连续重复）
  3. 流水有没有真的注入（每个会话的块大小、是否含【这边】）
  4. 要目补上了没（昨天、今天）

退出码：0 = 全绿；1 = 有异常。

用法：
  python check.py
  python check.py --date 2026-11-05
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys

# 控制台可能是 GBK：先把输出强制成 UTF-8，否则 ✓/✗ 会直接崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from datetime import datetime, timedelta
from pathlib import Path

import paths

ARCHIVE_DIR = paths.ARCHIVE_QQ
DIGEST_DIR = paths.ARCHIVE_DIGEST
MB_ROOT = paths.ENGINE
OUT = paths.OUT
SOCIAL = paths.SOCIAL

SERVICES = [(3132, "记忆注入"), (3100, "QQ 桥接"), (3000, "QQ 网关"), (8787, "机器人桥")]


def port_up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.8)
        return s.connect_ex(("127.0.0.1", port)) == 0


def safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(key)) + ".txt"


def archive_report(date_str: str) -> dict:
    f = ARCHIVE_DIR / f"{date_str}.md"
    if not f.exists():
        return {"exists": False}
    rows = [l for l in f.read_text("utf-8", errors="ignore").splitlines() if l.startswith("- ")]
    times, sessions, dup = [], {}, 0
    prev = None
    for l in rows:
        m = re.match(r"^- (\d\d:\d\d) \[([^\]]+)\]", l)
        if m:
            times.append(m.group(1))
            sessions[m.group(2)] = sessions.get(m.group(2), 0) + 1
        if l == prev:
            dup += 1
        prev = l
    return {
        "exists": True, "bytes": f.stat().st_size, "rows": len(rows),
        "sessions": sessions, "dup": dup,
        "range": (min(times), max(times)) if times else None,
    }


def blocks_report() -> list[dict]:
    try:
        convs = (json.loads(SOCIAL.read_text("utf-8")).get("conversations") or {})
    except Exception:
        convs = {}
    rows = []
    for key in convs:
        p = OUT / safe_key(key)
        if not p.exists():
            rows.append({"key": key, "exists": False, "chars": 0, "stream": False, "mtime": None})
            continue
        text = p.read_text("utf-8", errors="ignore")
        rows.append({
            "key": key, "exists": True, "chars": len(text),
            "stream": "【这边（QQ）刚才的几句】" in text,
            "desk": "【另一边（桌面）" in text,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M"),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    today = a.date or datetime.now().strftime("%Y-%m-%d")
    yday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    problems = []
    print("=" * 58)
    print(f"  大肥鱼记忆系统验收 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 58)

    print("\n[1] 服务")
    for port, name in SERVICES:
        ok = port_up(port)
        print(f"  {'✓' if ok else '✗'} {port:<5} {name}")
        if not ok:
            problems.append(f"服务 {name}({port}) 不在")

    print(f"\n[2] 归档 {today}")
    ar = archive_report(today)
    if not ar["exists"]:
        print("  ✗ 今天还没有归档文件")
        problems.append("今天没有归档")
    else:
        rng = f"{ar['range'][0]}~{ar['range'][1]}" if ar["range"] else "—"
        print(f"  ✓ {ar['bytes']} 字节 / {ar['rows']} 行 / {len(ar['sessions'])} 个会话 / {rng}")
        for k, n in sorted(ar["sessions"].items(), key=lambda x: -x[1])[:4]:
            print(f"      {k}  {n} 条")
        if ar["dup"]:
            print(f"  ⚠ 连续重复行 {ar['dup']} 条")
            problems.append(f"归档有 {ar['dup']} 条连续重复")

    print("\n[3] 流水注入")
    blocks = blocks_report()
    if not blocks:
        print("  ✗ 读不到会话列表")
        problems.append("读不到 social-v2 会话列表")
    for b in blocks:
        if not b["exists"]:
            print(f"  ✗ {b['key']:<24} 没有块文件")
            problems.append(f"{b['key']} 没有注入块")
            continue
        flag = "✓" if b["stream"] else "✗"
        desk = " +桌面" if b["desk"] else ""
        print(f"  {flag} {b['key']:<24} {b['chars']:>4} 字  {b['mtime']}{desk}")
        if not b["stream"]:
            problems.append(f"{b['key']} 的块里没有流水")

    print("\n[4] 要目")
    for d in (yday, today):
        p = DIGEST_DIR / f"{d}.md"
        has_archive = (ARCHIVE_DIR / f"{d}.md").exists()
        if p.exists():
            print(f"  ✓ {d}  {p.stat().st_size} 字节")
        elif not has_archive:
            print(f"  · {d}  没有原文，不需要要目")
        else:
            mark = "✗" if d == yday else "·"
            print(f"  {mark} {d}  未生成" + ("（昨天应该有了）" if d == yday else "（今天还没过完，正常）"))
            if d == yday:
                problems.append(f"{yday} 有原文但要目没生成")

    print("\n" + "=" * 58)
    if problems:
        print(f"  {len(problems)} 项异常：")
        for p in problems:
            print("   - " + p)
        return 1
    print("  全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())