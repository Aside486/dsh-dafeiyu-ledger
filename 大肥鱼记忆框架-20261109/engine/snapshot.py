# -*- coding: utf-8 -*-
"""
snapshot.py —— 可回滚的回缩（学「智种」的 collapse，但做成快照）

为什么要快照：记忆改坏了没法退回去，是这套系统最大的隐患。
落账写错一条、归档被误删、core 被改乱——以前只能认。

做法：把「不可再生」的东西打成一个 zip：
  · <archive>   （原文 / 要目 / 交接）
  · state\\her-notes  （她的笔记 + 自动落账）
  · state\\her-core.md（薄身份）
不打包 social-v2.json（大、且含令牌）、不打包 token 账本（可重算）。

用法：
  python snapshot.py save v1          # 存档，附一句备注
  python snapshot.py list             # 看有哪些
  python snapshot.py restore <名字>    # 回滚（回滚前自动先存一份 pre-restore）
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

import paths

ARCHIVE = paths.ARCHIVE
NOTES = paths.NOTES
CORE = paths.CORE
SNAPDIR = paths.ARCHIVE_SNAP


def _targets() -> list[tuple[Path, str]]:
    out = []
    if ARCHIVE.exists():
        for d in ("QQ", "归档要目", "交接"):
            p = ARCHIVE / d
            if p.exists():
                out.append((p, d))
    if NOTES.exists():
        out.append((NOTES, "her-notes"))
    if CORE.exists():
        out.append((CORE, "her-core.md"))
    return out


def save(note: str = "") -> dict:
    SNAPDIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"snap-{ts}" + (f"-{note}" if note else "")
    path = SNAPDIR / f"{name}.zip"
    n = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in _targets():
            if src.is_file():
                z.write(src, arc)
                n += 1
            else:
                for f in src.rglob("*"):
                    if f.is_file():
                        z.write(f, f"{arc}/{f.relative_to(src).as_posix()}")
                        n += 1
        z.writestr("MANIFEST.json", json.dumps(
            {"created": ts, "note": note, "files": n}, ensure_ascii=False))
    return {"ok": True, "path": str(path), "files": n, "bytes": path.stat().st_size}


def listing() -> list[dict]:
    if not SNAPDIR.exists():
        return []
    out = []
    for f in sorted(SNAPDIR.glob("*.zip"), reverse=True):
        out.append({"name": f.stem, "bytes": f.stat().st_size,
                    "at": time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime))})
    return out


def restore(name: str) -> dict:
    path = SNAPDIR / (name if name.endswith(".zip") else name + ".zip")
    if not path.exists():
        return {"ok": False, "error": f"没有 {path.name}"}
    # 回滚前先存一份现状，回滚也能回滚
    pre = save("pre-restore")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        n = 0
        for arc in names:
            if arc == "MANIFEST.json":
                continue
            if arc == "her-core.md":
                dst = CORE
            elif arc.startswith("her-notes/"):
                dst = NOTES / arc[len("her-notes/"):]
            elif arc.startswith("QQ/"):
                dst = ARCHIVE / "QQ" / arc[len("QQ/"):]
            elif arc.startswith("归档要目/"):
                dst = ARCHIVE / "归档要目" / arc[len("归档要目/"):]
            elif arc.startswith("交接/"):
                dst = ARCHIVE / "交接" / arc[len("交接/"):]
            else:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(arc) as fh, open(dst, "wb") as out:
                shutil.copyfileobj(fh, out)
            n += 1
    return {"ok": True, "restored": n, "from": path.name, "preRestore": pre["path"]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "save":
        print(json.dumps(save(" ".join(sys.argv[2:])), ensure_ascii=False))
    elif cmd == "restore":
        print(json.dumps(restore(sys.argv[2]), ensure_ascii=False))
    else:
        print(json.dumps(listing(), ensure_ascii=False, indent=2))
