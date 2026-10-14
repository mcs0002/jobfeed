/* Minimal column charts for the stats pages, no library.

   Markup: <div class="chart" data-chart='{"format":"int","points":[{"x","v","tip"}]}'>.
   One series, one colour (the accent), so no legend: the card title names it.
   Thin rounded columns from a single baseline, hairline gridlines, sparse
   x labels, the latest value labelled at its cap, and a hover tooltip on a
   full-height hit column so small bars are still easy to reach. A point with
   `lo` stacks two segments: `lo` solid at the base, the rest pale on top.
   A point with `segs` [{slug, v}] stacks one segment per entry from the base,
   each filled with var(--seg-<slug>), the stats page's area palette. Re-renders
   on resize so it fills whatever width the card has. */
(function () {
  var NS = "http://www.w3.org/2000/svg";

  function el(name, attrs) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  function fmt(v, format, axis) {
    if (format === "usd") return "$" + (axis && v >= 1 ? v.toFixed(0) : v.toFixed(2));
    if (format === "pct") return Math.round(v) + "%";
    return Math.round(v).toLocaleString("en-US");
  }

  // A "nice" axis max and step: 1, 2, 2.5 or 5 times a power of ten.
  // Whole-number data never gets a fractional step (a 2.5 tick reads as "3").
  function niceScale(max, ticks, whole) {
    if (max <= 0) return { max: 1, step: 1 };
    var raw = max / ticks;
    if (whole && raw < 1) raw = 1;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var steps = whole ? [1, 2, 5, 10] : [1, 2, 2.5, 5, 10];
    var step = mag;
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * mag >= raw) { step = steps[i] * mag; break; }
    }
    return { max: Math.ceil(max / step) * step, step: step };
  }

  function render(box) {
    var data;
    try { data = JSON.parse(box.getAttribute("data-chart")); } catch (e) { return; }
    var pts = data.points || [];
    box.innerHTML = "";
    if (!pts.length) return;

    var W = box.clientWidth || 600, H = parseInt(box.getAttribute("data-height") || "180", 10);
    var padL = 36, padR = 8, padT = 18, padB = 22;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var max = 0;
    pts.forEach(function (p) { if (p.v > max) max = p.v; });
    // `clip`: bars above clip x the median of the non-zero values are cut at
    // the top and labelled, so one outlier (a bulk first load) does not flatten
    // every other bar.
    if (data.clip) {
      var nz = pts.map(function (p) { return p.v; }).filter(function (v) { return v > 0; })
        .sort(function (a, b) { return a - b; });
      if (nz.length > 2) {
        var lim = data.clip * nz[Math.floor(nz.length / 2)], fit = 0;
        nz.forEach(function (v) { if (v <= lim && v > fit) fit = v; });
        if (fit && fit < max) max = fit;
      }
    }
    var sc = niceScale(max, 3, data.format === "int" || data.format === "pct");
    var y = function (v) { return Math.max(padT - 6, padT + plotH - (v / sc.max) * plotH); };
    var slot = plotW / pts.length;
    var barW = Math.max(3, Math.min(data.barMax || 24, slot * 0.62));

    var svg = el("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H,
      role: "img", "aria-label": box.getAttribute("aria-label") || "" });

    for (var t = 0; t <= sc.max + 1e-9; t += sc.step) {
      var gy = Math.round(y(t)) + 0.5;
      svg.appendChild(el("line", { x1: padL, x2: W - padR, y1: gy, y2: gy, class: "ch-grid" }));
      var lbl = el("text", { x: padL - 6, y: gy + 3.5, class: "ch-tick", "text-anchor": "end" });
      lbl.textContent = fmt(t, data.format, true);
      svg.appendChild(lbl);
    }

    // Aim for ~6 x labels whatever the width, always including the last.
    var every = Math.max(1, Math.ceil(pts.length / Math.max(2, Math.floor(plotW / 80))));
    var tip = box.parentNode.querySelector(".ch-tip");

    pts.forEach(function (p, i) {
      var cx = padL + slot * i + slot / 2;
      var h = Math.max(0, y(0) - y(p.v));
      var x0 = cx - barW / 2, base = y(0);
      // Hit column first so the band sits behind the bar it highlights.
      var hit = el("rect", { x: padL + slot * i, y: padT, width: slot, height: plotH, class: "ch-hit" });
      hit.addEventListener("mouseenter", function () {
        if (!tip) return;
        tip.textContent = p.tip || (p.x + ": " + fmt(p.v, data.format, false));
        tip.classList.toggle("multi", tip.textContent.indexOf("\n") >= 0);
        tip.hidden = false;
        var tx = Math.min(Math.max(cx - tip.offsetWidth / 2, 0), W - tip.offsetWidth);
        tip.style.left = tx + "px";
        tip.style.top = Math.max(0, base - h - tip.offsetHeight - 10) + "px";
        svg.classList.add("is-hovering");
        hit.classList.add("on");
      });
      hit.addEventListener("mouseleave", function () {
        if (tip) tip.hidden = true;
        svg.classList.remove("is-hovering");
        hit.classList.remove("on");
      });
      svg.appendChild(hit);
      if (h > 0) {
        var r = Math.min(4, barW / 2, h);
        // Rounded data end, square at the baseline.
        if (p.segs && p.segs.length) {
          var acc = 0;
          p.segs.forEach(function (sg, k) {
            var top = y(acc + sg.v), bot = y(acc);
            acc += sg.v;
            var sh = bot - top;
            if (sh <= 0) return;
            var attrs = { class: "ch-seg", style: "fill:var(--seg-" + sg.slug + ")" };
            if (k === p.segs.length - 1) {
              var rr = Math.min(3, barW / 2, sh);
              attrs.d = "M" + x0 + "," + bot + "V" + (top + rr)
                + "Q" + x0 + "," + top + " " + (x0 + rr) + "," + top
                + "H" + (x0 + barW - rr)
                + "Q" + (x0 + barW) + "," + top + " " + (x0 + barW) + "," + (top + rr)
                + "V" + bot + "Z";
              svg.appendChild(el("path", attrs));
            } else {
              attrs.x = x0; attrs.y = top; attrs.width = barW; attrs.height = sh;
              svg.appendChild(el("rect", attrs));
            }
          });
        } else {
          var d = "M" + x0 + "," + base + "V" + (base - h + r)
            + "Q" + x0 + "," + (base - h) + " " + (x0 + r) + "," + (base - h)
            + "H" + (x0 + barW - r)
            + "Q" + (x0 + barW) + "," + (base - h) + " " + (x0 + barW) + "," + (base - h + r)
            + "V" + base + "Z";
          var last = i === pts.length - 1 ? " is-last" : "";
          var split = typeof p.lo === "number" && p.lo < p.v;
          var tone = data.tone ? " tone-" + data.tone : "";
          svg.appendChild(el("path", { d: d, class: "ch-bar" + tone + (split ? " ch-bar-hi" : "") + last }));
          var lh = split ? Math.max(0, base - y(p.lo)) : 0;
          if (lh > 0) svg.appendChild(el("rect", { x: x0, y: base - lh, width: barW, height: lh, class: "ch-bar" + last }));
        }
      }
      if ((pts.length - 1 - i) % every === 0) {
        var xl = el("text", { x: cx, y: H - 6, class: "ch-tick", "text-anchor": "middle" });
        xl.textContent = p.x;
        svg.appendChild(xl);
      }
      if (p.v > sc.max) {
        var ol = el("text", { x: cx, y: padT - 10, class: "ch-val", "text-anchor": "middle" });
        ol.textContent = fmt(p.v, data.format, false) + " ↑";
        svg.appendChild(ol);
      } else if (i === pts.length - 1 && p.v > 0) {
        var vl = el("text", { x: cx, y: base - h - 5, class: "ch-val", "text-anchor": "middle" });
        vl.textContent = fmt(p.v, data.format, false);
        svg.appendChild(vl);
      }
    });
    box.appendChild(svg);
  }

  function renderAll() {
    document.querySelectorAll(".chart[data-chart]").forEach(render);
  }
  var t;
  window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(renderAll, 120); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", renderAll);
  else renderAll();
})();
