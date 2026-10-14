/* 轻量内联 SVG 图表（无依赖）。颜色全部来自 CSS 变量，深浅主题自动适配。
   lineChart(el, {labels, series:[{agent, name, values}], min, max, unit})
   - 2px 线、末端直接标注、图例、十字准线 + 悬停提示 */
(function () {
  function lineChart(el, o) {
    const W = el.clientWidth || 640, H = o.height || 220, pad = { l: 36, r: 110, t: 12, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const n = o.labels.length, min = o.min, max = o.max;
    const x = i => pad.l + (n === 1 ? iw / 2 : i * iw / (n - 1));
    const y = v => pad.t + ih - (v - min) / (max - min) * ih;
    const ticks = o.ticks || [min, (min + max) / 2, max];
    let svg = '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="' + (o.aria || '') + '">';
    ticks.forEach(v => { svg += '<line class="grid-line" x1="' + pad.l + '" x2="' + (pad.l + iw) + '" y1="' + y(v) + '" y2="' + y(v) + '"/><text class="axis-label" x="' + (pad.l - 8) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + v + (o.unit || '') + '</text>'; });
    o.labels.forEach((lb, i) => { if (i % Math.ceil(n / 7) === 0 || i === n - 1) svg += '<text class="axis-label" x="' + x(i) + '" y="' + (H - 6) + '" text-anchor="middle">' + lb + '</text>'; });
    // 末端标签防重叠
    const ends = o.series.map(s => ({ s, y: y(s.values[n - 1]) })).sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 16) ends[i].y = ends[i - 1].y + 16;
    o.series.forEach(s => {
      const d = s.values.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1)).join(' ');
      svg += '<g data-agent="' + s.agent + '"><path d="' + d + '" fill="none" stroke="var(--agent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
        '<circle cx="' + x(n - 1) + '" cy="' + y(s.values[n - 1]) + '" r="4" fill="var(--agent)" stroke="var(--color-surface)" stroke-width="2"/></g>';
      const e = ends.find(q => q.s === s);
      svg += '<text class="series-label" x="' + (x(n - 1) + 10) + '" y="' + (e.y + 4) + '">' + s.values[n - 1] + (o.unit || '') + ' · ' + s.name + '</text>';
    });
 (o.annotations || []).forEach(a => { svg += '<line x1="' + x(a.i) + '" x2="' + x(a.i) + '" y1="' + pad.t + '" y2="' + (pad.t + ih) + '" stroke="' + (a.bad ? 'var(--color-danger)' : 'var(--color-text-3)') + '" stroke-dasharray="2 4" stroke-width="1"/><text class="axis-label" x="' + (x(a.i) + 4) + '" y="' + (pad.t + 10) + '">' + a.label + '</text>'; });
    svg += '<line id="xh" x1="0" x2="0" y1="' + pad.t + '" y2="' + (pad.t + ih) + '" stroke="var(--color-border-strong)" stroke-dasharray="3 3" visibility="hidden"/>';
    svg += '<rect x="' + pad.l + '" y="' + pad.t + '" width="' + iw + '" height="' + ih + '" fill="transparent" id="hit"/></svg>';
    el.style.position = 'relative';
    el.innerHTML = svg + '<div class="chart-tooltip hidden"></div>';
    const hit = el.querySelector('#hit'), xh = el.querySelector('#xh'), tip = el.querySelector('.chart-tooltip');
    hit.addEventListener('mousemove', ev => {
      const r = el.querySelector('svg').getBoundingClientRect();
      const px = (ev.clientX - r.left) * (W / r.width);
      const i = Math.max(0, Math.min(n - 1, Math.round((px - pad.l) / (iw / (n - 1)))));
      xh.setAttribute('x1', x(i)); xh.setAttribute('x2', x(i)); xh.setAttribute('visibility', 'visible');
      tip.classList.remove('hidden');
      tip.innerHTML = '<div class="muted" style="margin-bottom:4px">' + o.labels[i] + '</div>' + o.series.map(s =>
        '<div class="row" data-agent="' + s.agent + '" style="justify-content:space-between;gap:12px"><span class="row" style="gap:6px"><i style="width:10px;height:3px;border-radius:2px;background:var(--agent);display:inline-block"></i>' + s.name + '</span><b>' + s.values[i] + (o.unit || '') + '</b></div>').join('');
      const left = x(i) * (r.width / W);
      tip.style.left = Math.min(left + 12, r.width - 160) + 'px'; tip.style.top = '8px';
    });
    hit.addEventListener('mouseleave', () => { xh.setAttribute('visibility', 'hidden'); tip.classList.add('hidden'); });
  }

  /* 迷你折线（无坐标轴），用于状态卡 */
  function sparkline(values, w, h, color) {
    w = w || 120; h = h || 32;
    const min = Math.min.apply(null, values) - 1, max = Math.max.apply(null, values) + 1;
    const pts = values.map((v, i) => (i * (w - 8) / (values.length - 1) + 4).toFixed(1) + ',' + (h - 4 - (v - min) / (max - min) * (h - 8)).toFixed(1));
    const last = pts[pts.length - 1].split(',');
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" aria-hidden="true"><polyline points="' + pts.join(' ') + '" fill="none" stroke="' + (color || 'var(--color-primary)') + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/><circle cx="' + last[0] + '" cy="' + last[1] + '" r="3" fill="' + (color || 'var(--color-primary)') + '"/></svg>';
  }

  window.CHARTS = { lineChart, sparkline };
})();
