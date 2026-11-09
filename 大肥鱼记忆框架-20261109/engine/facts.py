# -*- coding: utf-8 -*-
"""
facts.py —— 主动落账

问题：上下文一换，只靠"交接摘要"救的是**场**，不是**事**。
她说过的承诺、你透露的偏好、定下的约定，如果只活在那段上下文里，
轮换一次就淡了。

做法：每次换会话时（和交接摘要同一次触发），从最近的原话里抽出
**长期有效**的条目，写成 bullet 追加到她的笔记目录。notes 是 memory-brain
的既有数据源（load_notes 会读 *.md 中以 "- " 开头的行），所以写进去
下一轮就能被检索到——不需要她主动调工具，也不依赖上下文。

只收三类，宁缺毋滥：
  ① 关于他的事实（身份、偏好、习惯、在做什么）
  ② 约定 / 承诺（谁答应谁什么、什么时候）
  ③ 明确的时间点（考试、出门、回来）
闲聊、情绪、玩笑一律不收。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import paths

NOTES = paths.NOTES
STATE = paths.ARCHIVE_HANDOFF / "facts-state.json"
MAX_LINES = 120

PROMPT = """下面是一段聊天记录。请从中抽出**长期有效**的信息，写成条目。

只收这三类：
1. 关于「他」的事实：身份、职业、偏好、习惯、正在做的事、身体/情绪状态（长期那种）
2. 约定或承诺：谁答应谁什么、什么时候做
3. 明确的时间点：考试、出差、回来、见面等

**每条开头必须标可信度**，三选一：
- `（真）`  他自己说的、或可直接观察到的
- `（转述）` 他听别人说的、群里的传闻、没亲历的
- `（编）`  玩笑、假设、剧情、明显不是真的（比如"我是外星人"）

分不清就标（转述）——宁可低估，不要把玩笑记成事实。

不要收：闲聊、寒暄、玩笑、情绪发泄、一次性的临时安排、已经做完的事、
以及任何需要猜测才能得出的东西。

输出要求：
- 每行一条，直接写条目本身（不要编号、不要解释、不要"他"改写成"泡泡"以外的人）
- 每条 15~60 字，独立成立（脱离上下文也读得懂）
- 最多 8 条；一条都抽不出来就输出空
- 直接输出条目，一行一条，不要写任何前言

聊天记录：
"""


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:
        return {"seen": []}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")


def _norm(s: str) -> str:
    s = re.sub(r"^（(真|转述|编)）", "", s.strip())   # 指纹不带标签
    return re.sub(r"[\s，。、；:：,.;!?！？\"'“”‘’]+", "", s)


def extract(key: str, rows: list[str]) -> list[str]:
    if not rows:
        return []
    import daily_digest
    text = daily_digest._chat(PROMPT + "\n".join(rows[-MAX_LINES:]), max_tokens=2400)
    out = []
    for line in (text or "").splitlines():
        s = line.strip().lstrip("-·*•0123456789.、）) ").strip()
        if len(s) < 8 or len(s) > 120:
            continue
        if not s.startswith(("（真）", "（转述）", "（编）", "[真]", "[转述]", "[编]")):
            s = "（转述）" + s          # 模型漏标时按最保守处理
        out.append(s)
    return out[:8]


def save(items: list[str], key: str) -> dict:
    if not items:
        return {"ok": True, "added": 0}
    st = _load_state()
    seen = set(st.get("seen") or [])
    NOTES.mkdir(parents=True, exist_ok=True)
    f = NOTES / f"auto-facts-{time.strftime('%Y-%m')}.md"
    added = []
    for it in items:
        h = hashlib.sha1(_norm(it).encode("utf-8")).hexdigest()[:16]
        if h in seen:
            continue
        seen.add(h)
        added.append(it)
    if not added:
        return {"ok": True, "added": 0, "skipped": len(items)}
    if not f.exists():
        f.write_text(f"# 自动落账 · {time.strftime('%Y-%m')}\n\n"
                     "> 换会话时从原话里抽出的长期事实（memory-brain 自动写入）\n\n", "utf-8")
    day = time.strftime("%Y-%m-%d")
    with f.open("a", encoding="utf-8") as fh:
        for it in added:
            fh.write(f"- {day} [{'私聊' if key.startswith('private:') else '群'}] {it}\n")
    st["seen"] = sorted(seen)[-2000:]     # 只留最近 2000 条指纹，防止无限长
    _save_state(st)
    return {"ok": True, "added": len(added), "file": str(f), "items": added}


if __name__ == "__main__":
    import sys
    k = sys.argv[1] if len(sys.argv) > 1 else "private:1427148790"
    import handoff
    rows = handoff._rows_for(k)
    items = extract(k, rows)
    print(json.dumps({"key": k, "rows": len(rows), "items": items, **save(items, k)},
                     ensure_ascii=False, default=str))