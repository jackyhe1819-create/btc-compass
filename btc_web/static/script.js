/**
 * BTC Dashboard Frontend Script
 * 获取 API 数据并动态更新页面
 */

// 自动刷新间隔（毫秒）
const REFRESH_INTERVAL = 5 * 60 * 1000; // 5分钟 (指标数据)
const NEWS_REFRESH_INTERVAL = 10 * 60 * 1000; // 10分钟 (资讯/巨鲸/日历)

// History drawer state
const historyCache = {};        // { "Ahr999:30": {dates, values, thresholds} }
let drawerChartInstance = null; // Chart.js instance
let currentDrawerIndicator = null;

// 通用 SWR 轮询 helper：处理 computing/202 + 指数退避
// 用法: fetchWithComputingPoll('/api/dashboard', { onData, onBanner, onTimeout, pollKey: '_dashboardPollTimer' })
async function fetchWithComputingPoll(url, opts) {
    const {
        onData,                           // (data) => void
        onBanner = null,                  // () => void，首次进入 computing 时调用
        onHideBanner = null,              // () => void，结束轮询时调用
        onTimeout = null,                 // () => void
        onError = null,                   // (err) => void
        pollKey = '_pollTimer',           // window 上的句柄 key，防并发
        maxWaitMs = 10 * 60 * 1000,       // 总等待上限（10 分钟）
        delays = [8000, 16000, 30000, 30000, 30000],  // 指数退避序列
    } = opts || {};

    try {
        const response = await fetch(url);
        const data = await response.json();
        if (data.success) {
            onData(data);
            return;
        }

        if (data.computing) {
            if (window[pollKey]) return;   // 已有轮询在跑
            if (onBanner) onBanner();
            const start = Date.now();
            let attempt = 0;
            const schedule = () => {
                const delay = delays[Math.min(attempt, delays.length - 1)];
                window[pollKey] = setTimeout(async () => {
                    attempt++;
                    if (Date.now() - start > maxWaitMs) {
                        window[pollKey] = null;
                        if (onHideBanner) onHideBanner();
                        if (onTimeout) onTimeout();
                        return;
                    }
                    try {
                        const r = await fetch(url);
                        const d = await r.json();
                        if (d.success) {
                            window[pollKey] = null;
                            if (onHideBanner) onHideBanner();
                            onData(d);
                            return;
                        }
                    } catch (e) { /* 静默 */ }
                    schedule();
                }, delay);
            };
            schedule();
            return;
        }

        if (onError) onError(data.error || 'API 返回失败');
    } catch (e) {
        if (onError) onError(e);
    }
}

// ── 主题感知调色板：Chart.js 画布无法用 CSS 变量，构建时经 PAL 解析当前主题色 ──
function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
}
const PAL = {
    get up()     { return cssVar('--accent-green',  'var(--accent-green)'); },
    get down()   { return cssVar('--accent-red',    'var(--accent-red)'); },
    get mid()    { return cssVar('--accent-yellow', 'var(--accent-yellow)'); },
    get btc()    { return cssVar('--accent-btc',    '#f7931a'); },
    get blue()   { return cssVar('--accent-blue',   '#4488ff'); },
    get purple() { return cssVar('--accent-purple', '#aa66ff'); },
    get muted()  { return cssVar('--text-muted',    '#888888'); },
    grid: 'rgba(128,128,128,0.15)'
};

// Threshold reference lines for key indicators (pal = PAL 调色板键)
const INDICATOR_THRESHOLDS = {
    "Ahr999": [
        { value: 0.45, label: "定投线", pal: "up" },
        { value: 1.2,  label: "顶部区", pal: "down" }
    ],
    "Mayer Multiple": [
        { value: 1.0, label: "均值",     pal: "mid" },
        { value: 2.4, label: "历史高位", pal: "down" }
    ],
    "恐惧贪婪指数": [
        { value: 20, label: "极度恐惧", pal: "up" },
        { value: 80, label: "极度贪婪", pal: "down" }
    ]
};

/**
 * Render shimmer skeleton cards into indicator containers immediately on page load.
 */
function renderSkeletons() {
    const counts = {
        longTermIndicators:  8,
        shortTermIndicators: 7,
        auxIndicators:       7
    };
    for (const [id, count] of Object.entries(counts)) {
        const container = document.getElementById(id);
        if (!container) continue;
        container.innerHTML = Array.from({ length: count }, () => `
            <div class="indicator-skeleton">
                <div class="skel skel-name"></div>
                <div class="skel skel-value"></div>
                <div class="skel skel-status"></div>
                <div class="skel skel-chart"></div>
            </div>
        `).join('');
    }
}

// ── 主题切换 ────────────────────────────────────────────────────
function applyTheme(theme) {
    const html = document.getElementById('htmlRoot');
    if (theme === 'warm') {
        html.setAttribute('data-theme', 'warm');
        document.getElementById('themeBtn').textContent = '🌙';
        document.getElementById('themeBtn').title = '切换为暗色主题';
    } else {
        html.removeAttribute('data-theme');
        document.getElementById('themeBtn').textContent = '☀️';
        document.getElementById('themeBtn').title = '切换为米白主题';
    }
    localStorage.setItem('btc-theme', theme);

    // 同步 TradingView K 线图主题
    // tv.js CDN 加载失败时 TradingView 全局缺席, initTradingViewWidget 内部 new TradingView.widget
    // 会抛 ReferenceError; 若不守卫会从 DOMContentLoaded 首句冒出, 掐死整个看板启动。
    // 第三方图表单点故障只应跳过图表, 核心面板照常。
    if (typeof TradingView !== 'undefined' && typeof initTradingViewWidget === 'function') {
        try {
            initTradingViewWidget(theme === 'warm' ? 'light' : 'dark');
        } catch (e) {
            console.warn('⚠️ TradingView 图表初始化失败, 已跳过:', e);
        }
    }

    // Chart.js 画布颜色在构建时解析，切换主题后重建评分历史与衍生品图表
    if (window._compassBooted) {
        if (typeof fetchScoreHistory === 'function') setTimeout(() => fetchScoreHistory(_scoreHistoryDays), 60);
        if (typeof fetchDerivativesData === 'function') setTimeout(fetchDerivativesData, 60);
    }
}

// 页面加载时获取数据
document.addEventListener('DOMContentLoaded', () => {
    // 恢复主题按钮图标
    const savedTheme = localStorage.getItem('btc-theme') || 'warm';
    applyTheme(savedTheme);

    // 主题切换点击
    document.getElementById('themeBtn')?.addEventListener('click', () => {
        const current = localStorage.getItem('btc-theme') || 'warm';
        applyTheme(current === 'warm' ? 'dark' : 'warm');
    });

    // 资讯手动刷新
    document.getElementById('newsRefreshBtn')?.addEventListener('click', () => {
        fetchNewsData();
    });

    renderSkeletons();
    fetchDashboardData();
    setInterval(fetchDashboardData, REFRESH_INTERVAL);
    setTimeout(fetchBuildersData, 5000); // 延迟 5s，等待后台缓存预热
    setInterval(fetchBuildersData, 30 * 60 * 1000); // 每 30 分钟刷新

    // 评分历史 + 衍生品面板
    setTimeout(() => fetchScoreHistory(_scoreHistoryDays), 1500);
    setInterval(() => fetchScoreHistory(_scoreHistoryDays), REFRESH_INTERVAL);
    setTimeout(fetchDerivativesData, 2500);
    setInterval(fetchDerivativesData, 10 * 60 * 1000); // 每 10 分钟刷新
    setTimeout(renderOptions, 2700);
    setInterval(renderOptions, 10 * 60 * 1000); // 期权面板缓存 TTL 10 分钟，同步刷新
    setTimeout(renderProbDist, 3000);
    setInterval(renderProbDist, 10 * 60 * 1000); // 概率分布面板缓存 TTL 10 分钟，同步刷新
    // 规律三卡只在启动时拉一次：数据变更源是"改 JSON+重新部署"(部署伴随刷新),
    // 定时重拉既是免费层噪声唤醒, 又会把用户正展开阅读的 <details> 当面塌缩回折叠态
    setTimeout(fetchCycleEvents, 3000);
    setTimeout(fetchRoadmap, 3300);
    setTimeout(fetchMarketPatterns, 3500);
    window._compassBooted = true;

    // 评分历史天数切换
    document.querySelectorAll('#scoreHistoryTabs .dtab').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#scoreHistoryTabs .dtab').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _scoreHistoryDays = parseInt(btn.dataset.shdays, 10);
            fetchScoreHistory(_scoreHistoryDays);
        });
    });
});

// 刷新按钮点击事件（同时刷新指标和资讯）
document.getElementById('refreshBtn')?.addEventListener('click', () => {
    fetchDashboardData();
    fetchNewsData();
    fetchBuildersData();
    fetchScoreHistory(_scoreHistoryDays);
    fetchDerivativesData();
});

async function fetchBuildersData() {
    const refreshBtn = document.getElementById('buildersRefreshBtn');
    if (refreshBtn) refreshBtn.classList.add('spinning');

    const renderBuilders = (data) => {
        const grid = document.getElementById('buildersGrid');
        if (!grid) return;

        const updatedEl = document.getElementById('buildersUpdatedAt');
        if (updatedEl && data.updated_at) updatedEl.textContent = `更新于 ${data.updated_at}`;

        // 渲染 AI 摘要面板
        renderBuildersSummary(data.summary);

        if (refreshBtn) refreshBtn.classList.remove('spinning');

        if (!data.sources || data.sources.length === 0) {
            grid.innerHTML = '<p style="color:var(--text-muted);">暂无数据</p>';
            return;
        }

        grid.innerHTML = data.sources.map(src => {
            const items = (src.items || []).slice(0, 8);
            const badge = src.priority === 'critical'
                ? '<span class="builders-badge critical">核心</span>'
                : '<span class="builders-badge high">重要</span>';
            const itemsHtml = items.length > 0
                ? items.map(item => `
                    <a href="${escapeHtml(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer" class="builders-item">
                        <div class="builders-item-title">${escapeHtml(item.title)}</div>
                        ${item.summary ? `<div class="builders-item-summary">${escapeHtml(item.summary)}</div>` : ''}
                        ${item.date ? `<div class="builders-item-date">${escapeHtml(item.date)}</div>` : ''}
                    </a>`).join('')
                : `<p style="color:var(--text-muted);font-size:0.82rem;padding:8px 0;">${src.error ? '加载失败' : '暂无内容'}</p>`;

            return `
                <div class="builders-group">
                    <div class="builders-group-title">
                        ${src.icon} ${src.name} ${badge}
                    </div>
                    <div class="builders-items">${itemsHtml}</div>
                </div>`;
        }).join('');
    };

    await fetchWithComputingPoll('/api/builders', {
        pollKey: '_buildersPollTimer',
        maxWaitMs: 5 * 60 * 1000,
        delays: [10000, 20000, 30000, 60000],
        onData: renderBuilders,
        onError: (e) => {
            console.error('Builders feed error:', e);
            if (refreshBtn) refreshBtn.classList.remove('spinning');
        },
    });

    if (!window._buildersPollTimer && refreshBtn) {
        refreshBtn.classList.remove('spinning');
    }
}

/**
 * 渲染开发者动态 AI 摘要面板（离线模板聚合）
 */
function renderBuildersSummary(summary) {
    const body = document.getElementById('buildersSummaryBody');
    const meta = document.getElementById('buildersSummaryMeta');
    if (!body) return;

    if (!summary || summary.total_items === 0) {
        body.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem;">暂无摘要数据</p>';
        if (meta) meta.textContent = '';
        return;
    }

    if (meta) {
        meta.textContent = `· ${summary.total_items} 条 / ${summary.total_sources} 源 · 更新于 ${summary.generated_at}`;
    }

    // 把简易 markdown (**bold**, `code`) 渲染为安全 HTML
    const mdToHtml = (txt) => (txt || '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');

    // 顶部：中文叙述段落
    const narrativeHtml = summary.narrative
        ? `<div class="bs-narrative">${mdToHtml(summary.narrative)}</div>`
        : '';

    // 跨源热议（社区共识级别信号）
    const crossHtml = (summary.cross_source_topics || []).length > 0
        ? `<div class="bs-section">
              <div class="bs-section-title">🔥 跨源热议（全社区聚焦）</div>
              <div class="bs-cross-list">
                ${summary.cross_source_topics.map(t => `
                  <div class="bs-cross-item" title="${escapeHtml((t.sources || []).join(', '))}">
                    <span class="bs-cross-icon">${t.icon}</span>
                    <span class="bs-cross-topic">${escapeHtml(t.topic)}</span>
                    <span class="bs-cross-badge">${t.source_count} 源 · ${t.item_count} 条</span>
                  </div>
                `).join('')}
              </div>
           </div>`
        : '';

    // 热门主题（topic 内附 takeaway + top items）
    const highlightsHtml = (summary.highlights || []).length > 0
        ? `<div class="bs-section">
              <div class="bs-section-title">📌 热门主题 Top 5</div>
              <div class="bs-highlights">
                ${summary.highlights.map(h => `
                  <div class="bs-topic-card">
                    <div class="bs-topic-head">
                      <span class="bs-topic-icon">${h.icon}</span>
                      <span class="bs-topic-name">${escapeHtml(h.topic)}</span>
                      <span class="bs-topic-count">${h.count} 条</span>
                    </div>
                    ${h.takeaway ? `<div class="bs-topic-takeaway">${escapeHtml(h.takeaway)}</div>` : ''}
                    <ul class="bs-topic-items">
                      ${h.items.map(it => `
                        <li>
                          <a href="${escapeHtml(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer">
                            <span class="bs-item-src">${it.source_icon}</span>
                            <span class="bs-item-title">${escapeHtml(it.title)}</span>
                            ${it.date ? `<span class="bs-item-date">${escapeHtml(it.date)}</span>` : ''}
                          </a>
                        </li>`).join('')}
                    </ul>
                  </div>
                `).join('')}
              </div>
           </div>`
        : '';

    // 高频关键词标签云
    const kwHtml = (summary.trending_keywords || []).length > 0
        ? `<div class="bs-section">
              <div class="bs-section-title">🔤 高频关键词</div>
              <div class="bs-keywords">
                ${summary.trending_keywords.map(k => {
                  const weight = Math.min(1.2, 0.7 + k.count * 0.05);
                  return `<span class="bs-kw" style="font-size:${weight}rem;" title="出现 ${k.count} 次">${escapeHtml(k.word)}<sup>${k.count}</sup></span>`;
                }).join('')}
              </div>
           </div>`
        : '';

    const methodNote = summary.method
        ? `<div class="bs-method">⚙️ ${summary.method}</div>`
        : '';

    body.innerHTML = narrativeHtml + crossHtml + highlightsHtml + kwHtml + methodNote;
}

// 初始化：手动刷新按钮
document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('buildersRefreshBtn');
    if (btn) {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            // 强制后端触发刷新：用 cache-bust 参数命中冷路径不可行（GET 缓存仍命中）；
            // 直接重新调用 /api/builders 即可，过期时后端会自动 SWR 刷新
            fetchBuildersData();
        });
    }
});

/**
 * 获取仪表盘数据（支持冷启动轮询 + 指数退避 8s → 16s → 30s）
 */
async function fetchDashboardData(isRetry) {
    const refreshBtn = document.getElementById('refreshBtn');
    if (!isRetry && refreshBtn) refreshBtn.classList.add('spinning');

    await fetchWithComputingPoll('/api/dashboard', {
        pollKey: '_dashboardPollTimer',
        onData: (data) => {
            renderDashboard(data);
            if (refreshBtn) refreshBtn.classList.remove('spinning');
        },
        onBanner: () => _showComputingBanner(),
        onHideBanner: () => _hideComputingBanner(),
        onTimeout: () => {
            // 先停 spinner 再显示错误，防错误路径任何异常导致 spinner 永转
            if (refreshBtn) refreshBtn.classList.remove('spinning');
            showError('指标加载超时，请手动刷新页面');
        },
        onError: (err) => {
            console.error('Error fetching dashboard data:', err);
            if (refreshBtn) refreshBtn.classList.remove('spinning');
            showError(typeof err === 'string' ? err : '无法连接到服务器');
        },
    });

    // 非 computing 情况下立即停止 spinner（renderDashboard 成功也会停）
    if (!window._dashboardPollTimer && refreshBtn) {
        refreshBtn.classList.remove('spinning');
    }
}

function _showComputingBanner() {
    let banner = document.getElementById('computingBanner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'computingBanner';
        banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:rgba(247,147,26,0.92);color:#fff;text-align:center;padding:10px;font-size:0.9rem;font-weight:500;';
        banner.innerHTML = '⏳ 指标首次加载中（冷启动约需 2–4 分钟），请稍候，页面将自动更新…';
        document.body.prepend(banner);
    }
}

function _hideComputingBanner() {
    const banner = document.getElementById('computingBanner');
    if (banner) banner.remove();
}

/**
 * 渲染仪表盘
 */
function renderDashboard(data) {
    // 更新时间戳
    document.getElementById('timestamp').textContent = `更新时间: ${data.timestamp}`;

    // 更新价格 (safely check if element exists)
    const btcPriceEl = document.getElementById('btcPrice');
    if (btcPriceEl) {
        btcPriceEl.innerHTML = `<span class="currency">$</span>${formatNumber(data.btc_price)}`;
    }

    // 更新顶部摘要栏
    updateTopSummaryBar(data);

    // 更新周期分仪表（total_score 即周期分）
    updateGauge(data.total_score);
    document.getElementById('scoreValue').textContent = data.total_score.toFixed(2);
    const recommendationEl = document.getElementById('recommendation');
    recommendationEl.textContent = data.recommendation;
    recommendationEl.className = 'recommendation ' + getScoreColor(data.total_score);
    renderBucketBars('cycleBuckets', data.cycle_buckets);

    // 更新战术分仪表
    if (typeof data.tactical_score === 'number') {
        updateGauge(data.tactical_score, 'gaugeNeedleTactical');
        const tv = document.getElementById('tacticalScoreValue');
        if (tv) tv.textContent = data.tactical_score.toFixed(2);
        const tr = document.getElementById('tacticalRecommendation');
        if (tr) {
            tr.textContent = data.tactical_recommendation || '';
            tr.className = 'recommendation ' + getScoreColor(data.tactical_score);
        }
        renderBucketBars('tacticalBuckets', data.tactical_buckets);
    }

    // 渲染指标
    renderIndicators(data.indicators, data.sparklines);

    // 渲染指标总览表格
    renderSummaryTable(data.indicators);

    // 更新 DAT 动态卡片中的 mNAV
    renderDatMNAV(data.indicators['MSTR mNAV']);

    // 渲染今日量化决策面板
    renderDecisionPanel(data.decision);
    renderTriggerLevels(data.trigger_levels);
    renderCyclePhase(data.cycle_phase);
    renderHorizonGuide(data);
}

/* 按持有期使用指南 — 各读数的"有效射程"与当前指引 (纯展示, 数据取自决策/相位 payload) */
function renderHorizonGuide(data) {
    const el = document.getElementById('horizonGuideCard');
    const section = document.getElementById('horizonGuideSection');
    if (!el || !section) return;
    const d = data.decision, cp = data.cycle_phase;
    if (!d || !d.cycle) { section.style.display = 'none'; return; }
    section.style.display = '';

    const c = d.cycle, t = d.tactical || {};
    const stat = (stats, w) => {
        const s = stats && stats[w];
        return s ? `中位 ${s.median >= 0 ? '+' : ''}${s.median}% · 胜率 ${s.win}%` : '—';
    };
    const pstat = (w) => {
        const s = cp && cp.stats && cp.stats.fwd && cp.stats.fwd[w];
        return s ? `中位 ${s.median_pct >= 0 ? '+' : ''}${s.median_pct}% · ${s.pos_pct}% 为正` : '—';
    };
    const rows = [
        {
            h: '🕐 短线', span: '日内 ~ 2 周', watch: '战术分 (仅执行节奏)',
            now: `「${t.band || 'n/a'} · ${t.pace || 'n/a'}」`,
            stat: `该档 14d: ${stat(t.stats, '14d')}`,
            note: '⚠️ 本系统在此射程无方向性信息 — 只作执行节奏与杠杆闸门, 勿当短线信号',
            logic: '构成: 杠杆温度 35% (资金费率7d 0.4 / 期货基差 0.4 / 多空比 0.2 — 衍生品拥挤度, 逆向计分) + 动量结构 30% (MACD/RSI14/SOPR/布林带) + 市场情绪 20% (恐惧贪婪, 仅极值计分) + 链上资金流 15% (交易所净流7d)。' +
                   '为何"非方向信号": 快因子日频噪声主导, 7天方向 IC≈0.05; 12年回测中用战术分门控仓位对净值无显著贡献 — 因此设计上它只决定"分几批、隔多久、可否用杠杆", 不决定买卖方向。有效的部分是档位极值的条件统计 (入场窗口=杠杆出清+动量配合; 杠杆拥挤=防追高防爆仓, 非现货卖出信号)。',
        },
        {
            h: '📅 波段', span: '1 ~ 3 个月', watch: '周期档位(敞口上限) + 战术档(入场点)',
            now: `档位「${c.band}」 目标 ${c.target_lo}-${c.target_hi}%`,
            stat: `该档 90d: ${stat(c.stats, '90d')}`,
            note: '周期档定敞口, 战术入场窗口定分批时点, 相位作语境',
            logic: '周期分映射到七档仓位区间 (防守0-5% ~ 重仓80-100%), 换档走滞回规则: 分数须越过当前档边界 ±0.05 且新档连续 5 个快照确认 — 12年回测把换档从每年63次压到约7次, 决策才可执行。' +
                   '波段射程看 90d 列的原因: 周期分 90 天 IC≈0.29, 有判别力但弱于长窗 — 所以周期档只定"敞口上限"而非精确择时, 入场时点交给战术档, 语境交给相位。条件统计来自 12 年分档回测 (band_stats, 样本内)。',
        },
        {
            h: '📆 长线', span: '3 ~ 18 个月', watch: '周期分 + 滞回换档推送',
            now: `档位「${c.band}」 ${c.action || ''}`,
            stat: `该档 365d: ${stat(c.stats, '365d')}`,
            note: '周期分在此射程判别力最强 (IC 随窗口单调走强), 换档推送即操作信号',
            logic: '构成 (六桶加权): 趋势伸展 25% (200周热图/幂律走廊/PiCycle/Ahr999) + 链上筹码 25% (MVRV-Z/STH成本线/NUPL) + 资金流 20% (ETF净流/稳定币增速) + 趋势确认 15% (价格vs20周EMA+200日均线斜率) + 矿工经济 10% + 减半钟 5%。' +
                   '关键机制①分桶去相关: 同信息源的因子桶内平均, 不重复计票; ②滚动4年分位归一: 打"当前值在过去4年的百分位"而非绝对阈值 — 自适应周期振幅衰减 (绝对阈值会因每轮顶部越来越矮而永远打不出 -1)。慢因子(估值/筹码)的信息频率天然在季度-年度尺度, 故 IC 随窗口单调走强 (90d 0.23 → 365d 0.51, 样本内)。',
        },
        {
            h: '🗓️ 囤币', span: '> 18 个月', watch: '周期相位 + 温度计',
            now: cp ? `相位「${cp.name}」 温度计 ${cp.criteria && cp.criteria.thermometer != null ? cp.criteria.thermometer : 'n/a'}` : '相位不可用',
            stat: `该相位 365d: ${pstat('365d')}`,
            note: '历史最佳积累窗口 = 熊末/牛初相位 — 相位切换会推送提醒',
            logic: '温度计 = 只取反转侧因子重新合成 (趋势伸展 45% + 链上筹码 45% + 减半钟 10%), 排除趋势/资金流等顺周期因子 — 因为在顶部它们会把估值警报中和掉 (2025-10 顶点: 周期分 +0.01 而温度计 -0.55)。' +
                   '为何温度计不直接当仓位分: 反转信号长期性早到 (2024-03 即报 -0.52, 距最终顶还有19个月/+70%), 直接驱动仓位 12年回测 Sharpe 0.73 vs 现行周期分 1.00 — 故只作位置判读。' +
                   '相位 = 机械规则组合, 如 熊末 = 距前高回撤≥50% 且 温度计≥0.35 且 距顶>300天; 泡沫 = 距前高<12% 且 温度计≤-0.5。置信度 = 历史同相位的前瞻收益频率 (n=3~4个周期, 样本内, 非校准概率)。',
        },
    ];
    el.innerHTML = `
        <div class="decision-card-title">⏱️ 按持有期使用指南
            <span class="decision-freq">各读数的有效射程 · 条件统计为样本内历史频率</span></div>
        <div style="overflow-x:auto;">
        <table style="width:100%; min-width:640px; border-collapse:collapse; font-size:0.78rem; margin-top:6px;">
            <thead><tr style="color:var(--text-muted); text-align:left;">
                <th style="padding:4px 8px 4px 2px;">持有期</th><th style="padding:4px 8px;">该看什么</th>
                <th style="padding:4px 8px;">当前指引</th><th style="padding:4px 8px;">历史条件统计</th></tr></thead>
            <tbody>${rows.map(r => `
                <tr style="border-top:1px solid var(--border-color);">
                    <td style="padding:6px 8px 6px 2px; white-space:nowrap;"><b>${r.h}</b><br>
                        <span style="color:var(--text-muted); font-size:0.72rem;">${r.span}</span></td>
                    <td style="padding:6px 8px;">${r.watch}</td>
                    <td style="padding:6px 8px;">${r.now}</td>
                    <td style="padding:6px 8px; white-space:nowrap;">${r.stat}</td>
                </tr>
                <tr><td colspan="4" style="padding:0 8px 6px 2px; color:var(--text-muted); font-size:0.72rem;">${r.note}
                    <details style="margin-top:2px;"><summary style="cursor:pointer; user-select:none;">📖 底层逻辑</summary>
                        <div style="padding:6px 8px; margin-top:4px; background:var(--card-bg); border:1px solid var(--border-color); border-radius:8px; line-height:1.6;">${r.logic}</div>
                    </details></td></tr>`).join('')}
            </tbody>
        </table></div>
        <div class="decision-footnote" style="margin-top:4px;">
            条件统计 = 历史上处于同档位/相位时的前瞻收益分布 (12 年回测, 样本内, 非收益承诺);
            短线射程的诚实结论: 战术分 7 天方向 IC≈0.05, 接近无信息。</div>`;
}

/* 周期相位判读卡 — 规则式六相位 + 历史频率置信度 (叙事层, 不驱动仓位) */
function renderCyclePhase(cp) {
    const el = document.getElementById('cyclePhaseCard');
    const section = document.getElementById('cyclePhaseSection');
    if (!el || !section) return;
    if (!cp || !cp.phase || cp.phase === 'unknown') { section.style.display = 'none'; return; }
    section.style.display = '';

    const cr = cp.criteria || {};
    const fmt = (v, suf = '') => (v === null || v === undefined) ? 'n/a' : `${v}${suf}`;
    const stats = cp.stats || {};
    const fwd = stats.fwd || {};
    // 着色按"信念度"而非中位数符号: 掷硬币级统计 (如 +4.9%/51%) 不得以肯定色
    // 呈现诱导抄底/杀跌 (2026-07 对抗审查)
    const statChips = ['90d', '180d', '365d'].filter(w => fwd[w]).map(w => {
        const s = fwd[w];
        const cls = (s.median_pct > 0 && s.pos_pct >= 60) ? 'pos'
                  : (s.median_pct < 0 && s.pos_pct <= 40) ? 'neg' : '';
        return `<span class="decision-stat-chip ${cls}" title="日级样本 ${s.n} (窗口重叠, 独立性远低于表面 n)">
            ${w} 中位 ${s.median_pct >= 0 ? '+' : ''}${s.median_pct}% · ${s.pos_pct}% 为正</span>`;
    }).join('') || '<span class="decision-stat-empty">相位统计不可用 (phase_stats.json 未随部署?)</span>';
    const epLine = stats.episodes
        ? `<div class="decision-pending muted">历史同相位 ${stats.episodes} 段 / ${stats.days} 天 · 前瞻收益为日级重叠样本, 统计力弱于表面样本数</div>` : '';

    let resLine = '';
    if (stats.resolution) {
        const r = stats.resolution;
        resLine = `<div class="decision-pending muted">该模糊态历史事后分辨: 回调 ${r.confirmed_pullback} 段 / 熊初 ${r.confirmed_bear} 段` +
                  (r.pending ? ` / 确认中 ${r.pending} 段 (观察未满一年)` : '') + `</div>`;
    }
    let leadLine = '';
    if (stats.top_lead_days) {
        const l = stats.top_lead_days;
        leadLine = `<div class="decision-pending muted">泡沫信号距其后一年内最高点 ${l.min}~${l.max} 天 (中位 ${l.median}, n=${l.n} 段) — 过热≠立刻见顶</div>`;
    }

    el.innerHTML = `
        <div class="decision-card-title">🧬 周期相位判读
            <span class="decision-freq">规则式 · 历史频率置信度 · 叙事参考</span></div>
        <div style="font-size:1.25rem; font-weight:700; margin:6px 0 2px;">
            ${cp.emoji || ''} ${cp.name} <span style="font-size:0.75rem; color:var(--text-muted); font-weight:normal;">自 ${cp.since || 'n/a'}</span></div>
        <div style="font-size:0.82rem; color:var(--text-muted); margin-bottom:8px;">${cp.desc || ''}</div>
        <div style="font-size:0.78rem; margin-bottom:6px;">
            判定依据: 距前高 <b>${fmt(cr.drawdown_pct, '%')}</b> · 温度计 <b>${fmt(cr.thermometer)}</b>
            · 趋势过滤器 <b>${fmt(cr.trend)}</b> · 距ATH <b>${fmt(cr.ath_age_days, ' 天')}</b></div>
        <div>${statChips}</div>
        ${epLine}${resLine}${leadLine}
        <div class="decision-footnote" style="margin-top:6px;">${cp.note || ''}</div>`;
}

// 周期相位与事件规律卡：启动时拉一次（数据慢变，与快照解耦）
async function fetchCycleEvents() {
    try {
        const res = await fetch('/api/cycle-events');
        if (!res.ok) return;
        const data = await res.json();
        if (data.success) renderCycleEvents(data);
    } catch (e) { /* 附属卡，静默失败 */ }
}

// BTC 里程碑路线图：慢变，启动时拉一次
async function fetchRoadmap() {
    try {
        const res = await fetch('/api/roadmap');
        if (!res.ok) return;
        const data = await res.json();
        if (data.success) renderRoadmap(data);
    } catch (e) { /* 附属卡，静默失败 */ }
}

/**
 * BTC 里程碑路线图（事件研究 C 层）。分时代时间轴，减半为骨干，
 * 历史实心 / 预定虚线 / 提案标注，插入"你在这里"当前减半位置标记。
 */
function renderRoadmap(d) {
    const el = document.getElementById('roadmapCard');
    if (!el || !d.eras) return;
    el.style.display = '';
    const mut = 'var(--text-muted)';
    const catIcon = { '减半': '🟠', '协议': '⚙️', '市场': '📈', '监管': '⚖️', '机构': '🏛️', '黑天鹅': '🦢', '基础设施': '🔧' };
    const certStyle = {
        '历史': { dot: 'var(--accent-btc)', op: '1', badge: '' },
        '预定': { dot: 'var(--accent-orange)', op: '0.9', badge: '<span style="font-size:0.66rem;color:var(--accent-orange);border:1px solid var(--accent-orange);border-radius:3px;padding:0 3px;margin-left:4px;">预定</span>' },
        '提案': { dot: mut, op: '0.75', badge: '<span style="font-size:0.66rem;color:' + mut + ';border:1px dashed ' + mut + ';border-radius:3px;padding:0 3px;margin-left:4px;">提案·未定</span>' },
        '估计': { dot: mut, op: '0.7', badge: '<span style="font-size:0.66rem;color:' + mut + ';border:1px dashed ' + mut + ';border-radius:3px;padding:0 3px;margin-left:4px;">估计</span>' },
    };
    const cur = d.current || {};

    const milestoneRow = m => {
        const cs = certStyle[m.certainty] || certStyle['历史'];
        const isFuture = m.certainty === '提案' || m.certainty === '预定' || m.certainty === '估计';
        return `
        <div style="display:flex; gap:8px; opacity:${cs.op}; padding:3px 0;">
            <div style="flex:0 0 62px; font-size:0.7rem; color:${mut}; text-align:right; padding-top:1px; font-variant-numeric:tabular-nums;">${m.date}</div>
            <div style="flex:0 0 10px; display:flex; flex-direction:column; align-items:center; padding-top:4px;">
                <div style="width:7px; height:7px; border-radius:50%; background:${cs.dot}; ${isFuture ? 'border:1.5px solid ' + cs.dot + '; background:transparent;' : ''}"></div>
            </div>
            <div style="flex:1;">
                <div style="font-size:0.78rem; color:var(--text-secondary); font-weight:600;">${catIcon[m.category] || ''} ${m.name}${cs.badge}${m.price_context ? ` <span style="font-weight:400; color:${mut}; font-size:0.68rem;">${m.price_context}</span>` : ''}</div>
                <div style="font-size:0.68rem; color:${mut}; line-height:1.45;">${m.significance}</div>
            </div>
        </div>`;
    };

    let html = '';
    if (cur.note) {
        html += `<div style="font-size:0.74rem; color:var(--text-secondary); margin:4px 0 8px; background:#f0b90b12; padding:5px 8px; border-radius:4px;">📍 ${cur.note}</div>`;
    }

    let insertedYouAreHere = false;
    d.eras.forEach(era => {
        const isFutureEra = era.milestones.every(m => m.certainty !== '历史');
        if (isFutureEra && !insertedYouAreHere) {
            html += `<div style="display:flex; align-items:center; gap:6px; margin:8px 0 4px;">
                <div style="flex:0 0 62px;"></div>
                <div style="flex:1; border-top:2px dashed var(--accent-green); position:relative; height:0;">
                    <span style="position:absolute; top:-9px; left:4px; background:var(--panel-2,var(--panel,#1a1a1a)); color:var(--accent-green); font-size:0.7rem; font-weight:600; padding:0 6px;">▶ 你在这里 · 减半后 ${cur.months_since_halving} 月</span>
                </div></div>`;
            insertedYouAreHere = true;
        }
        html += `<div style="font-size:0.76rem; font-weight:600; color:${isFutureEra ? 'var(--accent-orange)' : 'var(--text-secondary)'}; margin:8px 0 2px; border-left:3px solid ${isFutureEra ? 'var(--accent-orange)' : 'var(--accent-btc)'}; padding-left:6px;">${era.era} <span style="font-weight:400; color:${mut}; font-size:0.68rem;">${era.span}</span></div>`;
        html += era.milestones.map(milestoneRow).join('');
    });
    html += `<div style="font-size:0.7rem; color:${mut}; margin-top:8px; border-top:1px solid var(--border-color,#333); padding-top:6px;">${d.honest_note || ''}${d.generated ? ` · 核实截至 ${d.generated}` : ''}</div>`;
    // 默认折叠（页面已很长）
    el.innerHTML = `<details class="pattern-collapse"><summary class="pattern-summary"><span>🗺️ BTC 里程碑路线图 <span class="decision-freq" style="font-weight:400;">历史→未来时间轴 · 展开</span></span><span class="pattern-chev">▾</span></summary>${html}</details>`;
}

// 市场规律与风险版块：慢变，启动时拉一次
async function fetchMarketPatterns() {
    try {
        const res = await fetch('/api/market-patterns');
        if (!res.ok) return;
        const data = await res.json();
        if (data.success) renderMarketPatterns(data);
    } catch (e) { /* 附属卡，静默失败 */ }
}

/**
 * 市场规律与风险（事件研究 C 层）：利率×周期证伪 + 季节性证伪 + 黑天鹅画像。
 * 全部经对抗核实。证伪类明确"民间规律不显著"，黑天鹅"n=1 仅风险画像"，非交易信号。
 */
function renderMarketPatterns(d) {
    const el = document.getElementById('marketPatternsCard');
    if (!el) return;
    el.style.display = '';
    const pos = 'var(--accent-green)', neg = 'var(--accent-red)', mut = 'var(--text-muted)';
    const sign = v => `<span style="color:${v >= 0 ? pos : neg};">${v >= 0 ? '+' : ''}${v}%</span>`;
    let html = '';

    // ── 利率 × 周期证伪 ──
    if (d.rates) {
        const r = d.rates;
        const pairs = r.natural_experiment.pairs.map(p =>
            `<tr><td style="padding:1px 8px 1px 0;color:var(--text-secondary);">${p.date} ${p.bp}bp</td>
             <td style="padding:1px 8px 1px 0;color:${mut};font-size:0.72rem;">减半后${p.cycle_year}年</td>
             <td style="padding:1px 0;text-align:right;">后90天 ${sign(p.fwd90)}</td></tr>`).join('');
        html += `<div style="margin-top:6px;">
            <div style="font-size:0.82rem;font-weight:600;color:var(--text-secondary);">📉 ${r.title}</div>
            ${r.natural_experiment.pairs.length ? `<div style="font-size:0.72rem;color:${mut};margin:3px 0;">${r.natural_experiment.desc}</div>
            <table style="width:100%;border-collapse:collapse;font-size:0.78rem;">${pairs}</table>` : ''}
            <div style="font-size:0.72rem;color:var(--text-secondary);margin-top:5px;">
                ${r.groups.map(g => `${g.name} n=${g.n}：后30天中位 ${sign(g.median)}、胜率 ${g.win}%（基线 ${g.baseline_win}%，p=${g.p} 不显著）`).join('　·　')}</div>
            <div style="font-size:0.68rem;color:${mut};margin-top:4px;line-height:1.45;">⚠️ ${r.verdict}</div>
        </div>`;
    }

    // ── 季节性证伪 ──
    if (d.seasonality) {
        const s = d.seasonality;
        const rows = s.items.map(it => {
            // verdict 文案在后端写死为证伪结论; 若重跑研究后 p<0.05, 如实亮"矛盾待复核"而非渲染 "✓ 不显著" 的自打脸行
            const contradicts = it.p < 0.05;
            const verdictCell = contradicts
                ? `<span style="color:var(--accent-orange);">⚠ p&lt;0.05 与证伪结论矛盾，待复核</span>`
                : `✗ ${it.verdict}`;
            return `<tr>
                <td style="padding:2px 8px 2px 0;color:var(--text-secondary);">${it.name}</td>
                <td style="padding:2px 8px 2px 0;color:${mut};font-size:0.72rem;">${it.claim}</td>
                <td style="padding:2px 8px 2px 0;text-align:right;color:${mut};font-size:0.72rem;">p=${it.p}</td>
                <td style="padding:2px 0;text-align:right;color:${neg};font-size:0.74rem;">${verdictCell}</td></tr>`;
        }).join('');
        html += `<div style="margin-top:12px;border-top:1px dashed var(--border-color,#333);padding-top:8px;">
            <div style="font-size:0.82rem;font-weight:600;color:var(--text-secondary);">🗓️ ${s.title}</div>
            <table style="width:100%;border-collapse:collapse;font-size:0.78rem;margin-top:3px;">${rows}</table>
            <div style="font-size:0.68rem;color:${mut};margin-top:4px;line-height:1.45;">⚠️ ${s.verdict}</div>
        </div>`;
    }

    // ── 黑天鹅画像 ──
    if (d.blackswan) {
        const b = d.blackswan;
        const rows = b.events.map(e => {
            const rec = e.recovery_days !== null ? `${e.recovery_days}天` : '未收复';
            const f365 = e.fwd365 !== null ? sign(e.fwd365) : (e.since !== null && e.since !== undefined ? `${sign(e.since)}<small>*</small>` : '—');
            const bear = e.cycle_month >= 18 && e.cycle_month <= 30;
            return `<tr style="${bear ? 'background:#ea394312;' : ''}">
                <td style="padding:2px 6px 2px 0;color:var(--text-secondary);white-space:nowrap;">${bear ? '🐻' : ''}${e.name}</td>
                <td style="padding:2px 6px 2px 0;color:${mut};font-size:0.7rem;">减半后${Math.round(e.cycle_month)}m</td>
                <td style="padding:2px 6px 2px 0;text-align:right;">${sign(e.dd_from_high)}<small style="color:${mut};">高</small></td>
                <td style="padding:2px 6px 2px 0;text-align:right;color:${mut};font-size:0.72rem;">${rec}</td>
                <td style="padding:2px 0;text-align:right;">${f365}<small style="color:${mut};">1y</small></td></tr>`;
        }).join('');
        const cnt = b.summary.counter_example;
        html += `<div style="margin-top:12px;border-top:1px dashed var(--border-color,#333);padding-top:8px;">
            <div style="font-size:0.82rem;font-weight:600;color:var(--text-secondary);">🦢 ${b.title}</div>
            <div style="font-size:0.7rem;color:${mut};margin:2px 0;">🐻=熊市扎堆（减半后18-30月，反身性）·「高」=相对前30日高点回撤·「1y」=一年后·「*」=不足一年，截至核实日</div>
            <table style="width:100%;border-collapse:collapse;font-size:0.78rem;margin-top:2px;">${rows}</table>
            ${cnt ? `<div style="font-size:0.7rem;color:var(--text-secondary);margin-top:4px;">💡 逆势反例 <b>${cnt.name}</b>：${cnt.note}</div>` : ''}
            <div style="font-size:0.68rem;color:${mut};margin-top:4px;line-height:1.45;">⚠️ ${b.verdict}</div>
        </div>`;
    }

    // ── 前瞻风险雷达（判断，非统计；与历史黑天鹅表并列）──
    if (d.forward_risk) {
        const fr = d.forward_risk;
        const roleBadge = role => {
            const isEpi = role.includes('震中');
            const isRefuge = role.includes('避风港') || role === '两可';
            const c = isEpi ? neg : (isRefuge ? 'var(--accent-orange)' : mut);
            const bg = isEpi ? '#ea394315' : '#f0864a15';
            return `<span style="font-size:0.7rem;color:${c};background:${bg};padding:1px 5px;border-radius:3px;white-space:nowrap;">${isEpi ? '⚠震中' : (isRefuge ? '🛡两可' : role)}</span>`;
        };
        const probColor = p => (p.includes('高') ? neg : (p === '中' ? 'var(--accent-orange)' : mut));
        const riskItem = r => `
            <div style="padding:5px 0;border-bottom:1px dashed var(--border-color,#333);">
                <div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px;">
                    <span style="font-size:0.8rem;color:var(--text-secondary);font-weight:600;">${r.name}</span>
                    <span style="white-space:nowrap;">${roleBadge(r.btc_role)} <span style="font-size:0.68rem;color:${probColor(r.probability)};">概率${r.probability}</span></span>
                </div>
                <div style="font-size:0.72rem;color:var(--text-secondary);margin:2px 0;">${r.summary} <span style="color:${mut};">· ${r.horizon}</span></div>
                <div style="font-size:0.68rem;color:${mut};line-height:1.45;">📊 ${r.fact}</div>
                <div style="font-size:0.68rem;color:${mut};line-height:1.45;">💥 ${r.impact}</div>
                <div style="font-size:0.68rem;color:var(--accent-orange);line-height:1.45;">👁 预警：${r.early_warning}</div>
            </div>`;
        html += `<div style="margin-top:14px;border-top:2px solid var(--accent-orange);padding-top:8px;">
            <div style="font-size:0.82rem;font-weight:600;color:var(--text-secondary);">🔭 ${fr.title}<span style="font-weight:400;font-size:0.68rem;color:${mut};"> · 事实快照核实截至 ${fr.generated || '—'}</span></div>
            <div style="font-size:0.7rem;color:${mut};margin:3px 0 6px;background:#f0864a10;padding:5px 7px;border-radius:4px;">🎯 近端重点：${fr.near_term_focus || ''}</div>
            <div style="font-size:0.76rem;font-weight:600;color:var(--text-secondary);margin-top:6px;">🦏 灰犀牛（看得见、慢移动、终将逼近）</div>
            ${fr.gray_rhino.map(riskItem).join('')}
            <div style="font-size:0.76rem;font-weight:600;color:var(--text-secondary);margin-top:8px;">🦢 黑天鹅（突发、难预测）</div>
            ${fr.black_swan.map(riskItem).join('')}
            ${fr.macro_note ? `<div style="margin-top:8px;padding:6px 8px;background:#ea394310;border-radius:4px;">
                <div style="font-size:0.78rem;font-weight:600;color:${neg};">➕ ${fr.macro_note.name} <span style="font-size:0.7rem;font-weight:400;">${roleBadge(fr.macro_note.btc_role)} 概率${fr.macro_note.probability}</span></div>
                <div style="font-size:0.7rem;color:var(--text-secondary);margin-top:2px;">${fr.macro_note.summary}</div>
                <div style="font-size:0.68rem;color:${mut};margin-top:2px;">📊 ${fr.macro_note.fact}</div>
                <div style="font-size:0.68rem;color:var(--accent-orange);">👁 预警：${fr.macro_note.early_warning}</div></div>` : ''}
            ${fr.secondary && fr.secondary.length ? `<div style="font-size:0.68rem;color:${mut};margin-top:6px;line-height:1.5;">次级/机制完整性：${fr.secondary.join('；')}</div>` : ''}
            <div style="font-size:0.7rem;color:${mut};margin-top:6px;line-height:1.5;">⚠️ ${fr.honest_note || ''}</div>
        </div>`;
    }

    html += `<div style="font-size:0.7rem;color:${mut};margin-top:8px;border-top:1px solid var(--border-color,#333);padding-top:6px;">${d.honest_note || ''}${d.generated ? ` · 核实截至 ${String(d.generated).slice(0, 10)}` : ''}</div>`;
    // 默认折叠（内容长），展开看证伪+黑天鹅+前瞻风险雷达
    el.innerHTML = `<details class="pattern-collapse"><summary class="pattern-summary"><span>🧭 市场规律与风险 <span class="decision-freq" style="font-weight:400;">证伪·黑天鹅·前瞻风险雷达 · 展开</span></span><span class="pattern-chev">▾</span></summary>${html}</details>`;
}

/**
 * 周期相位与历史大事件规律（事件研究 C 层）。
 * n=3~4，逐次列出 + 混杂标注，绝不包装成"规律信号"。
 */
function renderCycleEvents(a) {
    const el = document.getElementById('cycleEventsCard');
    if (!el || !a || !a.current) return;
    el.style.display = '';

    const cur = a.current;

    // 当前相位定位条 + 仲裁句（本卡与实时周期评分冲突时, 以评分为准）
    const header = `
        <div style="font-size:0.82rem; color:var(--text-secondary); margin:4px 0 2px;">
            当前：第 <b>${cur.cycle_no}</b> 周期 · 减半后 <b>${cur.months_since_halving}</b> 月 ·
            距下次减半约 <b>${cur.days_to_next_halving_est}</b> 天
        </div>
        <div style="font-size:0.7rem; color:var(--text-muted); margin:0 0 8px;">仓位以周期分为准 — 本卡为日历叙事背景（n=3~4），不构成独立信号</div>`;

    // 减半相位地图（逐周期，高亮当前所处相位）
    const curM = cur.months_since_halving;
    const phaseRows = (a.cycle_phases || []).map(ph => {
        const m = ph.phase.match(/(\d+)-(\d+)/);
        const inPhase = m && curM >= +m[1] && curM < +m[2];
        const cells = ph.cycles.map(c => {
            const cls = c.partial ? 'muted' : (c.ret >= 0 ? 'pos' : 'neg');
            return `<span class="decision-stat-chip ${cls}" title="第${c.cycle}周期${c.partial ? '（进行中）' : ` · 段内最大回撤 ${c.maxdd}%`}">C${c.cycle}: ${c.ret >= 0 ? '+' : ''}${c.ret}%</span>`;
        }).join(' ');
        return `<div style="padding:4px 0; ${inPhase ? 'background:#f0864a12; border-left:3px solid #f0864a; padding-left:8px; margin-left:-8px; border-radius:0 4px 4px 0;' : ''}">
            <div style="font-size:0.76rem; color:${inPhase ? '#f0864a' : 'var(--text-muted)'}; font-weight:${inPhase ? '700' : '500'};">${inPhase ? '▶ ' : ''}${ph.phase}</div>
            <div style="margin-top:3px;">${cells}</div>
        </div>`;
    }).join('');

    // 事件表（世界杯/换主席/大选）。since_event 是资产生成日快照, 如实标核实日而非"至今"。
    // 数值一律中性色 — 已证伪规律不穿红绿"信号外衣", 档案就长档案的样子
    const asOf = (a.generated || '').slice(0, 10);
    const fmtN = v => (v === null || v === undefined) ? '进行中' : `${v >= 0 ? '+' : ''}${v}%`;
    const evBlock = (name, ev) => {
        if (!ev || !ev.rows || !ev.rows.length) return '';
        const rows = ev.rows.map(r => {
            const fwd = r.fwd365 !== null ? fmtN(r.fwd365)
                : (r.since_event !== undefined ? `${fmtN(r.since_event)} <small>(至 ${asOf})</small>` : '进行中');
            return `<tr style="color:var(--text-muted);">
                <td style="padding:2px 8px 2px 0; color:var(--text-secondary);">${r.label}</td>
                <td style="padding:2px 8px 2px 0; font-size:0.72rem;">${r.cycle_month != null ? `减半后 ${r.cycle_month}月` : '首次减半前'}</td>
                <td style="padding:2px 8px 2px 0; text-align:right;">${fmtN(r.drawdown_at_event)}<small> 距前高</small></td>
                <td style="padding:2px 0; text-align:right;">${fwd}<small> +1y</small></td>
            </tr>`;
        }).join('');
        return `
            <div style="margin-top:10px;">
                <div style="font-size:0.8rem; font-weight:600; color:var(--text-secondary);">${name} <span style="font-weight:400; color:var(--text-muted); font-size:0.72rem;">(n=${ev.rows.length})</span></div>
                <table style="width:100%; border-collapse:collapse; font-size:0.78rem; margin-top:3px;">${rows}</table>
                <div style="font-size:0.68rem; color:var(--text-muted); margin-top:4px; line-height:1.45;">⚠️ ${ev.note}</div>
            </div>`;
    };

    const events = a.events || {};
    // 已证伪事件收进嵌套档案折叠 — 层级反映认识论地位: 一级只留相位地图(唯一候选结构)
    const archiveInner = evBlock('世界杯', events['世界杯'])
        + evBlock('美联储换主席', events['美联储换主席'])
        + evBlock('美国大选', events['美国大选']);
    const archive = archiveInner ? `
        <details style="margin-top:10px;">
            <summary style="cursor:pointer; font-size:0.78rem; font-weight:600; color:var(--text-muted);">📁 已证伪事件档案 — 世界杯 / 换主席 / 大选（均为减半周期的别名，仅查证用）</summary>
            ${archiveInner}
        </details>` : '';
    const body = header
        + `<div style="font-size:0.78rem; font-weight:600; color:var(--text-secondary); margin-top:4px;">减半周期相位地图（逐周期，不平均）</div>`
        + phaseRows
        + archive
        + `<div style="font-size:0.7rem; color:var(--text-muted); margin-top:8px; border-top:1px solid var(--border-color,#333); padding-top:6px;">${a.honest_note || ''}${asOf ? ` · 核实截至 ${asOf}` : ''}</div>`;
    // 默认折叠, 摘要保留中性相位定位 (不做"熊市段"之类 n=3 常驻暗示, 相位标签留在展开视图的地图里)
    const sumHint = `第 ${cur.cycle_no} 周期 · 减半后 ${cur.months_since_halving} 月`;
    el.innerHTML = `<details class="pattern-collapse"><summary class="pattern-summary"><span>🗓️ 周期相位与事件规律 <span class="decision-freq" style="font-weight:400;">${sumHint} · 展开</span></span><span class="pattern-chev">▾</span></summary>${body}</details>`;
}

/**
 * 触发价位表：机械反解"什么价格会翻转哪个信号"。
 * 事件研究 B 层 — 不承诺胜率，价位随均线/慢变量每日漂移。
 */
function renderTriggerLevels(tl) {
    const el = document.getElementById('decisionTriggers');
    if (!el) return;
    if (!tl || !tl.hard || !tl.hard.length) { el.style.display = 'none'; return; }
    el.style.display = '';

    const fmt = p => '$' + Number(p).toLocaleString('en-US', { maximumFractionDigits: 0 });
    const distBadge = d => {
        const cls = d >= 0 ? 'pos' : 'neg';
        return `<span class="decision-stat-chip ${cls}" style="margin-left:6px;">${d >= 0 ? '+' : ''}${d}%</span>`;
    };

    const hardRows = tl.hard.map(h => `
        <div style="display:flex; justify-content:space-between; align-items:baseline; gap:8px; padding:4px 0; border-bottom:1px dashed var(--border-color, #333);">
            <span style="color:var(--text-secondary); font-size:0.82rem;">${h.side === 'support' ? '🛡️' : '🚧'} ${h.name}</span>
            <span style="white-space:nowrap;"><b>${fmt(h.price)}</b>${distBadge(h.distance_pct)}</span>
        </div>
        <div style="color:var(--text-muted); font-size:0.68rem; margin:2px 0 6px;">${h.note}</div>`).join('');

    const bandRows = tl.bands.map(b => {
        if (b.price !== null && b.price !== undefined) {
            return `<div style="padding:2px 0; font-size:0.75rem; color:var(--text-secondary);">
                ${b.name}: <b>${fmt(b.price)}</b>${distBadge(b.distance_pct)}</div>`;
        }
        return `<div style="padding:2px 0; font-size:0.75rem; color:var(--text-muted);">
            ${b.name}: <span style="opacity:0.85;">单靠价格不可达 — 需趋势斜率/慢变量翻转</span></div>`;
    }).join('');

    const r = tl.reachable;
    const reachLine = r ? `<div style="color:var(--text-muted); font-size:0.7rem; margin-top:6px;">
        扫描 −50%~+100%: 评分可达上限 ${r.max.score >= 0 ? '+' : ''}${r.max.score}（${fmt(r.max.price)}, ${r.max.pct >= 0 ? '+' : ''}${r.max.pct}%）·
        下限 ${r.min.score >= 0 ? '+' : ''}${r.min.score}（${fmt(r.min.price)}, ${r.min.pct >= 0 ? '+' : ''}${r.min.pct}%）
        — 体系对瞬时价格脱敏，档位转换靠趋势结构而非单日行情</div>` : '';

    el.innerHTML = `
        <div class="decision-card-title">📐 触发价位表 <span class="decision-freq">机械反解 · 非预测</span></div>
        ${hardRows}
        <div style="margin-top:8px; color:var(--text-secondary); font-size:0.78rem; font-weight:600;">评分档位反解（近似，固定慢变量因子）</div>
        ${bandRows}
        ${reachLine}
        <div style="color:var(--text-muted); font-size:0.7rem; margin-top:6px;">${(tl.meta && tl.meta.note) || ''}</div>`;
}

/**
 * 今日量化决策面板：长期仓位（滞回换档）+ 短期执行节奏 + 回测分档统计
 */
function renderDecisionPanel(d) {
    const section = document.getElementById('decisionSection');
    if (!section) return;
    if (!d || !d.cycle) { section.style.display = 'none'; return; }
    section.style.display = '';

    const c = d.cycle, t = d.tactical;

    // 动作 badge：加仓绿 / 减仓红 / 维持中性
    const actionCls = { increase: 'up', decrease: 'down', hold: 'hold' }[c.action_type] || 'hold';
    const actionIcon = { increase: '↑', decrease: '↓', hold: '—' }[c.action_type] || '—';

    // 分档回测统计 chips（中位数 + 胜率）
    const statChips = (stats, windows) => {
        if (!stats) return '<span class="decision-stat-empty">回测统计不可用</span>';
        return windows.filter(w => stats[w]).map(w => {
            const s = stats[w];
            const cls = s.median >= 0 ? 'pos' : 'neg';
            return `<span class="decision-stat-chip ${cls}" title="样本 ${s.n} 天 · 均值 ${s.mean >= 0 ? '+' : ''}${s.mean}%">
                ${w} 中位 ${s.median >= 0 ? '+' : ''}${s.median}% · 胜率 ${s.win}%</span>`;
        }).join('');
    };

    // 滞回状态说明
    let hystNote = '';
    if (c.pending) {
        hystNote = `<div class="decision-pending">⏳ ${c.pending.note}</div>`;
    } else if (c.raw_differs) {
        hystNote = `<div class="decision-pending muted">滞回防抖：评分档位「${c.raw_band}」未越过 ±0.05 边界，目标仓位不变</div>`;
    }

    // 黑天鹅压力测试参考行 — 数值由后端从 blackswan_events.json 透传 (单一来源), 必须同给中位与最差
    const bs = d.blackswan_stress;
    const stressLine = (bs && bs.dd_median != null && bs.dd_worst != null && c.target_mid != null) ? `
        <div class="decision-stats" style="font-size:0.68rem; color:var(--text-muted); margin-top:4px; line-height:1.5;">压力测试参考：历史黑天鹅相对前30日高点急跌中位 ${bs.dd_median}%、最差 ${bs.dd_worst}%（n=${bs.n} 事后追认样本，尾部无上界）→ 目标仓位中值 ${c.target_mid}% 对应组合瞬时回撤约 ${(bs.dd_median * c.target_mid / 100).toFixed(0)}% ~ ${(bs.dd_worst * c.target_mid / 100).toFixed(0)}%</div>` : '';

    document.getElementById('decisionCycle').innerHTML = `
        <div class="decision-card-title">🧭 长期 · 仓位决策 <span class="decision-freq">周级变化</span></div>
        <div class="decision-main">
            <span class="decision-target">${c.target_lo}–${c.target_hi}<small>%</small></span>
            <div class="decision-main-right">
                <div class="decision-band">${c.band}</div>
                <span class="decision-action ${actionCls}">${actionIcon} ${c.action}</span>
            </div>
        </div>
        ${hystNote}
        <div class="decision-stats">该档位 12 年回测前瞻：${statChips(c.stats, ['90d', '180d', '365d'])}</div>
        ${stressLine}`;

    document.getElementById('decisionTactical').innerHTML = `
        <div class="decision-card-title">⚡ 短期 · 执行节奏 <span class="decision-freq">日级变化</span></div>
        <div class="decision-main">
            <span class="decision-pace">${t.pace}</span>
            <div class="decision-main-right">
                <div class="decision-band">${t.band}</div>
            </div>
        </div>
        <div class="decision-advice">${t.advice}</div>
        <div class="decision-stats">该档位回测前瞻：${statChips(t.stats, ['14d', '30d'])}</div>`;

    // 警示 + 元信息
    const warnEl = document.getElementById('decisionWarnings');
    if (d.warnings && d.warnings.length) {
        warnEl.style.display = '';
        warnEl.innerHTML = d.warnings.map(w => `<div>⚠️ ${w}</div>`).join('');
    } else {
        warnEl.style.display = 'none';
    }
    const h = d.hysteresis || {};
    document.getElementById('decisionMeta').textContent =
        `滞回换档 δ=${h.delta} · ${h.confirm}天确认`;
    document.getElementById('decisionFootnote').textContent =
        (d.stats_meta && d.stats_meta.note ? d.stats_meta.note : '') +
        '　·　短期节奏只影响执行快慢与杠杆约束，不改变目标仓位';
}

function renderDatMNAV(ind) {
    const el = document.getElementById('datMNAV');
    if (!el) return;
    if (!ind || ind.value === null) {
        el.innerHTML = '<span style="color:var(--text-muted);">MSTR mNAV 数据不可用</span>';
        return;
    }
    const colorMap = { '🟢': 'var(--accent-green)', '🟡': 'var(--accent-yellow)', '🟠': 'var(--accent-orange)', '🔴': 'var(--accent-red)', '⚪': 'var(--text-muted)' };
    const c = colorMap[ind.color] || 'var(--text-muted)';
    el.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="color:var(--text-secondary);">MSTR mNAV</span>
            <span style="color:${c}; font-size:1.1rem; font-weight:700;">${ind.value.toFixed(2)}×</span>
        </div>
        <div style="color:var(--text-muted); font-size:0.72rem; margin-top:4px;">${ind.status}</div>
        <a href="${ind.url}" target="_blank" rel="noopener noreferrer"
           style="display:inline-block; margin-top:6px; font-size:0.7rem; color:var(--accent-btc); opacity:0.7; text-decoration:none;">
            ↗ SaylorTracker 查看详情
        </a>`;
}

/**
 * 更新顶部摘要栏
 */
function updateTopSummaryBar(data) {
    const btcPrice = data.btc_price;
    const indicators = data.indicators || {};
    // 价格
    // API 契约: 失败指标/字段值为 null, 而 isNaN(null)===false 会放行 → 必须用 Number.isFinite
    const priceEl = document.getElementById('summaryPrice');
    if (priceEl && Number.isFinite(btcPrice)) {
        priceEl.textContent = '$' + btcPrice.toLocaleString(undefined, {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        });
    }

    // 价格趋势
    const changeEl = document.getElementById('summaryChange');
    if (changeEl && indicators['MACD']) {
        const macd = indicators['MACD'];
        if (macd.score > 0) {
            changeEl.textContent = '▲ 趋势向上';
            changeEl.className = 'change positive';
        } else if (macd.score < 0) {
            changeEl.textContent = '▼ 趋势向下';
            changeEl.className = 'change negative';
        } else {
            changeEl.textContent = '— 震荡';
            changeEl.className = 'change neutral';
        }
    }

    // 全网算力
    const hashrateEl = document.getElementById('summaryHashrate');
    if (hashrateEl && indicators['全网算力']) {
        const val = indicators['全网算力'].value;
        if (Number.isFinite(val)) {
            hashrateEl.textContent = val.toFixed(1) + ' EH/s';
        }
    }

    // Ahr999
    const ahr999El = document.getElementById('summaryAhr999');
    if (ahr999El && indicators['Ahr999']) {
        const val = indicators['Ahr999'].value;
        if (Number.isFinite(val)) {
            ahr999El.textContent = val.toFixed(2);
            ahr999El.style.color = val < 0.45 ? 'var(--accent-green)' : (val < 1.2 ? 'var(--accent-yellow)' : 'var(--accent-red)');
        }
    }

    // 恐惧贪婪
    const fgEl = document.getElementById('summaryFearGreed');
    if (fgEl && indicators['恐惧贪婪指数']) {
        const val = indicators['恐惧贪婪指数'].value;
        if (Number.isFinite(val)) {
            fgEl.textContent = val.toFixed(0);
            fgEl.style.color = val < 25 ? 'var(--accent-green)' : (val > 75 ? 'var(--accent-red)' : 'var(--accent-yellow)');
        }
    }

    // 周期仓位分 / 短期战术分
    const scoreColor = (s) => s >= 0.15 ? 'var(--accent-green)' : s <= -0.12 ? 'var(--accent-red)' : 'var(--accent-yellow)';
    const cycleEl = document.getElementById('summaryCycle');
    if (cycleEl && typeof data.total_score === 'number') {
        cycleEl.textContent = (data.total_score > 0 ? '+' : '') + data.total_score.toFixed(2);
        cycleEl.style.color = scoreColor(data.total_score);
    }
    const tacticalEl = document.getElementById('summaryTactical');
    if (tacticalEl && typeof data.tactical_score === 'number') {
        tacticalEl.textContent = (data.tactical_score > 0 ? '+' : '') + data.tactical_score.toFixed(2);
        tacticalEl.style.color = scoreColor(data.tactical_score);
    }
}

/**
 * 渲染指标总览表格
 */
function renderSummaryTable(indicators) {
    console.log('renderSummaryTable called with:', indicators);
    const tbody = document.getElementById('summaryTableBody');
    console.log('tbody element:', tbody);
    if (!tbody) {
        console.error('summaryTableBody not found!');
        return;
    }

    tbody.innerHTML = '';

    // 定义指标排序（按优先级）
    const priorityOrder = ['P0', 'P1', 'P2'];

    // 将指标转换为数组并排序
    const sortedIndicators = Object.entries(indicators)
        .sort((a, b) => {
            const pA = priorityOrder.indexOf(a[1].priority || 'P2');
            const pB = priorityOrder.indexOf(b[1].priority || 'P2');
            return pA - pB;
        });

    // 统计各分类数量
    const counts = { all: 0, P0: 0, P1: 0, P2: 0 };

    for (const [name, indicator] of sortedIndicators) {
        const row = document.createElement('tr');

        // 设置优先级分类属性
        const priority = indicator.priority || 'P2';
        // 兼容 "短期" 等中文优先级
        const normalizedPriority = priority === '短期' ? 'P1' : (priorityOrder.includes(priority) ? priority : 'P2');
        row.setAttribute('data-priority', normalizedPriority);

        counts.all++;
        counts[normalizedPriority] = (counts[normalizedPriority] || 0) + 1;

        // 获取结论和样式
        const conclusion = getConclusion(indicator);
        const conclusionClass = getConclusionClass(indicator);

        // 格式化数值
        let valueDisplay = '--';
        if (indicator.value !== null && !isNaN(indicator.value)) {
            if (indicator.value > 1000000000) {
                valueDisplay = `$${(indicator.value / 1e9).toFixed(1)}B`;
            } else if (indicator.value > 1000000) {
                valueDisplay = `$${(indicator.value / 1e6).toFixed(1)}M`;
            } else if (indicator.value > 100) {
                valueDisplay = `$${formatNumber(indicator.value)}`;
            } else {
                valueDisplay = indicator.value.toFixed(2);
            }
        }

        row.innerHTML = `
            <td>${indicator.name || name}</td>
            <td>${valueDisplay}</td>
            <td><span class="conclusion-badge ${conclusionClass}">${conclusion}</span></td>
        `;

        tbody.appendChild(row);
    }

    // 更新 tab 上的数量标注
    document.querySelectorAll('.summary-tab').forEach(tab => {
        const cat = tab.getAttribute('data-category');
        const count = counts[cat] || 0;
        const label = { all: '全部', P0: '长期', P1: '短期', P2: '辅助' }[cat] || cat;
        tab.textContent = `${label} (${count})`;
    });

    // 初始化 tab 事件
    initSummaryTabs();
}

/**
 * 初始化指标总览分类标签
 */
function initSummaryTabs() {
    const tabs = document.querySelectorAll('.summary-tab');
    tabs.forEach(tab => {
        tab.onclick = function () {
            // 切换 active 状态
            tabs.forEach(t => t.classList.remove('active'));
            this.classList.add('active');

            const category = this.getAttribute('data-category');
            const rows = document.querySelectorAll('#summaryTableBody tr');

            rows.forEach(row => {
                if (category === 'all' || row.getAttribute('data-priority') === category) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            });
        };
    });
}

/**
 * 根据指标获取结论文字
 */
function getConclusion(indicator) {
    const score = indicator.score;
    const color = indicator.color;

    // 根据 score 或 color 判断
    if (score >= 0.8) return '强烈看多';
    if (score >= 0.5) return '偏多';
    if (score >= 0.2) return '略偏多';
    if (score > -0.2) return '中立';
    if (score > -0.5) return '略偏空';
    if (score > -0.8) return '偏空';
    if (score <= -0.8) return '强烈看空';

    // 根据颜色 fallback
    if (color === '🟢') return '偏多';
    if (color === '🟡') return '中立';
    if (color === '🔴') return '偏空';
    if (color === '⚪') return '参考';

    return '中立';
}

/**
 * 根据指标获取结论样式类名
 */
function getConclusionClass(indicator) {
    const score = indicator.score;

    if (score >= 0.8) return 'strong-bullish';
    if (score >= 0.3) return 'bullish';
    if (score > -0.3) return 'neutral';
    if (score > -0.8) return 'bearish';
    if (score <= -0.8) return 'strong-bearish';

    // Fallback by color
    const color = indicator.color;
    if (color === '🟢') return 'bullish';
    if (color === '🟡') return 'neutral';
    if (color === '🔴') return 'bearish';

    return 'info';
}

/**
 * 更新仪表盘指针
 */
function updateGauge(score, needleId = 'gaugeNeedle') {
    const needle = document.getElementById(needleId);
    if (!needle) return;
    // score 范围是 -1 到 +1，映射到 -90 到 +90 度
    const angle = score * 90;
    needle.style.transform = `translateX(-50%) rotate(${angle}deg)`;
}

/**
 * 渲染因子桶得分条 (BTC Compass)
 */
function renderBucketBars(containerId, buckets) {
    const el = document.getElementById(containerId);
    if (!el || !buckets) return;
    let html = '';
    for (const [name, b] of Object.entries(buckets)) {
        const s = b.score;
        const hasData = s !== null && s !== undefined;
        const pct = hasData ? Math.abs(s) * 50 : 0;          // 半宽最大 50%
        const left = hasData && s < 0 ? 50 - pct : 50;        // 负分向左, 正分向右
        const color = !hasData ? 'var(--text-muted)' : s >= 0.3 ? 'var(--accent-green)' : s <= -0.3 ? 'var(--accent-red)' : 'var(--accent-yellow)';
        const memberTip = (b.members || [])
            .map(m => `${m.name}: ${m.score === null ? '✕' : m.score}`)
            .join(' · ');
        html += `
            <div class="bucket-row" title="${memberTip}">
                <span class="bucket-name">${name} <em>${Math.round(b.weight * 100)}%</em></span>
                <div class="bucket-track">
                    <div class="bucket-mid"></div>
                    <div class="bucket-fill" style="left:${left}%; width:${pct}%; background:${color};"></div>
                </div>
                <span class="bucket-score" style="color:${color};">${hasData ? (s > 0 ? '+' : '') + s.toFixed(2) : '–'}</span>
            </div>`;
    }
    el.innerHTML = html;
}

/**
 * Render an inline SVG sparkline from an array of values.
 */
function renderSparkline(values, colorKey) {
    if (!values || values.length < 2) {
        return '<svg class="card-v2-sparkline" viewBox="0 0 100 36"></svg>';
    }
    const w = 100, h = 36;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const pts = values.map((v, i) => {
        const x = (i / (values.length - 1)) * w;
        const y = h - ((v - min) / range) * (h - 6) - 3;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const fillPts = `${pts} ${w},${h} 0,${h}`;
    return `
        <svg class="card-v2-sparkline spark-${colorKey}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
            <polyline class="spark-line" points="${pts}"/>
            <polyline class="spark-area" points="${fillPts}"/>
        </svg>`;
}

/**
 * Return badge label text based on indicator color signal.
 */
function getBadgeLabel(color) {
    if (color === '🟢') return '定投';
    if (color === '🟡') return '观望';
    if (color === '🔴') return '警戒';
    return '持有';
}

/**
 * Return CSS class suffix for indicator color.
 */
function getColorClass(color) {
    if (color === '🟢') return 'green';
    if (color === '🟡') return 'yellow';
    if (color === '🔴') return 'red';
    return 'yellow';
}

/**
 * Render indicator cards using new v2 design with sparklines.
 */
function renderIndicators(indicators, sparklines) {
    sparklines = sparklines || {};
    const longTermContainer   = document.getElementById('longTermIndicators');
    const shortTermContainer  = document.getElementById('shortTermIndicators');
    const auxContainer        = document.getElementById('auxIndicators');

    if (longTermContainer)  longTermContainer.innerHTML  = '';
    if (shortTermContainer) shortTermContainer.innerHTML = '';
    if (auxContainer)       auxContainer.innerHTML       = '';

    for (const [name, indicator] of Object.entries(indicators)) {
        const card = createIndicatorCardV2(indicator, sparklines[name] || []);
        if (indicator.priority === 'P0') {
            if (longTermContainer) longTermContainer.appendChild(card);
        } else if (indicator.priority === 'P1') {
            if (shortTermContainer) shortTermContainer.appendChild(card);
        } else if (auxContainer) {
            auxContainer.appendChild(card);
        }
    }
}

/**
 * Create a new-style indicator card with color border, badge, score bar, sparkline.
 */
function createIndicatorCardV2(indicator, sparklineValues) {
    const colorKey   = getColorClass(indicator.color);
    const badgeLabel = getBadgeLabel(indicator.color);

    // Score bar width: map score (-1..+1) to 0..100%
    const scoreWidth = Math.round(((indicator.score + 1) / 2) * 100);

    // Format value
    const displayValue = indicator.value !== null
        ? (typeof indicator.value === 'number' ? indicator.value.toFixed(2) : indicator.value)
        : '—';

    const card = document.createElement('div');
    card.className = `indicator-card-v2 color-${colorKey}`;

    const linkHtml = indicator.url
        ? `<a href="${indicator.url}" target="_blank" rel="noopener noreferrer" class="card-v2-extlink" title="查看原始图表" onclick="event.stopPropagation()">↗</a>`
        : '';

    card.innerHTML = `
        <div class="card-v2-header">
            <span class="card-v2-name">${indicator.name}</span>
            ${linkHtml}
            <span class="card-v2-badge badge-${colorKey}">${badgeLabel}</span>
        </div>
        <div class="card-v2-value">${displayValue}</div>
        <div class="card-v2-status">${indicator.status || ''}</div>
        <div class="card-v2-score-bar">
            <div class="card-v2-score-fill fill-${colorKey}" style="width:${scoreWidth}%"></div>
        </div>
        ${renderSparkline(sparklineValues, colorKey)}
        <div class="card-v2-hint">点击展开历史图 →</div>
    `;

    // Click → open history drawer (openDrawer defined in Task 6)
    card.addEventListener('click', () => {
        if (typeof openDrawer === 'function') openDrawer(indicator.name, indicator);
    });

    // Hover tooltip
    if (indicator.description || indicator.method) {
        card.addEventListener('mouseenter', (e) => {
            showIndicatorTooltip(indicator, e);
        });
        card.addEventListener('mousemove', (e) => {
            positionTooltip(e);
        });
        card.addEventListener('mouseleave', () => {
            hideIndicatorTooltip();
        });
    }

    return card;
}

// ── 指标说明气泡 ────────────────────────────────────────────────
const _tip = () => document.getElementById('indicator-tooltip');

function showIndicatorTooltip(indicator, e) {
    const el = _tip();
    if (!el) return;
    document.getElementById('tip-name').textContent  = indicator.name;
    document.getElementById('tip-desc').textContent  = indicator.description || '';
    document.getElementById('tip-method').textContent = indicator.method ? '📐 ' + indicator.method : '';
    document.getElementById('tip-method').style.display = indicator.method ? '' : 'none';
    el.classList.add('visible');
    positionTooltip(e);
}

function hideIndicatorTooltip() {
    const el = _tip();
    if (el) el.classList.remove('visible');
}

function positionTooltip(e) {
    const el = _tip();
    if (!el) return;
    const margin = 14;
    const tw = el.offsetWidth  || 300;
    const th = el.offsetHeight || 120;
    let x = e.clientX + margin;
    let y = e.clientY + margin;
    if (x + tw > window.innerWidth)  x = e.clientX - tw - margin;
    if (y + th > window.innerHeight) y = e.clientY - th - margin;
    el.style.left = x + 'px';
    el.style.top  = y + 'px';
}

/**
 * 创建指标卡片（带迷你图表）
 */
function createIndicatorCard(indicator) {
    const card = document.createElement('div');
    card.className = `indicator-card ${getIndicatorColorClass(indicator.color)}`;

    // 生成唯一的 canvas ID
    const chartId = `chart-${indicator.name.replace(/\s+/g, '-')}`;

    // 支持图表的指标列表
    const chartableIndicators = ['Ahr999', '恐惧贪婪指数', '资金费率', '资金费率(7d)', '多空比', 'Pi Cycle Top'];
    const hasChart = chartableIndicators.includes(indicator.name);

    // 构建HTML
    let html = `
        <div class="indicator-header">
            <span class="indicator-name">${indicator.name}</span>
            <span class="indicator-priority ${indicator.priority}">${indicator.priority}</span>
        </div>
        <div class="indicator-status">
            <span class="status-icon">${indicator.color}</span>
            <span>${indicator.status}</span>
        </div>
        ${hasChart ? `<div class="indicator-chart"><canvas id="${chartId}" height="60"></canvas></div>` : ''}
    `;

    // 添加说明部分的容器 (如果有定义)
    if (indicator.description || indicator.method) {
        html += `
            <div class="indicator-details-toggle" onclick="toggleDetails(this)">
                <span>ℹ️ 指标说明</span>
                <span class="arrow">▼</span>
            </div>
            <div class="indicator-details" style="display: none;">
                ${indicator.description ? `<div class="detail-item"><strong>定义:</strong> ${indicator.description}</div>` : ''}
                ${indicator.method ? `<div class="detail-item"><strong>计算:</strong> ${indicator.method}</div>` : ''}
            </div>
        `;
    }

    card.innerHTML = html;

    // 如果由外部链接，添加点击事件和样式 (点击卡片头部跳转)
    if (indicator.url) {
        const header = card.querySelector('.indicator-header');
        header.classList.add('clickable');
        header.onclick = (e) => {
            e.stopPropagation();
            window.open(indicator.url, '_blank');
        };
        header.title = "点击查看原始图表";

        // 在名字旁添加链接图标
        const nameEl = card.querySelector('.indicator-name');
        if (nameEl) {
            nameEl.innerHTML += ' <span style="font-size: 0.8em; color: var(--text-muted);">↗</span>';
        }
    }

    // 延迟加载图表
    if (hasChart) {
        setTimeout(() => fetchAndRenderChart(indicator.name, chartId), 100);
    }

    return card;
}

/**
 * 获取历史数据并渲染图表
 */
async function fetchAndRenderChart(indicatorName, canvasId) {
    try {
        const response = await fetch(`/api/history/${encodeURIComponent(indicatorName)}?days=30`);
        const data = await response.json();

        if (!data.success || !data.dates || data.dates.length === 0) {
            return; // 无数据则不显示图表
        }

        renderMiniChart(canvasId, data);
    } catch (error) {
        console.log(`Chart for ${indicatorName} unavailable:`, error);
    }
}

/**
 * 渲染迷你图表
 */
function renderMiniChart(canvasId, data) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    const ctx = canvas.getContext('2d');

    // 准备阈值线
    const annotations = [];
    if (data.thresholds) {
        Object.values(data.thresholds).forEach(threshold => {
            annotations.push({
                type: 'line',
                yMin: threshold.value,
                yMax: threshold.value,
                borderColor: threshold.color,
                borderWidth: 1,
                borderDash: [3, 3],
            });
        });
    }

    // 简化日期标签
    const labels = data.dates.map((d, i) => {
        if (i === 0 || i === data.dates.length - 1) {
            return d.slice(5); // MM-DD
        }
        return '';
    });

    new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                data: data.values,
                borderColor: PAL.btc,
                backgroundColor: PAL.btc + '1a',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    enabled: true,
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        title: (items) => data.dates[items[0].dataIndex],
                        label: (item) => `${data.indicator}: ${item.raw}`
                    }
                }
            },
            scales: {
                x: {
                    display: true,
                    ticks: {
                        color: PAL.muted,
                        font: { size: 9 },
                        maxRotation: 0
                    },
                    grid: { display: false }
                },
                y: {
                    display: false,
                    grid: { display: false }
                }
            },
            interaction: {
                mode: 'index',
                intersect: false
            }
        }
    });

    // 手动绘制阈值线（Chart.js 4.x 需要插件，这里简化处理）
    drawThresholdLines(canvas, ctx, data);
}

/**
 * 切换指标说明显示/隐藏
 */
function toggleDetails(element) {
    const details = element.nextElementSibling;
    const arrow = element.querySelector('.arrow');

    if (details.style.display === 'none') {
        details.style.display = 'block';
        arrow.textContent = '▲';
        element.classList.add('active');
    } else {
        details.style.display = 'none';
        arrow.textContent = '▼';
        element.classList.remove('active');
    }
}

/**
 * 绘制阈值参考线
 */
function drawThresholdLines(canvas, ctx, data) {
    if (!data.thresholds || !data.values || data.values.length === 0) return;

    const minVal = Math.min(...data.values);
    const maxVal = Math.max(...data.values);
    const range = maxVal - minVal || 1;

    // 画布尺寸
    const chartArea = {
        left: 0,
        right: canvas.width,
        top: 10,
        bottom: canvas.height - 15
    };
    const height = chartArea.bottom - chartArea.top;

    Object.values(data.thresholds).forEach(threshold => {
        if (threshold.value >= minVal && threshold.value <= maxVal) {
            const y = chartArea.bottom - ((threshold.value - minVal) / range) * height;

            ctx.beginPath();
            ctx.setLineDash([3, 3]);
            ctx.strokeStyle = threshold.color;
            ctx.lineWidth = 1;
            ctx.moveTo(chartArea.left, y);
            ctx.lineTo(chartArea.right, y);
            ctx.stroke();
        }
    });
}

/**
 * 获取指标颜色类名
 */
function getIndicatorColorClass(color) {
    switch (color) {
        case '🟢': return 'green';
        case '🟡': return 'yellow';
        case '🔴': return 'red';
        default: return 'neutral';
    }
}

/**
 * 获取评分颜色
 */
function getScoreColor(score) {
    if (score >= 0.5) return 'green';
    if (score >= -0.3) return 'yellow';
    return 'red';
}

/**
 * 格式化数字
 */
function formatNumber(num) {
    return new Intl.NumberFormat('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    }).format(num);
}

/**
 * 显示错误信息
 * 复用 _showComputingBanner 的固定顶栏模式（红底），不再依赖已删除的 #loading 区块，
 * 保证任何错误路径都能显示且不抛异常（历史上 getElementById('loading') 返回 null 会 TypeError
 * 击穿回调，令 spinner 永转、页面停在骨架屏）。用 textContent 落地上游文案，避免 XSS。
 */
function showError(message) {
    let banner = document.getElementById('errorBanner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'errorBanner';
        banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:rgba(220,53,69,0.94);color:#fff;text-align:center;padding:10px;font-size:0.9rem;font-weight:500;cursor:pointer;';
        banner.title = '点击关闭';
        banner.addEventListener('click', () => banner.remove());
        document.body.prepend(banner);
    }
    banner.textContent = `❌ ${message} · 请点击右下角按钮重试`;
}

/**
 * 获取资讯数据（支持冷启动 202 轮询）
 */
async function fetchNewsData() {
    console.log('Fetching news data...');
    const newsRefreshBtn = document.getElementById('newsRefreshBtn');
    if (newsRefreshBtn) newsRefreshBtn.classList.add('spinning');

    const renderAll = (data) => {
        if (data.whales && data.whales.length > 0) renderWhaleActivity(data.whales);
        if (data.whale_stats) renderWhaleStats(data.whale_stats);
        if (data.exchange_balance) renderExchangeBalance(data.exchange_balance);
        if (data.calendar && data.calendar.length > 0) renderMacroCalendar(data.calendar);
        if (data.news && data.news.length > 0) renderCryptoNews(data.news);
        renderEtfFlow(data.etf_flow);
        renderDatHoldings(data.dat_holdings);
        const updatedEl = document.getElementById('newsUpdatedAt');
        if (updatedEl) {
            const now = new Date();
            updatedEl.textContent = `更新于 ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;
        }
        if (newsRefreshBtn) newsRefreshBtn.classList.remove('spinning');
    };

    await fetchWithComputingPoll('/api/news', {
        pollKey: '_newsPollTimer',
        maxWaitMs: 3 * 60 * 1000,         // 资讯冷启动最多等 3 分钟
        delays: [5000, 10000, 20000, 30000],
        onData: renderAll,
        onTimeout: () => {
            console.warn('News 加载超时');
            if (newsRefreshBtn) newsRefreshBtn.classList.remove('spinning');
        },
        onError: (err) => {
            console.error('Failed to fetch news:', err);
            if (newsRefreshBtn) newsRefreshBtn.classList.remove('spinning');
        },
    });

    if (!window._newsPollTimer && newsRefreshBtn) {
        newsRefreshBtn.classList.remove('spinning');
    }
}

/**
 * 渲染 BTC 资讯
 */
function renderCryptoNews(news) {
    const container = document.getElementById('cryptoNews');
    if (!container) return;

    const countBadge = `<div style="font-size:0.7rem; color:var(--text-muted); padding:4px 4px 8px; border-bottom:1px solid var(--border-color); margin-bottom:4px; flex-shrink:0;">
        共 ${news.length} 条 · 最近 72 小时
    </div>`;

    const items = news.map(item => {
        const summary = item.summary ? item.summary.trim() : '';
        return `<div class="news-flash-item${summary ? ' has-summary' : ''}">
            <div class="news-flash-head">
                <span class="news-flash-bolt">⚡</span>
                <a href="${escapeHtml(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer" class="news-flash-title">
                    ${escapeHtml(item.title)}
                </a>
                <span class="news-flash-time">${escapeHtml(item.time)}</span>
            </div>
            ${summary ? `<div class="news-flash-summary"><div class="news-flash-summary-inner">${summary}</div></div>` : ''}
        </div>`;
    }).join('');

    container.innerHTML = countBadge + items;

    // 等 DOM 布局完成后，将右栏高度锁定为左栏高度
    setTimeout(alignNewsColHeight, 200);
}

function alignNewsColHeight() {
    var left = document.querySelector('.news-col-left');
    var right = document.querySelector('.news-col-right');
    if (!left || !right) return;
    // 计算左栏所有子卡片的自然高度总和（含 gap）
    var cards = left.children;
    var totalH = 0;
    for (var i = 0; i < cards.length; i++) {
        totalH += cards[i].getBoundingClientRect().height;
    }
    // 加上卡片间的 gap（20px × (n-1)）
    if (cards.length > 1) totalH += 20 * (cards.length - 1);
    if (totalH > 100) {
        right.style.height = totalH + 'px';
        right.style.maxHeight = totalH + 'px';
    }
}

/**
 * 渲染鲸鱼动态
 */
/**
 * 渲染交易所BTC余额
 */
function renderExchangeBalance(data) {
    const container = document.getElementById('exchangeBalance');
    if (!container || !data.exchanges) return;

    const fmtBtc = (v) => v >= 1000 ? `${(v / 1000).toFixed(1)}K` : v.toLocaleString();
    const maxBalance = Math.max(...data.exchanges.map(e => e.balance));

    // 两列布局: 左=总额+变化 | 右=各交易所
    let leftHtml = '';
    let rightHtml = '';

    // 左侧: 总额 + 历史变化
    leftHtml += `
        <div class="exb-total">
            <span class="exb-total-label">监控总余额</span>
            <span class="exb-total-value">${fmtBtc(data.total)} BTC</span>
        </div>
    `;

    const changes = data.changes || {};
    const windows = [
        { label: '24小时', key: '24h' },
        { label: '7天', key: '7d' },
        { label: '30天', key: '30d' },
    ];

    leftHtml += '<div class="exb-history-row">';
    for (const w of windows) {
        const c = changes[w.key];
        if (c) {
            const pct = c.change_pct.toFixed(2);
            const cls = c.change_pct > 0 ? 'positive' : c.change_pct < 0 ? 'negative' : 'neutral';
            const sign = c.change_pct > 0 ? '+' : '';
            const hint = c.change_pct > 0 ? '流入 (卖压↑)' : c.change_pct < 0 ? '流出 (吸筹↑)' : '持平';
            leftHtml += `
                <div class="exb-history-item">
                    <span class="exb-history-label">${w.label}</span>
                    <span class="exb-history-change ${cls}">${sign}${pct}%</span>
                    <span class="exb-history-hint">${hint}</span>
                </div>
            `;
        } else {
            leftHtml += `
                <div class="exb-history-item">
                    <span class="exb-history-label">${w.label}</span>
                    <span class="exb-history-change neutral">--</span>
                    <span class="exb-history-hint">数据不足</span>
                </div>
            `;
        }
    }
    leftHtml += '</div>';

    // 右侧: 各交易所柱形图
    rightHtml += '<div class="exb-list">';
    for (const ex of data.exchanges) {
        const pct = maxBalance > 0 ? (ex.balance / maxBalance * 100) : 0;
        rightHtml += `
            <div class="exb-item">
                <span class="exb-name">${ex.name}</span>
                <div class="exb-bar-wrap">
                    <div class="exb-bar" style="width: ${pct}%"></div>
                </div>
                <span class="exb-value">${fmtBtc(ex.balance)}</span>
            </div>
        `;
    }
    rightHtml += '</div>';

    container.innerHTML = `
        <div class="exb-layout">
            <div class="exb-col-left">${leftHtml}</div>
            <div class="exb-col-right">${rightHtml}</div>
        </div>
    `;
}

/**
 * 渲染鲸鱼买卖量统计 (24h / 7d / 30d)
 */
function renderWhaleStats(stats) {
    const container = document.getElementById('whaleStats');
    if (!container) return;

    const periods = [
        { key: '24h', label: '24小时' },
        { key: '7d', label: '7天' },
        { key: '30d', label: '30天' }
    ];

    let html = '';
    for (const p of periods) {
        const d = stats[p.key];
        if (!d) continue;
        const ratio = d.buy_ratio || 50;
        const ratioClass = ratio > 52 ? 'bullish' : ratio < 48 ? 'bearish' : 'neutral';

        // Format volume numbers
        const fmtVol = (v) => v >= 1000 ? `${(v / 1000).toFixed(1)}K` : `${Math.round(v)}`;

        html += `
            <div class="whale-stat-item">
                <span class="whale-stat-label">${p.label}</span>
                <div class="whale-stat-ratio ${ratioClass}">${ratio.toFixed(1)}%</div>
                <div class="whale-stat-bar">
                    <div class="whale-stat-bar-fill" style="width: ${ratio}%"></div>
                </div>
                <div class="whale-stat-values">
                    <span class="whale-stat-buy">买 ${fmtVol(d.buy)}</span>
                    <span class="whale-stat-sell">卖 ${fmtVol(d.sell)}</span>
                </div>
            </div>
        `;
    }
    container.innerHTML = html;
}

/**
 * 渲染鲸鱼大额交易列表
 */
function renderWhaleActivity(whales) {
    const container = document.getElementById('whaleActivity');
    if (!container) return;

    // 根据交易类型返回颜色（按金额大小区分，而非买/卖方向）
    // 链上交易无法判断买/卖，仅展示转账金额
    function getWhaleColor(type) {
        if (type.includes('巨鲸')) return 'var(--accent-btc)';      // 橙金 - 超巨额
        if (type.includes('超大额')) return 'var(--accent-orange)';  // 橙色 - 超大额
        if (type.includes('大额')) return 'var(--accent-blue)';      // 蓝色 - 大额
        if (type.includes('中额')) return 'var(--text-secondary)';   // 次级灰 - 中额
        if (type.includes('⏳')) return 'var(--accent-yellow)';      // 黄色 - 待确认
        return 'var(--text-secondary)';                              // 灰色 - 普通
    }

    container.innerHTML = whales.map(item => {
        // 特殊处理 "链接" 类型
        if (item.type === '链接') {
            return `
            <a href="${item.url}" target="_blank" class="whale-item" style="display: block; text-decoration: none; margin-bottom: 8px; padding: 10px; background: var(--bg-glass); border-radius: 6px; font-size: 0.9rem; text-align: center; color: var(--accent-btc); font-weight: 500;">
                ${item.icon || '🔗'} ${item.amount || '查看更多'}
            </a>
            `;
        }

        const typeColor = getWhaleColor(item.type || '');

        return `
        <a href="${item.url}" target="_blank" class="whale-item" style="display: block; text-decoration: none; margin-bottom: 8px; padding: 8px; background: var(--bg-glass); border-radius: 6px; font-size: 0.9rem; transition: background 0.2s;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="color: ${typeColor}; display: flex; align-items: center; gap: 4px;">
                    ${item.icon || ''} ${item.type || '交易'}
                </span>
                <span style="color: var(--text-primary); font-weight: 500;">
                    ${item.amount}
                </span>
            </div>
            <div style="display: flex; justify-content: space-between; margin-top: 4px;">
                <span style="color: var(--text-muted); font-size: 0.75rem;">
                    ${item.time}
                </span>
                <span style="color: var(--text-secondary); font-size: 0.8rem;">
                    ≈ ${item.value_usd}
                </span>
            </div>
        </a>
    `}).join('');
}


/**
 * 渲染宏观经济日历
 */
function renderMacroCalendar(events) {
    const container = document.getElementById('macroCalendar');
    if (!container) return;

    // 影响程度颜色映射
    const impactColor = {
        '高': 'var(--accent-red)',
        '中': 'var(--accent-btc)',
        '低': 'var(--text-muted)'
    };

    container.innerHTML = events.map(item => {
        // 特殊处理 "链接" 类型
        if (item.type === '链接') {
            return `
            <a href="${escapeHtml(safeUrl(item.url))}" target="_blank" class="calendar-item" style="display: block; text-decoration: none; margin-bottom: 10px; padding: 10px; background: var(--bg-glass); border-radius: 8px; text-align: center; color: var(--accent-btc);">
                ${escapeHtml(item.event)}
            </a>
            `;
        }

        const impact = item.impact || '';
        const color = impactColor[impact] || 'var(--text-muted)';
        const hasActual = item.has_actual;
        const isPast = item.is_past;
        const eventStatus = item.event_status || '';
        const actual = escapeHtml(item.actual || '');
        const forecast = escapeHtml(item.forecast || '');
        const previous = escapeHtml(item.previous || '');

        // 状态徽章样式
        let statusBadge = '';
        if (eventStatus === '已公布') {
            statusBadge = `<span style="font-size: 0.65rem; padding: 1px 5px; border-radius: 3px; background: ${hasActual ? 'color-mix(in srgb, var(--accent-green) 14%, transparent)' : 'rgba(128,128,128,0.15)'}; color: ${hasActual ? 'var(--accent-green)' : 'var(--text-muted)'}; white-space: nowrap; margin-left: 6px; border: 1px solid ${hasActual ? 'color-mix(in srgb, var(--accent-green) 28%, transparent)' : 'rgba(128,128,128,0.2)'};">✓ 已公布</span>`;
        } else if (eventStatus === '待公布') {
            statusBadge = `<span style="font-size: 0.65rem; padding: 1px 5px; border-radius: 3px; background: color-mix(in srgb, var(--accent-btc) 8%, transparent); color: var(--accent-btc); white-space: nowrap; margin-left: 6px; border: 1px solid color-mix(in srgb, var(--accent-btc) 22%, transparent);">⏳ 待公布</span>`;
        }

        // 构建数据值行
        let dataRows = '';
        if (hasActual && actual) {
            // 有实际公布值 - 醒目显示
            dataRows += `<div style="margin-top: 5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">`;
            dataRows += `<span style="font-size: 0.82rem; color: var(--accent-green); font-weight: 600; background: color-mix(in srgb, var(--accent-green) 10%, transparent); padding: 1px 6px; border-radius: 4px;">📌 公布: ${actual}</span>`;
            if (forecast) {
                dataRows += `<span style="font-size: 0.75rem; color: var(--text-secondary);">预期: ${forecast}</span>`;
            }
            if (previous) {
                dataRows += `<span style="font-size: 0.75rem; color: var(--text-muted);">前值: ${previous}</span>`;
            }
            dataRows += `</div>`;
        } else if (isPast) {
            // 已过去但没有actual - 显示预期和前值
            dataRows += `<div style="margin-top: 5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">`;
            if (forecast) {
                dataRows += `<span style="font-size: 0.75rem; color: var(--text-secondary);">预期: ${forecast}</span>`;
            }
            if (previous) {
                dataRows += `<span style="font-size: 0.75rem; color: var(--text-muted);">前值: ${previous}</span>`;
            }
            dataRows += `</div>`;
        } else {
            // 未来事件 - 显示预期和前值
            dataRows += `<div style="margin-top: 5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">`;
            if (forecast) {
                dataRows += `<span style="font-size: 0.75rem; color: var(--text-secondary);">预期: ${forecast}</span>`;
            }
            if (previous) {
                dataRows += `<span style="font-size: 0.75rem; color: var(--text-muted);">前值: ${previous}</span>`;
            }
            dataRows += `</div>`;
        }

        // 整体透明度：已公布事件稍暗
        const opacity = isPast && !hasActual ? '0.75' : '1';

        return `
        <div class="calendar-item" style="margin-bottom: 8px; padding: 10px; background: var(--bg-glass); border-radius: 8px; border-left: 3px solid ${color}; opacity: ${opacity};">
            <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                <div style="color: var(--text-primary); font-weight: 500; font-size: 0.9rem; flex: 1; display: flex; align-items: center; flex-wrap: wrap;">
                    ${escapeHtml(item.event || item.title || '未知事件')}
                    ${statusBadge}
                </div>
                <span style="font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; background: ${color}22; color: ${color}; white-space: nowrap; margin-left: 8px;">
                    ${impact}
                </span>
            </div>
            <div style="margin-top: 4px; display: flex; justify-content: space-between; align-items: center;">
                <span style="font-size: 0.8rem; color: var(--text-secondary);">
                    📆 ${item.date || ''}
                </span>
            </div>
            ${dataRows}
        </div>
    `}).join('');
}

/**
 * 渲染加密日历
 */
function renderCryptoCalendar(events) {
    const container = document.getElementById('cryptoCalendar');
    if (!container) return;

    container.innerHTML = events.map(item => `
        <div class="calendar-item" style="margin-bottom: 10px; padding: 10px; background: rgba(255,255,255,0.03); border-radius: 8px;">
            <div style="color: var(--accent-btc); font-weight: 500;">
                ${item.icon || '📅'} ${item.event || item.title || '未知事件'}
                ${item.source ? `<span style="font-size: 0.7rem; color: var(--text-muted); margin-left: 8px;">[${item.source}]</span>` : ''}
            </div>
            <div style="margin-top: 4px; font-size: 0.85rem; color: var(--text-secondary);">
                ${item.status || item.description || ''}
            </div>
            <div style="margin-top: 4px; font-size: 0.75rem; color: var(--text-muted);">
                ${item.date || ''} ${item.type ? '· ' + item.type : ''} ${item.impact ? '· 影响: ' + item.impact : ''}
            </div>
        </div>
    `).join('');
}

// 页面加载后获取资讯数据 + 自动定时刷新
document.addEventListener('DOMContentLoaded', function () {
    // 延迟获取资讯，优先加载主要指标
    setTimeout(fetchNewsData, 3000);

    // 每 10 分钟自动刷新资讯/巨鲸/日历
    setInterval(fetchNewsData, NEWS_REFRESH_INTERVAL);
});

/* ============================================================
   HISTORY DRAWER
   ============================================================ */

/**
 * Open history drawer for the given indicator.
 */
async function openDrawer(name, indicator) {
    currentDrawerIndicator = { name, indicator };

    document.getElementById('drawerTitle').textContent = `${name} 历史走势`;
    document.getElementById('drawerMeta').textContent =
        `当前值: ${indicator.value !== null ? Number(indicator.value).toFixed(2) : '—'} · ${indicator.status || ''}`;

    // Reset tabs to 30d default
    document.querySelectorAll('.dtab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.days === '30');
    });

    document.getElementById('drawerOverlay').classList.add('visible');
    document.getElementById('historyDrawer').classList.add('open');

    await loadDrawerData(name, 30);
}

/**
 * Fetch and render history data. Uses client-side cache.
 */
async function loadDrawerData(name, days) {
    const cacheKey = `${name}:${days}`;

    if (!historyCache[cacheKey]) {
        try {
            const res = await fetch(`/api/history/${encodeURIComponent(name)}?days=${days}`);
            const data = await res.json();
            historyCache[cacheKey] = (data.success && data.dates && data.dates.length > 0) ? data : null;
        } catch (err) {
            console.error(`History fetch failed for ${name}:`, err);
            historyCache[cacheKey] = null;
        }
    }

    renderDrawerChart(historyCache[cacheKey], name);
}

/**
 * Render Chart.js line chart in the drawer canvas.
 */
function renderDrawerChart(data, indicatorName) {
    const canvas = document.getElementById('drawerChart');
    if (!canvas) return;

    if (drawerChartInstance) {
        drawerChartInstance.destroy();
        drawerChartInstance = null;
    }

    if (!data || !data.dates || data.dates.length === 0) {
        canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
        return;
    }

    const thresholdAnnotations = {};
    const thresholds = INDICATOR_THRESHOLDS[indicatorName] || [];
    thresholds.forEach((t, i) => {
        thresholdAnnotations[`line${i}`] = {
            type: 'line',
            yMin: t.value,
            yMax: t.value,
            borderColor: PAL[t.pal],
            borderWidth: 1,
            borderDash: [4, 3],
            label: {
                content: t.label,
                display: true,
                position: 'start',
                color: PAL[t.pal],
                font: { size: 10 }
            }
        };
    });

    drawerChartInstance = new Chart(canvas, {
        type: 'line',
        data: {
            labels: data.dates,
            datasets: [{
                data: data.values,
                borderColor: PAL.btc,
                borderWidth: 1.5,
                pointRadius: 0,
                fill: true,
                backgroundColor: PAL.btc + '18',
                tension: 0.3
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                annotation: { annotations: thresholdAnnotations }
            },
            scales: {
                x: {
                    ticks: { color: PAL.muted, maxTicksLimit: 8, font: { size: 10 } },
                    grid: { color: PAL.grid }
                },
                y: {
                    ticks: { color: PAL.muted, font: { size: 10 } },
                    grid: { color: PAL.grid }
                }
            }
        }
    });
}

function closeDrawer() {
    document.getElementById('historyDrawer').classList.remove('open');
    document.getElementById('drawerOverlay').classList.remove('visible');
}

// Event listeners for drawer
document.getElementById('drawerClose')?.addEventListener('click', closeDrawer);
document.getElementById('drawerOverlay')?.addEventListener('click', closeDrawer);

document.querySelectorAll('.dtab').forEach(btn => {
    btn.addEventListener('click', async () => {
        document.querySelectorAll('.dtab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const days = parseInt(btn.dataset.days, 10);
        if (currentDrawerIndicator) {
            await loadDrawerData(currentDrawerIndicator.name, days);
        }
    });
});

/* ============================================================
   评分历史 & 今日信号变化
   ============================================================ */

let _scoreHistoryDays = 90;
let _scoreHistoryChart = null;

async function fetchScoreHistory(days) {
    try {
        const res = await fetch(`/api/score-history?days=${days || 90}`);
        const data = await res.json();
        if (!data.success) return;
        renderScoreHistoryChart(data.series || [], data.events || []);
        renderScoreTrends(data.trends || null);
        renderSignalChanges(data.changes || {});
        // 180天+ 长视图: 显示回填段战术覆盖率的口径脚注 (诚实标注, 不静默混用)
        const covNote = document.getElementById('scoreHistoryCoverageNote');
        if (covNote) covNote.style.display = (days || 90) >= 180 ? 'block' : 'none';
    } catch (e) {
        console.error('Score history fetch failed:', e);
    }
}

/* 月/季/年尺度评分变化芯片 — trends 来自后端 compute_trends,
   深度不足的档为 null, 如实显示"历史不足"而非空占位 */
function renderScoreTrends(trends) {
    const el = document.getElementById('scoreTrendChips');
    if (!el) return;
    if (!trends) { el.innerHTML = ''; return; }
    const fmt = (t) => {
        if (!t) return '<span style="opacity:0.55;">— 历史不足</span>';
        const up = t.delta >= 0;
        const col = Math.abs(t.delta) < 0.005 ? 'var(--text-muted)'
                    : (up ? 'var(--positive, #26a17b)' : 'var(--negative, #e05c5c)');
        const arrow = Math.abs(t.delta) < 0.005 ? '→' : (up ? '↑' : '↓');
        const tip = `基准 ${t.base_date}: ${Number(t.base).toFixed(2)}`;
        return `<span style="color:${col};" title="${tip}">${up ? '+' : ''}${Number(t.delta).toFixed(2)} ${arrow}</span>`;
    };
    const c = trends.cycle || {}, ta = trends.tactical || {};
    el.innerHTML =
        `<span style="opacity:0.8;">周期分变化</span> ` +
        `30日 ${fmt(c.d30)} · 90日 ${fmt(c.d90)} · 1年 ${fmt(c.d365)}` +
        `<span style="margin:0 8px; opacity:0.4;">|</span>` +
        `<span style="opacity:0.8;">战术分</span> 7日 ${fmt(ta.d7)} · 30日 ${fmt(ta.d30)}`;
}

function renderScoreHistoryChart(series, events) {
    const canvas = document.getElementById('scoreHistoryChart');
    const emptyEl = document.getElementById('scoreHistoryEmpty');
    if (!canvas) return;

    if (!series || series.length < 2) {
        canvas.style.display = 'none';
        if (emptyEl) emptyEl.style.display = 'block';
        return;
    }
    canvas.style.display = '';
    if (emptyEl) emptyEl.style.display = 'none';

    if (_scoreHistoryChart) {
        _scoreHistoryChart.destroy();
        _scoreHistoryChart = null;
    }

    const labels = series.map(s => s.date.slice(5)); // MM-DD
    const scores = series.map(s => s.total_score);
    const prices = series.map(s => s.btc_price);
    const tacticals = series.map(s => (typeof s.tactical_score === 'number' ? s.tactical_score : null));
    const hasTactical = tacticals.some(v => v !== null);

    // 事件标记 (上穿档位/转负/滞回换档): 散点叠加, ▲买入侧 / ▼避险侧。
    // 事件研究诚实口径: 非胜率信号, tooltip 携带完整说明。
    const dateIdx = {};
    series.forEach((s, i) => { dateIdx[s.date] = i; });
    const evPoint = ev => {
        const i = dateIdx[ev.date];
        if (i === undefined) return null;
        return { x: labels[i], y: series[i].total_score, _ev: ev };
    };
    const buyPts = (events || []).filter(e => e.side === 'buy').map(evPoint).filter(Boolean);
    const riskPts = (events || []).filter(e => e.side === 'risk').map(evPoint).filter(Boolean);
    const eventDatasets = [];
    if (buyPts.length) {
        eventDatasets.push({
            label: '事件·买入侧', data: buyPts, type: 'scatter', showLine: false,
            pointStyle: 'triangle', pointRadius: 7, pointHoverRadius: 9, rotation: 0,
            borderColor: '#00d26a', backgroundColor: '#00d26a99', yAxisID: 'yScore', order: -1,
        });
    }
    if (riskPts.length) {
        eventDatasets.push({
            label: '事件·避险侧', data: riskPts, type: 'scatter', showLine: false,
            pointStyle: 'triangle', pointRadius: 7, pointHoverRadius: 9, rotation: 180,
            borderColor: '#ff4444', backgroundColor: '#ff444499', yAxisID: 'yScore', order: -1,
        });
    }

    _scoreHistoryChart = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: '周期分',
                    data: scores,
                    borderColor: PAL.btc,
                    backgroundColor: PAL.btc + '18',
                    borderWidth: 2,
                    pointRadius: series.length > 60 ? 0 : 2,
                    fill: true,
                    tension: 0.3,
                    yAxisID: 'yScore',
                },
                ...(hasTactical ? [{
                    label: '战术分',
                    data: tacticals,
                    borderColor: PAL.purple,
                    borderWidth: 1.5,
                    pointRadius: 0,
                    fill: false,
                    tension: 0.3,
                    yAxisID: 'yScore',
                }] : []),
                {
                    label: 'BTC 价格',
                    data: prices,
                    borderColor: PAL.blue,
                    borderWidth: 1.5,
                    borderDash: [4, 3],
                    pointRadius: 0,
                    fill: false,
                    tension: 0.3,
                    yAxisID: 'yPrice',
                },
                ...eventDatasets
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: {
                    display: true,
                    labels: { color: PAL.muted, font: { size: 10 }, boxWidth: 16 }
                },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            if (ctx.raw && ctx.raw._ev) return `${ctx.raw._ev.label}`;
                            return `${ctx.dataset.label}: ${ctx.formattedValue}`; // 复刻默认格式
                        },
                        afterBody: (items) => {
                            const evItem = items.find(it => it.raw && it.raw._ev);
                            if (evItem) {
                                // 事件研究诚实口径: 每个事件标记必须携带统计力说明
                                const note = evItem.raw._ev.note || '';
                                return note.match(/.{1,26}/g) || [];
                            }
                            const i = items[0].dataIndex;
                            return series[i].recommendation ? `建议: ${series[i].recommendation}` : '';
                        }
                    }
                },
                annotation: {
                    annotations: {
                        zero: {
                            type: 'line', yMin: 0, yMax: 0, yScaleID: 'yScore',
                            borderColor: PAL.muted, borderWidth: 1, borderDash: [2, 4]
                        },
                        buy: {
                            type: 'line', yMin: 0.30, yMax: 0.30, yScaleID: 'yScore',
                            borderColor: PAL.up + '66', borderWidth: 1, borderDash: [4, 4],
                            label: { content: '偏多 +0.30', display: true, position: 'start', color: PAL.up, font: { size: 9 }, backgroundColor: 'transparent' }
                        },
                        reduce: {
                            type: 'line', yMin: -0.12, yMax: -0.12, yScaleID: 'yScore',
                            borderColor: PAL.down + '66', borderWidth: 1, borderDash: [4, 4],
                            label: { content: '减配 -0.12', display: true, position: 'start', color: PAL.down, font: { size: 9 }, backgroundColor: 'transparent' }
                        }
                    }
                }
            },
            scales: {
                x: {
                    ticks: { color: PAL.muted, maxTicksLimit: 10, font: { size: 10 } },
                    grid: { display: false }
                },
                yScore: {
                    position: 'left',
                    min: -1, max: 1,
                    ticks: { color: PAL.btc, font: { size: 10 } },
                    grid: { color: 'rgba(128,128,128,0.12)' }
                },
                yPrice: {
                    position: 'right',
                    ticks: {
                        color: PAL.blue, font: { size: 10 },
                        callback: (v) => '$' + (v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v)
                    },
                    grid: { display: false }
                }
            }
        }
    });
}

function renderSignalChanges(changes) {
    const container = document.getElementById('signalChanges');
    const meta = document.getElementById('signalChangesMeta');
    if (!container) return;

    if (!changes || !changes.prev_date) {
        container.innerHTML = '<p style="color:var(--text-muted); font-size:0.85rem;">📭 暂无对比基准，明天起自动显示与前一日的信号变化</p>';
        if (meta) meta.textContent = '';
        return;
    }

    if (meta) meta.textContent = `vs ${changes.prev_date}`;

    let html = '';

    // 综合评分变化
    const t = changes.total;
    if (t) {
        const deltaCls = t.delta > 0 ? 'positive' : t.delta < 0 ? 'negative' : 'neutral';
        const deltaColor = t.delta > 0 ? 'var(--accent-green)' : t.delta < 0 ? 'var(--accent-red)' : 'var(--text-muted)';
        const arrow = t.delta > 0 ? '▲' : t.delta < 0 ? '▼' : '—';
        html += `
            <div class="signal-change-item total ${t.band_changed ? 'band-changed' : ''}">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-weight:600; color:var(--text-primary);">综合评分</span>
                    <span style="color:${deltaColor}; font-weight:600;">
                        ${t.prev_score.toFixed(2)} → ${t.curr_score.toFixed(2)} ${arrow}
                    </span>
                </div>
                ${t.band_changed
                    ? `<div style="margin-top:4px; font-size:0.8rem; color:var(--accent-btc);">⚡ 档位变化: ${t.prev_band} → <b>${t.curr_band}</b></div>`
                    : `<div style="margin-top:4px; font-size:0.78rem; color:var(--text-muted);">档位维持「${t.curr_band}」</div>`}
            </div>`;
    }

    // 指标跨档变化
    const inds = changes.indicators || [];
    if (inds.length === 0) {
        html += '<p style="color:var(--text-muted); font-size:0.82rem; margin-top:8px;">✅ 各指标信号与前一日一致，无跨档变化</p>';
    } else {
        html += inds.map(c => {
            const isBull = c.direction === 'bullish';
            const color = isBull ? 'var(--accent-green)' : 'var(--accent-red)';
            const icon = isBull ? '🟢' : '🔴';
            const dirText = isBull ? '转多' : '转空';
            return `
                <div class="signal-change-item">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="color:var(--text-primary); font-size:0.85rem;">${icon} ${c.name}</span>
                        <span style="color:${color}; font-size:0.8rem; font-weight:600;">
                            ${c.prev_score} → ${c.curr_score}（${dirText}）
                        </span>
                    </div>
                    ${c.curr_status ? `<div style="margin-top:3px; font-size:0.74rem; color:var(--text-muted);">${c.curr_status}</div>` : ''}
                </div>`;
        }).join('');
    }

    container.innerHTML = html;
}

/* ============================================================
   衍生品杠杆面板
   ============================================================ */

let _derivOiChart = null;

async function fetchDerivativesData() {
    await fetchWithComputingPoll('/api/derivatives', {
        pollKey: '_derivativesPollTimer',
        maxWaitMs: 3 * 60 * 1000,
        delays: [5000, 10000, 20000, 30000],
        onData: renderDerivatives,
        onError: (e) => console.error('Derivatives fetch failed:', e),
    });
}

function _fmtUsdB(v) {
    if (v === null || v === undefined) return '--';
    if (Math.abs(v) >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
    if (Math.abs(v) >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
    return '$' + Math.round(v).toLocaleString();
}

function _pctSpan(v, inverse) {
    if (v === null || v === undefined) return '<span style="color:var(--text-muted);">--</span>';
    const positive = inverse ? v < 0 : v > 0;
    const color = v === 0 ? 'var(--text-muted)' : positive ? 'var(--accent-green)' : 'var(--accent-red)';
    const sign = v > 0 ? '+' : '';
    return `<span style="color:${color}; font-weight:600;">${sign}${v.toFixed(2)}%</span>`;
}

function renderDerivatives(data) {
    const panel = document.getElementById('derivativesPanel');
    if (!panel) return;

    const updatedEl = document.getElementById('derivUpdatedAt');
    if (updatedEl && data.updated_at) updatedEl.textContent = `更新于 ${data.updated_at}`;

    const regime = data.regime || {};
    const toneColors = {
        bullish: 'var(--accent-green)', bearish: 'var(--accent-red)',
        warning: 'var(--accent-orange)', neutral: 'var(--text-muted)'
    };
    const rColor = toneColors[regime.tone] || 'var(--text-muted)';

    // ── 顶部：行情性质徽章 ──
    const regimeHtml = `
        <div class="deriv-regime" style="border-left: 3px solid ${rColor};">
            <div class="deriv-regime-label" style="color:${rColor};">${regime.label || '--'}</div>
            <div class="deriv-regime-desc">${regime.desc || ''}</div>
        </div>`;

    // ── 统计卡片 ──
    const oi = data.oi, fd = data.funding, ls = data.long_short, liq = data.liquidations, px = data.price;
    const cards = [];

    if (px) {
        cards.push(`
            <div class="deriv-stat">
                <span class="deriv-stat-label">BTC 24h</span>
                <span class="deriv-stat-value">${_pctSpan(px.change_24h_pct)}</span>
                <span class="deriv-stat-sub">$${Math.round(px.last).toLocaleString()}</span>
            </div>`);
    }
    if (oi) {
        cards.push(`
            <div class="deriv-stat">
                <span class="deriv-stat-label">未平仓合约 OI</span>
                <span class="deriv-stat-value" style="color:var(--text-primary);">${_fmtUsdB(oi.current_usd)}</span>
                <span class="deriv-stat-sub">24h ${_pctSpan(oi.change_24h_pct)} · 7d ${_pctSpan(oi.change_7d_pct)}</span>
            </div>`);
    }
    if (fd) {
        const frColor = fd.rate_pct > 0.03 ? 'var(--accent-orange)' : fd.rate_pct < -0.03 ? 'var(--accent-green)' : 'var(--text-muted)';
        cards.push(`
            <div class="deriv-stat">
                <span class="deriv-stat-label">资金费率 (8h)</span>
                <span class="deriv-stat-value" style="color:${frColor};">${fd.rate_pct.toFixed(4)}%</span>
                <span class="deriv-stat-sub">年化 ${fd.annualized_pct}%${fd.next_time ? ` · 下次 ${fd.next_time}` : ''}</span>
            </div>`);
    }
    if (ls) {
        const lsColor = ls.ratio > 1.5 ? 'var(--accent-orange)' : ls.ratio < 0.7 ? 'var(--accent-green)' : 'var(--text-muted)';
        cards.push(`
            <div class="deriv-stat">
                <span class="deriv-stat-label">多空账户比</span>
                <span class="deriv-stat-value" style="color:${lsColor};">${ls.ratio}</span>
                <span class="deriv-stat-sub">多 ${ls.long_pct}% / 空 ${ls.short_pct}%</span>
            </div>`);
    }
    if (liq && liq.total_usd > 0) {
        const longShare = liq.total_usd > 0 ? (liq.long_usd / liq.total_usd * 100) : 50;
        cards.push(`
            <div class="deriv-stat" title="${liq.note || ''}">
                <span class="deriv-stat-label">清算样本</span>
                <span class="deriv-stat-value" style="color:var(--text-primary);">${_fmtUsdB(liq.total_usd)}</span>
                <span class="deriv-stat-sub">
                    <span style="color:var(--accent-red);">多单 ${longShare.toFixed(0)}%</span> /
                    <span style="color:var(--accent-green);">空单 ${(100 - longShare).toFixed(0)}%</span>
                </span>
            </div>`);
    }

    panel.innerHTML = `
        ${regimeHtml}
        <div class="deriv-stats-row">${cards.join('')}</div>
        ${oi && oi.history && oi.history.length > 2 ? `
            <div style="margin-top:14px;">
                <div style="font-size:0.78rem; color:var(--text-muted); margin-bottom:6px;">
                    OI 走势（30 天 · ${oi.source}）vs BTC 价格
                </div>
                <div style="position:relative; height:200px;">
                    <canvas id="derivOiChart"></canvas>
                </div>
            </div>` : ''}
    `;

    // ── OI 历史图 ──
    if (oi && oi.history && oi.history.length > 2) {
        if (_derivOiChart) { _derivOiChart.destroy(); _derivOiChart = null; }
        const canvas = document.getElementById('derivOiChart');
        if (canvas) {
            _derivOiChart = new Chart(canvas, {
                type: 'line',
                data: {
                    labels: oi.history.map(h => h.date),
                    datasets: [
                        {
                            label: 'OI (USD)',
                            data: oi.history.map(h => h.oi_usd),
                            borderColor: PAL.btc,
                            backgroundColor: PAL.btc + '15',
                            borderWidth: 2,
                            pointRadius: 0,
                            fill: true,
                            tension: 0.3,
                            yAxisID: 'yOi',
                        },
                        {
                            label: 'BTC 价格',
                            data: oi.history.map(h => h.price),
                            borderColor: PAL.blue,
                            borderWidth: 1.5,
                            borderDash: [4, 3],
                            pointRadius: 0,
                            fill: false,
                            tension: 0.3,
                            yAxisID: 'yPx',
                            spanGaps: true,
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: true, labels: { color: PAL.muted, font: { size: 10 }, boxWidth: 16 } },
                        tooltip: {
                            callbacks: {
                                label: (item) => item.dataset.yAxisID === 'yOi'
                                    ? `OI: ${_fmtUsdB(item.raw)}`
                                    : `价格: $${Math.round(item.raw).toLocaleString()}`
                            }
                        }
                    },
                    scales: {
                        x: { ticks: { color: PAL.muted, maxTicksLimit: 10, font: { size: 10 } }, grid: { display: false } },
                        yOi: {
                            position: 'left',
                            ticks: { color: PAL.btc, font: { size: 10 }, callback: (v) => '$' + (v / 1e9).toFixed(0) + 'B' },
                            grid: { color: 'rgba(128,128,128,0.12)' }
                        },
                        yPx: {
                            position: 'right',
                            ticks: { color: PAL.blue, font: { size: 10 }, callback: (v) => '$' + (v / 1000).toFixed(0) + 'K' },
                            grid: { display: false }
                        }
                    }
                }
            });
        }
    }
}

/* ============================================================
   BTC 期权 · 波动率与情绪 (DVOL/偏斜/PC/期限/最大痛点)
   ============================================================ */

let optSparkSpan = '90d';   // DVOL sparkline 跨度状态(跨 10min 自动重渲染保持)
let _optionsData = null;    // 最近一次 /api/options 数据(切换跨度免重新 fetch)

function setOptSparkSpan(span) {
    optSparkSpan = span;
    if (_optionsData) renderOptionsCard(_optionsData);
}

async function renderOptions() {
    let a;
    try { a = await (await fetch('/api/options')).json(); }
    catch (e) { return; }
    if (!a || a.dvol_now == null) return;
    _optionsData = a;
    renderOptionsCard(a);
}

function renderOptionsCard(a) {
    const el = document.getElementById('optionsCard');
    if (!el) return;
    el.style.display = '';
    const winYrs = a.dvol_window_days ? a.dvol_window_days / 365 : null;
    const winLbl = winYrs == null ? '分位' : (winYrs >= 3.9 ? '4年分位' : `${winYrs.toFixed(1)}年分位`);
    const hasFull = (a.spark_full || []).length >= 2;
    const useFull = optSparkSpan === 'all' && hasFull;
    const spark = useFull ? a.spark_full : (a.spark || []);
    let pts = '';
    const W = 320, H = 40;
    if (spark.length >= 2) {
        const min = Math.min(...spark), max = Math.max(...spark), pad = 3;
        pts = spark.map((v, i) =>
            `${(i / (spark.length - 1) * W).toFixed(1)},${(H - pad - (v - min) / (max - min || 1) * (H - 2 * pad)).toFixed(1)}`).join(' ');
    }
    const caption = useFull ? `自 ${a.spark_full_start || '2021-03'} · DVOL 全史` : '近 90 天';
    const toggle = hasFull ? `
        <span class="opt-span-toggle">
          <button class="opt-span-btn ${useFull ? '' : 'active'}" onclick="setOptSparkSpan('90d')">90d</button>
          <button class="opt-span-btn ${useFull ? 'active' : ''}" onclick="setOptSparkSpan('all')">全部</button>
        </span>` : '';
    const tile = (label, val, sub) =>
        `<div class="opt-tile"><div class="opt-tile-label">${label}</div>
         <div class="opt-tile-val">${val}</div><div class="opt-tile-sub">${sub}</div></div>`;
    el.innerHTML = `
      <div style="text-align:right; font-size:0.72rem; color:var(--text-muted); margin-bottom:4px;">Deribit · ${a.updated_at || ''}</div>
      <div class="opt-hero">
        <div><div class="opt-hero-label">DVOL 隐含波动率 · ${winLbl}</div>
          <div class="opt-hero-num">${a.dvol_now}<span class="opt-hero-unit"> 当前 IV</span></div></div>
        <div style="text-align:right;">
          <span class="opt-pct">${winLbl} ${a.dvol_pct == null ? '—' : a.dvol_pct + '%'}</span></div>
      </div>
      <div class="opt-spark-bar"><span class="opt-spark-caption">${caption}</span>${toggle}</div>
      ${pts ? `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none" class="opt-spark" role="img" aria-label="DVOL 隐含波动率走势">
        <polyline points="${pts}" fill="none" stroke="var(--accent-btc,#f0864a)" stroke-width="1.5"
          stroke-linejoin="round" stroke-linecap="round"/></svg>` : ''}
      <div class="opt-grid">
        ${tile('翼部偏斜(价外5-15%)', a.skew_wing == null ? '—' : (a.skew_wing > 0 ? '+' : '') + a.skew_wing, a.skew_wing == null ? '' : (a.skew_wing > 0 ? '看跌溢价·防御' : '看涨溢价'))}
        ${tile('Put/Call OI', a.put_call_oi == null ? '—' : a.put_call_oi, a.put_call_oi == null ? '' : (a.put_call_oi < 0.45 ? '低于常态区间(≈0.5–0.7)' : a.put_call_oi > 0.8 ? '高于常态区间(≈0.5–0.7)' : '常态区间(≈0.5–0.7)'))}
        ${tile('期限结构', a.term_slope == null ? '—' : (a.term_slope > 0 ? '+' : '') + a.term_slope, a.term_slope == null ? '' : (a.term_slope > 0 ? 'contango' : 'backwardation'))}
        ${tile('最大痛点', a.max_pain == null ? '—' : '$' + a.max_pain.toLocaleString(), a.max_pain_exp || '')}
      </div>
      <div class="opt-foot">全部仅展示 · DVOL 分位回测 IC 不足（|IC|≤0.05 且方向不稳），未计入战术分 · 当下快照 · 现价 $${(a.spot || 0).toLocaleString()}</div>`;
}

/* ============================================================
   BTC 概率分布（期权隐含风险中性密度）
   ============================================================ */

// HTML 转义：外部字符串（如 Polymarket 市场标题、新闻/RSS 标题、宏观事件名）拼进 innerHTML 前必须过一遍，防 XSS
function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

// 链接白名单：只放行 http(s)，挡掉 javascript:/data: 等 href 注入向量；非法值回退为无跳转的 '#'
function safeUrl(u) {
    const s = String(u == null ? '' : u).trim();
    return /^https?:\/\//i.test(s) ? s : '#';
}

async function renderProbDist() {
    const el = document.getElementById('probDistCard');
    if (!el) return;
    let a; try { a = await (await fetch('/api/probdist')).json(); } catch (e) { return; }
    if (!a || a.median == null || !Array.isArray(a.pdf) || a.pdf.length < 3) return;
    el.style.display = '';
    const W = 640, H = 190, padT = 14, padB = 24, padX = 10;
    // 裁掉 <0.2% 峰值的零密度平尾(实测右侧 40%+ 宽度是平尾, 有效分布被挤扁)
    const peak = Math.max(...a.pdf.map(p => p[1])) || 1;
    const vis = a.pdf.filter(p => p[1] > peak * 0.002);
    const xmin = Math.min(...vis.map(p => p[0])), xmax = Math.max(...vis.map(p => p[0]));
    const shown = a.pdf.filter(p => p[0] >= xmin && p[0] <= xmax);
    const dmax = Math.max(...shown.map(p => p[1])) || 1;
    const X = v => padX + (Math.min(Math.max(v, xmin), xmax) - xmin) / (xmax - xmin || 1) * (W - 2 * padX);
    const Y = d => (H - padB) - d / dmax * (H - padT - padB);
    const area = `M ${X(shown[0][0]).toFixed(1)} ${H - padB} ` +
        shown.map(p => `L ${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join(' ') +
        ` L ${X(shown[shown.length - 1][0]).toFixed(1)} ${H - padB} Z`;
    const line = 'M ' + shown.map(p => `${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join(' L ');
    const acc = 'var(--accent-btc,#f0864a)';
    const band = (a.p16 != null && a.p84 != null)
        ? `<rect x="${X(a.p16).toFixed(1)}" y="${padT}" width="${(X(a.p84) - X(a.p16)).toFixed(1)}" height="${H - padB - padT}" fill="${acc}" opacity="0.10"/>` : '';
    const spotX = X(a.spot);
    // 自适应刻度步长: 目标 ≤9 个刻度
    const span = xmax - xmin;
    const tickStep = span > 60000 ? 10000 : span > 25000 ? 5000 : 2000;
    const ticks = [];
    for (let k = Math.ceil(xmin / tickStep) * tickStep; k <= xmax; k += tickStep)
        ticks.push(`<text x="${X(k).toFixed(1)}" y="${H - 8}" font-size="10" fill="var(--text-muted)" text-anchor="middle">$${k / 1000}k</text>`);
    const chip = (l, v) => `<span class="prob-chip">${l} <b>${v}</b></span>`;
    const tailChips = (a.tails || []).filter(t => t.P_gt > 2 && t.P_gt < 98)
        .map(t => chip(`P(&gt;$${(t.K / 1000)}k)`, t.P_gt + '%')).slice(0, 5).join('');
    // Kalshi 年底真金分布(独立 bar strip; 期限与上图不同, 不叠轴)
    let kalRow = '';
    const kal = a.kalshi;
    if (kal && Array.isArray(kal.buckets) && kal.buckets.length >= 10) {
        const bs = kal.buckets, KW = 640, KH = 56, kPadB = 13;
        const pmax = Math.max(...bs.map(b => b.p)) || 1;
        const bw = KW / bs.length;
        const bars = bs.map((b, i) => {
            const h = Math.max(1, (b.p / pmax) * (KH - kPadB - 4));
            const lbl = b.lo == null ? `<$${b.hi / 1000}k` : b.hi == null ? `>$${b.lo / 1000}k`
                : `$${b.lo / 1000}k–$${b.hi / 1000}k`;
            return `<rect x="${(i * bw + 1).toFixed(1)}" y="${(KH - kPadB - h).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}" fill="${acc}" opacity="0.55"><title>${lbl}: ${b.p}%</title></rect>`;
        }).join('');
        const kticks = bs.map((b, i) => (i % 5 === 2 && b.lo != null)
            ? `<text x="${(i * bw + bw / 2).toFixed(1)}" y="${KH - 2}" font-size="9" fill="var(--text-muted)" text-anchor="middle">$${b.lo / 1000}k</text>` : '').join('');
        kalRow = `
      <div class="prob-kalshi-head"><span class="prob-lbl">年底分布 · Kalshi 真金市场(截至 ${kal.close || '—'})</span>
        <span class="prob-muted">成交 ${(kal.volume / 1e6).toFixed(1)}M 张 · 期限与上图不同, 不可直接对照</span></div>
      <svg viewBox="0 0 ${KW} ${KH}" width="100%" class="prob-svg prob-kalshi" role="img" aria-label="Kalshi BTC 年底价格分布">${bars}${kticks}</svg>`;
    }
    const poly = (a.polymarket || []);
    const polyRow = poly.length
        ? `<div class="prob-chips" style="margin-top:6px;"><span class="prob-lbl" style="align-self:center;">Polymarket:</span>` +
          poly.slice(0, 3).map(m => `<span class="prob-poly">${escapeHtml(m.q)}${m.yes != null ? ' · ' + m.yes + '%' : ''}${m.end ? ' · 至 ' + escapeHtml(m.end) : ''}</span>`).join('') + `</div>`
        : '';
    el.innerHTML = `
      <div class="decision-card-title">📊 BTC 概率分布 · 风险中性
        <span class="decision-freq" style="font-weight:400;">Deribit 期权 · 到期 ${a.expiry || '—'} (${a.days ?? '—'}天)</span></div>
      <div class="prob-stats">
        <div><div class="prob-lbl">68% 区间 (P16–P84)</div><div class="prob-val">$${(a.p16 / 1000).toFixed(1)}k–$${(a.p84 / 1000).toFixed(1)}k</div></div>
        <div><div class="prob-lbl">中位</div><div class="prob-val">$${(a.median / 1000).toFixed(1)}k</div></div>
        <div><div class="prob-lbl">上涨概率</div><div class="prob-val">${a.p_up}%</div></div>
        <div><div class="prob-lbl">预期振幅</div><div class="prob-val">±${a.expected_move_pct}%</div></div>
      </div>
      <svg viewBox="0 0 ${W} ${H}" width="100%" class="prob-svg" role="img" aria-label="BTC 到期价格风险中性概率密度">
        ${band}
        <path d="${area}" fill="${acc}" opacity="0.18"/>
        <path d="${line}" fill="none" stroke="${acc}" stroke-width="2" stroke-linejoin="round"/>
        <line x1="${spotX.toFixed(1)}" y1="${padT}" x2="${spotX.toFixed(1)}" y2="${H - padB}" stroke="var(--text-secondary)" stroke-width="1" stroke-dasharray="3 3" opacity="0.6"/>
        <text x="${(spotX + 4).toFixed(1)}" y="${padT + 10}" font-size="11" fill="var(--text-secondary)">现价 $${(a.spot / 1000).toFixed(1)}k</text>
        ${ticks.join('')}
      </svg>
      <div class="prob-chips">${tailChips}</div>
      ${kalRow}
      ${polyRow}
      <div class="prob-foot">上图: 风险中性概率 · <b>非真实世界预测</b>（期权定价含风险溢价，下行概率偏高）· Breeden-Litzenberger 于 Deribit 期权链 · Kalshi/Polymarket 为真实世界众筹概率(P 测度), 尾部或含 favorite-longshot 高估, <b>与上图之差不构成任何信号</b> · 仅展示</div>`;
}

/* ============================================================
   ETF 日度净流入
   ============================================================ */

function renderEtfFlow(etf) {
    const panel = document.getElementById('etfFlowPanel');
    if (!panel) return;

    if (!etf || !etf.series || etf.series.length === 0) {
        // 数据源不可用，保留原有链接引导文案
        return;
    }

    const series = etf.series;
    const maxAbs = Math.max(...series.map(s => Math.abs(s.total)), 1);
    const latest = etf.latest || {};
    const latestColor = (latest.total || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
    const sum5Color = (etf.sum_5d || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';

    const fmtM = (v) => {
        if (v === null || v === undefined) return '--';
        const sign = v > 0 ? '+' : '';
        return Math.abs(v) >= 1000 ? `${sign}${(v / 1000).toFixed(2)}B` : `${sign}${v.toFixed(0)}M`;
    };

    // 柱状图（纯 CSS，红绿柱以中线为基准）
    const barsHtml = series.map(s => {
        const positive = s.total >= 0;
        const hPct = Math.min(100, Math.abs(s.total) / maxAbs * 100);
        const color = positive ? 'var(--accent-green)' : 'var(--accent-red)';
        const fundsTip = Object.entries(s.funds || {})
            .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
            .slice(0, 5)
            .map(([k, v]) => `${k}: ${v > 0 ? '+' : ''}${v}M`)
            .join('  ');
        return `
            <div class="etf-bar-col" title="${s.date}  净流${positive ? '入' : '出'} ${Math.abs(s.total).toFixed(0)}M${fundsTip ? '\n' + fundsTip : ''}">
                <div class="etf-bar-half top">${positive ? `<div class="etf-bar" style="height:${hPct}%; background:${color};"></div>` : ''}</div>
                <div class="etf-bar-half bottom">${!positive ? `<div class="etf-bar" style="height:${hPct}%; background:${color};"></div>` : ''}</div>
                <span class="etf-bar-date">${s.date.slice(3)}</span>
            </div>`;
    }).join('');

    panel.innerHTML = `
        <div class="etf-flow-stats">
            <div class="etf-flow-stat">
                <span class="etf-flow-label">最新 (${latest.date || '--'})</span>
                <span class="etf-flow-value" style="color:${latestColor};">${fmtM(latest.total)}</span>
            </div>
            <div class="etf-flow-stat">
                <span class="etf-flow-label">近5日累计</span>
                <span class="etf-flow-value" style="color:${sum5Color};">${fmtM(etf.sum_5d)}</span>
            </div>
            <div class="etf-flow-stat">
                <span class="etf-flow-label">${series.length}日累计</span>
                <span class="etf-flow-value" style="color:${(etf.sum_total || 0) >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'};">${fmtM(etf.sum_total)}</span>
            </div>
            ${etf.cum_total ? `
            <div class="etf-flow-stat">
                <span class="etf-flow-label">上市累计</span>
                <span class="etf-flow-value" style="color:var(--text-primary);">$${(etf.cum_total / 1000).toFixed(1)}B</span>
            </div>` : ''}
        </div>
        <div class="etf-bars-row">${barsHtml}</div>
        <p style="margin:8px 0 0; color:var(--text-muted); font-size:0.68rem; text-align:right;">
            单位: 百万美元 · 数据源 ${etf.source} · 更新 ${etf.updated_at}
        </p>
    `;
}

/* ============================================================
   DAT 公司持仓（CoinGecko 动态数据）
   ============================================================ */

function renderDatHoldings(data) {
    const panel = document.getElementById('datHoldings');
    if (!panel) return;

    if (!data || !data.companies || data.companies.length === 0) {
        // API 失败 → 保留 HTML 内置静态回退表
        return;
    }

    const hdr = (txt, align) => `<span style="color: var(--text-muted); font-size: 0.7rem; padding-bottom: 4px; border-bottom: 1px solid var(--border-color);${align ? ' text-align: right;' : ''}">${txt}</span>`;
    const rows = data.companies.map(c => `
        <span title="${c.pct_supply ? `占总供应量 ${c.pct_supply}%` : ''}">${escapeHtml(c.name)}</span>
        <span style="text-align:right; color:var(--accent-btc);">${c.holdings.toLocaleString()}</span>
        <span style="text-align:right; color:var(--text-muted);">${escapeHtml(c.symbol)}</span>
    `).join('');

    panel.innerHTML = `
        <div style="display: grid; grid-template-columns: 1fr auto auto; gap: 4px 12px; align-items: center;">
            ${hdr('公司')}${hdr('持仓 (BTC)', true)}${hdr('代码', true)}
            ${rows}
        </div>
        <p style="margin: 10px 0 0; color: var(--text-muted); font-size: 0.68rem; text-align: right;">
            上市公司合计 ${data.total.toLocaleString()} BTC · CoinGecko · ${data.updated_at}
        </p>
    `;
}
