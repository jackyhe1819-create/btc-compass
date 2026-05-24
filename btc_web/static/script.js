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
});

// 刷新按钮点击事件（同时刷新指标和资讯）
document.getElementById('refreshBtn')?.addEventListener('click', () => {
    fetchDashboardData();
    fetchNewsData();
    fetchBuildersData();
});

async function fetchBuildersData() {
    try {
        const response = await fetch('/api/builders');
        const data = await response.json();
        if (!data.success) return;

        const grid = document.getElementById('buildersGrid');
        if (!grid) return;

        const updatedEl = document.getElementById('buildersUpdatedAt');
        if (updatedEl && data.updated_at) updatedEl.textContent = `更新于 ${data.updated_at}`;

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

    } catch (e) {
        console.error('Builders feed error:', e);
    }
}

/**
 * 获取仪表盘数据（支持冷启动轮询）
 * 服务器返回 computing:true (202) 时，每 8 秒自动重试，最多等待 10 分钟
 */
async function fetchDashboardData(isRetry) {
    const refreshBtn = document.getElementById('refreshBtn');
    if (!isRetry && refreshBtn) refreshBtn.classList.add('spinning');

    try {
        const response = await fetch('/api/dashboard');
        const data = await response.json();

        if (data.success) {
            renderDashboard(data);
            if (refreshBtn) refreshBtn.classList.remove('spinning');
            return;
        }

        // 服务器正在计算（冷启动），显示提示并轮询
        if (data.computing) {
            _showComputingBanner();
            if (!window._dashboardPollTimer) {
                let attempts = 0;
                window._dashboardPollTimer = setInterval(async () => {
                    attempts++;
                    if (attempts > 75) { // 最多等 10 分钟 (75 × 8s)
                        clearInterval(window._dashboardPollTimer);
                        window._dashboardPollTimer = null;
                        _hideComputingBanner();
                        showError('指标加载超时，请手动刷新页面');
                        if (refreshBtn) refreshBtn.classList.remove('spinning');
                        return;
                    }
                    try {
                        const r2 = await fetch('/api/dashboard');
                        const d2 = await r2.json();
                        if (d2.success) {
                            clearInterval(window._dashboardPollTimer);
                            window._dashboardPollTimer = null;
                            _hideComputingBanner();
                            renderDashboard(d2);
                            if (refreshBtn) refreshBtn.classList.remove('spinning');
                        }
                    } catch(e) { /* 静默忽略轮询错误 */ }
                }, 8000);
            }
            return;
        }

        showError(data.error || '获取数据失败');
    } catch (error) {
        console.error('Error fetching dashboard data:', error);
        showError('无法连接到服务器');
    } finally {
        // 非 computing 情况下才立即停止 spinner
        if (!window._dashboardPollTimer && refreshBtn) {
            refreshBtn.classList.remove('spinning');
        }
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
 * 获取资讯数据
 */
async function fetchNewsData() {
    console.log('Fetching news data...');
    const newsRefreshBtn = document.getElementById('newsRefreshBtn');
    if (newsRefreshBtn) newsRefreshBtn.classList.add('spinning');
    try {
        const response = await fetch('/api/news');
        const data = await response.json();

        if (data.success) {
            // 渲染鲸鱼动态（先渲染左栏，再渲染资讯）
            if (data.whales && data.whales.length > 0) {
                renderWhaleActivity(data.whales);
            }
            // 渲染鲸鱼买卖量统计
            if (data.whale_stats) {
                renderWhaleStats(data.whale_stats);
            }
            // 渲染交易所BTC余额
            if (data.exchange_balance) {
                renderExchangeBalance(data.exchange_balance);
            }
            // 渲染宏观经济日历
            if (data.calendar && data.calendar.length > 0) {
                renderMacroCalendar(data.calendar);
            }
            // 渲染资讯
            if (data.news && data.news.length > 0) {
                renderCryptoNews(data.news);
            }
            // 更新时间
            const updatedEl = document.getElementById('newsUpdatedAt');
            if (updatedEl) {
                const now = new Date();
                updatedEl.textContent = `更新于 ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;
            }
            console.log('News data loaded successfully');
        } else {
            console.error('News API error:', data.error);
        }
    } catch (error) {
        console.error('Failed to fetch news:', error);
    } finally {
        if (newsRefreshBtn) newsRefreshBtn.classList.remove('spinning');
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
