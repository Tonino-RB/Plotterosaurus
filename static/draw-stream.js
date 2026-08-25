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

  // colorToHex / isPaintedColor / SWATCH_DRAW_SELECTOR / resolveLayerColor
  // come from static/svg-colors.js, shared with static/app.js — see
  // index.html / draw-stream.html for the script tag.

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

  // The document served by /jobs/{id}/svg is the source artwork at its own
  // size (see app/main.py get_job_svg) — placement (margins, fit_content,
  // transform_scale) hasn't been applied to it yet. So the ratio between its
  // rendered CSS px and *paper* mm is not just rect.width / paperWmm: that
  // only holds when the artwork happens to fill the page edge-to-edge.
  // app/placement.py is the single source of truth for how document mm map
  // onto page mm (CLAUDE.md); ask it via the same /placement endpoint the
  // main SPA uses instead of assuming the two are the same size.
  async function fetchJobPlacement(job) {
    try {
      const res = await fetch(`/jobs/${job.job_id}/placement`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paper_width_mm: job.paper_width_mm,
          paper_height_mm: job.paper_height_mm,
          margin_top_mm: job.margin_top_mm,
          margin_right_mm: job.margin_right_mm,
          margin_bottom_mm: job.margin_bottom_mm,
          margin_left_mm: job.margin_left_mm,
          fit_content: !!job.fit_content,
          transform_scale: job.transform_scale ?? 1,
          transform_rotation_deg: job.transform_rotation_deg ?? 0,
          transform_offset_x_mm: job.transform_offset_x_mm ?? 0,
          transform_offset_y_mm: job.transform_offset_y_mm ?? 0,
          layer_indices: (job.layer_selections || [])
            .filter((s) => s.selected !== false)
            .map((s) => s.index),
        }),
      });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  async function deriveLayerStyles(jobId, job) {
    layerColors = {};
    layerWidthsMm = {};
    try {
      const [res, placement] = await Promise.all([
        fetch(`/jobs/${jobId}/svg`),
        fetchJobPlacement(job),
      ]);
      if (!res.ok) return;
      svgHost.innerHTML = await res.text();
      const svgRoot = svgHost.querySelector("svg");
      if (!svgRoot) return;
      const rect = svgRoot.getBoundingClientRect();
      // docWmm/mmScale come straight from the placement engine's own answer:
      // the document's real-world width, and document-mm -> page-mm (see
      // Placement.mm_scale in app/placement.py — fit_scale * transform_scale).
      // Falls back to the paper width when the document declares no size of
      // its own, mirroring the same fallback the /placement endpoint uses.
      const docWmm = placement && placement.doc_width_mm > 0 ? placement.doc_width_mm : job.paper_width_mm;
      const mmScale = placement ? placement.fit_scale * (job.transform_scale ?? 1) : 1;
      const pxPerMm = docWmm > 0 && mmScale > 0 ? rect.width / (docWmm * mmScale) : 0;
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
  // emit_position / draw_trace_snapshot_lines) so a browser refresh mid-plot
  // shows everything drawn so far instead of starting blank. The endpoint
  // streams newline-delimited JSON rather than one big JSON array — a
  // multi-hour job's trace can be hundreds of thousands of points, and
  // reading the whole response into memory before drawing any of it defeats
  // the point of having moved the trace to disk in the first place. Read and
  // drawn incrementally instead, chunk by chunk, off the response's own byte
  // stream.
  async function replayTrace(jobId, job) {
    try {
      const res = await fetch("/draw-stream/trace");
      if (!res.ok || !res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let sawHeader = false;
      while (true) {
        const { done, value } = await reader.read();
        if (value) buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx);
          buf = buf.slice(idx + 1);
          if (!line) continue;
          if (!sawHeader) {
            sawHeader = true;
            let header;
            try { header = JSON.parse(line); } catch (e) { return; }
            if (header.job_id !== jobId) return;
            continue;
          }
          let pt;
          try { pt = JSON.parse(line); } catch (e) { continue; }
          resolveStrokeStyle(job, pt.stage_index);
          const p = mmToCanvas(pt.x_mm, pt.y_mm);
          if (pt.pen_down && lastPt) strokeSegment(lastPt, p);
          lastPt = p;
        }
        if (done) break;
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
      currentJobId = targetId;
      if (job && job.paper_width_mm > 0 && job.paper_height_mm > 0) {
        sizeCanvasForPaper(job.paper_width_mm, job.paper_height_mm);
      } else {
        paperScale = 0;
      }
      if (job) await deriveLayerStyles(job.job_id, job);
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
  }

  function handlePosition(msg) {
    if (paperScale <= 0) return;
    const p = mmToCanvas(msg.x_mm, msg.y_mm);
    if (msg.pen_down && lastPt) strokeSegment(lastPt, p);
    lastPt = p;
  }

  // Messages are handled strictly in arrival order, one at a time: handleState
  // is async (it awaits deriveLayerStyles/replayTrace, which can take seconds
  // on a Pi), and without this a second "state" message arriving mid-await
  // could interleave with the first, or a "position" message could jump ahead
  // and draw before resetCanvas()/replayTrace() for the job it belongs to have
  // even run. Chaining onto one promise makes every message wait for every
  // earlier one to fully finish instead — a position that arrives during a
  // slow init is drawn right after init completes rather than dropped.
  let msgChain = Promise.resolve();

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/state`);
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      msgChain = msgChain.then(() => {
        if (msg.type === "state") return handleState(msg);
        if (msg.type === "position") handlePosition(msg);
      }).catch(() => {});
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

  // Idle placeholder page, used until a job's real paper size is known: A3
  // portrait, the plotter's own paper size — so a stream that's up and
  // waiting already frames like a page instead of a 16:9 rectangle nothing
  // will ever be drawn on.
  const IDLE_PAPER_W_MM = 297;
  const IDLE_PAPER_H_MM = 420;

  (async function init() {
    await loadSettings();
    await loadBackgroundImage();
    sizeCanvasForPaper(IDLE_PAPER_W_MM, IDLE_PAPER_H_MM);
    paperScale = 0;  // placeholder paper, not a real one — stray position samples must not draw on it
    resetCanvas();
    connectWs();
  })();
})();
