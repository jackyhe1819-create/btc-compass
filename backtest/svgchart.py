#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.svgchart
=================
零依赖 SVG 折线图 (matplotlib 未安装, 不引入新依赖)。
支持多面板共享 X 轴: 价格(对数) + 评分序列 + 净值曲线。
"""

import math
from datetime import date

import numpy as np
import pandas as pd

W = 1080
PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 28, 34
GAP = 30

COLORS = ["#f7931a", "#4488ff", "#22aa66", "#cc4444", "#9966cc", "#888888"]


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;")


def _ticks(lo, hi, n=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    start = math.ceil(lo / step) * step
    out = []
    v = start
    while v <= hi + 1e-9:
        out.append(v)
        v += step
    return out


def _fmt(v):
    if abs(v) >= 1e6:
        return f"{v/1e6:.1f}M"
    if abs(v) >= 1000:
        return f"{v/1000:.0f}K"
    if abs(v) >= 10:
        return f"{v:.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Panel:
    def __init__(self, title, height=200, log=False, ylim=None, bands=None):
        self.title = title
        self.height = height
        self.log = log
        self.ylim = ylim
        self.bands = bands or []   # [(y0, y1, color, alpha)]
        self.series = []           # [(name, pd.Series, color, width)]

    def add(self, name, s: pd.Series, color=None, width=1.6):
        s = s.dropna()
        if len(s):
            self.series.append((name, s, color, width))
        return self


def render(panels, x0: pd.Timestamp, x1: pd.Timestamp, out_path: str):
    total_h = PAD_T + sum(p.height for p in panels) + GAP * (len(panels) - 1) + PAD_B
    plot_w = W - PAD_L - PAD_R
    t0, t1 = x0.timestamp(), x1.timestamp()

    def X(ts):
        return PAD_L + (ts.timestamp() - t0) / max(t1 - t0, 1) * plot_w

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{total_h}" '
             f'viewBox="0 0 {W} {total_h}" font-family="Helvetica,Arial,sans-serif">',
             f'<rect width="{W}" height="{total_h}" fill="#ffffff"/>']

    # X 轴年刻度 (全图共享)
    years = range(x0.year, x1.year + 1)
    year_xs = []
    for y in years:
        ts = pd.Timestamp(date(y, 1, 1))
        if x0 <= ts <= x1:
            year_xs.append((X(ts), y))

    y_cursor = PAD_T
    for p in panels:
        top, h = y_cursor, p.height
        bottom = top + h

        # Y 范围
        vals = np.concatenate([s.values for _, s, _, _ in p.series]) if p.series else np.array([0, 1])
        if p.ylim:
            lo, hi = p.ylim
        else:
            lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
            if p.log:
                # 对数轴用乘性 padding, 避免线性外扩跌破 0
                lo, hi = max(lo, 1e-9) * 0.8, hi * 1.25
            else:
                span = (hi - lo) or 1
                lo, hi = lo - span * 0.05, hi + span * 0.05
        if p.log:
            lo = max(lo, 1e-9)

        def Y(v, _lo=lo, _hi=hi, _top=top, _h=h, _log=p.log):
            if _log:
                v = max(v, 1e-9)
                frac = (math.log10(v) - math.log10(_lo)) / (math.log10(_hi) - math.log10(_lo))
            else:
                frac = (v - _lo) / (_hi - _lo)
            return _top + _h - frac * _h

        # 背景色带
        for (b0, b1, color, alpha) in p.bands:
            yb0, yb1 = Y(min(b1, hi)), Y(max(b0, lo))
            if yb1 > yb0:
                parts.append(f'<rect x="{PAD_L}" y="{yb0:.1f}" width="{plot_w}" '
                             f'height="{yb1-yb0:.1f}" fill="{color}" opacity="{alpha}"/>')

        # 网格 + Y 刻度
        if p.log:
            tick_vals, v = [], 10 ** math.floor(math.log10(lo))
            while v <= hi:
                if v >= lo:
                    tick_vals.append(v)
                v *= 10
        else:
            tick_vals = _ticks(lo, hi)
        for tv in tick_vals:
            yy = Y(tv)
            parts.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W-PAD_R}" y2="{yy:.1f}" '
                         f'stroke="#e8e8e8" stroke-width="1"/>')
            parts.append(f'<text x="{PAD_L-6}" y="{yy+3.5:.1f}" text-anchor="end" '
                         f'font-size="10" fill="#999">{_fmt(tv)}</text>')
        for xx, yname in year_xs:
            parts.append(f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{bottom}" '
                         f'stroke="#f0f0f0" stroke-width="1"/>')

        # 序列
        for i, (name, s, color, width) in enumerate(p.series):
            c = color or COLORS[i % len(COLORS)]
            pts, step = [], max(1, len(s) // 2400)
            sub = s.iloc[::step]
            for ts, v in sub.items():
                if np.isnan(v):
                    continue
                pts.append(f"{X(ts):.1f},{Y(v):.1f}")
            if pts:
                parts.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                             f'stroke="{c}" stroke-width="{width}"/>')

        # 标题 + 图例
        parts.append(f'<text x="{PAD_L}" y="{top-7}" font-size="12" font-weight="bold" '
                     f'fill="#333">{_esc(p.title)}</text>')
        lx = PAD_L + 240
        for i, (name, s, color, width) in enumerate(p.series):
            c = color or COLORS[i % len(COLORS)]
            parts.append(f'<line x1="{lx}" y1="{top-11}" x2="{lx+18}" y2="{top-11}" '
                         f'stroke="{c}" stroke-width="2.5"/>')
            parts.append(f'<text x="{lx+22}" y="{top-7}" font-size="10.5" fill="#555">{_esc(name)}</text>')
            lx += 30 + 7.5 * len(str(name))

        # 边框
        parts.append(f'<rect x="{PAD_L}" y="{top}" width="{plot_w}" height="{h}" '
                     f'fill="none" stroke="#cccccc" stroke-width="1"/>')
        y_cursor = bottom + GAP

    # X 轴年标签
    for xx, yname in year_xs:
        parts.append(f'<text x="{xx:.1f}" y="{total_h-12}" text-anchor="middle" '
                     f'font-size="10.5" fill="#777">{yname}</text>')

    parts.append("</svg>")
    with open(out_path, "w") as f:
        f.write("\n".join(parts))
    return out_path
