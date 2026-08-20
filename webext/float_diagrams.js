// Sidebar float diagrams: price-vs-float, input-float-vs-output-revenue, and
// EV-vs-float, computed from braindamage.mono_trade_table.build_float_diagram_data's
// reply (see steam_offers_host.handle_fetch_offers' "float_diagrams" field).
//
// Split into two halves on purpose: the top half (bucketing/weighting/curve
// math) is pure and DOM-free so it stays easy to extend or test on its own;
// only render() at the bottom touches Chart.js/canvas. Loaded as a content
// script ahead of sidebar.js (see manifest.json), which calls into the
// window.__bdFloatDiagrams namespace this file installs -- same pattern as
// sidebar.render.generated.js's window.__bdSidebarRender.
(function () {
  const NUM_BUCKETS = 100;
  const INPUT_QUANTITY = 10; // one mono trade-up contract buys 10 of the input skin
  const ROI_FLOOR = -0.15; // diagram 3 clips display at this ROI so a losing trade's depth doesn't dominate the y-axis

  // --- Pure math -----------------------------------------------------------

  function normalizedFloat(rawFloat, minFloat, maxFloat) {
    const span = maxFloat - minFloat;
    if (span <= 0) return 0.0;
    const clamped = Math.min(Math.max(rawFloat, minFloat), maxFloat);
    return (clamped - minFloat) / span;
  }

  function outputFloat(x, minOut, maxOut) {
    return minOut + (maxOut - minOut) * x;
  }

  function wearForFloat(f, wearBuckets) {
    const clamped = Math.min(Math.max(f, 0), 1);
    for (const w of wearBuckets) {
      if (clamped <= w.hi) return w.wear_name;
    }
    return wearBuckets[wearBuckets.length - 1].wear_name;
  }

  function bucketIndex(f) {
    return Math.min(NUM_BUCKETS - 1, Math.max(0, Math.floor(f * NUM_BUCKETS)));
  }

  function median(values) {
    const sorted = [...values].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 !== 0 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  }

  // Diagram 1: per-bucket price, robust against outliers -- the median of
  // whichever fetch batch is the most recent to actually have a point in
  // that bucket (batch_rank 0 == most recent, see build_float_diagram_data;
  // older batches are only consulted per-bucket, never blended in). Buckets
  // with no points at all step outward to the nearest bucket(s) that DO
  // have real data (never another estimate, so a gap doesn't smear outward
  // indefinitely) and are flagged `estimated` so the chart can dim them.
  function computeBucketPrices(offerPoints) {
    const byBucket = Array.from({ length: NUM_BUCKETS }, () => []);
    for (const p of offerPoints) {
      if (p.float_value == null) continue;
      byBucket[bucketIndex(p.float_value)].push(p);
    }
    const raw = byBucket.map((points) => {
      if (!points.length) return null;
      const newestRank = Math.min(...points.map((p) => p.batch_rank));
      return median(points.filter((p) => p.batch_rank === newestRank).map((p) => p.price));
    });

    const buckets = raw.map((price, i) => ({
      lo: i / NUM_BUCKETS,
      hi: (i + 1) / NUM_BUCKETS,
      price,
      estimated: false,
    }));

    for (let i = 0; i < NUM_BUCKETS; i++) {
      if (raw[i] !== null) continue;
      for (let radius = 1; radius < NUM_BUCKETS; radius++) {
        const left = i - radius >= 0 ? raw[i - radius] : null;
        const right = i + radius < NUM_BUCKETS ? raw[i + radius] : null;
        if (left === null && right === null) continue;
        buckets[i].price = left !== null && right !== null ? (left + right) / 2 : left !== null ? left : right;
        buckets[i].estimated = true;
        break;
      }
    }
    return buckets;
  }

  function priceAtFloat(buckets, f) {
    const b = buckets[bucketIndex(f)];
    return b ? b.price : null;
  }

  // Diagram 2: expected mono-trade output revenue if the current input
  // skin's float were `rawX` -- normalized against the input skin's own
  // float range, then remapped into each possible outcome's own range
  // (tradeup.output_float's client-side twin), priced net of Steam's sell
  // fee at whichever wear that lands in.
  function revenueAt(rawX, inputSkin, outcomes, wearBuckets) {
    const nx = normalizedFloat(rawX, inputSkin.min_float, inputSkin.max_float);
    let revenue = 0;
    for (const o of outcomes) {
      const outF = outputFloat(nx, o.min_float, o.max_float);
      const wear = wearForFloat(outF, wearBuckets);
      const price = o.net_price_by_wear[wear];
      if (price !== undefined) revenue += o.probability * price;
    }
    return revenue;
  }

  function computeRevenueCurve(inputSkin, outcomes, wearBuckets, numPoints) {
    numPoints = numPoints || NUM_BUCKETS + 1;
    const points = [];
    for (let i = 0; i < numPoints; i++) {
      const x = i / (numPoints - 1);
      points.push({ x, y: revenueAt(x, inputSkin, outcomes, wearBuckets) });
    }
    return points;
  }

  // Diagram 3: EV at each bucket's midpoint -- input cost is 10x that
  // bucket's estimated per-unit price (diagram 1), revenue is diagram 2's
  // curve evaluated at the same x. `displayEv` clamps to ROI_FLOOR * cost
  // for rendering only (`ev` keeps the real, unclamped value).
  function computeEvCurve(buckets, inputSkin, outcomes, wearBuckets) {
    return buckets.map((b) => {
      const x = (b.lo + b.hi) / 2;
      if (b.price === null) return { x, lo: b.lo, hi: b.hi, ev: null, displayEv: null, inputCost: null };
      const inputCost = b.price * INPUT_QUANTITY;
      const revenue = revenueAt(x, inputSkin, outcomes, wearBuckets);
      const ev = revenue - inputCost;
      const floor = ROI_FLOOR * inputCost;
      return { x, lo: b.lo, hi: b.hi, ev, displayEv: Math.max(ev, floor), inputCost };
    });
  }

  // Every reported range is at least this wide -- a single-bucket peak
  // already meets it (buckets are 1/NUM_BUCKETS wide), this only kicks in
  // as a safety net if that ever changes.
  const MIN_RANGE_SPAN = 0.01;

  // The `count` most promising non-overlapping float ranges: each is a
  // local EV maximum, expanded outward while EV stays within a 20% band of
  // that peak (a plateau around the peak, not just the single best bucket).
  // A candidate peak already covered by an earlier (better) range's plateau
  // is skipped outright -- otherwise a single wide flat plateau (common
  // when a whole wear tier prices as one estimated bucket) would report as
  // several near-duplicate "ranges" instead of moving on to the next
  // genuinely distinct one.
  function topFloatRanges(evPoints, count) {
    count = count || 3;
    const valid = evPoints.filter((p) => p.ev !== null);
    if (!valid.length) return [];

    const peaks = [];
    for (let i = 0; i < valid.length; i++) {
      const prevEv = i > 0 ? valid[i - 1].ev : null;
      const nextEv = i < valid.length - 1 ? valid[i + 1].ev : null;
      const isPeak = (prevEv === null || valid[i].ev >= prevEv) && (nextEv === null || valid[i].ev >= nextEv);
      if (isPeak) peaks.push(i);
    }
    peaks.sort((a, b) => valid[b].ev - valid[a].ev);

    const isClaimed = (i) => ranges.some((r) => i >= r.loIdx && i <= r.hiIdx);

    const ranges = [];
    for (const idx of peaks) {
      if (ranges.length >= count) break;
      if (isClaimed(idx)) continue;

      const peakEv = valid[idx].ev;
      const band = Math.max(Math.abs(peakEv) * 0.2, 0.01);
      const threshold = peakEv - band;
      let lo = idx;
      let hi = idx;
      // Never expand into a better, already-claimed range's territory --
      // otherwise a loose band on a middling peak can swallow a sharper,
      // higher-EV plateau right next to it (already reported as its own
      // range), producing two overlapping "distinct" ranges.
      while (lo > 0 && valid[lo - 1].ev >= threshold && !isClaimed(lo - 1)) lo--;
      while (hi < valid.length - 1 && valid[hi + 1].ev >= threshold && !isClaimed(hi + 1)) hi++;

      let rangeLo = valid[lo].lo;
      let rangeHi = valid[hi].hi;
      if (rangeHi - rangeLo < MIN_RANGE_SPAN) {
        const short = MIN_RANGE_SPAN - (rangeHi - rangeLo);
        rangeHi = Math.min(1, rangeHi + short);
        rangeLo = Math.max(0, rangeHi - MIN_RANGE_SPAN);
      }
      ranges.push({ loIdx: lo, hiIdx: hi, lo: rangeLo, hi: rangeHi, ev: peakEv });
    }

    return ranges.map(({ lo, hi, ev }) => ({ lo, hi, ev }));
  }

  // --- Chart.js rendering ----------------------------------------------------

  const AXIS_COLOR = "#8f98a0";
  const GRID_COLOR = "rgba(255,255,255,0.06)";
  // Same purple/green/(stale) age scheme as the sidebar table's cells (see
  // mono_trade_table._age_color), just with red for offer-point dots
  // instead of the table's orange.
  const AGE_DOT_COLORS = { purple: "#a855f7", green: "#6fd06f", red: "#ff6b6b" };
  const DEFAULT_DOT_COLOR = "rgba(255,255,255,0.9)";

  // Custom Chart.js plugin: wear-tier separators (dashed verticals) and an
  // emphasized zero line, drawn on top of whichever chart opts into them via
  // `options.plugins.bdChartGuides`. Kept generic/reusable rather than
  // one-off per chart.
  const bdChartGuides = {
    id: "bdChartGuides",
    afterDraw(chart, _args, opts) {
      if (!opts) return;
      const { ctx, chartArea, scales } = chart;
      ctx.save();
      if (opts.verticalLines && opts.verticalLines.length) {
        ctx.strokeStyle = opts.verticalColor || "rgba(255,255,255,0.35)";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        for (const xVal of opts.verticalLines) {
          const xPix = scales.x.getPixelForValue(xVal);
          ctx.beginPath();
          ctx.moveTo(xPix, chartArea.top);
          ctx.lineTo(xPix, chartArea.bottom);
          ctx.stroke();
        }
      }
      if (opts.horizontalZero) {
        ctx.setLineDash([]);
        ctx.strokeStyle = opts.zeroColor || "rgba(255,255,255,0.6)";
        ctx.lineWidth = 1.5;
        const yPix = scales.y.getPixelForValue(0);
        ctx.beginPath();
        ctx.moveTo(chartArea.left, yPix);
        ctx.lineTo(chartArea.right, yPix);
        ctx.stroke();
      }
      ctx.restore();
    },
  };
  Chart.register(bdChartGuides);

  function baseScales(extra) {
    return Object.assign(
      {
        x: { type: "linear", min: 0, max: 1, ticks: { color: AXIS_COLOR }, grid: { color: GRID_COLOR } },
        y: { ticks: { color: AXIS_COLOR }, grid: { color: GRID_COLOR } },
      },
      extra || {}
    );
  }

  function buildBucketChart(canvas, buckets, offerPoints, wearBoundaries) {
    return new Chart(canvas, {
      type: "bar",
      data: {
        datasets: [
          {
            type: "bar",
            label: "Price",
            data: buckets.map((b) => ({ x: (b.lo + b.hi) / 2, y: b.price })),
            backgroundColor: buckets.map((b) => (b.estimated ? "rgba(102,192,244,0.25)" : "rgba(102,192,244,0.7)")),
            barPercentage: 1.0,
            categoryPercentage: 1.0,
            order: 2,
          },
          {
            type: "scatter",
            label: "Offers",
            data: offerPoints.map((p) => ({ x: p.float_value, y: p.price })),
            backgroundColor: offerPoints.map((p) => AGE_DOT_COLORS[p.color] || DEFAULT_DOT_COLOR),
            pointRadius: 2,
            pointHoverRadius: 3,
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: baseScales({ y: { beginAtZero: true, ticks: { color: AXIS_COLOR }, grid: { color: GRID_COLOR } } }),
        plugins: {
          legend: { display: false },
          bdChartGuides: { verticalLines: wearBoundaries },
        },
      },
    });
  }

  function buildRevenueChart(canvas, revenuePoints, wearBoundaries) {
    return new Chart(canvas, {
      type: "line",
      data: {
        datasets: [
          {
            data: revenuePoints,
            borderColor: "#6fd06f",
            backgroundColor: "transparent",
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: baseScales({ y: { beginAtZero: true, ticks: { color: AXIS_COLOR }, grid: { color: GRID_COLOR } } }),
        plugins: {
          legend: { display: false },
          bdChartGuides: { verticalLines: wearBoundaries },
        },
      },
    });
  }

  function buildEvChart(canvas, evPoints, wearBoundaries) {
    const displayValues = evPoints.filter((p) => p.displayEv !== null).map((p) => p.displayEv);
    const minY = displayValues.length ? Math.min(0, ...displayValues) : 0;
    const maxY = displayValues.length ? Math.max(0, ...displayValues) : 0;
    return new Chart(canvas, {
      type: "line",
      data: {
        datasets: [
          {
            data: evPoints.map((p) => ({ x: p.x, y: p.displayEv })),
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0,
            segment: {
              borderColor: (ctx) => (ctx.p0.parsed.y < 0 || ctx.p1.parsed.y < 0 ? "#ff6b6b" : "#6fd06f"),
            },
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: baseScales({
          y: { suggestedMin: minY, suggestedMax: maxY, ticks: { color: AXIS_COLOR }, grid: { color: GRID_COLOR } },
        }),
        plugins: {
          legend: { display: false },
          bdChartGuides: { verticalLines: wearBoundaries, horizontalZero: true },
        },
      },
    });
  }

  // Computes everything from one float_diagrams reply and (re)draws all 3
  // canvases. Callers own chart lifetime -- destroy() tears down every
  // Chart.js instance this call created, meant to be invoked again before
  // the next render() (see sidebar.js's floatDiagrams watcher).
  function render(canvases, floatDiagrams) {
    const wearBuckets = floatDiagrams.wear_buckets;
    const wearBoundaries = wearBuckets.slice(0, -1).map((w) => w.hi);

    const buckets = computeBucketPrices(floatDiagrams.offer_points);
    const revenuePoints = computeRevenueCurve(floatDiagrams.input_skin, floatDiagrams.outcomes, wearBuckets);
    const evPoints = computeEvCurve(buckets, floatDiagrams.input_skin, floatDiagrams.outcomes, wearBuckets);
    const topRanges = topFloatRanges(evPoints, 3);

    const charts = {
      bucketChart: buildBucketChart(canvases.bucketCanvas, buckets, floatDiagrams.offer_points, wearBoundaries),
      revenueChart: buildRevenueChart(canvases.revenueCanvas, revenuePoints, wearBoundaries),
      evChart: buildEvChart(canvases.evCanvas, evPoints, wearBoundaries),
    };

    return {
      charts,
      topRanges,
      destroy() {
        charts.bucketChart.destroy();
        charts.revenueChart.destroy();
        charts.evChart.destroy();
      },
    };
  }

  window.__bdFloatDiagrams = {
    NUM_BUCKETS,
    INPUT_QUANTITY,
    ROI_FLOOR,
    normalizedFloat,
    outputFloat,
    wearForFloat,
    computeBucketPrices,
    priceAtFloat,
    revenueAt,
    computeRevenueCurve,
    computeEvCurve,
    topFloatRanges,
    render,
  };
})();
