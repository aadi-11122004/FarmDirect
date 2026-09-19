"""KrishiSetu — lightweight SVG chart builders (no external JS libs).

All charts render as inline SVG strings marked safe for Jinja, so the app
works fully offline / inside sandboxed previews.
"""

import json

from markupsafe import Markup


def _fmt(v):
    if v >= 1000:
        return f"{v/1000:.1f}k"
    if v >= 100:
        return f"{v:.0f}"
    return f"{v:,.1f}".rstrip("0").rstrip(".")


def line_chart(labels, series, w=680, h=260, unit="", y_pad=0.08):
    """Multi-series line chart.

    labels : list of x labels
    series : list of (name, color, values[list[float]])
    """
    n = len(labels)
    if n == 0:
        return Markup("<div class='chart-empty'>No data</div>")
    pad_l, pad_r, pad_t, pad_b = 46, 14, (34 if len(series) > 1 else 16), 30
    cw, ch = w - pad_l - pad_r, h - pad_t - pad_b

    allv = [v for _, _, vals in series for v in vals if v is not None] or [0]
    lo, hi = min(allv), max(allv)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    rng = hi - lo
    lo -= rng * y_pad
    hi += rng * y_pad

    def X(i):
        return pad_l + (cw * i / max(n - 1, 1))

    def Y(v):
        return pad_t + ch - ch * (v - lo) / (hi - lo)

    grid, ylabels = [], []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        grid.append(f"<line x1='{pad_l}' y1='{y:.1f}' x2='{w-pad_r}' y2='{y:.1f}' "
                    f"stroke='#e3e9df' stroke-width='1'/>")
        ylabels.append(f"<text x='{pad_l-6}' y='{y+4:.1f}' text-anchor='end' "
                       f"class='ax'>{_fmt(v)}{unit}</text>")

    xlabels = []
    step = max(1, n // 6)
    for i in range(0, n, step):
        xlabels.append(f"<text x='{X(i):.1f}' y='{h-8}' text-anchor='middle' "
                       f"class='ax'>{labels[i]}</text>")

    paths = []
    for si, (name, color, vals) in enumerate(series):
        gid = f"grad{si}_{abs(hash(name)) % 99999}"
        paths.append(
            f"<linearGradient id='{gid}' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0%' stop-color='{color}' stop-opacity='.22'/>"
            f"<stop offset='100%' stop-color='{color}' stop-opacity='0'/></linearGradient>")
        if any(v is None for v in vals):
            # split into contiguous segments (for bridged forecast overlays)
            segs, seg = [], []
            for i, v in enumerate(vals):
                if v is None:
                    if seg:
                        segs.append(seg)
                        seg = []
                else:
                    seg.append((i, v))
            if seg:
                segs.append(seg)
            for seg in segs:
                pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in seg)
                paths.append(f"<polyline points='{pts}' fill='none' stroke='{color}' "
                             f"stroke-width='2.4' stroke-linejoin='round' "
                             f"stroke-linecap='round'{' stroke-dasharray=|7 5|'.replace('|', chr(34)) if 'forecast' in name.lower() else ''}/>")
            last_i, last_v = segs[-1][-1]
            paths.append(f"<circle cx='{X(last_i):.1f}' cy='{Y(last_v):.1f}' r='3.6' "
                         f"fill='{color}' stroke='#fff' stroke-width='1.5'/>")
            paths.append(f"<text x='{X(last_i):.1f}' y='{Y(last_v)-8:.1f}' text-anchor='end' "
                         f"fill='{color}' class='ax' font-weight='700'>"
                         f"{_fmt(last_v)}{unit}</text>")
        else:
            pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
            fill_pts = f"{X(0):.1f},{pad_t+ch} {pts} {X(n-1):.1f},{pad_t+ch}"
            paths.append(f"<polygon points='{fill_pts}' fill='url(#{gid})'/>")
            paths.append(f"<polyline points='{pts}' fill='none' stroke='{color}' "
                         f"stroke-width='2.4' stroke-linejoin='round' stroke-linecap='round'/>")
            lx, ly = X(n - 1), Y(vals[-1])
            paths.append(f"<circle cx='{lx:.1f}' cy='{ly:.1f}' r='3.6' fill='{color}' "
                         f"stroke='#fff' stroke-width='1.5'/>")
            paths.append(f"<text x='{lx:.1f}' y='{ly-8:.1f}' text-anchor='end' fill='{color}' "
                         f"class='ax' font-weight='700'>{_fmt(vals[-1])}{unit}</text>")

    legend, lx = "", pad_l
    if len(series) > 1:
        for n_, c, _ in series:
            legend += (f"<text x='{lx:.0f}' y='13' class='ax'><tspan fill='{c}'>■</tspan>"
                       f"<tspan fill='#5b6b5d' dx='4'>{n_}</tspan></text>")
            lx += 22 + len(n_) * 5.6

    svg = (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' class='chart'>"
           f"<defs>{''.join(p for p in paths if p.startswith('<linear'))}</defs>"
           f"{''.join(grid)}{''.join(ylabels)}{''.join(xlabels)}"
           f"{''.join(p for p in paths if not p.startswith('<linear'))}{legend}</svg>")
    return Markup(svg)


def dual_forecast_chart(labels, price_vals, demand_vals, price_unit="₹/qtl", demand_unit=" qtl",
                         price_color="#2e7d32", demand_color="#1565c0", w=860, h=320):
    """Single chart, two y-axes, hover tooltip — forecast-only (no history overlay).

    labels      : x labels (dates), forecast horizon only
    price_vals  : predicted price per point (left axis, solid line)
    demand_vals : predicted demand per point (right axis, dashed line)
    Colors/line-styles mirror the reference Plotly forecaster (solid green price line,
    dashed blue demand line, axis titles on both sides). Returns a self-contained <div>
    (chart + crosshair + tooltip); hover is wired up by static/js/main.js reading the
    embedded data-points JSON, so no chart JS library is needed.
    """
    n = len(labels)
    if n == 0:
        return Markup("<div class='chart-empty'>No data</div>")
    pad_l, pad_r, pad_t, pad_b = 64, 64, 24, 42
    cw, ch = w - pad_l - pad_r, h - pad_t - pad_b

    def scale(vals):
        lo, hi = min(vals), max(vals)
        if lo == hi:
            lo, hi = lo - 1, hi + 1
        rng = hi - lo
        return lo - rng * 0.12, hi + rng * 0.12

    p_lo, p_hi = scale(price_vals)
    d_lo, d_hi = scale(demand_vals)

    def X(i):
        return pad_l + (cw * i / max(n - 1, 1))

    def Yp(v):
        return pad_t + ch - ch * (v - p_lo) / (p_hi - p_lo)

    def Yd(v):
        return pad_t + ch - ch * (v - d_lo) / (d_hi - d_lo)

    grid, ylabels = [], []
    for k in range(5):
        y = pad_t + ch * k / 4
        grid.append(f"<line x1='{pad_l}' y1='{y:.1f}' x2='{w-pad_r}' y2='{y:.1f}' stroke='#e3e9df' stroke-width='1'/>")
        pv = p_hi - (p_hi - p_lo) * k / 4
        dv = d_hi - (d_hi - d_lo) * k / 4
        ylabels.append(f"<text x='{pad_l-8}' y='{y+4:.1f}' text-anchor='end' class='ax' fill='{price_color}'>{_fmt(pv)}</text>")
        ylabels.append(f"<text x='{w-pad_r+8}' y='{y+4:.1f}' text-anchor='start' class='ax' fill='{demand_color}'>{_fmt(dv)}</text>")

    step = max(1, n // 6)
    xlabels = [f"<text x='{X(i):.1f}' y='{h-pad_b+16:.1f}' text-anchor='middle' class='ax'>{labels[i]}</text>"
               for i in range(0, n, step)]
    axis_titles = (
        f"<text x='{14}' y='{pad_t+ch/2:.1f}' text-anchor='middle' class='ax' fill='{price_color}' "
        f"transform='rotate(-90 14 {pad_t+ch/2:.1f})'>Price ({price_unit})</text>"
        f"<text x='{w-14}' y='{pad_t+ch/2:.1f}' text-anchor='middle' class='ax' fill='{demand_color}' "
        f"transform='rotate(90 {w-14} {pad_t+ch/2:.1f})'>Demand ({demand_unit.strip()})</text>"
        f"<text x='{pad_l+cw/2:.1f}' y='{h-6}' text-anchor='middle' class='ax'>Date</text>"
    )

    p_pts = " ".join(f"{X(i):.1f},{Yp(v):.1f}" for i, v in enumerate(price_vals))
    d_pts = " ".join(f"{X(i):.1f},{Yd(v):.1f}" for i, v in enumerate(demand_vals))
    lines = (
        f"<polyline points='{p_pts}' fill='none' stroke='{price_color}' stroke-width='2.6' "
        f"stroke-linejoin='round' stroke-linecap='round'/>"
        f"<polyline points='{d_pts}' fill='none' stroke='{demand_color}' stroke-width='2.6' "
        f"stroke-linejoin='round' stroke-linecap='round' stroke-dasharray='7 5'/>"
    )

    legend = (
        f"<text x='{pad_l}' y='13' class='ax'><tspan fill='{price_color}'>■</tspan>"
        f"<tspan fill='#5b6b5d' dx='4'>Predicted price ({price_unit})</tspan></text>"
        f"<text x='{pad_l + 190}' y='13' class='ax'><tspan fill='{demand_color}'>■</tspan>"
        f"<tspan fill='#5b6b5d' dx='4'>Predicted demand ({demand_unit.strip()})</tspan></text>"
    )

    # crosshair + two dots, hidden until JS moves/shows them on hover
    cursor = (
        f"<g class='fchart-cursor' opacity='0'>"
        f"<line x1='0' y1='{pad_t}' x2='0' y2='{pad_t+ch}' stroke='#8d9488' stroke-width='1' stroke-dasharray='3 3'/>"
        f"<circle r='4.5' fill='{price_color}' stroke='#fff' stroke-width='2' class='fchart-dot-price'/>"
        f"<circle r='4.5' fill='{demand_color}' stroke='#fff' stroke-width='2' class='fchart-dot-demand'/>"
        f"</g>"
    )

    svg = (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' class='chart fchart-svg'>"
           f"{''.join(grid)}{''.join(ylabels)}{''.join(xlabels)}{axis_titles}{lines}{legend}{cursor}</svg>")

    points = [dict(label=labels[i], x=round(X(i), 1), price=price_vals[i], demand=demand_vals[i],
                   yp=round(Yp(price_vals[i]), 1), yd=round(Yd(demand_vals[i]), 1))
              for i in range(n)]
    payload = json.dumps(points).replace("'", "&#39;")
    div = (f"<div class='fchart' data-points='{payload}' data-price-unit='{price_unit}' "
           f"data-demand-unit='{demand_unit}' data-padl='{pad_l}' data-padr='{pad_r}'>"
           f"{svg}<div class='fchart-tip'></div></div>")
    return Markup(div)


def bar_chart(labels, values, color="#245536", w=680, h=260, unit=""):
    """Horizontal bar chart."""
    if not values:
        return Markup("<div class='chart-empty'>No data</div>")
    pad_l = max(112, max(len(l) for l in labels) * 7 + 12)
    pad_r, pad_t, pad_b = 58, 10, 10
    cw, ch = w - pad_l - pad_r, h - pad_t - pad_b
    hi = max(values) * 1.08 or 1
    row_h = ch / len(values)
    bar_h = min(18, row_h * 0.62)

    parts = []
    for k in range(1, 5):
        x = pad_l + cw * k / 4
        parts.append(f"<line x1='{x:.1f}' y1='{pad_t}' x2='{x:.1f}' y2='{h-pad_b}' "
                     f"stroke='#e3e9df' stroke-width='1'/>")
    for i, (lab, v) in enumerate(zip(labels, values)):
        y = pad_t + row_h * i + (row_h - bar_h) / 2
        bw = cw * v / hi
        parts.append(f"<text x='{pad_l-8}' y='{y+bar_h/2+4:.1f}' text-anchor='end' "
                     f"class='ax'>{lab}</text>")
        parts.append(f"<rect x='{pad_l}' y='{y:.1f}' width='{bw:.1f}' height='{bar_h}' "
                     f"rx='{bar_h/2}' fill='{color}' fill-opacity='.85'/>")
        parts.append(f"<text x='{pad_l+bw+6:.1f}' y='{y+bar_h/2+4:.1f}' class='ax' "
                     f"font-weight='700' fill='#33443a'>{_fmt(v)}{unit}</text>")

    svg = (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' class='chart'>"
           f"{''.join(parts)}</svg>")
    return Markup(svg)
