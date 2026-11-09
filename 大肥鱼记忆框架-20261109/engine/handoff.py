# -*- coding: utf-8 -*-
"""
handoff.py —— 会话轮换时的「交接摘要」

为什么需要它：
  轮数一多，每一步都要把整段历史重发一次，钱是这么烧掉的。桥接本来就会在
  上下文超预算时换一条新会话（上下文归零），但新会话开头只有最近 6 句原话
  和检索到的旧记忆——"二十轮前答应过的那件事"如果没被记成事实，就断了。

  交接摘要补的就是这一段：换会话的一瞬间，把"这段聊到哪儿了"压成 400 字，
  注入新会话开头。它不进长期账本（那是 mnemon / 笔记的事），只活 90 分钟，
  过期自动消失，不会污染以后的对话。

产物：
  <archive>\\交接\\<key>.md      人工可读
  <archive>\\交接\\state.json    各 key 的时间戳 + 摘要正文
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import paths

ARCHIVE_DIR = paths.ARCHIVE_QQ
HANDOFF_DIR = paths.ARCHIVE_HANDOFF
STATE = HANDOFF_DIR / "state.json"

MAX_LINES = 150          # 取最近多少条原话去压
MAX_CHARS = 420          # 注入时的上限（约占块预算的 1/3）
FRESH_MIN = 90           # 多少分钟内算"刚换过会话"

PROMPT = """下面是两个人刚结束的一段聊天记录（按时间顺序，一行一句）。他们马上要开始新一轮，
但上文的细节会被清空。请写一段交接，让接下来的自己读完后能自然接上。

要求：
1. 只写记录里确实有的内容，不要推测、不要编。
2. 必须分四块，四块都要写，不要只写一块：
   · 正在聊什么
   · 她的判断和态度
   · 答应过 / 说过要做的事
   · 悬而未决的问题
3. 保留人名和具体约定，不要写成"聊了一些事情"这种空话。
4. 总长度不超过 400 字。直接输出，不要写思考过程。

聊天记录：
"""


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")


def _rows_for(key: str) -> list[str]:
    """从当天的归档里挑出这个会话的行。"""
    out = []
    for f in sorted(ARCHIVE_DIR.glob("*.md"))[-2:]:
        for line in f.read_text("utf-8", errors="ignore").splitlines():
            if line.startswith("- ") and f"[{key}]" in line:
                m = re.match(r"^- \d\d:\d\d \[[^\]]+\] ([^：]+)：(.*)$", line)
                out.append(f"{m.group(1)}：{m.group(2)}" if m else line[2:])
    return out


def build(key: str, force: bool = False) -> dict:
    st = _load_state()
    prev = st.get(key) or {}
    rows = _rows_for(key)
    if len(rows) < 8:
        return {"ok": False, "key": key, "reason": f"原话太少（{len(rows)} 条）"}

    # 同一批内容不重复花一次模型调用
    fingerprint = str(hash("\n".join(rows[-30:])))
    if not force and prev.get("fp") == fingerprint and prev.get("text"):
        return {"ok": True, "key": key, "skipped": True}

    text = ""
    try:
        import daily_digest
        text = daily_digest._chat(PROMPT + "\n".join(rows[-MAX_LINES:]), max_tokens=3200)
    except Exception as e:
        return {"ok": False, "key": key, "error": str(e)}

    text = (text or "").strip()
    if not text:
        return {"ok": False, "key": key, "error": "模型没吐出正文"}

    text = text[:MAX_CHARS]
    # 顺手把"长期有效"的东西落账（承诺、他的偏好、明确时间点）。
    # 失败不影响交接——两件事互不依赖。
    facts_added = 0
    try:
        import facts as _facts
        _r = _facts.save(_facts.extract(key, rows), key)
        facts_added = int(_r.get("added") or 0)
    except Exception:
        pass

    st[key] = {"ts": time.time(), "text": text, "fp": fingerprint, "rows": len(rows)}
    _save_state(st)

    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    (HANDOFF_DIR / f"{safe}.md").write_text(
        f"# {key} 交接摘要\n\n> 生成于 {time.strftime('%Y-%m-%d %H:%M')}，基于最近 {min(len(rows), MAX_LINES)} 条原话\n\n{text}\n",
        "utf-8")
    return {"ok": True, "key": key, "chars": len(text), "rows": len(rows),
            "factsAdded": facts_added, "text": text}


def _clip(text: str, n: int = 340) -> str:
    """按完整句子截断，别在半句话上砍一刀。"""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n]
    for sep in ("。", "；", "\n", "，", "、"):
        i = cut.rfind(sep)
        if i > n * 0.55:
            return cut[:i + 1]
    return cut + "…"


def fresh(key: str, minutes: int = FRESH_MIN) -> str:
    """刚换过会话就给，过期返回空串。"""
    st = _load_state()
    e = st.get(key) or {}
    if not e.get("text"):
        return ""
    if time.time() - float(e.get("ts") or 0) > minutes * 60:
        return ""
    return _clip(str(e["text"]), 340)


if __name__ == "__main__":
    import sys
    k = sys.argv[1] if len(sys.argv) > 1 else "private:1427148790"
    print(json.dumps(build(k, "--force" in sys.argv), ensure_ascii=False, default=str))