// SVG color helpers shared by app.js (the main SPA) and draw-stream.js (the
// standalone OBS overlay page) — see index.html / draw-stream.html for the
// script tags. No build step in this project, so sharing means a plain
// script both pages load, rather than a module or a bundler.

// CSS color string → "#rrggbb", or null when it can't be parsed (named
// colors, gradients). getComputedStyle yields rgb()/rgba() forms; the SVG's
// own attributes may carry #rgb / #rrggbb / #rrggbbaa.
function colorToHex(c) {
  if (!c) return null;
  c = c.trim().toLowerCase();
  let m = c.match(/^#([0-9a-f]{3})$/);
  if (m) return "#" + m[1].split("").map((x) => x + x).join("");
  if (/^#[0-9a-f]{6}$/.test(c)) return c;
  if (/^#[0-9a-f]{8}$/.test(c)) return c.slice(0, 7);  // drop alpha
  m = c.match(/^rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(",").map((x) => x.trim());
    if (p.length >= 3) {
      return "#" + p.slice(0, 3).map((n) => {
        const v = Math.max(0, Math.min(255, Math.round(parseFloat(n))));
        return v.toString(16).padStart(2, "0");
      }).join("");
    }
  }
  return null;
}

// True for a color that actually paints something (not none/transparent/α0).
function isPaintedColor(c) {
  if (!c || c === "none" || c === "transparent") return false;
  const m = c.match(/^rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(",").map((x) => x.trim());
    if (p.length === 4 && parseFloat(p[3]) === 0) return false;
  }
  return true;
}

const SWATCH_DRAW_SELECTOR = "path, line, polyline, polygon, circle, ellipse, rect";

// Representative pen color of a layer <g> — its most common stroke color, or
// null when nothing in it is stroked. A bounded sample keeps a huge SVG cheap.
function resolveLayerColor(layerG) {
  const els = layerG.querySelectorAll(SWATCH_DRAW_SELECTOR);
  const limit = Math.min(els.length, 400);
  const counts = new Map();
  for (let i = 0; i < limit; i++) {
    const stroke = getComputedStyle(els[i]).stroke;
    if (isPaintedColor(stroke)) counts.set(stroke, (counts.get(stroke) || 0) + 1);
  }
  let best = null, bestN = 0;
  for (const [c, n] of counts) if (n > bestN) { best = c; bestN = n; }
  return best ? colorToHex(best) : null;
}
