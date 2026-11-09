/**
 * host-hooks.js —— 记忆框架挂在宿主（QQ 桥接）上的四处钩子
 *
 * 这个框架不是独立跑的黑盒：一半代码在引擎里（检索/注入/生成），
 * 另一半必须挂在宿主的消息流上（什么时候写、什么时候注入）。
 *
 * 下面是从实际项目里摘出来的四个函数，**去掉了与记忆无关的代码**。
 * 接进你自己的宿主时按这个形状改：
 *
 *   ① archiveQQ(key, msg)          —— 每条消息 → 原文归档
 *   ② coreIdentityBlock()          —— 每轮 → 薄身份注入
 *   ③ askHandoff(key)              —— 换会话前 → 调 /handoff
 *   ④ rotateByTurnCount(sessionId) —— 聊满 N 轮 → 换会话（换之前先 askHandoff）
 *
 * 依赖的宿主变量（按你的框架替换）：
 *   path / fs / log          —— 标准库与日志
 *   STATE_DIR                —— 宿主的状态目录（对应 paths.json 的 state）
 *   cfg.socialV2.*           —— 配置（contextBudgetTokens / contextRotateTurns / memoryBrain）
 *   reverse                  —— sessionId → 会话 key 的反查表
 *   v2TurnStartAt            —— 回合起始时间（判断"这轮是不是还在跑"）
 *   retireSession(key)       —— 退役旧会话，下次唤醒自动建新的
 *
 * 占位约定：<engine> = 记忆引擎目录，<state> = 状态目录
 */

/* ── 原文归档：每条消息落盘 ── */
function archiveQQ(key, m) {
  try {
    const t = new Date(m.at || Date.now());
    const ymd = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
    const text = String(m.plain || m.text || '').replace(/\s+/g, ' ').trim();
    if (!text) return;
    const who = m.isSelf ? '她' : String(m.sender || '某人').slice(0, 16);
    const hhmm = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
    fs.mkdirSync(ARCHIVE_DIR, { recursive: true });
    const sig = `${key}|${hhmm}|${who}|${text.slice(0, 120)}`;
    if (archiveLast.get(sig)) return;      // 一分钟内同一条不重复写
    archiveLast.set(sig, 1);
    if (archiveLast.size > 500) archiveLast.clear();
    fs.appendFileSync(`${ARCHIVE_DIR}\\${ymd}.md`, `- ${hhmm} [${key}] ${who}：${text.slice(0, 300)}\n`, 'utf8');
  } catch (e) {
    // 归档失败绝不影响聊天
  }

/* ── 薄身份：每轮注入 ── */
function coreIdentityBlock() {
    try {
      const p = path.join('<state>', 'her-core.md');
      const st2 = fs.statSync(p);
      if (_coreCache.at !== st2.mtimeMs) {
        let t = fs.readFileSync(p, 'utf8').trim();
        // 去掉注释行和目录提示，只留正文（注入要短）
        t = t.split('\n').filter((l) => !/^\s*>/.test(l) && !/^\s*#/.test(l)).join('\n').trim();
        _coreCache = { text: t.slice(0, 600), at: st2.mtimeMs };
        log(`[core] 注入薄身份 ${_coreCache.text.length} 字`);
      }
      if (!_coreCache.text) return '';
      return `【我是谁】\n${_coreCache.text}\n\n`;
    } catch (e) {
      log('[core] 失败: ' + (e?.message ?? e));
      return '';
    }
  }

/* ── 交接摘要：换会话前生成 ── */
function askHandoff(key) {
    try {
      const mb = cfg.socialV2?.memoryBrain;
      if (!mb || mb.enabled !== true) return;
      const port = Number(mb.port) || 3132;
      void fetch(`http://127.0.0.1:${port}/handoff?key=${encodeURIComponent(key)}`)
        .then((r) => r.text())
        .then((t) => log(`[handoff] ${key} 交接摘要：${String(t).slice(0, 120)}`))
        .catch((e) => log(`[handoff] 生成失败（不影响聊天）：${e?.message ?? e}`));
    } catch {}
  }

/* ── 轮数轮换：聊满 N 轮换会话 ── */
function rotateByTurnCount(sessionId) {
    if (!(CONTEXT_ROTATE_TURNS > 0)) return;
    const key = reverse.get(sessionId);
    if (!key) return;
    const n = (sessionTurnCount.get(sessionId) ?? 0) + 1;
    sessionTurnCount.set(sessionId, n);
    if (n < CONTEXT_ROTATE_TURNS) return;
    const endedAt = Date.now();
    setTimeout(() => {
      try {
        const startedAt = Number(v2TurnStartAt.get(sessionId));
        if (Number.isFinite(startedAt) && startedAt > endedAt) return;   // 又开了一轮，等它
        log(`[ctx] ${key} 已聊 ${n} 轮（上限 ${CONTEXT_ROTATE_TURNS}），轮换会话`);
        askHandoff(key);
        retireSession(key);
        sessionTurnCount.delete(sessionId);
        sessionPromptTokens.delete(sessionId);
        sessionOverBudget.delete(sessionId);
      } catch (error) {
        log('[ctx] 轮数轮换失败：' + (error?.message ?? error));
      }
    }, CTX_ROTATE_DELAY_MS)?.unref?.();
  }
