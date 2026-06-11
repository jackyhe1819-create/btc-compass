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

// Threshold reference lines for key indicators
const INDICATOR_THRESHOLDS = {
    "Ahr999": [
        { value: 0.45, label: "定投线", color: "#00ff88" },
        { value: 1.2,  label: "顶部区", color: "#ff4466" }
    ],
    "Mayer Multiple": [
        { value: 1.0, label: "均值",     color: "#ffcc00" },
        { value: 2.4, label: "历史高位", color: "#ff4466" }
    ],
    "恐惧贪婪指数": [
        { value: 20, label: "极度恐惧", color: "#00ff88" },
        { value: 80, label: "极度贪婪", color: "#ff4466" }
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
    if (typeof initTradingViewWidget === 'function') {
        initTradingViewWidget(theme === 'warm' ? 'light' : 'dark');
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

    // 指标总览高度与"更多看板"对齐
    function syncSummaryHeight() {
        const ext = document.querySelector('.ext-links-container');
        const summary = document.querySelector('.summary-table-container');
        if (!ext || !summary) return;
        const extH = ext.getBoundingClientRect().height;
        if (extH > 0) {
            summary.style.height = extH + 'px';
        }
    }
    // 页面渲染后执行，并在窗口大小变化时重算
    setTimeout(syncSummaryHeight, 100);
    window.addEventListener('resize', syncSummaryHeight);

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
            grid.innerHTML = '<p style="color:#888;">暂无数据</p>';
            return;
        }

        grid.innerHTML = data.sources.map(src => {
            const items = (src.items || []).slice(0, 8);
            const badge = src.priority === 'critical'
                ? '<span class="builders-badge critical">核心</span>'
                : '<span class="builders-badge high">重要</span>';
            const itemsHtml = items.length > 0
                ? items.map(item => `
                    <a href="${item.url}" target="_blank" rel="noopener noreferrer" class="builders-item">
                        <div class="builders-item-title">${item.title}</div>
                        ${item.summary ? `<div class="builders-item-summary">${item.summary}</div>` : ''}
                        ${item.date ? `<div class="builders-item-date">${item.date}</div>` : ''}
                    </a>`).join('')
                : `<p style="color:#666;font-size:0.82rem;padding:8px 0;">${src.error ? '加载失败' : '暂无内容'}</p>`;

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
        body.innerHTML = '<p style="color:#888;font-size:0.85rem;">暂无摘要数据</p>';
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
                  <div class="bs-cross-item" title="${t.sources.join(', ')}">
                    <span class="bs-cross-icon">${t.icon}</span>
                    <span class="bs-cross-topic">${t.topic}</span>
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
                      <span class="bs-topic-name">${h.topic}</span>
                      <span class="bs-topic-count">${h.count} 条</span>
                    </div>
                    ${h.takeaway ? `<div class="bs-topic-takeaway">${h.takeaway}</div>` : ''}
                    <ul class="bs-topic-items">
                      ${h.items.map(it => `
                        <li>
                          <a href="${it.url}" target="_blank" rel="noopener noreferrer">
                            <span class="bs-item-src">${it.source_icon}</span>
                            <span class="bs-item-title">${it.title}</span>
                            ${it.date ? `<span class="bs-item-date">${it.date}</span>` : ''}
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
                  return `<span class="bs-kw" style="font-size:${weight}rem;" title="出现 ${k.count} 次">${k.word}<sup>${k.count}</sup></span>`;
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
            showError('指标加载超时，请手动刷新页面');
            if (refreshBtn) refreshBtn.classList.remove('spinning');
        },
        onError: (err) => {
            console.error('Error fetching dashboard data:', err);
            showError(typeof err === 'string' ? err : '无法连接到服务器');
            if (refreshBtn) refreshBtn.classList.remove('spinning');
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
    updateTopSummaryBar(data.btc_price, data.indicators);

    // 更新仪表盘指针
    updateGauge(data.total_score);

    // 更新评分
    document.getElementById('scoreValue').textContent = data.total_score.toFixed(2);

    // 更新建议
    const recommendationEl = document.getElementById('recommendation');
    recommendationEl.textContent = data.recommendation;
    recommendationEl.className = 'recommendation ' + getScoreColor(data.total_score);

    // 渲染指标
    renderIndicators(data.indicators, data.sparklines);

    // 渲染指标总览表格
    renderSummaryTable(data.indicators);

    // 更新 DAT 动态卡片中的 mNAV
    renderDatMNAV(data.indicators['MSTR mNAV']);
}

function renderDatMNAV(ind) {
    const el = document.getElementById('datMNAV');
    if (!el) return;
    if (!ind || ind.value === null) {
        el.innerHTML = '<span style="color:#555;">MSTR mNAV 数据不可用</span>';
        return;
    }
    const colorMap = { '🟢': '#00ff88', '🟡': '#ffcc00', '🟠': '#ff9800', '🔴': '#ff4466', '⚪': '#888' };
    const c = colorMap[ind.color] || '#888';
    el.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="color:#aaa;">MSTR mNAV</span>
            <span style="color:${c}; font-size:1.1rem; font-weight:700;">${ind.value.toFixed(2)}×</span>
        </div>
        <div style="color:#666; font-size:0.72rem; margin-top:4px;">${ind.status}</div>
        <a href="${ind.url}" target="_blank" rel="noopener noreferrer"
           style="display:inline-block; margin-top:6px; font-size:0.7rem; color:#f7931a; opacity:0.7; text-decoration:none;">
            ↗ SaylorTracker 查看详情
        </a>`;
}

/**
 * 更新顶部摘要栏
 */
function updateTopSummaryBar(btcPrice, indicators) {
    // 价格
    const priceEl = document.getElementById('summaryPrice');
    if (priceEl) {
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
        if (!isNaN(val)) {
            hashrateEl.textContent = val.toFixed(1) + ' EH/s';
        }
    }

    // Ahr999
    const ahr999El = document.getElementById('summaryAhr999');
    if (ahr999El && indicators['Ahr999']) {
        const val = indicators['Ahr999'].value;
        if (!isNaN(val)) {
            ahr999El.textContent = val.toFixed(2);
            ahr999El.style.color = val < 0.45 ? '#00ff88' : (val < 1.2 ? '#ffcc00' : '#ff4466');
        }
    }

    // 恐惧贪婪
    const fgEl = document.getElementById('summaryFearGreed');
    if (fgEl && indicators['恐惧贪婪指数']) {
        const val = indicators['恐惧贪婪指数'].value;
        if (!isNaN(val)) {
            fgEl.textContent = val.toFixed(0);
            fgEl.style.color = val < 25 ? '#00ff88' : (val > 75 ? '#ff4466' : '#ffcc00');
        }
    }

    // 减半倒计时
    const halvingEl = document.getElementById('summaryHalving');
    if (halvingEl && indicators['减半周期']) {
        const status = indicators['减半周期'].status;
        const match = status.match(/(\d+)\s*天/);
        if (match) {
            halvingEl.textContent = match[1] + '天';
        } else {
            halvingEl.textContent = Math.round(indicators['减半周期'].value) + '月';
        }
    }

    // 均衡价格
    const balancedEl = document.getElementById('summaryBalanced');
    if (balancedEl && indicators['均衡价格']) {
        const val = indicators['均衡价格'].value;
        if (!isNaN(val)) {
            balancedEl.textContent = '$' + val.toLocaleString(undefined, {
                minimumFractionDigits: 0,
                maximumFractionDigits: 0
            });
        }
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
function updateGauge(score) {
    const needle = document.getElementById('gaugeNeedle');
    // score 范围是 -1 到 +1，映射到 -90 到 +90 度
    const angle = score * 90;
    needle.style.transform = `translateX(-50%) rotate(${angle}deg)`;
}

/**
 * Render an inline SVG sparkline from an array of values.
 */
function renderSparkline(values, color) {
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
        <svg class="card-v2-sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
            <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round"/>
            <polyline points="${fillPts}" fill="${color}18" stroke="none"/>
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
    const colorHex   = colorKey === 'green' ? '#00ff88'
                     : colorKey === 'red'   ? '#ff4466'
                     : '#ffcc00';

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
            <div class="card-v2-score-fill" style="width:${scoreWidth}%;background:${colorHex}"></div>
        </div>
        ${renderSparkline(sparklineValues, colorHex)}
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
    const chartableIndicators = ['Ahr999', '恐惧贪婪指数', '资金费率', '多空比', 'Pi Cycle Top'];
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
            nameEl.innerHTML += ' <span style="font-size: 0.8em; color: #888;">↗</span>';
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
                borderColor: '#f7931a',
                backgroundColor: 'rgba(247, 147, 26, 0.1)',
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
                        color: '#666',
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
 */
function showError(message) {
    const mainContent = document.getElementById('mainContent');
    const loadingEl = document.getElementById('loading');

    loadingEl.innerHTML = `
        <div class="error">
            <h2>❌ 错误</h2>
            <p>${message}</p>
            <p style="margin-top: 20px; color: var(--text-muted);">请点击右下角按钮重试</p>
        </div>
    `;
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

    const countBadge = `<div style="font-size:0.7rem; color:#555; padding:4px 4px 8px; border-bottom:1px solid rgba(255,255,255,0.06); margin-bottom:4px; flex-shrink:0;">
        共 ${news.length} 条 · 最近 72 小时
    </div>`;

    const items = news.map(item => {
        const summary = item.summary ? item.summary.trim() : '';
        return `<div class="news-flash-item${summary ? ' has-summary' : ''}">
            <div class="news-flash-head">
                <span class="news-flash-bolt">⚡</span>
                <a href="${item.url}" target="_blank" rel="noopener noreferrer" class="news-flash-title">
                    ${item.title}
                </a>
                <span class="news-flash-time">${item.time}</span>
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
        if (type.includes('巨鲸')) return '#ffd700';   // 金色 - 超巨额
        if (type.includes('超大额')) return '#ff9800'; // 橙色 - 超大额
        if (type.includes('大额')) return '#00e5ff';   // 青色 - 大额
        if (type.includes('中额')) return '#90caf9';   // 浅蓝 - 中额
        if (type.includes('⏳')) return '#ffc107';     // 黄色 - 待确认
        return '#aaa';                                  // 灰色 - 普通
    }

    container.innerHTML = whales.map(item => {
        // 特殊处理 "链接" 类型
        if (item.type === '链接') {
            return `
            <a href="${item.url}" target="_blank" class="whale-item" style="display: block; text-decoration: none; margin-bottom: 8px; padding: 10px; background: rgba(255,255,255,0.05); border-radius: 6px; font-size: 0.9rem; text-align: center; color: #f79322; font-weight: 500;">
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
        '高': '#ff4466',
        '中': '#f79322',
        '低': '#888'
    };

    container.innerHTML = events.map(item => {
        // 特殊处理 "链接" 类型
        if (item.type === '链接') {
            return `
            <a href="${item.url}" target="_blank" class="calendar-item" style="display: block; text-decoration: none; margin-bottom: 10px; padding: 10px; background: rgba(255,255,255,0.05); border-radius: 8px; text-align: center; color: #f79322;">
                ${item.event}
            </a>
            `;
        }

        const impact = item.impact || '';
        const color = impactColor[impact] || '#888';
        const hasActual = item.has_actual;
        const isPast = item.is_past;
        const eventStatus = item.event_status || '';
        const actual = item.actual || '';
        const forecast = item.forecast || '';
        const previous = item.previous || '';

        // 状态徽章样式
        let statusBadge = '';
        if (eventStatus === '已公布') {
            statusBadge = `<span style="font-size: 0.65rem; padding: 1px 5px; border-radius: 3px; background: ${hasActual ? '#00c85322' : 'rgba(128,128,128,0.15)'}; color: ${hasActual ? '#00c853' : 'var(--text-muted)'}; white-space: nowrap; margin-left: 6px; border: 1px solid ${hasActual ? '#00c85344' : 'rgba(128,128,128,0.2)'};">✓ 已公布</span>`;
        } else if (eventStatus === '待公布') {
            statusBadge = `<span style="font-size: 0.65rem; padding: 1px 5px; border-radius: 3px; background: #f7932211; color: #f79322; white-space: nowrap; margin-left: 6px; border: 1px solid #f7932233;">⏳ 待公布</span>`;
        }

        // 构建数据值行
        let dataRows = '';
        if (hasActual && actual) {
            // 有实际公布值 - 醒目显示
            dataRows += `<div style="margin-top: 5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">`;
            dataRows += `<span style="font-size: 0.82rem; color: #00e676; font-weight: 600; background: #00e67615; padding: 1px 6px; border-radius: 4px;">📌 公布: ${actual}</span>`;
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
                    ${item.event || item.title || '未知事件'}
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
            <div style="color: #f79322; font-weight: 500;">
                ${item.icon || '📅'} ${item.event || item.title || '未知事件'}
                ${item.source ? `<span style="font-size: 0.7rem; color: #666; margin-left: 8px;">[${item.source}]</span>` : ''}
            </div>
            <div style="margin-top: 4px; font-size: 0.85rem; color: #aaa;">
                ${item.status || item.description || ''}
            </div>
            <div style="margin-top: 4px; font-size: 0.75rem; color: #666;">
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
            borderColor: t.color,
            borderWidth: 1,
            borderDash: [4, 3],
            label: {
                content: t.label,
                display: true,
                position: 'start',
                color: t.color,
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
                borderColor: '#f7931a',
                borderWidth: 1.5,
                pointRadius: 0,
                fill: true,
                backgroundColor: '#f7931a18',
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
                    ticks: { color: '#666', maxTicksLimit: 8, font: { size: 10 } },
                    grid: { color: '#1e2535' }
                },
                y: {
                    ticks: { color: '#666', font: { size: 10 } },
                    grid: { color: '#1e2535' }
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
        renderScoreHistoryChart(data.series || []);
        renderSignalChanges(data.changes || {});
    } catch (e) {
        console.error('Score history fetch failed:', e);
    }
}

function renderScoreHistoryChart(series) {
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

    _scoreHistoryChart = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: '综合评分',
                    data: scores,
                    borderColor: '#f7931a',
                    backgroundColor: '#f7931a18',
                    borderWidth: 2,
                    pointRadius: series.length > 60 ? 0 : 2,
                    fill: true,
                    tension: 0.3,
                    yAxisID: 'yScore',
                },
                {
                    label: 'BTC 价格',
                    data: prices,
                    borderColor: '#4488ff',
                    borderWidth: 1.5,
                    borderDash: [4, 3],
                    pointRadius: 0,
                    fill: false,
                    tension: 0.3,
                    yAxisID: 'yPrice',
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: {
                    display: true,
                    labels: { color: '#888', font: { size: 10 }, boxWidth: 16 }
                },
                tooltip: {
                    callbacks: {
                        afterBody: (items) => {
                            const i = items[0].dataIndex;
                            return series[i].recommendation ? `建议: ${series[i].recommendation}` : '';
                        }
                    }
                },
                annotation: {
                    annotations: {
                        zero: {
                            type: 'line', yMin: 0, yMax: 0, yScaleID: 'yScore',
                            borderColor: '#888', borderWidth: 1, borderDash: [2, 4]
                        },
                        buy: {
                            type: 'line', yMin: 0.382, yMax: 0.382, yScaleID: 'yScore',
                            borderColor: '#00ff8866', borderWidth: 1, borderDash: [4, 4],
                            label: { content: '买入 0.382', display: true, position: 'start', color: '#00ff88', font: { size: 9 }, backgroundColor: 'transparent' }
                        },
                        reduce: {
                            type: 'line', yMin: -0.382, yMax: -0.382, yScaleID: 'yScore',
                            borderColor: '#ff446666', borderWidth: 1, borderDash: [4, 4],
                            label: { content: '卖出 -0.382', display: true, position: 'start', color: '#ff4466', font: { size: 9 }, backgroundColor: 'transparent' }
                        }
                    }
                }
            },
            scales: {
                x: {
                    ticks: { color: '#666', maxTicksLimit: 10, font: { size: 10 } },
                    grid: { display: false }
                },
                yScore: {
                    position: 'left',
                    min: -1, max: 1,
                    ticks: { color: '#f7931a', font: { size: 10 } },
                    grid: { color: 'rgba(128,128,128,0.12)' }
                },
                yPrice: {
                    position: 'right',
                    ticks: {
                        color: '#4488ff', font: { size: 10 },
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
        container.innerHTML = '<p style="color:#888; font-size:0.85rem;">📭 暂无对比基准，明天起自动显示与前一日的信号变化</p>';
        if (meta) meta.textContent = '';
        return;
    }

    if (meta) meta.textContent = `vs ${changes.prev_date}`;

    let html = '';

    // 综合评分变化
    const t = changes.total;
    if (t) {
        const deltaCls = t.delta > 0 ? 'positive' : t.delta < 0 ? 'negative' : 'neutral';
        const deltaColor = t.delta > 0 ? '#00ff88' : t.delta < 0 ? '#ff4466' : '#888';
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
                    ? `<div style="margin-top:4px; font-size:0.8rem; color:#f7931a;">⚡ 档位变化: ${t.prev_band} → <b>${t.curr_band}</b></div>`
                    : `<div style="margin-top:4px; font-size:0.78rem; color:var(--text-muted);">档位维持「${t.curr_band}」</div>`}
            </div>`;
    }

    // 指标跨档变化
    const inds = changes.indicators || [];
    if (inds.length === 0) {
        html += '<p style="color:#888; font-size:0.82rem; margin-top:8px;">✅ 各指标信号与前一日一致，无跨档变化</p>';
    } else {
        html += inds.map(c => {
            const isBull = c.direction === 'bullish';
            const color = isBull ? '#00ff88' : '#ff4466';
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
    if (v === null || v === undefined) return '<span style="color:#888;">--</span>';
    const positive = inverse ? v < 0 : v > 0;
    const color = v === 0 ? '#888' : positive ? '#00ff88' : '#ff4466';
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
        bullish: '#00ff88', bearish: '#ff4466',
        warning: '#ff9800', neutral: '#888'
    };
    const rColor = toneColors[regime.tone] || '#888';

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
        const frColor = fd.rate_pct > 0.03 ? '#ff9800' : fd.rate_pct < -0.03 ? '#00ff88' : '#888';
        cards.push(`
            <div class="deriv-stat">
                <span class="deriv-stat-label">资金费率 (8h)</span>
                <span class="deriv-stat-value" style="color:${frColor};">${fd.rate_pct.toFixed(4)}%</span>
                <span class="deriv-stat-sub">年化 ${fd.annualized_pct}%${fd.next_time ? ` · 下次 ${fd.next_time}` : ''}</span>
            </div>`);
    }
    if (ls) {
        const lsColor = ls.ratio > 1.5 ? '#ff9800' : ls.ratio < 0.7 ? '#00ff88' : '#888';
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
                    <span style="color:#ff4466;">多单 ${longShare.toFixed(0)}%</span> /
                    <span style="color:#00ff88;">空单 ${(100 - longShare).toFixed(0)}%</span>
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
                            borderColor: '#f7931a',
                            backgroundColor: '#f7931a15',
                            borderWidth: 2,
                            pointRadius: 0,
                            fill: true,
                            tension: 0.3,
                            yAxisID: 'yOi',
                        },
                        {
                            label: 'BTC 价格',
                            data: oi.history.map(h => h.price),
                            borderColor: '#4488ff',
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
                        legend: { display: true, labels: { color: '#888', font: { size: 10 }, boxWidth: 16 } },
                        tooltip: {
                            callbacks: {
                                label: (item) => item.dataset.yAxisID === 'yOi'
                                    ? `OI: ${_fmtUsdB(item.raw)}`
                                    : `价格: $${Math.round(item.raw).toLocaleString()}`
                            }
                        }
                    },
                    scales: {
                        x: { ticks: { color: '#666', maxTicksLimit: 10, font: { size: 10 } }, grid: { display: false } },
                        yOi: {
                            position: 'left',
                            ticks: { color: '#f7931a', font: { size: 10 }, callback: (v) => '$' + (v / 1e9).toFixed(0) + 'B' },
                            grid: { color: 'rgba(128,128,128,0.12)' }
                        },
                        yPx: {
                            position: 'right',
                            ticks: { color: '#4488ff', font: { size: 10 }, callback: (v) => '$' + (v / 1000).toFixed(0) + 'K' },
                            grid: { display: false }
                        }
                    }
                }
            });
        }
    }
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
    const latestColor = (latest.total || 0) >= 0 ? '#00ff88' : '#ff4466';
    const sum5Color = (etf.sum_5d || 0) >= 0 ? '#00ff88' : '#ff4466';

    const fmtM = (v) => {
        if (v === null || v === undefined) return '--';
        const sign = v > 0 ? '+' : '';
        return Math.abs(v) >= 1000 ? `${sign}${(v / 1000).toFixed(2)}B` : `${sign}${v.toFixed(0)}M`;
    };

    // 柱状图（纯 CSS，红绿柱以中线为基准）
    const barsHtml = series.map(s => {
        const positive = s.total >= 0;
        const hPct = Math.min(100, Math.abs(s.total) / maxAbs * 100);
        const color = positive ? '#00ff88' : '#ff4466';
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
                <span class="etf-flow-value" style="color:${(etf.sum_total || 0) >= 0 ? '#00ff88' : '#ff4466'};">${fmtM(etf.sum_total)}</span>
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

    const hdr = (txt, align) => `<span style="color: #555; font-size: 0.7rem; padding-bottom: 4px; border-bottom: 1px solid #2a3040;${align ? ' text-align: right;' : ''}">${txt}</span>`;
    const rows = data.companies.map(c => `
        <span title="${c.pct_supply ? `占总供应量 ${c.pct_supply}%` : ''}">${c.name}</span>
        <span style="text-align:right; color:#f7931a;">${c.holdings.toLocaleString()}</span>
        <span style="text-align:right; color:#555;">${c.symbol}</span>
    `).join('');

    panel.innerHTML = `
        <div style="display: grid; grid-template-columns: 1fr auto auto; gap: 4px 12px; align-items: center;">
            ${hdr('公司')}${hdr('持仓 (BTC)', true)}${hdr('代码', true)}
            ${rows}
        </div>
        <p style="margin: 10px 0 0; color: #444; font-size: 0.68rem; text-align: right;">
            上市公司合计 ${data.total.toLocaleString()} BTC · CoinGecko · ${data.updated_at}
        </p>
    `;
}
