# -*- coding: utf-8 -*-
"""大肥鱼的「记忆注入块」服务（记忆系统插件包 · DSH 落地壳 · 只读版）

主人转述她的原话（2026-10-28）：
    「库我自己建，注入按它那 350 token 收，比现在这一坨省一半」

按这句话定的边界：
  · **库她自己建** —— 这里是**只读**检索层，不新建库、不复制库、不写任何记忆。
    数据来源就是她自己的：Mnemon「大肥鱼的记忆库」+ 日记 + her-notes + her-desk。
  · **注入按 350 token 收** —— 每个会话写一份 ≤350 token 的注入块（带 marker），
    qq-bridge 在唤醒提示里原样贴；超预算就先砍条数、再砍字数。
  · 只在她和主人的私聊里注入（群里/陌生人不贴），config.json 里可放开。

它只做三件事：
  1. 每 30 秒把上面几个来源读进内存，建一个临时索引（不落盘）；
  2. 拿每个会话最后一条真人消息当 query，按「关键词 + 重要性 + 新鲜度」打分检索；
  3. 把 top-N 写成 <engine>\\out\\<key>.txt（带 <!--memory-brain:* --> marker）。

用法：
  python memory_inject.py          常驻（HTTP 3132 + 每 30 秒刷一轮）
  python memory_inject.py --once   跑一轮，打印每个会话的块和 token 估算
  python memory_inject.py --debug "query"  手动检索一次
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import retention  # 同目录：保持度 / 分层 / 情绪 / 联想
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import paths

ROOT = Path(__file__).resolve().parent
CONF_PATH = ROOT / "config.json"
OUT = paths.OUT
LOG = paths.LOGS / "inject.log"
STATE = ROOT / "inject-state.json"

MNEMON_DB = paths.MNEMON_DB
DIARY = paths.DIARY
NOTES = paths.NOTES
DESK = paths.DESK
SOCIAL = paths.SOCIAL

try:
    import daily_digest          # 要目：每天压一份 1~2KB
    _DIGEST_OK = True
except Exception:
    _DIGEST_OK = False

try:
    import handoff                # 交接摘要：换会话时接上之前的上下文
    _HANDOFF_OK = True
except Exception:
    _HANDOFF_OK = False

DEFAULTS = {
    "port": 3132,
    "loopSec": 30,
    "budget": {"maxTokens": 350, "maxChars": 520, "maxItems": 3},
    "inject": {
        "enabled": True,
        "keys": ["private:1427148790"],
        "injectGroups": False,
        "header": "【你记得的事】相关才提，别照念、别一次全说",
        "privateK": 3,
        "privateMin": 0.32,
        "groupK": 1,
        "groupMin": 0.5,
    },
    "sources": {"mnemon": True, "diary": True, "notes": True, "desk": True},
    "weights": {"semantic": 0.22, "keyword": 0.45, "importance": 0.18, "recency": 0.15},
    "recencyHalfLifeDays": 30.0,
    "dedupe": 0.82,
}


def _conf() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        if CONF_PATH.exists():
            user = json.loads(CONF_PATH.read_text("utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except Exception as e:
        log(f"配置读取失败，用默认：{e}")
    return cfg


CONF = _conf()


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─────────────────────── 分词 / 打分（纯本地，不调模型） ───────────────────────
_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]{2,}")


def bigrams(text: str) -> set[str]:
    """中文按 bigram，英文/数字按词。短到切不出 bigram 时退回单字。"""
    text = re.sub(r"\s+", "", str(text or ""))
    out: set[str] = set()
    cjk = _CJK.findall(text)
    if len(cjk) == 1:
        out.add(cjk[0])
    for i in range(len(cjk) - 1):
        out.add(cjk[i] + cjk[i + 1])
    for w in _ASCII_WORD.findall(text):
        out.add(w.lower())
    return out


def keyword_score(query_bg: set[str], text: str) -> float:
    """IDF 加权覆盖率：稀有 bigram（"挖到""矿道"）比到处都有的（"麦块"）值钱。

    为什么不用余弦语义：实测本机 nomic-embed-text 在中文短 query 上几乎全是
    0.70~0.84 的“假相似”（"我上课呢" 和 "挖掉的东西会掉在地上" 也有 0.76），
    门控没法用 —— 标定记录见 logs/sem-calib.txt。
    """
    if not query_bg:
        return 0.0
    tb = text if isinstance(text, (set, frozenset)) else bigrams(text)
    if not tb:
        return 0.0
    matched = query_bg & tb
    if not matched:
        return 0.0
    # 分母只算**语料里真的存在**的 query bigram：语料里一个都没有的（"块挖""到哪"）
    # 谁也匹配不上，算进分母只会把分数稀释成 0.3x，反而把正确记忆压下去。
    total = sum(_idf(bg) for bg in query_bg if _DF.get(bg, 0) > 0)
    if total <= 0:
        return 0.0
    got = sum(_idf(bg) for bg in matched)
    coverage = got / total
    density = len(matched) / max(1, min(len(tb), 24))
    return min(1.0, 0.85 * coverage + 0.15 * density)


# 中文虚词：query 里这些字不提供信息，先剔掉再切 bigram，否则覆盖率被稀释
_STOP_CHARS = set("我你他她它们的了吗呢啊吧嘛呀哦是在有给把被这那个里上下去就都也还和跟对说会能要不很太")


def norm_query(text: str) -> str:
    s = re.sub(r"\s+", "", str(text or ""))
    return "".join(ch for ch in s if ch not in _STOP_CHARS)


_DF: dict[str, int] = {}


def _idf(bg: str) -> float:
    n = max(1, len(_INDEX))
    df = _DF.get(bg, 0)
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def similarity(a: str, b: str) -> float:
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, min(len(ga), len(gb)))


def est_tokens(text: str) -> int:
    """粗估 token：中文 0.7/字，ASCII 0.25/字符。宁可估高一点。"""
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(math.ceil(cjk * 0.7 + other * 0.25))


def _ts(value) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        try:
            return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
        except Exception:
            return 0.0


# ─────────────────────── 只读索引 ───────────────────────
ITEM_KIND = {
    "fact": "事实", "preference": "偏好", "decision": "决定", "insight": "懂的事",
    "lesson": "道理", "context": "背景", "general": "记得", "reflection": "心事",
    "episode": "往事", "skill": "做法",
}


def load_mnemon(items: list[dict]) -> int:
    if not CONF["sources"].get("mnemon", True) or not MNEMON_DB.exists():
        return 0
    n = 0
    try:
        conn = sqlite3.connect(f"file:{MNEMON_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, content, category, importance, created_at, updated_at, last_accessed_at, tags, embedding "
            "FROM insights WHERE deleted_at IS NULL"
        ).fetchall()
        for r in rows:
            text = re.sub(r"\s+", " ", str(r["content"] or "")).strip()
            if len(text) < 8:
                continue
            vec = None
            blob = r["embedding"]
            if blob and isinstance(blob, (bytes, bytearray, memoryview)) and len(blob) >= 4:
                try:
                    import numpy as np
                    arr = np.frombuffer(bytes(blob), dtype="<f4").astype("float32")
                    norm = float(np.linalg.norm(arr))
                    if arr.size >= 64 and norm > 0:
                        vec = arr / norm
                except Exception:
                    vec = None
            items.append({
                "id": "mn:" + str(r["id"]),
                "text": text,
                "kind": ITEM_KIND.get(str(r["category"] or "general").lower(), "记得"),
                "importance": max(0.1, min(1.0, float(r["importance"] or 3) / 5.0)),
                "ts": _ts(r["updated_at"] or r["created_at"]),
                "vis": "private",
                "vec": vec,
            })
            n += 1
        conn.close()
    except Exception as e:
        log(f"读 Mnemon 失败：{e}")
    return n


def load_diary(items: list[dict]) -> int:
    if not CONF["sources"].get("diary", True) or not DIARY.exists():
        return 0
    n = 0
    for f in sorted(DIARY.glob("*.md")):
        try:
            text = f.read_text("utf-8", errors="ignore")
        except Exception:
            continue
        parts = re.split(r"\n##\s*(\d{1,2}:\d{2})", "\n" + text)
        for i in range(1, len(parts) - 1, 2):
            hhmm, body = parts[i], re.sub(r"\s+", " ", parts[i + 1]).strip()
            if len(body) < 10:
                continue
            try:
                ts = datetime.strptime(f"{f.stem} {hhmm}", "%Y-%m-%d %H:%M").timestamp()
            except Exception:
                ts = 0.0
            items.append({
                "id": f"diary:{f.stem}:{hhmm}",
                "text": body[:600],
                "kind": "心事",
                "importance": 0.55,
                "ts": ts,
                "vis": "private",
            })
            n += 1
    return n


def load_notes(items: list[dict]) -> int:
    if not CONF["sources"].get("notes", True) or not NOTES.exists():
        return 0
    n = 0
    for f in sorted(NOTES.glob("*.md")):
        if f.name == "mc-记忆.md":
            continue
        try:
            text = f.read_text("utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            m = re.match(r"^\s*[-*]\s+(.{6,})$", line)
            if not m:
                continue
            body = re.sub(r"\s+", " ", m.group(1)).strip()
            items.append({
                "id": f"note:{f.name}:{hash(body) & 0xffffff}",
                "text": f"{body[:300]}（她的笔记 {f.name}）",
                "kind": "做法",
                "importance": 0.5,
                "ts": f.stat().st_mtime if f.exists() else 0.0,
                "vis": "public",
            })
            n += 1
    return n


def load_desk(items: list[dict]) -> int:
    if not CONF["sources"].get("desk", True) or not DESK.exists():
        return 0
    n = 0
    try:
        text = DESK.read_text("utf-8", errors="ignore")
    except Exception:
        return 0
    chunks = re.split(r"\n##\s+", "\n" + text)
    for ch in chunks[1:]:
        lines = ch.strip().splitlines()
        if not lines:
            continue
        head = lines[0].strip()[:30]
        # 按条切：桌上每一条（- 开头）单独成项，别整段塞进检索（整段还会把 350 token 吃光）
        mday = re.match(r"(\d{4}-\d{2}-\d{2})", head)
        ts = DESK.stat().st_mtime
        if mday:
            try:
                ts = datetime.strptime(mday.group(1), "%Y-%m-%d").timestamp() + 12 * 3600
            except Exception:
                pass
        for line in lines[1:]:
            m = re.match(r"^\s*[-*]\s+(.{10,})$", line)
            if not m:
                continue
            body = re.sub(r"\s+", " ", m.group(1)).strip()
            items.append({
                "id": f"desk:{head}:{len(items)}",
                "text": f"{head}｜{body[:400]}",
                "kind": "往事",
                "importance": 0.45,
                "ts": ts,
                "vis": "private",
            })
            n += 1
    return n


_INDEX: list[dict] = []
_INDEX_AT = 0.0
_INDEX_LOCK = threading.Lock()

# 她此刻的心情（桥接每次唤醒带过来）。2 小时内有效，过期当作平静。
_CUR_MOOD: dict = {"label": "", "intensity": 0.0, "at": 0.0}
_MOOD_TTL = 2 * 3600


def set_mood(label: str, intensity) -> None:
    try:
        _CUR_MOOD["label"] = str(label or "")[:24]
        _CUR_MOOD["intensity"] = max(0.0, min(1.0, float(intensity or 0)))
        _CUR_MOOD["at"] = time.time()
    except Exception:
        pass


def get_mood() -> dict | None:
    """当前有效心情；过期或太淡 → None（平静）。"""
    if not _CUR_MOOD.get("label"):
        return None
    if time.time() - float(_CUR_MOOD.get("at") or 0) > _MOOD_TTL:
        return None
    if float(_CUR_MOOD.get("intensity") or 0) < 0.2:
        return None
    return dict(_CUR_MOOD)


def build_index(force: bool = False) -> dict:
    global _INDEX, _INDEX_AT, _DF
    with _INDEX_LOCK:
        if not force and _INDEX and time.time() - _INDEX_AT < 45:
            return {"items": len(_INDEX), "cached": True}
        items: list[dict] = []
        c = {
            "mnemon": load_mnemon(items),
            "diary": load_diary(items),
            "notes": load_notes(items),
            "desk": load_desk(items),
        }
        # 预先切好 bigram 并统计 df（IDF 用），检索时不再重复算
        df: dict[str, int] = {}
        for it in items:
            bg = bigrams(it["text"])
            it["bg"] = bg
            for g in bg:
                df[g] = df.get(g, 0) + 1
        _DF = df
        # 保持度：给每条记忆建/取一条记录（旁路库 state/retention.db，绝不改 mnemon）
        try:
            retention.ensure(items, mood=get_mood())
            snap = retention.snapshot()
            for it in items:
                it["_ret"] = snap.get(it["id"])
                it["_sal"] = retention.salience(it["text"])
        except Exception as e:
            log(f"保持度初始化失败（不影响注入）：{e}")
            for it in items:
                it["_ret"] = None
                it["_sal"] = 0.0
        _INDEX = items
        _INDEX_AT = time.time()
    return {"items": len(items), **c, "cached": False}


# ─────────────────────── 检索 ───────────────────────
_EMB_CACHE: dict[str, object] = {}


def embed_query(text: str):
    """用本机 Ollama（nomic-embed-text，768 维）给 query 算向量 —— 和 Mnemon 里存的是同一套。
    失败就返回 None：那就只剩关键词 + 重要性 + 新鲜度，功能不受影响。"""
    cfg = CONF.get("embed") or {}
    if cfg.get("enabled") is False:
        return None
    key = (text or "").strip()[:200]
    if not key:
        return None
    if key in _EMB_CACHE:
        return _EMB_CACHE[key]
    arr = None
    try:
        import numpy as np
        body = json.dumps({"model": cfg.get("model") or "nomic-embed-text", "prompt": key}).encode("utf-8")
        req = urllib.request.Request(
            cfg.get("endpoint") or "http://127.0.0.1:11434/api/embeddings",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            vec = json.loads(r.read().decode("utf-8")).get("embedding")
        if vec:
            a = np.asarray(vec, dtype="float32")
            nrm = float(np.linalg.norm(a))
            arr = a / nrm if nrm > 0 else None
    except Exception:
        arr = None
    if len(_EMB_CACHE) > 300:
        _EMB_CACHE.clear()
    _EMB_CACHE[key] = arr
    return arr


def retrieve(query: str, allow: set[str], k: int, allow_cold: bool = False, mood: dict | None = None) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    build_index()
    qb = bigrams(norm_query(query))
    if not qb:
        return []
    # 去掉虚词后还短（"我上课呢" → "上课"）就不注入：命中一两个 bigram 太容易，注进来的都是噪音。
    if len(qb) < int(CONF.get("minQueryBigrams") or 2):
        return []
    min_kw = float(CONF.get("minKeyword") or 0.5)
    min_hits = int(CONF.get("minHits") or 2)
    min_sem = float(CONF.get("minSemantic") or 0.7)
    w = CONF["weights"]
    qvec = embed_query(query)   # 默认关（见 config.embed.enabled 与 logs/sem-calib.txt）
    try:
        import numpy as np
    except Exception:
        np = None
    half = max(1.0, float(CONF.get("recencyHalfLifeDays") or 30.0)) * 86400.0
    rt = CONF.get("retention") or {}
    rt_on = bool(rt.get("enabled", True))
    now = time.time()
    # 她现在的心情：同一情绪倾向的记忆更容易被想起来（情绪一致性回忆）
    mood = mood if mood is not None else get_mood()
    m_val, m_aro, m_int = 0.0, 0.0, 0.0
    if rt_on and mood:
        try:
            m_val, m_aro = retention.mood_axes(str(mood.get("label") or ""))
            m_int = float(mood.get("intensity") or 0)
        except Exception:
            m_val, m_aro, m_int = 0.0, 0.0, 0.0

    def _rt(it):
        if not rt_on:
            return 1.0, "warm", 0.0
        st = retention.strength(it.get("_ret"), float(it["importance"]), now, rt)
        return st, retention.layer_of(st, rt), float(it.get("_sal") or 0.0)

    scored = []
    for it in _INDEX:
        if it["vis"] not in allow:
            continue
        bg = it.get("bg") or bigrams(it["text"])
        s_kw = keyword_score(qb, bg)
        hits = len(qb & bg)
        s_sem = 0.0
        if qvec is not None and np is not None and it.get("vec") is not None:
            try:
                s_sem = max(0.0, float(np.dot(qvec, it["vec"])))
            except Exception:
                s_sem = 0.0
        # 两条路进得来：IDF 关键词够硬，或者语义确实够像（默认语义关着）
        if not ((s_kw >= min_kw and hits >= min_hits) or s_sem >= min_sem):
            continue
        st, layer, sal = _rt(it)
        # 冷层：已经快忘了，不参与主动注入，只在显式「按需检索」里出现
        if layer == "cold" and not allow_cold:
            continue
        s_imp = float(it["importance"])
        age = max(0.0, now - float(it["ts"] or now))
        s_rec = 0.5 ** (age / half)
        score = (float(w.get("semantic", 0.22)) * s_sem
                 + float(w["keyword"]) * s_kw
                 + float(w["importance"]) * s_imp
                 + float(w["recency"]) * s_rec)
        if rt_on:
            # 保持度是乘性因子：快忘掉的就算关键词像，也排后面
            score = score * (0.62 + 0.38 * st) + 0.10 * sal
            # 心情一致：难过时更容易想起难过的事，兴奋时更容易想起高兴的事
            if m_int >= 0.2 and m_val != 0:
                tv = retention.valence(it["text"])
                if tv != 0 and (tv > 0) == (m_val > 0):
                    score += 0.12 * m_int * min(1.0, abs(tv) * abs(m_val))
                elif tv != 0:
                    score -= 0.05 * m_int
        scored.append({"_score": score, "_kw": s_kw, "_sem": s_sem, "_st": st,
                       "_layer": layer, "_sal": sal, "_via": None, "item": it})
    scored.sort(key=lambda x: x["_score"], reverse=True)

    # ── 扩散激活（联想）：由最硬的几颗星，沿 Mnemon 的边把分借给邻居 ──────
    spread = (rt.get("spread") or {})
    if rt_on and spread.get("enabled", True) and scored:
        hop_decay = float(spread.get("decay") or 0.45)
        min_act = float(spread.get("minActivation") or 0.18)
        top = int(spread.get("seeds") or 5)
        by_id = {it["id"]: it for it in _INDEX}
        adj = retention.neighbors(MNEMON_DB) if MNEMON_DB.exists() else {}
        already = {c["item"]["id"] for c in scored}
        seen = set(already)
        frontier = []
        for c in scored[:top]:
            mid = c["item"]["id"]
            if mid.startswith("mn:"):
                frontier.append((mid[3:], c["_score"]))
        for _depth in (1, 2):
            nxt = []
            for mid, act in frontier:
                for nid, typ, wt in (adj.get(mid) or [])[:24]:
                    key = "mn:" + nid
                    if key in seen:
                        continue
                    gain = act * hop_decay * (0.55 + 0.45 * min(1.0, wt))
                    if gain < min_act:
                        continue
                    seen.add(key)
                    it2 = by_id.get(key)
                    if not it2 or it2["vis"] not in allow:
                        continue
                    st2, lay2, _sal2 = _rt(it2)
                    if lay2 == "cold" and not allow_cold:
                        continue
                    scored.append({"_score": gain, "_kw": 0.0, "_sem": 0.0, "_st": st2,
                                   "_layer": lay2, "_sal": 0.0, "_via": typ, "item": it2})
                    nxt.append((nid, gain))
            frontier = nxt
            if not frontier:
                break
        scored.sort(key=lambda x: x["_score"], reverse=True)

    picked: list[dict] = []
    for c in scored:
        if len(picked) >= k:
            break
        it = c["item"]
        if any(similarity(it["text"], p["text"]) >= float(CONF.get("dedupe") or 0.82) for p in picked):
            continue
        picked.append({**it, "score": round(c["_score"], 4), "kw": round(c["_kw"], 4),
                       "sem": round(c["_sem"], 4), "strength": round(c["_st"], 3),
                       "layer": c["_layer"], "salience": round(c["_sal"], 3),
                       "via": c["_via"]})
    # 被想起 = 复习一次（异步、带节流，不拖慢检索）
    if rt_on and picked:
        try:
            import threading as _th
            _th.Thread(target=retention.touch, args=([p["id"] for p in picked], rt, query), daemon=True).start()
        except Exception:
            pass
    return picked


# ─────────────────────── 流水（刚才的原话）───────────────────────
def _fmt_time(ms) -> str:
    try:
        import datetime
        ts = float(ms)
        if ts > 1e12:      # 毫秒
            ts /= 1000.0
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return "--:--"


def compose_stream(key: str) -> str:
    """从 social-v2.json 的 recentMessages 生成「这边刚才的几句」。

    只放现场原话：谁、几点、说了什么。结论和偏好不进这里（那是主账本的事），
    否则流水会慢慢长成第二个记忆库。
    """
    inj = CONF.get("inject") or {}
    if not inj.get("streamEnabled", True):
        return ""
    n = int(inj.get("streamLines") or 6)
    max_chars = int(inj.get("streamChars") or 120)

    try:
        conv = (_load_social().get("conversations") or {}).get(key) or {}
    except Exception:
        return ""
    msgs = conv.get("recentMessages") or []
    if not msgs:
        return ""

    rows, last = [], None
    for m in msgs[-n * 2:]:
        text = str(m.get("plain") or m.get("text") or "").strip()
        if not text:
            continue
        text = text.replace("\n", " ")[:max_chars]
        if m.get("isSelf"):
            who = "你"
        elif m.get("isOwner"):
            who = "他"                      # 私聊里另一边是管理员
        else:
            who = str(m.get("sender") or "").strip()
            if who in ("", "私聊", "群聊"):
                who = "对方"
            who = who[:12]
        row = f"- {_fmt_time(m.get('time'))} {who}：{text}"
        if row == last:          # 连着两条一样的，去重
            continue
        rows.append(row)
        last = row

    rows = rows[-n:]
    parts = []

    # 刚换过会话 → 先把"之前这段聊到哪儿"放最前面。
    # 过期（90 分钟）自动不注入，避免整天背着一段旧摘要。
    if _HANDOFF_OK:
        try:
            _h = handoff.fresh(key)
            if _h:
                parts.append("【刚换了会话，之前这段聊到这儿】\n" + _h[:320])
        except Exception:
            pass

    if rows:
        parts.append("【这边（QQ）刚才的几句】\n" + "\n".join(rows))

    # 另一边：桌面上最近在忙什么。只给 2 行，并且说清"那不是这段对话"——
    # 混在一起时人名会在"自称"和"第三人称"之间打架（模板里那条教训）。
    other = int(inj.get("otherSideLines") or 2)
    # 只在私聊给「另一边的桌面」，群里说桌面上的事会显得突兀
    if not str(key).startswith("private:"):
        other = 0
    if other > 0 and DESK.exists():
        try:
            lines = [l.strip(" -\t") for l in DESK.read_text("utf-8", errors="ignore").splitlines() if l.strip()]
            lines = [l[:100] for l in lines[-other:]]
            if lines:
                parts.append("【另一边（桌面）最近几句——那是桌面上的事，不是这段对话】\n"
                             + "\n".join("- " + l for l in lines))
        except Exception:
            pass

    return "\n".join(parts)


# ─────────────────────── 注入块 ───────────────────────
def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(key)) + ".txt"


def compose(key: str, items: list[dict]) -> str:
    stream = compose_stream(key)       # 流水：原话，先注入
    if not items:
        if not stream:
            return ""
        return "\n".join(["<!--memory-brain:start-->", stream, "<!--memory-brain:end-->"])
    b = CONF["budget"]
    max_chars = int(b.get("maxChars") or 520)
    max_tokens = int(b.get("maxTokens") or 350)
    header = CONF["inject"].get("header") or "【你记得的事】"
    lines = ["<!--memory-brain:start-->"]
    if stream:
        lines.append(stream)
    lines.append(header)
    for it in items[: int(b.get("maxItems") or 3)]:
        text = it["text"]
        # 单条也别太长：先按 140 字截
        text = text[:140]
        candidate = f"- （{it['kind']}）{text}"
        probe = "\n".join(lines + [candidate, "<!--memory-brain:end-->"])
        if len(probe) > max_chars or est_tokens(probe) > max_tokens:
            # 砍到刚好放进去
            room = max_chars - len("\n".join(lines + ["<!--memory-brain:end-->"])) - len(f"- （{it['kind']}）") - 1
            if room < 20:
                break
            candidate = f"- （{it['kind']}）{text[:room]}"
            probe = "\n".join(lines + [candidate, "<!--memory-brain:end-->"])
            if est_tokens(probe) > max_tokens:
                break
        lines.append(candidate)
    lines.append("<!--memory-brain:end-->")
    return "\n".join(lines)


def _load_social() -> dict:
    try:
        return json.loads(SOCIAL.read_text("utf-8"))
    except Exception:
        return {}


def _last_query(conv: dict) -> str:
    for m in reversed(conv.get("recentMessages") or []):
        if m.get("isSelf"):
            continue
        q = str(m.get("plain") or m.get("text") or "").strip()
        if q:
            return q[:200]
    return ""


_CUE_RE = __import__("re").compile(r"(记得|想起|忘了|忘记|上次|上回|之前|以前|那天|那次|我们说好|答应|回忆|记不记得)")


def _cued(query: str) -> bool:
    """这句话是在"回忆"吗？是的话按需检索范围放宽到冷层（快忘掉的那些）。"""
    return bool(_CUE_RE.search(query or ""))


def _block_for_key(key: str, conv: dict) -> tuple[str, list[dict]]:
    inj = CONF["inject"]
    is_group = str(key).startswith("group:")
    k = int(inj.get("groupK") if is_group else inj.get("privateK") or 3)
    min_score = float(inj.get("groupMin") if is_group else inj.get("privateMin") or 0.45)
    allow = {"group", "public"} if is_group else {"private", "public"}
    query = _last_query(conv)
    if not query:
        # 没有真人发言时仍然要出流水（她自己的几句也有用），别整块丢掉
        return compose(key, []), []
    items = [i for i in retrieve(query, allow, max(k * 2, 4), allow_cold=_cued(query)) if i["score"] >= min_score][:max(1, k)]
    return compose(key, items), items


def _write_block(key: str, block: str) -> bool:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / _safe(key)
    try:
        old = p.read_text("utf-8") if p.exists() else ""
    except Exception:
        old = None
    if old != block:
        try:
            p.write_text(block, "utf-8")
            return True
        except Exception as e:
            log(f"写块失败 {key}: {e}")
    return False


def _block_from_query(key: str, query: str) -> tuple[str, list[dict]]:
    inj = CONF["inject"]
    is_group = str(key).startswith("group:")
    k = int(inj.get("groupK") if is_group else inj.get("privateK") or 3)
    min_score = float(inj.get("groupMin") if is_group else inj.get("privateMin") or 0.45)
    allow = {"group", "public"} if is_group else {"private", "public"}
    items = [i for i in retrieve(query, allow, max(k * 2, 4), allow_cold=_cued(query)) if i["score"] >= min_score][:max(1, k)]
    return compose(key, items), items


def refresh_one(key: str, _q: str = "") -> dict:
    """按**当前这条消息**重算某个会话的块。
    桥接在拼唤醒提示前调一次 —— 轮询是 30 秒一轮，等它就等于拿上一条消息当 query。"""
    inj = CONF["inject"]
    if not inj.get("enabled", True):
        return {"ok": False, "reason": "disabled"}
    allow_keys = inj.get("keys") or []
    if (allow_keys and key not in allow_keys) or (str(key).startswith("group:") and not inj.get("injectGroups", False)):
        _write_block(key, "")
        return {"ok": True, "skipped": True}
    conv = (_load_social().get("conversations") or {}).get(key) or {}
    build_index()
    # q 只给手工验证用（/refresh?key=…&q=…）：正常唤醒不带它，一律用最后一条真人消息当 query
    override = (_q or "").strip()
    block, items = (_block_from_query(key, override) if override else _block_for_key(key, conv))
    _write_block(key, block)
    return {"ok": True, "chars": len(block), "tokens": est_tokens(block),
            "items": [i.get("id") for i in items], "query": _last_query(conv)[:60]}


def refresh_blocks() -> dict:
    social = _load_social()
    convs = social.get("conversations") or {}
    OUT.mkdir(parents=True, exist_ok=True)
    inj = CONF["inject"]
    written = {}
    idx = build_index(force=True) if CONF.get("refreshIndexEachCycle") else build_index()
    if not inj.get("enabled", True):
        for key in convs:
            _write_block(key, "")
        return {"disabled": True, **idx}
    allow_keys = inj.get("keys") or []
    for key, conv in convs.items():
        if allow_keys and key not in allow_keys:
            _write_block(key, "")
            continue
        if str(key).startswith("group:") and not inj.get("injectGroups", False):
            _write_block(key, "")
            continue
        block, items = _block_for_key(key, conv)
        _write_block(key, block)
        if block:
            written[key] = {"chars": len(block), "tokens": est_tokens(block), "items": [i["id"] for i in items]}
    return {"blocks": written, **idx}


# ─────────────────────── 星图数据（只读） ───────────────────────
GRAPH_CACHE: dict = {"at": 0.0, "data": None}
_EDGE_PRI = {"causal": 4, "semantic": 3, "temporal": 2, "entity": 1}


def graph_data(max_edges: int = 900, ttl: float = 20.0) -> dict:
    """把 Mnemon 库里的记忆和图谱边读出来给星图用。**只读**，绝不写库。"""
    if GRAPH_CACHE["data"] and time.time() - GRAPH_CACHE["at"] < ttl:
        return GRAPH_CACHE["data"]
    nodes: list[dict] = []
    edges: list[dict] = []
    counts = {"fact": 0, "insight": 0, "context": 0, "other": 0}
    rtconf = CONF.get("retention") or {}
    try:
        snap = retention.snapshot()
        recents = set(retention.recent(45))
    except Exception:
        snap, recents = {}, set()
    now = time.time()
    if not MNEMON_DB.exists():
        return {"nodes": [], "edges": [], "counts": counts}
    try:
        conn = sqlite3.connect(f"file:{MNEMON_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, content, category, importance, tags, created_at, updated_at "
            "FROM insights WHERE deleted_at IS NULL"
        ).fetchall()
        ids = set()
        for r in rows:
            text = re.sub(r"\s+", " ", str(r["content"] or "")).strip()
            if len(text) < 6:
                continue
            cat = str(r["category"] or "general").lower()
            cat = cat if cat in ("fact", "insight", "context") else "other"
            counts[cat] = counts.get(cat, 0) + 1
            tags = ""
            try:
                raw_tags = json.loads(r["tags"] or "[]")
                if isinstance(raw_tags, list):
                    tags = " ".join(str(t) for t in raw_tags[:4])
            except Exception:
                pass
            ids.add(r["id"])
            imp = max(0.1, min(1.0, float(r["importance"] or 3) / 5.0))
            rec = snap.get("mn:" + str(r["id"]))
            st = retention.strength(rec, imp, now, rtconf)
            nodes.append({
                "id": r["id"],
                "short": text[:88],
                "text": text[:700],
                "category": cat,
                "importance": imp,
                "date": str(r["updated_at"] or r["created_at"] or "")[:10],
                "tags": tags[:48],
                "strength": round(st, 3),
                "layer": retention.layer_of(st, rtconf),
                "hits": int((rec or {}).get("hits") or 0),
                "salience": round(float((rec or {}).get("sal") or 0), 3),
                "recent": ("mn:" + str(r["id"])) in recents,
            })
        seen: dict[tuple, dict] = {}
        for e in conn.execute("SELECT source_id, target_id, edge_type, weight FROM edges").fetchall():
            a, b = e["source_id"], e["target_id"]
            if a not in ids or b not in ids or a == b:
                continue
            key = (a, b) if a < b else (b, a)
            typ = str(e["edge_type"] or "entity")
            pri = _EDGE_PRI.get(typ, 0)
            if key not in seen or seen[key]["pri"] < pri:
                seen[key] = {"a": key[0], "b": key[1], "type": typ,
                             "weight": float(e["weight"] or 0), "pri": pri}
        edges = [{"a": v["a"], "b": v["b"], "type": v["type"], "weight": v["weight"]}
                 for v in seen.values()]
        edges.sort(key=lambda x: -x["weight"])
        edges = edges[:max_edges]
        conn.close()
    except Exception as e:
        log(f"星图取数失败：{e}")
    data = {"nodes": nodes, "edges": edges, "counts": counts, "at": time.time()}
    GRAPH_CACHE["data"] = data
    GRAPH_CACHE["at"] = time.time()
    return data


# ─────────────────────── HTTP ───────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "MemoryInject/1.0"

    def log_message(self, fmt, *args):
        return

    def _html(self, text: str, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/health":
                return self._json({
                    "ok": True,
                    "index": build_index(),
                    "conf": {"budget": CONF["budget"], "inject": CONF["inject"], "sources": CONF["sources"]},
                })
            if u.path == "/recall":
                text = (q.get("text") or [""])[0]
                if (q.get("mood") or [""])[0]:
                    set_mood((q.get("mood") or [""])[0], (q.get("mi") or ["0.5"])[0])
                key = (q.get("key") or ["private"])[0]
                k = int((q.get("k") or ["3"])[0])
                is_group = key.startswith("group:")
                allow = {"group", "public"} if is_group else {"private", "public"}
                cold = (q.get("cold") or ["0"])[0] in ("1", "true", "yes")
                items = retrieve(text, allow, k, allow_cold=cold)
                return self._json({
                    "query": text,
                    "items": items,
                    "layers": {i: sum(1 for x in items if x.get("layer") == i) for i in ("hot", "warm", "cold")},
                })
            if u.path == "/refresh":
                key = (q.get("key") or [""])[0]
                if (q.get("mood") or [""])[0]:
                    set_mood((q.get("mood") or [""])[0], (q.get("mi") or ["0.5"])[0])
                return self._json(refresh_one(key, (q.get("q") or [""])[0]))
            if u.path in ("/", "/star", "/star.html"):
                f = ROOT / "star.html"
                if not f.exists():
                    return self._html("<h1>star.html 不见了</h1>", 404)
                return self._html(f.read_text("utf-8"))
            if u.path == "/graph":
                g = graph_data()
                return self._json({"nodes": g["nodes"], "edges": g["edges"], "counts": g["counts"]})
            if u.path == "/stats":
                return self._json({"ok": True, "retention": retention.stats(CONF),
                                   "mood": get_mood(), "index": build_index()})
            if u.path == "/dormant":
                lim = int((q.get("limit") or ["50"])[0])
                return self._json({"ok": True, "dormant": retention.dormant_list(lim),
                                   "count": len(retention.dormant_list(lim))})
            if u.path == "/wake":
                mid = (q.get("id") or [""])[0]
                if not mid:
                    return self._json({"ok": False, "reason": "缺 id"}, 400)
                return self._json({"ok": retention.wake(mid), "id": mid})
            if u.path == "/handoff":
                k = (q.get("key") or [""])[0]
                force = (q.get("force") or ["0"])[0] in ("1", "true", "yes")
                if not _HANDOFF_OK:
                    return self._json({"ok": False, "reason": "handoff 未加载"}, 500)
                if not k:
                    return self._json({"ok": False, "reason": "缺 key"}, 400)
                return self._json(handoff.build(k, force))
            if u.path == "/digest":
                d = (q.get("date") or [""])[0] or __import__("datetime").datetime.now().strftime("%Y-%m-%d")
                force = (q.get("force") or ["0"])[0] in ("1", "true", "yes")
                if not _DIGEST_OK:
                    return self._json({"ok": False, "reason": "daily_digest 未加载"}, 500)
                return self._json(daily_digest.build(d, force))
            if u.path == "/block":
                key = (q.get("key") or [""])[0]
                p = OUT / _safe(key)
                text = p.read_text("utf-8") if p.exists() else ""
                return self._json({"key": key, "text": text, "chars": len(text), "tokens": est_tokens(text)})
            return self._json({"ok": False}, 404)
        except Exception as e:
            log("HTTP 异常: " + traceback.format_exc(limit=3))
            return self._json({"ok": False, "error": str(e)}, 500)


def loop():
    _digest_day = ""            # 记录今天是否已经补过要目
    _digest_day_housekeeping = ""
    while True:
        try:
            # 每天第一次循环时补昨天的要目（跨零点时自动触发）
            _today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
            if _DIGEST_OK and _today != _digest_day:
                _digest_day = _today
                try:
                    _r = daily_digest.ensure_yesterday()
                    if _r.get("ok") and not _r.get("skipped"):
                        log("要目已生成: " + str(_r.get("date")) + " " + str(_r.get("bytes")) + " 字节")
                except Exception as _e:
                    log("要目生成失败: " + str(_e))
            # 每天一次：把很久没被想起的记忆标成睡着（不删，可唤醒）
            if _today != _digest_day_housekeeping:
                _digest_day_housekeeping = _today
                try:
                    _n = retention.mark_dormant(float(CONF.get("dormantDays") or 60))
                    if _n:
                        log(f"睡着 {_n} 条记忆（{CONF.get('dormantDays') or 60} 天没被想起）")
                except Exception as _e:
                    log("睡着标记失败: " + str(_e))

            r = refresh_blocks()
            if r.get("blocks"):
                summary = {k: {"chars": v["chars"], "tokens": v["tokens"]} for k, v in r["blocks"].items()}
                log(f"出块 {summary}｜索引 {r.get('items')} 条")
            else:
                log(f"本轮无块｜索引 {r.get('items')} 条")
        except Exception:
            log("循环异常: " + traceback.format_exc(limit=3))
        time.sleep(max(10, int(CONF.get("loopSec") or 30)))


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--once":
        print(json.dumps(build_index(force=True), ensure_ascii=False))
        print(json.dumps(refresh_blocks(), ensure_ascii=False, default=str))
        return
    if arg == "--debug":
        q = " ".join(sys.argv[2:]) or "你还记得之前的事吗"
        items = retrieve(q, {"private", "public"}, 5)
        for it in items:
            print(f"{it['score']:.3f} kw={it['kw']:.2f} [{it['kind']}] {it['text'][:80]}")
        print("---")
        print(compose("debug", items[:3]))
        print("tokens≈", est_tokens(compose("debug", items[:3])))
        return
    port = int(CONF.get("port") or 3132)
    # 🔴 Windows 上 SO_REUSEADDR 允许两个进程抢同一个端口（实测真的会各跑一套循环、
    #    往同一个日志里写双份）。所以先问一句 /health：有人应就直接退出，别抢。
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
            if r.status == 200:
                log(f"端口 {port} 上已有实例在跑（/health 有响应）⇒ 本份退出")
                return
    except Exception:
        pass
    try:
        srv = HTTPServer(("127.0.0.1", port), Handler)
        srv.allow_reuse_address = False
    except OSError as e:
        log(f"端口 {port} 被占（已有实例在跑）⇒ 退出：{e}")
        return
    log(f"记忆注入服务启动：port={port} budget={CONF['budget']} keys={CONF['inject'].get('keys')}")
    build_index(force=True)
    threading.Thread(target=loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    main()