# -*- coding: utf-8 -*-
"""
digest.py —— 归档要目（模板里那本「一天压成 1~2KB」）

分工：
  原文  →  <archive>\\QQ\\YYYY-MM-DD.md        （一行一句，不加工，永久）
  要目  →  <archive>\\归档要目\\YYYY-MM-DD.md   （模型筛过一遍，1~2KB）

细节丢了回原文查——所以两份都留，要目不替代原文。

用法：
  python digest.py 2026-11-05            # 生成（已有就跳过）
  python digest.py 2026-11-05 --force    # 强制重做
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path

import paths

ARCHIVE_DIR = paths.ARCHIVE_QQ
DIGEST_DIR = paths.ARCHIVE_DIGEST

# 复用 fishbot 的 DeepSeek 配置，避免再多一处密钥
_CFG = None   # 模型配置改由 paths.json 提供
MAX_CHUNK = 8000          # 单块字符数
MAX_CHUNKS = 6            # 最多拼几块（控制成本）

PROMPT_ONE = """你在整理一天的聊天逐字稿，产出给"她自己"看的要目。

要求：
1. 只写逐字稿里**确实出现**的内容，不要推测、不要补充背景。
2. 保留人名原样，不要改写成"某人"。
3. 按这四节写，没内容就写"无"：
   ## 发生了什么
   ## 在意的事
   ## 约定与待办
   ## 出现的人
4. 每节用短句罗列，不要长段落。
5. 总长度控制在 1200 字以内。
6. 直接输出结果，不要写思考过程、不要解释你要做什么。

逐字稿：
"""

PROMPT_MERGE = """下面是同一天分段的要目，请合并成一份，去重、按主题归并，保留人名和具体约定。

仍然按四节输出，总长度 1200 字以内，只保留逐字稿里确实有的内容：

"""


def _api_conf() -> tuple[str, str, str]:
    """返回 (base, key, model)。"""
    return (
        str(paths.CFG.get("apiBase") or "https://api.deepseek.com/v1").rstrip("/"),
        str(paths.CFG.get("apiKey") or ""),
        str(paths.CFG.get("apiModel") or "deepseek-flash"),
    )


def _call(prompt: str, max_tokens: int) -> tuple[str, str]:
    """返回 (正文, finish_reason)。"""
    base, key, model = _api_conf()
    if not key:
        raise RuntimeError("没有 DeepSeek key")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.loads(r.read().decode("utf-8"))
    choice = d["choices"][0]
    return (choice["message"].get("content") or "").strip(), str(choice.get("finish_reason") or "")


def _chat(prompt: str, max_tokens: int = 4000) -> str:
    """带空正文保护：这个模型会先把 token 花在 reasoning 上，
    预算不够时正文就是空的（finish_reason=length），必须重试或降级。"""
    text, reason = _call(prompt, max_tokens)
    if text:
        return text
    text, reason = _call(prompt, max_tokens * 2)
    if text:
        return text
    raise RuntimeError(f"模型没吐出正文（finish_reason={reason}）")


def archive_rows(date_str: str) -> list[str]:
    src = ARCHIVE_DIR / f"{date_str}.md"
    if not src.exists():
        return []
    return [l for l in src.read_text("utf-8", errors="ignore").splitlines() if l.startswith("- ")]


def quick_stats(rows: list[str]) -> dict:
    keys, times, people = {}, [], {}
    for l in rows:
        m = re.match(r"^- (\d\d:\d\d) \[([^\]]+)\] ([^：]+)：(.*)$", l)
        if not m:
            continue
        t, key, who, _ = m.groups()
        times.append(t)
        keys[key] = keys.get(key, 0) + 1
        people[who] = people.get(who, 0) + 1
    return {
        "rows": len(rows),
        "sessions": len(keys),
        "top_sessions": sorted(keys.items(), key=lambda x: -x[1])[:5],
        "people": sorted(people.items(), key=lambda x: -x[1])[:6],
        "range": (min(times), max(times)) if times else None,
    }


def _mechanical(date_str: str, st: dict) -> str:
    """模型不通时的兜底：机械统计也要能落地，不让她某天突然没有要目。"""
    rng = f"{st['range'][0]}~{st['range'][1]}" if st["range"] else "无"
    sessions = "、".join(f"{k}({n})" for k, n in st["top_sessions"]) or "无"
    people = "、".join(f"{k}({n})" for k, n in st["people"]) or "无"
    return (
        f"# {date_str} 要目（机械摘要）\n\n"
        f"> 模型不可用，这份只有统计，细节请看原文 `{ARCHIVE_DIR / (date_str + '.md')}`\n\n"
        f"## 发生了什么\n- 共 {st['rows']} 条消息，覆盖 {st['sessions']} 个会话，时间 {rng}\n\n"
        f"## 在意的事\n- （未能提炼）\n\n"
        f"## 约定与待办\n- （未能提炼）\n\n"
        f"## 出现的人\n- {people}\n\n"
        f"## 会话分布\n- {sessions}\n"
    )


def build(date_str: str, force: bool = False) -> dict:
    rows = archive_rows(date_str)
    if not rows:
        return {"ok": False, "date": date_str, "reason": "当天没有归档"}

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    dst = DIGEST_DIR / f"{date_str}.md"
    if dst.exists() and not force:
        return {"ok": True, "date": date_str, "skipped": True, "path": str(dst)}

    st = quick_stats(rows)
    text = "\n".join(rows)

    # 太长就分块先压，再合并；避免一次塞爆上下文
    chunks = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)][:MAX_CHUNKS]
    try:
        if len(chunks) == 1:
            body = _chat(PROMPT_ONE + chunks[0])
        else:
            parts = [_chat(PROMPT_ONE + c, max_tokens=1200) for c in chunks]
            body = _chat(PROMPT_MERGE + "\n\n---\n\n".join(parts), max_tokens=3000)
        mode = "model"
    except Exception as e:
        body = _mechanical(date_str, st)
        mode = f"mechanical({e})"

    header = (
        f"# {date_str} 要目\n\n"
        f"> {st['rows']} 条 / {st['sessions']} 个会话"
        + (f" / {st['range'][0]}~{st['range'][1]}" if st["range"] else "")
        + f"\n> 原文：`{ARCHIVE_DIR / (date_str + '.md')}`\n\n"
    )
    dst.write_text(header + body.strip() + "\n", "utf-8")
    return {"ok": True, "date": date_str, "mode": mode, "path": str(dst),
            "bytes": dst.stat().st_size, "stats": st}


def ensure_yesterday() -> dict:
    """给 memory-brain 的循环调用：每天只做一次，失败不抛。"""
    from datetime import timedelta
    d = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        return build(d)
    except Exception as e:
        return {"ok": False, "date": d, "error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    d = a.date or datetime.now().strftime("%Y-%m-%d")
    r = build(d, a.force)
    print(json.dumps(r, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()