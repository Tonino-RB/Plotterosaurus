(function () {
  "use strict";

  const canvas = document.getElementById("c");
  const ctx = canvas.getContext("2d");
  const idleMsg = document.getElementById("idle-msg");

  const FALLBACK_STROKE_COLOR = "#ff2d55";
  const FALLBACK_STROKE_WIDTH_PX = 4;

  const settings = { stroke_width_px: FALLBACK_STROKE_WIDTH_PX, background: "black", max_resolution_px: 2560 };
  let hasBgImage = false;
  let bgImage = null;

  let currentJobId;                 // undefined so the first "state" msg always looks like a change
  let prevActiveId = null;          // last-seen active_id, to catch a requeued job (same job_id) starting a fresh run
  let paperScale = 0;                // canvas px per document mm (canvas is always sized to the paper's own aspect ratio, so this is the only conversion factor needed)
  let layerColors = {};              // svg layer index -> "#rrggbb", auto-derived from stroke paint
  let layerWidthsMm = {};            // svg layer index -> stroke-width in mm, auto-derived from the SVG
  let lastPt = null;                 // last drawn point, canvas px
  let lastRunDelta = { dx: 0, dy: 0 };
  let currentStrokeColor = FALLBACK_STROKE_COLOR;
  let currentStrokeWidthMm = null;   // null = use the fallback px width directly, unscaled
  let initializingJob = false;       // true while handleState awaits deriveLayerStyles/replayTrace for a (re)started job — blocks handlePosition so live points can't jump the queue and draw with the still-default fallback style

  // ---- color/width helpers, ported from static/app.js (colorToHex /
  // isPaintedColor / resolveLayerColor) — this page has no shared module
  // with the main SPA, so these are small standalone copies. ----

  function colorToHex(c) {
    if (!c) return null;
    c = c.trim().toLowerCase();
    let m = c.match(/^#([0-9a-f]{3})$/);
    if (m) return "#" + m[1].split("").map((x) => x + x).join("");
    if (/^#[0-9a-f]{6}$/.test(c)) return c;
    if (/^#[0-9a-f]{8}$/.test(c)) return c.slice(0, 7);
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

  // The dominant stroke-width among a layer's drawable elements, in mm.
  // getComputedStyle resolves an SVG's own (possibly inherited-from-<g>)
  // stroke-width to actual rendered CSS px; pxPerMm (measured off the same
  // injected SVG's own rendered size, see deriveLayerStyles) converts that
  // back to the document's real-world mm — the same unit x_mm/y_mm are in.
  function resolveLayerWidthMm(layerG, pxPerMm) {
    const els = layerG.querySelectorAll(SWATCH_DRAW_SELECTOR);
    const limit = Math.min(els.length, 400);
    const counts = new Map();
    for (let i = 0; i < limit; i++) {
      const w = parseFloat(getComputedStyle(els[i]).strokeWidth);
      if (isFinite(w) && w > 0) counts.set(w, (counts.get(w) || 0) + 1);
    }
    let best = null, bestN = 0;
    for (const [w, n] of counts) if (n > bestN) { best = w; bestN = n; }
    return best != null && pxPerMm > 0 ? best / pxPerMm : null;
  }

  // getComputedStyle needs a live, attached DOM node to resolve CSS-class-based
  // stroke colors — a detached DOMParser document won't do it (same constraint
  // static/app.js's ensureSvgColors works around for the job-preview swatches).
  // visibility:hidden (not display:none) keeps it laid out so geometry queries
  // (getBoundingClientRect, getComputedStyle) still resolve correctly.
  const svgHost = document.createElement("div");
  svgHost.style.cssText = "position:absolute; width:0; height:0; overflow:hidden; visibility:hidden;";
  document.body.appendChild(svgHost);

  async function deriveLayerStyles(jobId, paperWmm) {
    layerColors = {};
    layerWidthsMm = {};
    try {
      const res = await fetch(`/jobs/${jobId}/svg`);
      if (!res.ok) return;
      svgHost.innerHTML = await res.text();
      const svgRoot = svgHost.querySelector("svg");
      if (!svgRoot) return;
      const rect = svgRoot.getBoundingClientRect();
      const pxPerMm = paperWmm > 0 ? rect.width / paperWmm : 0;
      let index = 0;
      for (const g of svgRoot.children) {
        if (g.tagName && g.tagName.toLowerCase() === "g" &&
            g.getAttribute("inkscape:groupmode") === "layer") {
          const c = resolveLayerColor(g);
          if (c) layerColors[index] = c;
          const w = resolveLayerWidthMm(g, pxPerMm);
          if (w) layerWidthsMm[index] = w;
          index++;
        }
      }
    } catch (e) {
      // nothing derived — resolveStrokeStyle falls back to global defaults
    } finally {
      svgHost.innerHTML = "";
    }
  }

  // ---- canvas sizing: always exactly the paper's own aspect ratio (no
  // separate ratio setting, no letterbox bars — the whole canvas *is* the
  // page), capped so its longer edge is draw_stream_max_resolution_px for
  // sharp lines regardless of physical paper size. ----
  function sizeCanvasForPaper(paperWmm, paperHmm) {
    const maxRes = settings.max_resolution_px > 0 ? settings.max_resolution_px : 2560;
    if (paperWmm >= paperHmm) {
      canvas.width = maxRes;
      canvas.height = Math.max(1, Math.round(maxRes * (paperHmm / paperWmm)));
    } else {
      canvas.height = maxRes;
      canvas.width = Math.max(1, Math.round(maxRes * (paperWmm / paperHmm)));
    }
    paperScale = canvas.width / paperWmm;
  }

  function backgroundFillColor() {
    return settings.background === "white" ? "#ffffff" : "#000000";
  }

  function outlineColor() {
    return settings.background === "white" ? "rgba(0,0,0,0.35)" : "rgba(255,255,255,0.35)";
  }

  // A border at the very edge of the canvas — since the canvas exactly
  // matches the paper, this is the page's own boundary, visible even before
  // any ink, so a viewer can confirm framing/scale at a glance.
  function drawPaperOutline() {
    ctx.globalCompositeOperation = "source-over";
    ctx.strokeStyle = outlineColor();
    ctx.lineWidth = 2;
    ctx.strokeRect(1, 1, canvas.width - 2, canvas.height - 2);
  }

  function resetCanvas() {
    ctx.globalCompositeOperation = "source-over";
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = backgroundFillColor();
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (hasBgImage && bgImage) {
      ctx.drawImage(bgImage, 0, 0, canvas.width, canvas.height);
    }
    drawPaperOutline();
    lastPt = null;
  }

  // Same manual-origin-jog correction the pen-cursor overlay applies
  // (static/app.js activeRunDelta) — raw x_mm/y_mm are document-frame
  // coordinates that drift from the canvas without it whenever the operator
  // nudges the origin mid-plot. Applied uniformly using the *current* delta,
  // including when replaying history — a delta changed mid-plot won't be
  // reflected exactly for earlier points, a minor known limitation.
  function activeRunDelta(state, job) {
    if (!job || job.job_id !== state.active_id) return { dx: 0, dy: 0 };
    return {
      dx: (state.manual_origin_offset_x_mm || 0) + (state.origin_nudge_x_mm || 0),
      dy: (state.manual_origin_offset_y_mm || 0) + (state.origin_nudge_y_mm || 0),
    };
  }

  // Which layer a given stage index refers to — precise when each layer is
  // its own stage (the pause_between_layers default); falls back to the
  // stage's first layer when several layers share one continuous stage.
  function layerForStage(job, stageIndex) {
    if (!job || !job.stages || job.stages.length === 0) return null;
    const stage = job.stages[stageIndex || 0];
    if (!stage || !stage.layer_indices || stage.layer_indices.length === 0) return null;
    return stage.layer_indices[0];
  }

  function resolveStrokeStyle(job, stageIndex) {
    const idx = layerForStage(job, stageIndex);
    currentStrokeColor = (idx != null && layerColors[idx]) || FALLBACK_STROKE_COLOR;
    currentStrokeWidthMm = (idx != null && layerWidthsMm[idx]) || null;
  }

  function currentStrokeWidthPx() {
    if (currentStrokeWidthMm != null && paperScale > 0) {
      return Math.max(0.5, currentStrokeWidthMm * paperScale);
    }
    return settings.stroke_width_px || FALLBACK_STROKE_WIDTH_PX;
  }

  function strokeSegment(p0, p1) {
    ctx.globalCompositeOperation = hasBgImage ? "multiply" : "source-over";
    ctx.strokeStyle = currentStrokeColor;
    ctx.lineWidth = currentStrokeWidthPx();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(p0.x, p0.y);
    ctx.lineTo(p1.x, p1.y);
    ctx.stroke();
  }

  function mmToCanvas(xMm, yMm) {
    return { x: (xMm + lastRunDelta.dx) * paperScale, y: (yMm + lastRunDelta.dy) * paperScale };
  }

  // Replays every recorded sample for this job (see app/state.py
  // emit_position / draw_trace_snapshot) so a browser refresh mid-plot shows
  // everything drawn so far instead of starting blank.
  async function replayTrace(jobId, job) {
    try {
      const res = await fetch("/draw-stream/trace");
      if (!res.ok) return;
      const data = await res.json();
      if (data.job_id !== jobId) return;
      for (const pt of data.points) {
        resolveStrokeStyle(job, pt.stage_index);
        const p = mmToCanvas(pt.x_mm, pt.y_mm);
        if (pt.pen_down && lastPt) strokeSegment(lastPt, p);
        lastPt = p;
      }
    } catch (e) {}
  }

  async function handleState(msg) {
    // Keep showing the most recent job's paper + trace after it finishes
    // (active_id back to null) until a new one actually starts, matching
    // the app's own last_active_id "hold the last run" convention.
    const targetId = msg.active_id || msg.last_active_id;
    const job = (msg.queue || []).find((j) => j.job_id === targetId);
    idleMsg.hidden = !!msg.active_id;

    // A requeued job keeps its job_id, so targetId alone can't tell a fresh
    // run apart from the same job just finishing — catch the actual start
    // transition (active_id going from empty to set) too.
    const isNewPlotStart = !!msg.active_id && msg.active_id !== prevActiveId;
    prevActiveId = msg.active_id;

    if (targetId !== currentJobId || isNewPlotStart) {
      initializingJob = true;
      currentJobId = targetId;
      if (job && job.paper_width_mm > 0 && job.paper_height_mm > 0) {
        sizeCanvasForPaper(job.paper_width_mm, job.paper_height_mm);
      } else {
        paperScale = 0;
      }
      if (job) await deriveLayerStyles(job.job_id, job.paper_width_mm);
      else { layerColors = {}; layerWidthsMm = {}; }
      resetCanvas();
      if (job) {
        lastRunDelta = activeRunDelta(msg, job);
        await replayTrace(job.job_id, job);
      }
    }

    if (job) {
      lastRunDelta = activeRunDelta(msg, job);
      resolveStrokeStyle(job, job.current_stage_index);
    }
    initializingJob = false;
  }

  function handlePosition(msg) {
    if (paperScale <= 0 || initializingJob) return;
    const p = mmToCanvas(msg.x_mm, msg.y_mm);
    if (msg.pen_down && lastPt) strokeSegment(lastPt, p);
    lastPt = p;
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/state`);
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === "state") handleState(msg);
      else if (msg.type === "position") handlePosition(msg);
    };
    ws.onclose = () => setTimeout(connectWs, 2000);
  }

  async function loadSettings() {
    try {
      const res = await fetch("/settings");
      if (!res.ok) return;
      const data = await res.json();
      settings.stroke_width_px = data.draw_stream_stroke_width_px ?? FALLBACK_STROKE_WIDTH_PX;
      settings.background = data.draw_stream_background === "white" ? "white" : "black";
      settings.max_resolution_px = data.draw_stream_max_resolution_px > 0 ? data.draw_stream_max_resolution_px : 2560;
    } catch (e) {}
  }

  async function loadBackgroundImage() {
    try {
      const res = await fetch("/draw-stream/background");
      if (!res.ok) { hasBgImage = false; return; }
      const blob = await res.blob();
      const img = new Image();
      await new Promise((resolve, reject) => {
        img.onload = resolve;
        img.onerror = reject;
        img.src = URL.createObjectURL(blob);
      });
      bgImage = img;
      hasBgImage = true;
    } catch (e) {
      hasBgImage = false;
    }
  }

  (async function init() {
    await loadSettings();
    await loadBackgroundImage();
    canvas.width = settings.max_resolution_px;
    canvas.height = Math.round(settings.max_resolution_px * 9 / 16);  // placeholder until a job's real paper size is known
    resetCanvas();
    connectWs();
  })();
})();
