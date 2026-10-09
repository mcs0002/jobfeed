/* Minimal column charts for the stats pages, no library.

   Markup: <div class="chart" data-chart='{"format":"int","points":[{"x","v","tip"}]}'>.
   One series, one colour (the accent), so no legend: the card title names it.
   Thin rounded columns from a single baseline, hairline gridlines, sparse
   x labels, the latest value labelled at its cap, and a hover tooltip on a
   full-height hit column so small bars are still easy to reach. Re-renders
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
    var sc = niceScale(max, 3, data.format === "int");
    var y = function (v) { return padT + plotH - (v / sc.max) * plotH; };
    var slot = plotW / pts.length;
    var barW = Math.max(3, Math.min(24, slot * 0.62));

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
        var d = "M" + x0 + "," + base + "V" + (base - h + r)
          + "Q" + x0 + "," + (base - h) + " " + (x0 + r) + "," + (base - h)
          + "H" + (x0 + barW - r)
          + "Q" + (x0 + barW) + "," + (base - h) + " " + (x0 + barW) + "," + (base - h + r)
          + "V" + base + "Z";
        svg.appendChild(el("path", { d: d, class: "ch-bar" + (i === pts.length - 1 ? " is-last" : "") }));
      }
      if ((pts.length - 1 - i) % every === 0) {
        var xl = el("text", { x: cx, y: H - 6, class: "ch-tick", "text-anchor": "middle" });
        xl.textContent = p.x;
        svg.appendChild(xl);
      }
      if (i === pts.length - 1 && p.v > 0) {
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
