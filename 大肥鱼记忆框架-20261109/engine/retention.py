# -*- coding: utf-8 -*-
"""记忆保持度：分层 + 艾宾浩斯遗忘曲线 + 情绪显著性 + 联想扩散。

设计原则：
  · 只读 Mnemon 的库（那是她的记忆），**所有状态存在自己的旁路库**
    `state/retention.db`——绝不改 mnemon.db 的结构或数据。
  · 全部 fail-open：这里任何异常都不能影响注入。

三层：
  hot  保持度 ≥ hot         （随时可被注入）
  warm hot > 保持度 ≥ warm  （可注入，排序靠后）
  cold 保持度 < warm        （不参与主动注入，只在显式「按需检索」里出现）

保持度（Ebbinghaus）：
  τ(天) = base * (0.55 + 1.5*重要度) * (1 + 1.1*ln(1+回忆次数)) * (1 + 0.8*情绪)
  S(t)  = s0 * exp(-(now - 上次想起) / τ)
  被想起一次：hits+1、s0 + reinforce（封顶 1.0）——回忆就是复习。
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from pathlib import Path

import paths

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "state" / "retention.db"
_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

# ── 情绪词表：重大情绪波动 = 更容易被记住、忘得更慢 ──────────────────────
STRONG_EMOTION = (
    "哭", "怕", "吓", "生气", "气死", "崩溃", "讨厌", "最喜欢", "最讨厌", "永远",
    "第一次", "答应", "承诺", "对不起", "谢谢", "想你", "舍不得", "难过", "心疼",
    "抱住", "抱抱", "激动", "开心死", "高兴坏", "震惊", "后悔", "丢人", "委屈",
)
MID_EMOTION = (
    "开心", "高兴", "喜欢", "讨厌", "累", "烦", "难过", "想", "后悔", "骄傲",
    "紧张", "不好意思", "害羞", "期待", "担心", "放心", "温柔", "暖", "冷",
)

_EMO_RE_CACHE: dict[str, float] = {}


def salience(text: str) -> float:
    """0~1 的情绪强度。命中强词权重高，语气符号/短句加强。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    if t in _EMO_RE_CACHE:
        return _EMO_RE_CACHE[t]
    strong = sum(1 for w in STRONG_EMOTION if w in t)
    mid = sum(1 for w in MID_EMOTION if w in t)
    punc = 1 if re.search(r"[!！]{2,}|…{2,}|[?？]{2,}", t) else 0
    v = min(1.0, 0.55 * min(strong, 2) + 0.22 * min(mid, 2) + 0.18 * punc)
    if len(_EMO_RE_CACHE) > 4000:
        _EMO_RE_CACHE.clear()
    _EMO_RE_CACHE[t] = v
    return v


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=5, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS ret ("
        " id TEXT PRIMARY KEY, s0 REAL, hits INTEGER DEFAULT 0,"
        " last_hit REAL, salience REAL DEFAULT 0, first_seen REAL)"
    )
    try:
        c.execute("ALTER TABLE ret ADD COLUMN dormant INTEGER DEFAULT 0")
    except Exception:
        pass
    c.execute("CREATE TABLE IF NOT EXISTS recall (id TEXT, at REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_recall_at ON recall(at)")
    c.commit()
    _CONN = c
    return c


def ensure(items: list[dict], now: float | None = None, mood: dict | None = None) -> int:
    """给还没建档的记忆建一条保持度记录。返回新建条数。"""
    now = now or time.time()
    created = 0
    try:
        with _LOCK:
            c = _conn()
            have = {r[0] for r in c.execute("SELECT id FROM ret").fetchall()}
            rows = []
            for it in items:
                mid = it.get("id")
                if not mid or mid in have:
                    continue
                imp = float(it.get("importance") or 0.5)
                sal = salience(it.get("text") or "")
                # 重大情绪波动：那条记忆正撞上她当时的心情 → 记更牢
                if mood and float(mood.get("intensity") or 0) >= 0.35:
                    mv, _ma = mood_axes(str(mood.get("label") or ""))
                    tv = valence(it.get("text") or "")
                    if mv != 0 and tv != 0 and (mv > 0) == (tv > 0):
                        sal = min(1.0, sal + 0.28 * float(mood.get("intensity") or 0.5))
                s0 = min(1.0, 0.34 + 0.46 * imp + 0.30 * sal)
                rows.append((mid, s0, 0, float(it.get("ts") or now) or now, sal, now))
            if rows:
                c.executemany(
                    "INSERT OR IGNORE INTO ret (id,s0,hits,last_hit,salience,first_seen) VALUES (?,?,?,?,?,?)",
                    rows,
                )
                c.commit()
                created = len(rows)
    except Exception:
        return 0
    return created


def snapshot() -> dict[str, dict]:
    """一次性取出所有保持度记录（检索时在内存里算，不逐条查库）。"""
    out: dict[str, dict] = {}
    try:
        with _LOCK:
            c = _conn()
            for mid, s0, hits, last_hit, sal in c.execute(
                "SELECT id,s0,hits,last_hit,salience FROM ret"
            ).fetchall():
                out[mid] = {"s0": float(s0 or 0.5), "hits": int(hits or 0),
                            "last": float(last_hit or 0), "sal": float(sal or 0)}
    except Exception:
        return {}
    return out


def strength(rec: dict | None, importance: float, now: float | None = None, conf: dict | None = None) -> float:
    """艾宾浩斯：当前保持度 0~1。没有记录就用默认值现算（不写库）。"""
    now = now or time.time()
    conf = conf or {}
    base = float(conf.get("baseTauDays") or 12.0)
    if not rec:
        imp = max(0.1, min(1.0, float(importance or 0.5)))
        return min(1.0, 0.34 + 0.46 * imp)
    imp = max(0.1, min(1.0, float(importance or 0.5)))
    hits = int(rec.get("hits") or 0)
    sal = float(rec.get("sal") or 0)
    last = float(rec.get("last") or now)
    tau = base * (0.55 + 1.5 * imp) * (1.0 + 1.1 * math.log1p(hits)) * (1.0 + 0.8 * sal)
    age = max(0.0, now - last) / 86400.0
    return max(0.0, min(1.0, float(rec.get("s0") or 0.5) * math.exp(-age / max(0.5, tau))))


def layer_of(st: float, conf: dict | None = None) -> str:
    conf = conf or {}
    hot = float(conf.get("hot") or 0.60)
    warm = float(conf.get("warm") or 0.28)
    if st >= hot:
        return "hot"
    if st >= warm:
        return "warm"
    return "cold"


def touch(ids: list[str], conf: dict | None = None, query: str = "") -> int:
    """被想起 = 复习一次。带节流：同一条 6 小时内只强化一次。"""
    if not ids:
        return 0
    conf = conf or {}
    now = time.time()
    gap = float(conf.get("reinforceThrottleSec") or 21600)
    gain = float(conf.get("reinforce") or 0.12)
    n = 0
    try:
        with _LOCK:
            c = _conn()
            for mid in ids:
                row = c.execute("SELECT s0,hits,last_hit FROM ret WHERE id=?", (mid,)).fetchone()
                if not row:
                    continue
                s0, hits, last_hit = float(row[0] or 0.5), int(row[1] or 0), float(row[2] or 0)
                if now - last_hit < gap:
                    continue
                c.execute(
                    "UPDATE ret SET s0=?, hits=?, last_hit=? WHERE id=?",
                    (min(1.0, s0 + gain), hits + 1, now, mid),
                )
                c.execute("INSERT INTO recall (id,at) VALUES (?,?)", (mid, now))
                n += 1
            c.commit()
    except Exception:
        return 0
    return n


def recent(minutes: float = 30) -> list[str]:
    """最近被想起过的记忆 id（星图拿来打高亮）。"""
    try:
        with _LOCK:
            c = _conn()
            rows = c.execute(
                "SELECT id, MAX(at) FROM recall WHERE at > ? GROUP BY id ORDER BY 2 DESC LIMIT 40",
                (time.time() - minutes * 60,),
            ).fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []


def mark_dormant(days: float = 60) -> int:
    """超过 N 天没被想起的，标成睡着。

    睡着不是删除：文件还在（Mnemon/笔记/归档都有原文），只是不再进检索，
    省得每次都把几十条陈年旧事翻出来。要用时 wake() 叫醒。
    """
    cutoff = time.time() - days * 86400
    try:
        with _LOCK:
            c = _conn()
            cur = c.execute(
                "UPDATE ret SET dormant=1 WHERE COALESCE(dormant,0)=0 "
                "AND COALESCE(last_hit, first_seen) < ?", (cutoff,))
            c.commit()
            return cur.rowcount or 0
    except Exception:
        return 0


def dormant_list(limit: int = 50) -> list[dict]:
    try:
        with _LOCK:
            c = _conn()
            rows = c.execute(
                "SELECT id, last_hit, hits FROM ret WHERE COALESCE(dormant,0)=1 "
                "ORDER BY COALESCE(last_hit,0) ASC LIMIT ?", (limit,)).fetchall()
            return [{"id": r0, "lastHit": r1, "hits": r2} for r0, r1, r2 in rows]
    except Exception:
        return []


def wake(mid: str) -> bool:
    """把一条睡着的记忆叫醒：清掉标记，并把 last_hit 提到现在。"""
    try:
        with _LOCK:
            c = _conn()
            cur = c.execute("UPDATE ret SET dormant=0, last_hit=? WHERE id=?", (time.time(), mid))
            c.commit()
            return bool(cur.rowcount)
    except Exception:
        return False


def stats(conf: dict | None = None) -> dict:
    conf = conf or {}
    conf = conf.get("retention") if "retention" in conf else conf
    out = {"hot": 0, "warm": 0, "cold": 0, "total": 0, "recalls24h": 0, "dormant": 0}
    try:
        with _LOCK:
            c = _conn()
            rows = c.execute("SELECT s0,hits,last_hit,salience FROM ret").fetchall()
            now = time.time()
            for s0, hits, last_hit, sal in rows:
                st = strength({"s0": s0, "hits": hits, "last": last_hit, "sal": sal}, 0.5, now, conf)
                out[layer_of(st, conf)] = out.get(layer_of(st, conf), 0) + 1
                out["total"] += 1
            out["dormant"] = c.execute(
                "SELECT COUNT(*) FROM ret WHERE COALESCE(dormant,0)=1"
            ).fetchone()[0]
            out["recalls24h"] = c.execute(
                "SELECT COUNT(*) FROM recall WHERE at > ?", (now - 86400,)
            ).fetchone()[0]
    except Exception:
        pass
    return out

# ── 联想（扩散激活）用的邻接表：直接读 Mnemon 的 edges，缓存 3 分钟 ──────────
_EDGES = {"at": 0.0, "adj": {}}


def neighbors(db_path, ttl: float = 180.0) -> dict[str, list[tuple[str, str, float]]]:
    """返回 {记忆id: [(邻居id, 边类型, 权重)]}。id 用 Mnemon 原生 uuid（不带 mn: 前缀）。"""
    now = time.time()
    if _EDGES["adj"] and now - _EDGES["at"] < ttl:
        return _EDGES["adj"]
    adj: dict[str, list[tuple[str, str, float]]] = {}
    try:
        p = Path(db_path)
        if not p.exists():
            return {}
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        for a, b, t, w in c.execute("SELECT source_id, target_id, edge_type, weight FROM edges").fetchall():
            if not a or not b or a == b:
                continue
            typ = str(t or "entity")
            ww = float(w or 0.0) or 0.5
            adj.setdefault(a, []).append((b, typ, ww))
            adj.setdefault(b, []).append((a, typ, ww))
        c.close()
    except Exception:
        return {}
    _EDGES["adj"] = adj
    _EDGES["at"] = now
    return adj

# ── 情绪极性（valence −1..1）：用来做「心情一致」的回忆偏好 ──────────────
_POS = ("开心", "高兴", "喜欢", "兴奋", "期待", "得意", "甜", "暖", "温柔", "骄傲",
        "满足", "放心", "惊喜", "笑", "好玩", "舒服", "安心", "想你")
_NEG = ("难过", "委屈", "哭", "疼", "怕", "吓", "烦", "生气", "气死", "累", "崩溃",
        "讨厌", "后悔", "丢人", "烦死", "火大", "失落", "孤单", "舍不得", "担心", "难过死")


def valence(text: str) -> float:
    """粗估一句话的情绪极性：-1 很负，+1 很正，0 中性。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    p = sum(1 for w in _POS if w in t)
    n = sum(1 for w in _NEG if w in t)
    if p == 0 and n == 0:
        return 0.0
    return max(-1.0, min(1.0, (p - n) / max(1, p + n)))


# 她自己的心情词 → (valence, arousal)。认不出就当中性。
_MOOD_MAP = {
    "开心": (0.8, 0.7), "高兴": (0.8, 0.6), "很爽": (0.7, 0.7), "兴奋": (0.7, 0.9),
    "期待": (0.6, 0.6), "得意": (0.6, 0.5), "温柔": (0.5, 0.2), "平静": (0.0, 0.1),
    "无聊": (-0.2, 0.2), "有点累": (-0.3, 0.1), "累": (-0.3, 0.1), "困": (-0.2, 0.05),
    "烦": (-0.5, 0.6), "烦死": (-0.8, 0.9), "生气": (-0.7, 0.9), "火大": (-0.8, 1.0),
    "难过": (-0.9, 0.5), "委屈": (-0.8, 0.5), "失落": (-0.7, 0.3), "想你": (0.6, 0.4),
    "舍不得": (-0.4, 0.4), "怕": (-0.6, 0.8), "心疼": (-0.5, 0.5), "害羞": (0.4, 0.6),
    "紧张": (-0.3, 0.8),
}


def mood_axes(label: str) -> tuple[float, float]:
    """把她的心情词映射成 (valence, arousal)。"""
    t = (label or "").strip()
    if not t:
        return 0.0, 0.0
    if t in _MOOD_MAP:
        return _MOOD_MAP[t]
    for k, v in _MOOD_MAP.items():
        if k in t or t in k:
            return v
    v = valence(t)
    return v, min(1.0, abs(v))
