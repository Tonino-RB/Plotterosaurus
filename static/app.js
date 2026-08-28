const $ = (id) => document.getElementById(id);

const statusEl = $("status");
const dropZone = $("drop-zone");
const fileInput = $("file-input");
const uploadError = $("upload-error");
const queueList = $("queue-list");
const queueEmpty = $("queue-empty");
const queueControls = $("queue-controls");
const topMessage = $("top-message");
const plotBtn = $("plot-btn");
const pauseBtn = $("pause-btn");
const pausePenUpBtn = $("pause-pen-up-btn");
const resumeBtn = $("resume-btn");
const continueBtn = $("continue-btn");
const calibrateBtn = $("calibrate-btn");
const cancelBtn = $("cancel-btn");
const redrawControls = $("redraw-controls");
const redrawBtn = $("redraw-btn");
const redrawMm = $("redraw-mm");
const penUpBtn = $("pen-up-btn");
const penDownBtn = $("pen-down-btn");
const motorsEnableBtn = $("motors-enable-btn");
const motorsDisableBtn = $("motors-disable-btn");
const penControlsMessage = $("pen-controls-message");
const originNudge = $("origin-nudge");
const nudgeXReadout = $("nudge-x-readout");
const nudgeYReadout = $("nudge-y-readout");
const opticalReg = $("optical-reg");
const opticalRegMeasureBtn = $("optical-reg-measure-btn");
const opticalRegWidenBtn = $("optical-reg-widen-btn");
const opticalRegStatus = $("optical-reg-status");
const opticalRegResult = $("optical-reg-result");
const opticalRegPreview = $("optical-reg-preview");
const opticalRegReadout = $("optical-reg-readout");
const opticalRegApplyBtn = $("optical-reg-apply-btn");
const opticalRegDismissBtn = $("optical-reg-dismiss-btn");
const jogXReadout = $("jog-x-readout");
const jogYReadout = $("jog-y-readout");
const jogXInput = $("jog-x-input");
const jogYInput = $("jog-y-input");
const jogMoveBtn = $("jog-move-btn");
const jogShortcutBtn = $("jog-shortcut-btn");
const jogHomeBtn = $("jog-home-btn");
const jogOriginBtn = $("jog-origin-btn");
const calibrationFileRow = $("calibration-file-row");
const calibrationFileSelect = $("calibration-file-select");
const calibrationFileRunBtn = $("calibration-file-run-btn");

// Camera / plot recording
const cameraControls = $("camera-controls");
const cameraStartBtn = $("camera-start-btn");
const cameraPauseBtn = $("camera-pause-btn");
const cameraResumeBtn = $("camera-resume-btn");
const cameraStopBtn = $("camera-stop-btn");
const cameraRecordingIndicator = $("camera-recording-indicator");
const cameraControlsMessage = $("camera-controls-message");
const cameraSettingsBtn = $("camera-settings-btn");
const cameraSettingsModal = $("camera-settings-modal");
const cameraPreviewFrame = $("camera-preview-frame");
const cameraPreviewPausedOverlay = $("camera-preview-paused-overlay");
const cameraAfMode = $("camera-af-mode");
const cameraAfSpeed = $("camera-af-speed");
const cameraLensPositionField = $("camera-lens-position-field");
const cameraLensPosition = $("camera-lens-position");
const cameraResolutionWidth = $("camera-resolution-width");
const cameraResolutionHeight = $("camera-resolution-height");
const cameraFps = $("camera-fps");
const cameraBitrate = $("camera-bitrate");
const cameraBrightness = $("camera-brightness");
const cameraContrast = $("camera-contrast");
const cameraSaturation = $("camera-saturation");
const cameraSharpness = $("camera-sharpness");
const cameraEv = $("camera-ev");
const cameraExposureMode = $("camera-exposure-mode");
const cameraShutterUs = $("camera-shutter-us");
const cameraGain = $("camera-gain");
const cameraAwbMode = $("camera-awb-mode");
const cameraDenoise = $("camera-denoise");
const cameraFlickerMode = $("camera-flicker-mode");
const cameraHflip = $("camera-hflip");
const cameraVflip = $("camera-vflip");
const cameraRecordPlotDefault = $("camera-record-plot-default");
const cameraRecordingMode = $("camera-recording-mode");
const cameraTimelapseInterval = $("camera-timelapse-interval");
const cameraSpeedMultiplier = $("camera-speed-multiplier");
const cameraOutputFolder = $("camera-output-folder");
const cameraRcloneTarget = $("camera-rclone-target");
const cameraRcloneDeleteLocal = $("camera-rclone-delete-local");
const cameraRetentionGb = $("camera-retention-gb");
const cameraRecordingsList = $("camera-recordings-list");
const cameraRecordingsNote = $("camera-recordings-note");
const cameraRecordingsRefresh = $("camera-recordings-refresh");
const cameraFinalizeFailures = $("camera-finalize-failures");
const opticalRegMarkX = $("optical-reg-mark-x");
const opticalRegMarkY = $("optical-reg-mark-y");
const opticalRegMarkSize = $("optical-reg-mark-size");
const opticalRegProbeOffset = $("optical-reg-probe-offset");
const opticalRegCalibrateBtn = $("optical-reg-calibrate-btn");
const opticalRegCalibrateStatus = $("optical-reg-calibrate-status");
const cameraRtspUrl = $("camera-rtsp-url");
const cameraHlsUrl = $("camera-hls-url");
const cameraSettingsMessage = $("camera-settings-message");
const jobCardTemplate = $("job-card-template");
const queueProgress = $("queue-progress");

// Jobs the plot worker is not touching: editable, and planned in the
// background so the estimate is ready before they are plotted. There is only
// one such status — a job is plottable the moment it reaches the top of the
// list, with no separate "committed to the queue" step.
const IDLE_JOB_STATUSES = ["ready"];

function statusLabel(key) {
  return t(`status.${key}`);
}

let appSettings = {
  plotter_model: 2,
  pause_between_layers_default: true,
  delete_on_complete_default: false,
  disable_motors_on_complete_default: false,
  speed_pendown_default: 25,
  speed_penup_default: 75,
  acceleration_default: 75,
  optimize_svg_default: false,
  optimize_svg_tolerance_default_mm: 0.10,
  optimize_svg_linemerge_default: true,
  optimize_svg_linesimplify_default: true,
  optimize_svg_linesort_default: true,
  optimize_svg_reloop_default: true,
  optimize_expert_1_cmd_default: "",
  optimize_expert_2_cmd_default: "",
  optimize_expert_3_cmd_default: "",
  display_unit: null,
};

// Length-unit conversion. Internal storage and inputs are always mm; this
// table is for display-only formatting. Tolerance (vpype) keeps mm.
const MM_TO = { mm: 1, cm: 0.1, in: 1 / 25.4 };

// Initial-only fallback used when the user hasn't picked a unit yet:
// en-US locale → inches, everywhere else → millimetres. Once the user
// saves a choice, that value wins on every subsequent page load.
function localeDefaultUnit() {
  try {
    const lang = (navigator.language || "").toLowerCase();
    if (lang === "en-us" || lang.startsWith("en-us-")) return "in";
  } catch {}
  return "mm";
}

// The sentence a failed job carries. The plot worker writes an English one
// onto the job record and, since it is the message a user meets at the worst
// moment, a `joberror.` key and its arguments beside it (plot_worker._fail).
// Prefer the key; fall back to the English when the catalog has not caught up
// or an older server sent none, so a message never disappears entirely.
function jobErrorText(job) {
  if (!job.error_code) return job.error || "";
  const key = "joberror." + job.error_code;
  const translated = t(key, job.error_params || {});
  return translated === key ? (job.error || "") : translated;
}

function effectiveDisplayUnit() {
  const u = appSettings.display_unit;
  if (u === "mm" || u === "cm" || u === "in") return u;
  return localeDefaultUnit();
}

// mm renders as a whole number (paper presets are always integer mm anyway);
// cm and in get one decimal so a 297 mm height shows as "29.7 cm" / "11.7 in".
function formatLengthValue(mm, unit) {
  unit = unit || effectiveDisplayUnit();
  const v = mm * MM_TO[unit];
  return unit === "mm" ? Math.round(v).toString() : v.toFixed(1);
}

function fmtLength(mm, unit) {
  unit = unit || effectiveDisplayUnit();
  if (mm == null || !isFinite(mm)) return "—";
  return `${formatLengthValue(mm, unit)} ${unit}`;
}

// Paper size database (portrait dims). Landscape swaps them.
const PAPER_SIZES = {
  A0: { w: 841, h: 1189 },
  A1: { w: 594, h: 841 },
  A2: { w: 420, h: 594 },
  A3: { w: 297, h: 420 },
  A4: { w: 210, h: 297 },
  A5: { w: 148, h: 210 },
  B0: { w: 1000, h: 1414 },
  B1: { w: 707, h: 1000 },
  B2: { w: 500, h: 707 },
  B3: { w: 353, h: 500 },
  B4: { w: 250, h: 353 },
  B5: { w: 176, h: 250 },
  Letter: { w: 216, h: 279 },
  Legal: { w: 216, h: 356 },
  Ledger: { w: 279, h: 432 },
  "ANSI-C": { w: 432, h: 559 },
  "ANSI-D": { w: 559, h: 864 },
  "ANSI-E": { w: 864, h: 1118 },
};

// Runtime state mirrored from the server.
let serverState = { queue: [], active_id: null, status: "idle" };
const cardEls = new Map();                 // job_id → card DOM element
const cardCtx = new Map();                 // job_id → per-card state (svg metadata, manual-fit flag, render timer)
let sharedElapsedTimer = null;             // single interval for the sticky-bar progress


// Turn a FastAPI error `detail` into a display string. Coded errors arrive as
// {code, params} and are localized via the apierror.* catalog; plain-string
// and structured-message details fall through to their text.
function apiErrText(detail) {
  if (detail && typeof detail === "object") {
    if (detail.code) return t(`apierror.${detail.code}`, detail.params || {});
    if (detail.message) return detail.message;
  }
  return detail != null ? String(detail) : "";
}

// Pull a readable message out of a fetch Response. FastAPI errors look like
// {"detail": ...}; plain text is passed through unchanged.
async function readErr(res) {
  return (await readErrDetail(res)).text;
}

// Same, but also exposes the machine-readable code (see _coded in main.py) so
// a caller can single out one specific rejection instead of just reporting it
// — the below-origin confirmation needs to tell "not allowed yet" apart from
// a real failure. A response body can only be read once, so callers that need
// both take this and use .text.
async function readErrDetail(res) {
  const text = await res.text();
  try {
    const data = JSON.parse(text);
    if (data && typeof data === "object" && data.detail != null) {
      const detail = data.detail;
      return {
        code: (detail && typeof detail === "object" && detail.code) || null,
        text: apiErrText(detail),
      };
    }
  } catch {}
  return { code: null, text };
}

// ───── Upload ────────────────────────────────────────────────────────────

fileInput.addEventListener("change", (e) => {
  handleDroppedFiles(e.target.files);
  fileInput.value = "";
});
dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag");
  handleDroppedFiles(e.dataTransfer.files);
});

// The native `accept=".svg,..."` only filters the file picker; drag-and-drop
// bypasses it. Reject non-SVGs up front so the user sees a clean message
// instead of an XML parse error from the server.
function isSvgFile(file) {
  if (file.type === "image/svg+xml") return true;
  return /\.svg$/i.test(file.name || "");
}

function handleDroppedFiles(fileList) {
  const files = Array.from(fileList || []);
  const ok = files.filter(isSvgFile);
  const bad = files.filter((f) => !isSvgFile(f));
  if (bad.length) {
    uploadError.textContent = bad.length === 1
      ? t("upload.not_svg", { name: bad[0].name })
      : t("upload.files_skipped", { count: bad.length });
    uploadError.hidden = false;
  } else {
    uploadError.hidden = true;
    uploadError.textContent = "";
  }
  for (const f of ok) uploadAndQueue(f);
}

// The job a document should become, with every field at its configured
// default. Shared by a fresh drop and a library pick — those two used to be one
// code path because only one existed, and letting them diverge would mean a
// drawing behaves differently depending on which way it entered the queue.
//
// `svg` is the payload from /upload or /library/select; both return the same
// shape (id, filename, layers, width_mm, height_mm), which is why one function
// can serve both.
function buildJobPayload(svg, fallbackName) {
  // Select all layers: on a fresh upload that means a clean start, and on a
  // library pick it means the drawing arrives whole rather than inheriting a
  // selection from whatever job last used it.
  const layer_selections = svg.layers.map((l) => ({ index: l.index, label: l.label }));

  // Auto-detect paper
  const detected = detectPaper(svg.width_mm, svg.height_mm);
  const { w, h } = applyMachineAutoRotate(computePaperDims(detected.preset, detected.orientation,
    svg.width_mm || 210, svg.height_mm || 297));

  return {
      svg_id: svg.id,
      filename: svg.filename || fallbackName || "upload.svg",
      // Set when the source is a copy promoted out of a .opt.svg. The server
      // forces optimize_svg off for it; the card locks the panel to match.
      pre_optimized: !!svg.pre_optimized,
      layer_selections,
      layer_mode: appSettings.layer_mode_default || "layer",
      pause_between_layers: appSettings.pause_between_layers_default,
      delete_on_complete: appSettings.delete_on_complete_default,
      disable_motors_on_complete: appSettings.disable_motors_on_complete_default,
      paper_width_mm: w,
      paper_height_mm: h,
      margin_top_mm: 0,
      margin_right_mm: 0,
      margin_bottom_mm: 0,
      margin_left_mm: 0,
      fit_content: false,
      transform_scale: 1.0,
      transform_rotation_deg: 0,
      transform_offset_x_mm: 0,
      transform_offset_y_mm: 0,
      speed_pendown: appSettings.speed_pendown_default,
      speed_penup: appSettings.speed_penup_default,
      acceleration: appSettings.acceleration_default,
      pen_pos_up: appSettings.pen_pos_up_default,
      pen_pos_down: appSettings.pen_pos_down_default,
      record_plot: appSettings.record_plot_default,
      optical_reg: appSettings.optical_reg_default,
      record_mode: appSettings.camera_recording_mode_default,
      record_timelapse_interval_s: appSettings.camera_timelapse_interval_s_default,
      record_speed_multiplier: appSettings.camera_speed_multiplier_default,
      optimize_svg: appSettings.optimize_svg_default,
      optimize_svg_tolerance_mm: appSettings.optimize_svg_tolerance_default_mm,
      optimize_svg_linemerge: appSettings.optimize_svg_linemerge_default,
      optimize_svg_linesimplify: appSettings.optimize_svg_linesimplify_default,
      optimize_svg_linesort: appSettings.optimize_svg_linesort_default,
      optimize_svg_reloop: appSettings.optimize_svg_reloop_default,
      // Expert mode always starts off for a new job; only the boxes' last-typed
      // text is remembered (see config.OPTIMIZE_EXPERT_*_CMD_DEFAULT).
      optimize_mode: "beginner",
      optimize_expert_1_cmd: appSettings.optimize_expert_1_cmd_default,
      optimize_expert_2_cmd: appSettings.optimize_expert_2_cmd_default,
      optimize_expert_3_cmd: appSettings.optimize_expert_3_cmd_default,
  };
}

// POST the job. It lands as `ready` (see main.create_job) — the card appears
// via the WebSocket broadcast, and createCardForJob fetches the document
// itself, so there is nothing to insert here.
async function createJobFromSvg(svg, fallbackName) {
  const res = await fetch("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildJobPayload(svg, fallbackName)),
  });
  if (!res.ok) throw new Error(await readErr(res));
  return res.json();
}

async function uploadAndQueue(file) {
  const label = dropZone.querySelector("span");
  uploadError.hidden = true;
  uploadError.textContent = "";
  dropZone.classList.add("loading");
  label.textContent = t("upload.processing", { name: file.name });
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch("/upload", { method: "POST", body: fd });
    if (!res.ok) throw new Error(await readErr(res));
    await createJobFromSvg(await res.json(), file.name);
    loadLibrary();   // the drawing is on disk now, so it has a library row
  } catch (e) {
    uploadError.textContent = t("upload.failed", { message: e.message });
    uploadError.hidden = false;
  } finally {
    dropZone.classList.remove("loading");
    label.textContent = t("upload.drop_hint");
  }
}

// ───── Library ───────────────────────────────────────────────────────────
//
// The uploads folder, made addressable. Every drawing ever uploaded stays on
// disk after its job is deleted and nothing reclaimed it, so the folder only
// grew. Rather than sweep it automatically — it holds the user's own artwork,
// and deleting that to save space is the wrong default — it is shown, and the
// decisions are theirs: replot a previous drawing, delete one, or empty
// everything nothing is using.
//
// A drawing with a cached optimization appears twice, as the original and as
// the optimized copy, because the two plot differently. Picking the optimized
// row promotes it server-side into a source of its own (see
// main._promote_optimized), so nothing below has to know variants exist beyond
// naming one in the request.

const libraryList = $("library-list");
const libraryUsage = $("library-usage");
const libraryCleanBtn = $("library-clean");
let libraryEntries = [];
let libraryBusy = false;

function formatBytes(n) {
  if (!n) return "0 kB";
  const mb = n / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} kB`;
}

async function loadLibrary() {
  try {
    const res = await fetch("/library");
    if (!res.ok) return;
    const body = await res.json();
    libraryEntries = body.entries || [];
    libraryUsage.textContent = t("library.usage", {
      total: formatBytes(body.total_bytes),
      free: formatBytes(body.reclaimable_bytes),
    });
    libraryCleanBtn.disabled = !body.reclaimable_bytes;
    renderLibrary();
  } catch (e) {
    // A library that won't load is not worth an error banner over the drop
    // zone — the drop zone itself still works, which is the important half.
    console.warn("library fetch failed", e);
  }
}

function renderLibrary() {
  libraryList.innerHTML = "";
  if (!libraryEntries.length) {
    const li = document.createElement("li");
    li.className = "library-empty";
    li.textContent = t("library.empty");
    libraryList.appendChild(li);
    return;
  }
  // A promoted copy of another row's optimized variant (see
  // main._promote_optimized) is an internal snapshot, not a drawing the user
  // separately added — listing it here read as a new file silently appearing
  // every time the original "optimized" row was loaded. Its bytes are real
  // though, and /library counts them in the usage line above, so charge them
  // to the row they came from rather than dropping them: the sizes on screen
  // then add up to the total. A copy whose parent is gone has no row to hide
  // behind, so it is listed on its own instead of vanishing from both.
  const hostKey = new Map();        // parent svg_id -> key of the row to charge
  for (const e of libraryEntries) {
    if (e.derived_from) continue;
    // The optimized row when there is one: that variant is what was promoted.
    if (e.variant === "optimized" || !hostKey.has(e.svg_id)) hostKey.set(e.svg_id, e.key);
  }
  const derivedBytes = new Map();   // row key -> bytes of the copies it hosts
  for (const e of libraryEntries) {
    const host = e.derived_from ? hostKey.get(e.derived_from) : null;
    if (!host) continue;
    derivedBytes.set(host, (derivedBytes.get(host) || 0) + e.size_bytes);
  }
  for (const e of libraryEntries) {
    if (e.derived_from && hostKey.has(e.derived_from)) continue;
    const li = document.createElement("li");
    li.dataset.svgId = e.svg_id;
    li.dataset.variant = e.variant;
    const badges = [];
    if (e.variant === "optimized") {
      badges.push(`<span class="library-badge" title="${escapeHtml(t("library.optimized_title"))}">${escapeHtml(t("library.optimized"))}</span>`);
    }
    if (e.in_use) {
      badges.push(`<span class="library-badge in-use" title="${escapeHtml(t("library.in_use_title"))}">${escapeHtml(t("library.in_use"))}</span>`);
    }
    li.innerHTML = `
      <span class="library-name" title="${escapeHtml(e.filename)}">${escapeHtml(e.filename)}</span>
      ${badges.join("")}
      <span class="library-meta">${escapeHtml(formatBytes(e.size_bytes + (derivedBytes.get(e.key) || 0)))}</span>
      <span class="library-actions">
        <button type="button" class="icon-btn library-add" title="${escapeHtml(t("library.add"))}" data-i18n-title="library.add">+</button>
        <button type="button" class="icon-btn library-del" title="${escapeHtml(t("library.delete_title"))}" data-i18n-title="library.delete_title">✕</button>
      </span>`;
    libraryList.appendChild(li);
  }
}

// Delegated once, so re-rendering rows never stacks duplicate listeners.
libraryList.addEventListener("click", async (ev) => {
  const li = ev.target.closest("li[data-svg-id]");
  if (!li || libraryBusy) return;
  const svg_id = li.dataset.svgId;
  const variant = li.dataset.variant;
  const entry = libraryEntries.find((e) => e.svg_id === svg_id && e.variant === variant);

  if (ev.target.closest(".library-del")) {
    if (!confirm(t("library.confirm_delete", { name: entry ? entry.filename : svg_id }))) return;
    await libraryAction(() => fetch(`/library/${svg_id}?variant=${variant}`, { method: "DELETE" }));
    return;
  }
  if (ev.target.closest(".library-add")) {
    await libraryAction(async () => {
      const res = await fetch("/library/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ svg_id, variant }),
      });
      if (!res.ok) return res;
      const svg = await res.json();
      // Re-adding a drawing that's already sitting untouched shouldn't spawn
      // a second copy of it — jump to the existing one instead of piling up
      // duplicate cards for the same load.
      const existing = serverState.queue.find((j) => j.svg_id === svg.id && j.status === "ready");
      if (existing) {
        const card = queueList.querySelector(`[data-id="${existing.job_id}"]`);
        if (card) {
          card.scrollIntoView({ behavior: "smooth", block: "center" });
          card.classList.add("card-highlight");
          setTimeout(() => card.classList.remove("card-highlight"), 1200);
        }
      } else {
        await createJobFromSvg(svg);
      }
      return res;
    });
  }
});

// One place for the shared shape of every library mutation: block re-entry,
// surface a failure where the user is looking, and refresh the list either way.
async function libraryAction(run) {
  libraryBusy = true;
  try {
    const res = await run();
    if (res && !res.ok) {
      uploadError.textContent = t("library.action_failed", { message: await readErr(res) });
      uploadError.hidden = false;
    } else {
      uploadError.hidden = true;
      uploadError.textContent = "";
    }
  } catch (e) {
    uploadError.textContent = t("library.action_failed", { message: e.message });
    uploadError.hidden = false;
  } finally {
    libraryBusy = false;
    loadLibrary();
  }
}

// Clean-up confirm. Modelled on the shutdown modal rather than the dirty-update
// one: a destructive folder operation wants its result reported in place.
const libraryCleanModal = $("library-clean-modal");
const libraryCleanNote = $("library-clean-note");
const libraryCleanCancel = $("library-clean-cancel");
const libraryCleanConfirm = $("library-clean-confirm");
const libraryCleanMessage = $("library-clean-message");

function closeLibraryClean() { libraryCleanModal.hidden = true; }

libraryCleanBtn.addEventListener("click", () => {
  const spare = libraryEntries.filter((e) => !e.in_use);
  const bytes = spare.reduce((n, e) => n + e.size_bytes, 0);
  libraryCleanMessage.textContent = "";
  libraryCleanMessage.className = "muted";
  libraryCleanConfirm.disabled = !spare.length;
  libraryCleanCancel.disabled = false;
  libraryCleanNote.textContent = spare.length
    ? t("library.clean_confirm", { count: spare.length, size: formatBytes(bytes) })
    : t("library.nothing_to_clean");
  libraryCleanModal.hidden = false;
});
libraryCleanCancel.addEventListener("click", closeLibraryClean);
libraryCleanModal.addEventListener("click", (e) => {
  if (e.target === libraryCleanModal) closeLibraryClean();
});
libraryCleanConfirm.addEventListener("click", async () => {
  libraryCleanConfirm.disabled = true;
  libraryCleanCancel.disabled = true;
  try {
    const res = await fetch("/library/clean", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    const body = await res.json();
    libraryCleanMessage.textContent = t("library.cleaned", {
      count: body.removed, size: formatBytes(body.freed_bytes),
    });
    loadLibrary();
  } catch (e) {
    libraryCleanMessage.textContent = t("library.action_failed", { message: e.message });
    libraryCleanMessage.className = "error";
  } finally {
    libraryCleanCancel.disabled = false;
  }
});

// ───── Calibration library ────────────────────────────────────────────────
//
// A second, read-only tab in the same panel: standalone test SVGs kept in
// the calibration/ folder (managed on disk, not from this UI), for trying
// paper/pen alignment before committing to a real job. Picking one copies it
// into the uploads folder under a fresh id (see main._promote_calibration_file)
// so the rest of the pipeline treats it exactly like any other drawing — the
// original file in calibration/ is never touched, so it's immune to Clean up
// the same way any file outside the uploads folder would be.

const libraryTabs = $("library-tabs");
const calibrationList = $("calibration-list");
let calibrationFiles = null; // null = not yet fetched
let calibrationBusy = false;

libraryTabs.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-tab]");
  if (!btn) return;
  const isCal = btn.dataset.tab === "calibration";
  for (const b of libraryTabs.querySelectorAll("button")) {
    b.classList.toggle("active", b === btn);
  }
  libraryList.hidden = isCal;
  calibrationList.hidden = !isCal;
  libraryUsage.hidden = isCal;
  libraryCleanBtn.hidden = isCal;
  if (isCal && calibrationFiles === null) loadCalibrationLibrary();
});

async function loadCalibrationLibrary() {
  try {
    const res = await fetch("/calibration/files");
    if (!res.ok) return;
    const body = await res.json();
    calibrationFiles = body.files || [];
    renderCalibrationLibrary();
  } catch (e) {
    console.warn("calibration library fetch failed", e);
  }
}

function renderCalibrationLibrary() {
  calibrationList.innerHTML = "";
  const files = calibrationFiles || [];
  if (!files.length) {
    const li = document.createElement("li");
    li.className = "library-empty";
    li.textContent = t("library.calibration_empty");
    calibrationList.appendChild(li);
    return;
  }
  for (const filename of files) {
    const li = document.createElement("li");
    li.dataset.filename = filename;
    li.innerHTML = `
      <span class="library-name" title="${escapeHtml(filename)}">${escapeHtml(filename)}</span>
      <span class="library-actions">
        <button type="button" class="neutral calibration-run" title="${escapeHtml(t("library.calibration_run_title"))}">${escapeHtml(t("library.calibration_run"))}</button>
        <button type="button" class="icon-btn calibration-add" title="${escapeHtml(t("library.add"))}" data-i18n-title="library.add">+</button>
      </span>`;
    calibrationList.appendChild(li);
  }
}

async function resolveCalibrationSvg(filename) {
  const res = await fetch("/calibration/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  });
  if (!res.ok) throw new Error(await readErr(res));
  return res.json();
}

// Both actions resolve the file into a job the same way library-add does —
// reusing an already-queued ready job for it rather than piling up duplicates.
async function jobForCalibrationFile(filename) {
  const svg = await resolveCalibrationSvg(filename);
  const existing = serverState.queue.find((j) => j.svg_id === svg.id && j.status === "ready");
  return existing || createJobFromSvg(svg);
}

calibrationList.addEventListener("click", async (ev) => {
  const li = ev.target.closest("li[data-filename]");
  if (!li || calibrationBusy) return;
  const filename = li.dataset.filename;
  calibrationBusy = true;
  try {
    if (ev.target.closest(".calibration-add")) {
      const job = await jobForCalibrationFile(filename);
      const card = queueList.querySelector(`[data-id="${job.job_id}"]`);
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("card-highlight");
        setTimeout(() => card.classList.remove("card-highlight"), 1200);
      }
    } else if (ev.target.closest(".calibration-run")) {
      const job = await jobForCalibrationFile(filename);
      // Jump the queue: this is meant to run right now, ahead of anything
      // already waiting. A plot already in progress can't be interrupted —
      // start_plot() below is a no-op then, and this job simply runs next.
      const moveRes = await fetch(`/jobs/${job.job_id}/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_index: 0 }),
      });
      if (!moveRes.ok) throw new Error(await readErr(moveRes));
      const startRes = await fetch("/queue/start", { method: "POST" });
      if (!startRes.ok) throw new Error(await readErr(startRes));
    }
    uploadError.hidden = true;
    uploadError.textContent = "";
  } catch (e) {
    uploadError.textContent = t("library.action_failed", { message: e.message });
    uploadError.hidden = false;
  } finally {
    calibrationBusy = false;
    loadLibrary(); // the promoted copy now counts toward library usage
  }
});

function detectPaper(w_mm, h_mm) {
  if (!w_mm || !h_mm) return { preset: "A4", orientation: "portrait" };
  const rW = Math.round(w_mm * 10) / 10;
  const rH = Math.round(h_mm * 10) / 10;
  for (const [name, p] of Object.entries(PAPER_SIZES)) {
    if (Math.abs(p.w - rW) < 0.5 && Math.abs(p.h - rH) < 0.5) return { preset: name, orientation: "portrait" };
    if (Math.abs(p.h - rW) < 0.5 && Math.abs(p.w - rH) < 0.5) return { preset: name, orientation: "landscape" };
  }
  return { preset: "Custom", orientation: rW >= rH ? "landscape" : "portrait" };
}

// Inject a "<name> (W × H mm)" option for jobs whose `paper_size_name` does
// not match the auto-detected preset. Returns true if injected. The option
// remembers its dimensions on `dataset.w/h` so swapping orientation works.
function injectNamedCustomOption(selectEl, job) {
  selectEl.querySelectorAll('option[data-named-custom="1"]').forEach((o) => o.remove());
  if (!job.paper_size_name) return false;
  const guessed = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm);
  if (guessed.preset !== "Custom" &&
      guessed.preset.toLowerCase() === job.paper_size_name.toLowerCase()) {
    return false;
  }
  const opt = document.createElement("option");
  opt.value = "__named_custom__";
  opt.dataset.namedCustom = "1";
  opt.dataset.name = job.paper_size_name;
  opt.dataset.w = String(job.paper_width_mm);
  opt.dataset.h = String(job.paper_height_mm);
  opt.textContent = formatPaperOptionLabel(job.paper_size_name, job.paper_width_mm, job.paper_height_mm);
  const customOpt = selectEl.querySelector('option[value="Custom"]');
  selectEl.insertBefore(opt, customOpt);
  return true;
}

function formatPaperOptionLabel(name, w_mm, h_mm) {
  const u = effectiveDisplayUnit();
  return `${name} (${formatLengthValue(w_mm, u)} × ${formatLengthValue(h_mm, u)} ${u})`;
}

// Re-format every option in a paper-size dropdown with the current display
// unit. Static preset options always show portrait dims (matching the
// existing convention); the named-custom option pulls live dims from its
// dataset (kept in sync by readPaperFromCard on orientation flips).
function relabelPaperOptions(selectEl) {
  if (!selectEl) return;
  selectEl.querySelectorAll("option").forEach((opt) => {
    if (opt.value === "Custom") return;
    if (opt.dataset.namedCustom === "1") {
      const w = parseFloat(opt.dataset.w);
      const h = parseFloat(opt.dataset.h);
      opt.textContent = formatPaperOptionLabel(opt.dataset.name, w, h);
      return;
    }
    const dims = PAPER_SIZES[opt.value];
    if (!dims) return;
    opt.textContent = formatPaperOptionLabel(opt.value, dims.w, dims.h);
  });
}

// Resolve the current paper-size selection on a card to {w, h, paper_size_name}.
// Encapsulates the named-custom logic so onPaperChange and sendCardUpdate
// don't both have to know about it.
function readPaperFromCard(card) {
  const sel = card.querySelector(".paper-size");
  const opt = sel.options[sel.selectedIndex];
  const orientation = getSegmentedValue(card.querySelector(".orientation"), "portrait");
  if (sel.value === "__named_custom__" && opt) {
    let w = parseFloat(opt.dataset.w);
    let h = parseFloat(opt.dataset.h);
    if (orientation === "landscape" && h > w) [w, h] = [h, w];
    if (orientation === "portrait" && w > h) [w, h] = [h, w];
    // Keep the option's stored dims and visible label in sync with orientation
    // toggles so the dropdown text doesn't drift from the actual dimensions.
    opt.dataset.w = String(w);
    opt.dataset.h = String(h);
    opt.textContent = formatPaperOptionLabel(opt.dataset.name, w, h);
    return { w, h, paper_size_name: opt.dataset.name };
  }
  const customW = parseFloat(card.querySelector(".paper-w").value) || 210;
  const customH = parseFloat(card.querySelector(".paper-h").value) || 297;
  const { w, h } = computePaperDims(sel.value, orientation, customW, customH);
  return { w, h, paper_size_name: null };
}

function computePaperDims(preset, orientation, customW, customH) {
  if (preset === "Custom") {
    let w = customW || 210;
    let h = customH || 297;
    if (orientation === "landscape" && h > w) [w, h] = [h, w];
    if (orientation === "portrait" && w > h) [w, h] = [h, w];
    return { w, h };
  }
  const p = PAPER_SIZES[preset];
  return orientation === "landscape" ? { w: p.h, h: p.w } : { w: p.w, h: p.h };
}

// When a custom machine profile has auto-rotate on, force the longer/shorter
// side to match its policy — the per-job orientation toggle is disabled in
// that case (see applyTopControls/onPaperChange) so this always wins.
function applyMachineAutoRotate({ w, h }) {
  if (!appSettings.machine_custom_enabled) return { w, h };
  const rotate = appSettings.machine_auto_rotate;
  if (rotate === "landscape" && h > w) return { w: h, h: w };
  if (rotate === "portrait" && w > h) return { w: h, h: w };
  return { w, h };
}

// ───── Placement (server-authoritative) ──────────────────────────────────
//
// Where the artwork lands on paper is decided by the server, not here. It has
// to be: the only tool a browser has for measuring artwork is getBBox(),
// which counts live text and raster images that vpype drops on the way to the
// plotter — so a preview that measured for itself was answering a different
// question than the machine. The rules (fit scales the canvas, anchor at the
// margin box's top-left, auto-rotate turns the artwork with the page) all
// live in app/placement.py. This file renders the answer and derives none of
// it.

const placementInflight = new Map();   // job_id -> {geom, ink} AbortControllers
const placementTimers = new Map();     // job_id -> {geom, ink} debounce timers

// Everything the answer depends on. A card refetches only when one of these
// actually changes, so dragging a slider back where it started costs nothing.
function placementKey(job) {
  return JSON.stringify([
    job.paper_width_mm, job.paper_height_mm,
    job.margin_top_mm, job.margin_right_mm, job.margin_bottom_mm, job.margin_left_mm,
    job.fit_content, job.transform_scale, job.transform_rotation_deg,
    job.transform_offset_x_mm, job.transform_offset_y_mm,
    (job.layer_selections || []).filter((l) => l.selected !== false).map((l) => l.index),
  ]);
}

// Two requests, because the two halves of the answer cost wildly different
// amounts. Placement is arithmetic over the document's size and viewBox and
// comes back in microseconds. The ink rectangle needs vpype to re-read the
// whole file — seconds on a real drawing. The preview only needs the first,
// so it must never wait on the second: asking for both together left the
// canvas blank for the length of the parse.
//
// Geometry is *throttled*, not debounced, and the difference is the whole
// reason a drag feels alive. A debounce re-arms its timer on every input
// event, so a slider held down and moved continuously never fires at all and
// the artwork sits frozen until the mouse comes up. A throttle fires on a
// steady cadence throughout. Ink keeps its debounce: it is expensive and only
// the final value matters, so settling once the drag stops is exactly right.
const PLACEMENT_INTERVAL_MS = 60;    // drives the preview; a steady ~16fps confirm
const INK_DEBOUNCE_MS = 400;         // drives the size readout; settles after a drag
const INK_RETRY_MS = 1500;           // while the server measures in the background

function fetchPlacement(card, job, { wantInk }, onReady) {
  const id = job.job_id;
  const ctx = cardCtx.get(id);
  if (!ctx) return;
  const key = placementKey(job);
  const slot = wantInk ? "ink" : "geom";
  if ((wantInk ? ctx.inkKey : ctx.placementKey) === key) return;   // already current

  const timers = placementTimers.get(id) || {};
  // Debounce waits out the drag; the throttle's deadline is measured from the
  // last send, so re-arming it on each event keeps the same cadence instead of
  // pushing the request further away.
  const wait = wantInk
    ? INK_DEBOUNCE_MS
    : Math.max(0, PLACEMENT_INTERVAL_MS - (performance.now() - (timers.geomSentAt || 0)));
  clearTimeout(timers[slot]);
  timers[slot] = setTimeout(async () => {
    if (!wantInk) timers.geomSentAt = performance.now();
    const inflight = placementInflight.get(id) || {};
    inflight[slot]?.abort();
    const ctrl = new AbortController();
    inflight[slot] = ctrl;
    placementInflight.set(id, inflight);
    try {
      const res = await fetch(`/jobs/${id}/placement`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: ctrl.signal,
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
            .filter((l) => l.selected !== false).map((l) => l.index),
          include_ink: wantInk,
        }),
      });
      if (!res.ok) return;
      const body = await res.json();
      const fresh = cardCtx.get(id);
      if (!fresh) return;
      if (wantInk) {
        // Not measured yet: the server has started a background read and is
        // telling us to come back. Leave inkKey unset so this stays stale, and
        // schedule the retry ourselves — a state broadcast might be a long way
        // off on an idle plotter, and without one the readout never fills in.
        if (body.ink_measured === false) {
          clearTimeout(fresh.inkRetry);
          fresh.inkRetry = setTimeout(() => {
            const j = serverState.queue.find((x) => x.job_id === id);
            if (j) requestInk(card, j, onReady);
          }, INK_RETRY_MS);
          if (onReady) onReady();       // let the status badge say "measuring"
          return;
        }
        fresh.ink = body.ink;          // null = nothing plottable in this document
        fresh.inkKey = key;
        fresh.layerLengthsMm = body.layer_lengths_mm || null;
      } else {
        fresh.placement = body;
        fresh.placementKey = key;
        // The transform this answer describes. effectivePlacement() measures
        // the editor's drift from it.
        fresh.placementAt = {
          scale: job.transform_scale ?? 1,
          rot: job.transform_rotation_deg ?? 0,
          offx: job.transform_offset_x_mm ?? 0,
          offy: job.transform_offset_y_mm ?? 0,
        };
      }
      if (onReady) onReady();
    } catch (e) {
      if (e.name !== "AbortError") console.warn("placement fetch failed", e);
    } finally {
      const cur = placementInflight.get(id);
      if (cur && cur[slot] === ctrl) delete cur[slot];
    }
  }, wait);
  placementTimers.set(id, timers);
}

// Geometry for the preview. Fast, and nothing blocks on anything slow.
function requestPlacement(card, job, onReady) {
  fetchPlacement(card, job, { wantInk: false }, onReady);
}

// The server's last answer, carried forward to the transform the editor is
// showing *right now*.
//
// The server owns placement, and that is not weakened here: nothing below
// re-derives any of it. No viewBox mapping, no `meet`, no fit-to-page, no
// auto-rotate policy — those exist in exactly one place and still arrive only
// by HTTP. What this does is move an answer the server already gave along the
// two axes the engine guarantees are linear:
//
//   offset   — enters the computation once, additively, and touches nothing
//              else, so a drag is a pure translation of the whole answer;
//   scale    — multiplies the footprint, since fit_scale is computed per unit
//              of scale and so does not move underneath it;
//   rotation — turns the canvas, and the on-page extent of a rotated rectangle
//              is its axis-aligned bounding box. Skipped when "fit to page" is
//              on, because then the angle feeds back into fit_scale, and that
//              *is* placement policy. Fit is the one case that waits.
//
// In all three the anchor does the rest of the work: the footprint's top-left
// corner sits at (margin + offset), which does not depend on angle or scale,
// so recentring is the same arithmetic whichever input moved.
//
// Two things are deliberately recovered from the server's answer rather than
// recomputed. The machine's auto-rotate contribution is `rotation_deg` minus
// the job's own angle — it does not depend on that angle, so subtraction gets
// it without re-implementing the policy. The document's resolved size is
// `layout / fit_scale`, which avoids restating the fallback for documents that
// declare no size. Every property relied on here is asserted in
// tests/test_placement_engine.py, and this function itself runs under quickjs
// in tests/test_static_js.py.
//
// The extrapolation is provisional by construction: it is measured as drift
// from the inputs the cached answer was computed at, so the next reply — 60ms
// away at most — collapses it to zero.
function effectivePlacement(job) {
  const ctx = cardCtx.get(job.job_id);
  const place = ctx?.placement;
  if (!place) return null;
  const at = ctx.placementAt;
  if (!at) return place;

  const scale = job.transform_scale ?? 1;
  const rot = job.transform_rotation_deg ?? 0;
  const k = at.scale ? scale / at.scale : 1;
  const dRot = rot - at.rot;
  const dx = (job.transform_offset_x_mm ?? 0) - at.offx;
  const dy = (job.transform_offset_y_mm ?? 0) - at.offy;
  if (k === 1 && dRot === 0 && dx === 0 && dy === 0) return place;

  let rotationDeg = place.rotation_deg;
  let fw = place.footprint_width_mm * k;
  let fh = place.footprint_height_mm * k;

  if (dRot !== 0 && !job.fit_content && place.fit_scale > 0) {
    rotationDeg = rot + (place.rotation_deg - at.rot);   // keep the auto-rotate part
    const rad = (rotationDeg * Math.PI) / 180;
    const cos = Math.abs(Math.cos(rad)), sin = Math.abs(Math.sin(rad));
    const docW = place.layout_width_mm / place.fit_scale;
    const docH = place.layout_height_mm / place.fit_scale;
    const unit = place.fit_scale * scale;
    fw = (docW * cos + docH * sin) * unit;
    fh = (docW * sin + docH * cos) * unit;
  }

  return {
    ...place,
    rotation_deg: rotationDeg,
    footprint_width_mm: fw,
    footprint_height_mm: fh,
    // The footprint's top-left corner is the fixed point; resize about it,
    // then translate by however far the offset has moved.
    center_x_mm: place.center_x_mm - place.footprint_width_mm / 2 + fw / 2 + dx,
    center_y_mm: place.center_y_mm - place.footprint_height_mm / 2 + fh / 2 + dy,
  };
}

// The measured ink rectangle, for the size readout and the bounds overlay.
// Deliberately lower priority: it can arrive late without the preview caring.
function requestInk(card, job, onReady) {
  fetchPlacement(card, job, { wantInk: true }, onReady);
}

// The ink's on-page rectangle in mm, or null when it hasn't been measured yet
// or the selected layers hold nothing plottable (a document of live text or
// raster images draws nothing, and the UI must not report a size for artwork
// that will never exist).
function inkFootprint(job) {
  const ink = cardCtx.get(job.job_id)?.ink;
  if (!ink) return null;
  return { left: ink.left_mm, top: ink.top_mm, width: ink.width_mm, height: ink.height_mm };
}

// ───── Preview status ────────────────────────────────────────────────────
//
// A real drawing is several megabytes and spends genuine time being fetched,
// injected into the DOM, optimized by vpype and estimated by the driver. The
// preview is blank or stale for all of it, and a blank canvas that says
// nothing is indistinguishable from a broken one — which is exactly how it
// read. So name the stage in progress.
//
// The states are ordered most-blocking first: whatever is stopping the user
// seeing a finished preview is what gets reported.

const STATUS_DONE_MS = 1200;         // how long "Ready" lingers before fading

function previewStatusKey(job, ctx) {
  const svgInfo = (serverState.svgs || {})[job.svg_id];
  // Nothing on screen yet — the case that looked like a crash.
  if (!ctx.svg) return "preview.status.loading";
  // The plot worker's pre-flight bounds check, waiting on the same ink
  // measurement the readout below waits on — but from the server side, where
  // this card's own copy of the ink can be current while the server's cache
  // is cold (a restart empties it). The job still reads "ready" throughout,
  // so this is the only thing that names it.
  if (job.plan_status === "measuring") return "preview.status.measuring";
  if (job.optimize_svg && svgInfo) {
    if (svgInfo.status === "optimizing") return "preview.status.optimizing";
    if (svgInfo.status === "pending") return "preview.status.queued_optimize";
  }
  if (IDLE_JOB_STATUSES.includes(job.status)) {
    if (job.plan_status === "planning") return "preview.status.estimating";
    if (job.plan_status === "pending") return "preview.status.queued_estimate";
  }
  // The placement is drawn (extrapolated if need be), but the measured size
  // and the bounds overlay are still catching up — this is the slow vpype
  // read, deliberately off the render path.
  if (ctx.inkKey !== placementKey(job)) return "preview.status.measuring";
  return null;
}

function previewStatus(card, job) {
  const el = card.querySelector(".preview-status");
  if (!el) return;
  const ctx = cardCtx.get(job.job_id) || {};
  const key = previewStatusKey(job, ctx);
  if (key === ctx.statusKey) return;               // nothing changed; don't churn the DOM
  const had = ctx.statusKey;
  ctx.statusKey = key;
  clearTimeout(ctx.statusTimer);

  if (key) {
    el.querySelector(".preview-status-text").textContent = t(key);
    el.classList.remove("done", "fading");
    el.hidden = false;
    return;
  }
  // Finished. Say so briefly, but only if there was something to finish —
  // a card that was never busy should not flash "Ready" at page load.
  if (!had) { el.hidden = true; return; }
  el.querySelector(".preview-status-text").textContent = t("preview.status.ready");
  el.classList.add("done");
  el.classList.remove("fading");
  el.hidden = false;
  ctx.statusTimer = setTimeout(() => {
    el.classList.add("fading");
    ctx.statusTimer = setTimeout(() => { el.hidden = true; }, 600);
  }, STATUS_DONE_MS);
}

// ───── Queue rendering ───────────────────────────────────────────────────

function renderQueue() {
  const ids = new Set(serverState.queue.map((j) => j.job_id));
  // Remove cards for jobs that no longer exist
  for (const id of Array.from(cardEls.keys())) {
    if (!ids.has(id)) {
      cardEls.get(id).remove();
      cardEls.delete(id);
      cardCtx.delete(id);
    }
  }
  // Append/move cards in order
  for (let i = 0; i < serverState.queue.length; i++) {
    const job = serverState.queue[i];
    let card = cardEls.get(job.job_id);
    if (!card) {
      card = createCardForJob(job);
      cardEls.set(job.job_id, card);
    }
    if (card.parentElement !== queueList || Array.from(queueList.children).indexOf(card) !== i) {
      queueList.insertBefore(card, queueList.children[i] || null);
    }
    updateCard(card, job);
  }
  queueEmpty.hidden = serverState.queue.length > 0;
  queueControls.hidden = serverState.queue.length === 0;
}

function createCardForJob(job) {
  const frag = jobCardTemplate.content.cloneNode(true);
  const card = frag.querySelector(".job-card");
  card.dataset.id = job.job_id;
  // Cards are cloned from a <template> after I18N.init()'s one-time pass, so
  // translate this card's static labels now.
  I18N.applyStatic(card);

  // Clamp typed-in number values when the user leaves the field. We hook
  // both `focusout` (fires on blur, before change) and `change` in capture
  // phase (covers the Enter-key path that may not blur). After clamping,
  // dispatch input + change so the paired slider re-syncs and queueCardUpdate
  // sees the corrected value. Clamping during typing would mangle
  // partially-entered numbers like "1100" → "110", so we only do it on commit.
  const clampOnLeave = (e) => {
    const el = e.target;
    if (el instanceof HTMLInputElement && el.type === "number") {
      if (clampNumberInput(el)) {
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  };
  card.addEventListener("focusout", clampOnLeave);
  card.addEventListener("change", clampOnLeave, true);

  // Populate paper-size options & defaults from job data
  const paperSize = card.querySelector(".paper-size");
  const namedInjected = injectNamedCustomOption(paperSize, job);
  relabelPaperOptions(paperSize);
  if (namedInjected) {
    paperSize.value = "__named_custom__";
  } else {
    paperSize.value = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm).preset;
  }
  const orientation = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm).orientation;
  setSegmentedValue(card.querySelector(".orientation"), orientation);
  applyMachineAutoRotateToCard(card);
  // The `custom-dims` block is `hidden` in the template; only `onPaperChange`
  // reveals it. Sync visibility on initial render so a job that arrives with
  // Custom selected actually shows its dimension fields.
  card.querySelector(".custom-dims").hidden = paperSize.value !== "Custom";

  card.querySelector(".paper-w").value = job.paper_width_mm;
  card.querySelector(".paper-h").value = job.paper_height_mm;
  card.querySelector(".margin-top").value = job.margin_top_mm || 0;
  card.querySelector(".margin-right").value = job.margin_right_mm || 0;
  card.querySelector(".margin-bottom").value = job.margin_bottom_mm || 0;
  card.querySelector(".margin-left").value = job.margin_left_mm || 0;
  card.querySelector(".fit-content").checked = !!job.fit_content;
  card.querySelector(".transform-scale").value = (job.transform_scale ?? 1).toFixed(2);
  card.querySelector(".transform-rotation").value = job.transform_rotation_deg ?? 0;
  card.querySelector(".transform-offset-x").value = job.transform_offset_x_mm ?? 0;
  card.querySelector(".transform-offset-y").value = job.transform_offset_y_mm ?? 0;
  applyOffsetBoundsToCard(card, job.paper_width_mm, job.paper_height_mm);
  card.querySelector(".speed-pendown").value = job.speed_pendown;
  card.querySelector(".speed-penup").value = job.speed_penup;
  card.querySelector(".accel").value = job.acceleration;
  card.querySelector(".pen-pos-up").value = job.pen_pos_up ?? appSettings.pen_pos_up_default;
  card.querySelector(".pen-pos-down").value = job.pen_pos_down ?? appSettings.pen_pos_down_default;
  card.querySelector(".pause-between-layers").checked = job.pause_between_layers;
  card.querySelector(".delete-on-complete").checked = !!job.delete_on_complete;
  card.querySelector(".disable-motors-on-complete").checked = !!job.disable_motors_on_complete;
  card.querySelector(".camera-job-options").hidden = !appSettings.camera_enabled;
  card.querySelector(".record-plot").checked = !!job.record_plot;
  card.querySelector(".record-plot-options").hidden = !job.record_plot;
  card.querySelector(".optical-reg").checked = !!job.optical_reg;
  card.querySelector(".record-mode").value = job.record_mode || appSettings.camera_recording_mode_default;
  card.querySelector(".record-timelapse-interval").value =
    job.record_timelapse_interval_s ?? appSettings.camera_timelapse_interval_s_default;
  card.querySelector(".record-speed-multiplier").value =
    job.record_speed_multiplier ?? appSettings.camera_speed_multiplier_default;
  card.querySelector(".optimize").checked = !!job.optimize_svg;
  card.querySelector(".optimize-linemerge").checked = job.optimize_svg_linemerge !== false;
  card.querySelector(".optimize-linesimplify").checked = job.optimize_svg_linesimplify !== false;
  card.querySelector(".optimize-linesort").checked = job.optimize_svg_linesort !== false;
  card.querySelector(".optimize-reloop").checked = job.optimize_svg_reloop !== false;
  card.querySelector(".optimize-tolerance").value = (job.optimize_svg_tolerance_mm ?? 0.10).toFixed(2);
  applyOptimizeEnabledStyle(card);
  applyOptimizeMode(card, job);
  resumeOptimizeExpertStatus(card, job);

  // Clicking the card header toggles expansion; action buttons stop propagation.
  card.querySelector(".job-card-head").addEventListener("click", () => toggleCardExpanded(card));
  // Double-click the title to rename the job — which also renames the drawing
  // in the library (see update_job). Only while the job isn't running.
  card.querySelector(".job-filename").addEventListener("dblclick", (e) => {
    e.stopPropagation();
    startTitleRename(card);
  });
  card.querySelectorAll(".job-actions button").forEach((b) =>
    b.addEventListener("click", (e) => e.stopPropagation())
  );
  card.querySelector(".job-delete").addEventListener("click", () => deleteJob(job.job_id));
  card.querySelector(".job-move-up").addEventListener("click", () => moveJob(job.job_id, -1));
  card.querySelector(".job-move-down").addEventListener("click", () => moveJob(job.job_id, +1));
  card.querySelector(".job-requeue").addEventListener("click", () => requeueJob(job.job_id));
  card.querySelectorAll(".export-scope button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setSegmentedValue(card.querySelector(".export-scope"), btn.dataset.val);
      const placed = btn.dataset.val === "placed";
      card.querySelector(".export-note-optimized").hidden = placed;
      card.querySelector(".export-note-placed").hidden = !placed;
    });
  });
  card.querySelector(".export-download").addEventListener("click", () => exportJob(card, job.job_id));
  card.querySelector(".job-error-nudge-btn").addEventListener("click", (e) =>
    nudgeBack(job.job_id, e.currentTarget.dataset.dx, e.currentTarget.dataset.dy)
  );

  // Settings changes
  const paperInputs = [
    card.querySelector(".paper-size"),
    card.querySelector(".paper-w"),
    card.querySelector(".paper-h"),
    card.querySelector(".margin-top"),
    card.querySelector(".margin-right"),
    card.querySelector(".margin-bottom"),
    card.querySelector(".margin-left"),
  ];
  paperInputs.forEach((el) => el.addEventListener("input", () => onPaperChange(card)));
  paperInputs.forEach((el) => el.addEventListener("change", () => onPaperChange(card)));
  card.querySelectorAll(".orientation button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setSegmentedValue(card.querySelector(".orientation"), btn.dataset.val);
      onPaperChange(card);
    });
  });
  card.querySelector(".fit-content").addEventListener("change", () => {
    const ctx = cardCtx.get(job.job_id);
    if (ctx) ctx.fitLocked = true;
    queueCardUpdate(card);
  });
  card.querySelector(".pause-between-layers").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".delete-on-complete").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".disable-motors-on-complete").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".record-plot").addEventListener("change", () => {
    card.querySelector(".record-plot-options").hidden = !card.querySelector(".record-plot").checked;
    queueCardUpdate(card);
  });
  [card.querySelector(".record-mode"),
   card.querySelector(".record-timelapse-interval"),
   card.querySelector(".record-speed-multiplier")]
    .forEach((el) => el.addEventListener("change", () => queueCardUpdate(card)));
  card.querySelector(".optimize").addEventListener("change", () => {
    // Master ON while every sub-option is off would be a no-op pipeline —
    // re-enable all four so the toggle actually does something.
    const master = card.querySelector(".optimize");
    if (master.checked) {
      const subs = ["linemerge", "linesimplify", "linesort", "reloop"];
      const anyOn = subs.some((s) => card.querySelector(".optimize-" + s).checked);
      if (!anyOn) {
        subs.forEach((s) => { card.querySelector(".optimize-" + s).checked = true; });
      }
    }
    applyOptimizeEnabledStyle(card);
    queueCardUpdate(card);
  });
  ["optimize-linemerge", "optimize-linesimplify", "optimize-linesort", "optimize-reloop"]
    .forEach((cls) => card.querySelector("." + cls).addEventListener("change", () => {
      syncOptimizeMaster(card);
      queueCardUpdate(card);
    }));
  card.querySelector(".optimize-tolerance").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".optimize-mode").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setSegmentedValue(card.querySelector(".optimize-mode"), btn.dataset.val);
      card.querySelector(".optimize-beginner-panel").hidden = btn.dataset.val === "expert";
      card.querySelector(".optimize-expert-panel").hidden = btn.dataset.val !== "expert";
      queueCardUpdate(card);
    });
  });
  // Layer grouping mode: server re-partitions the drawing and rebuilds the
  // rows; the broadcast re-renders. Sent on its own so the whole form isn't
  // re-read (a mode switch already invalidates the estimate server-side).
  card.querySelector(".layer-mode").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.classList.contains("active")) return;
      setSegmentedValue(card.querySelector(".layer-mode"), btn.dataset.val);
      queueCardUpdate(card, { layer_mode: btn.dataset.val });
    });
  });
  for (const n of [1, 2, 3]) {
    const enabledEl = card.querySelector(`.optimize-expert-${n}-enabled`);
    const cmdEl = card.querySelector(`.optimize-expert-${n}-cmd`);
    enabledEl.addEventListener("change", () => {
      cmdEl.disabled = !enabledEl.checked;
      queueCardUpdate(card);
    });
    cmdEl.addEventListener("input", () => {
      autoGrowTextarea(cmdEl);
      queueCardUpdate(card);
    });
  }
  card.querySelector(".optimize-expert-execute").addEventListener("click", () => runOptimizeExpert(card));
  [card.querySelector(".speed-pendown"),
   card.querySelector(".speed-penup"),
   card.querySelector(".accel"),
   card.querySelector(".pen-pos-up"),
   card.querySelector(".pen-pos-down")]
    .forEach((el) => el.addEventListener("change", () => {
      // While the job is actively plotting (speed/accel/pen height) or
      // paused at a pen-change (pen height), the "input" listeners below
      // already pushed this value live and it's already persisted
      // server-side (see applyLiveSetting/applyLivePenHeight and
      // set_live_plot_settings/set_live_pen_heights). A full-form PATCH here
      // would just hit the "can't edit an active job" guard and surface a
      // spurious save-failed error for a value that in fact did apply.
      const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
      const isPenHeight = el.classList.contains("pen-pos-up") || el.classList.contains("pen-pos-down");
      if (job && job.job_id === serverState.active_id &&
          (job.status === "plotting" || (isPenHeight && job.status === "awaiting_pen_change"))) return;
      queueCardUpdate(card);
    }));
  // While this card's job is paused between layers, dragging the pen height
  // also live-moves the physical pen (debounced) so the user can see/feel
  // the new height, the same way the camera picture sliders push live.
  card.querySelector(".pen-pos-up").addEventListener("input", () => applyLivePenHeight(card, "up"));
  card.querySelector(".pen-pos-down").addEventListener("input", () => applyLivePenHeight(card, "down"));
  // While this card's job is actively plotting, dragging any of these
  // sliders also pushes the change live to the running plot (see
  // applyLiveSetting) — applied at the next motion/pen command.
  card.querySelector(".speed-pendown").addEventListener("input", () => applyLiveSetting(card, "speed_pendown", ".speed-pendown"));
  card.querySelector(".speed-penup").addEventListener("input", () => applyLiveSetting(card, "speed_penup", ".speed-penup"));
  card.querySelector(".accel").addEventListener("input", () => applyLiveSetting(card, "acceleration", ".accel"));
  card.querySelector(".pen-pos-up").addEventListener("input", () => applyLiveSetting(card, "pen_pos_up", ".pen-pos-up"));
  card.querySelector(".pen-pos-down").addEventListener("input", () => applyLiveSetting(card, "pen_pos_down", ".pen-pos-down"));

  const transformInputs = [
    card.querySelector(".transform-scale"),
    card.querySelector(".transform-rotation"),
    card.querySelector(".transform-offset-x"),
    card.querySelector(".transform-offset-y"),
  ];
  transformInputs.forEach((el) => {
    el.addEventListener("input", () => {
      const j = serverState.queue.find((x) => x.job_id === card.dataset.id);
      if (!j) return;
      updatePreviewTransform(card, { ...j, ...readTransformFromCard(card) });
    });
    el.addEventListener("change", () => queueCardUpdate(card));
  });

  pairSlider(card, ".transform-scale", ".transform-scale-slider");
  pairSlider(card, ".transform-rotation", ".transform-rotation-slider");
  pairSlider(card, ".transform-offset-x", ".transform-offset-x-slider");
  pairSlider(card, ".transform-offset-y", ".transform-offset-y-slider");
  pairSlider(card, ".speed-pendown", ".speed-pendown-slider");
  pairSlider(card, ".speed-penup", ".speed-penup-slider");
  pairSlider(card, ".accel", ".accel-slider");
  pairSlider(card, ".pen-pos-up", ".pen-pos-up-slider");
  pairSlider(card, ".pen-pos-down", ".pen-pos-down-slider");

  // Collapsible section headers
  card.querySelectorAll(".card-section-head").forEach((head) => {
    head.addEventListener("click", (e) => {
      if (e.target.closest(".card-section-reset")) return;
      head.parentElement.classList.toggle("collapsed");
      syncSectionCaret(head.parentElement);
    });
    syncSectionCaret(head.parentElement);
  });
  // Reset buttons
  card.querySelectorAll(".card-section-reset").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const kind = btn.dataset.reset;
      if (kind === "margins") resetMargins(card);
      else if (kind === "transform") resetTransform(card);
      else if (kind === "parameters") resetParameters(card);
      else if (kind === "optimize") resetOptimize(card);
    });
  });

  const ctx = cardCtx.get(job.job_id) || { svg: null, fitLocked: false };
  cardCtx.set(job.job_id, ctx);
  if (!ctx.svg || !ctx.svg.text) {
    fetchSvgMeta(job.job_id, job.svg_id).then((meta) => {
      if (meta) {
        ctx.svg = meta;
        renderPreview(card, job);
        renderLayers(card, job);
        // The size readout needs ctx.svg and the rendered preview DOM, both
        // of which just became available — refresh now instead of waiting for
        // the next broadcast.
        updateCard(card, job);
      }
    });
  } else {
    renderPreview(card, job);
    renderLayers(card, job);
  }

  // Auto-expand if this is the first card in the queue, or the currently-active job.
  const isFirst = serverState.queue.length > 0 && serverState.queue[0].job_id === job.job_id;
  if (isFirst || job.job_id === serverState.active_id) {
    card.classList.add("expanded");
    card.querySelector(".job-body").hidden = false;
  }
  syncJobCardCaret(card);

  return card;
}

// Fetches the SVG the job would actually plot right now (the optimized
// .opt.svg once "Optimize SVG" has finished, otherwise the raw upload) so the
// on-screen preview matches what gets sent to the machine.
// The document to display, plus what the server knows about it.
//
// The layer list and page size come from the server rather than being
// re-derived here. This used to run the whole document through DOMParser a
// second time — a full parse of several megabytes on the UI thread, measured
// at 281ms on a 2.4MB curved drawing and 368ms on a 4.5MB hatched one, with
// everything frozen for the duration — to rebuild six fields the server
// already computes with `svg_utils.parse_layers` and already returns from
// `/upload`.
//
// It also removes a place the two could disagree. Layer indices decide what
// gets plotted, and the server's are the ones the machine uses; deriving a
// second set in the browser meant two answers to the same question, which is
// the bug class the placement engine was extracted to end.
//
// Both requests go out together: the metadata is small and the document is
// not, so serialising them would spend a round trip for nothing.
async function fetchSvgMeta(job_id, svg_id) {
  try {
    const [docRes, metaRes] = await Promise.all([
      fetch(`/jobs/${job_id}/svg`),
      fetch(`/jobs/${job_id}/svg-meta`),
    ]);
    if (!docRes.ok || !metaRes.ok) return null;
    const [text, meta] = await Promise.all([docRes.text(), metaRes.json()]);
    return {
      id: svg_id,
      width: meta.width || "",
      height: meta.height || "",
      width_mm: meta.width_mm,
      height_mm: meta.height_mm,
      viewBox: meta.viewBox || "",
      layers: meta.layers || [],
      subpath_count: meta.subpath_count,
      // Present only once a drawing has actually defeated the preview; see
      // app/svg_complexity.py.
      complexity: meta.complexity || null,
      text,
    };
  } catch (e) {
    return null;
  }
}

// Re-fetch a card's effective SVG (whatever GET /jobs/{id}/svg now resolves
// to) and redraw the preview/layers/card from it. Used both when beginner
// mode's background optimize settles (see updateCard) and when an
// expert-mode Execute finishes (see runOptimizeExpert) — either way the
// document on disk changed out from under the already-rendered preview.
function reloadCardSvg(card, job) {
  return fetchSvgMeta(job.job_id, job.svg_id).then((meta) => {
    if (!meta) return;
    const ctx = cardCtx.get(job.job_id);
    if (ctx) ctx.svg = meta;
    // renderPreview only injects ctx.svg.text into the DOM once (guarded by
    // data-rendered); clear that so the upgraded content actually replaces
    // the stale markup instead of being ignored.
    const previewEl = card.querySelector(".svg-preview");
    if (previewEl) delete previewEl.dataset.rendered;
    renderPreview(card, job);
    renderLayers(card, job);
    updateCard(card, job);
  });
}

// Absolute CSS length units in mm per unit. Relative units (%, em, ex) aren't
// resolvable here, so they return null and the caller falls back to the
// viewBox. Mirrors parse_dim_to_mm() in app/svg_utils.py.
const UNIT_TO_MM = {
  mm: 1, cm: 10, in: 25.4, px: 25.4 / 96, "": 25.4 / 96,
  pt: 25.4 / 72, pc: 25.4 / 6, q: 0.25,
};

function parseDimToMm(s) {
  const m = String(s).trim().match(/^([\d.eE+\-]+)\s*([a-z%]*)$/i);
  if (!m) return null;
  const factor = UNIT_TO_MM[(m[2] || "px").toLowerCase()];
  return factor === undefined ? null : parseFloat(m[1]) * factor;
}

// Falls back to the viewBox (treated as CSS px at 96dpi) when width/height
// are missing or use a non-physical unit like `%` — mirrors svg_size_mm() in
// app/svg_utils.py so the preview matches what actually gets plotted.
function formatDim(s) {
  const v = parseDimToMm(s);
  return v != null ? fmtLength(v) : (s || "—");
}

function guessPresetFromDims(w, h) {
  if (!w || !h) return { preset: "A4", orientation: "portrait" };
  const rW = Math.round(w * 10) / 10;
  const rH = Math.round(h * 10) / 10;
  for (const [name, p] of Object.entries(PAPER_SIZES)) {
    if (Math.abs(p.w - rW) < 0.5 && Math.abs(p.h - rH) < 0.5) return { preset: name, orientation: "portrait" };
    if (Math.abs(p.h - rW) < 0.5 && Math.abs(p.w - rH) < 0.5) return { preset: name, orientation: "landscape" };
  }
  return { preset: "Custom", orientation: rW >= rH ? "landscape" : "portrait" };
}

function setCaretTooltip(caret, isExpanded) {
  if (caret) caret.title = isExpanded ? t("a11y.collapse") : t("a11y.expand");
}

function syncSectionCaret(section) {
  if (!section) return;
  const caret = section.querySelector(":scope > .card-section-head > .card-section-caret");
  setCaretTooltip(caret, !section.classList.contains("collapsed"));
}

function syncJobCardCaret(card) {
  if (!card) return;
  const caret = card.querySelector(".job-card-head .card-section-caret");
  setCaretTooltip(caret, card.classList.contains("expanded"));
}

function setSegmentedValue(seg, val) {
  seg.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.val === val));
}

// When a custom machine profile has auto-rotate on, force the card's
// orientation toggle to match it and disable the buttons (the policy always
// wins) rather than just leaving it as a one-time default. `forceDisabled`
// additionally disables regardless of policy — used by updateCard's active-job
// readonly pass, which would otherwise re-enable these buttons on every state
// broadcast (its blanket ".col-form button" selector includes them) and undo
// the lock a moment after it was applied.
function applyMachineAutoRotateToCard(card, forceDisabled = false) {
  const seg = card.querySelector(".orientation");
  const rotate = appSettings.machine_custom_enabled ? appSettings.machine_auto_rotate : "off";
  const locked = rotate !== "off";
  if (locked) setSegmentedValue(seg, rotate);
  seg.querySelectorAll("button").forEach((b) => {
    b.disabled = locked || forceDisabled;
    b.title = locked ? t("settings.machine.auto_rotate_locked_title") : "";
  });
}

function getSegmentedValue(seg, fallback) {
  return seg.querySelector("button.active")?.dataset.val ?? fallback;
}

function toggleCardExpanded(card) {
  const body = card.querySelector(".job-body");
  body.hidden = !body.hidden;
  card.classList.toggle("expanded", !body.hidden);
  syncJobCardCaret(card);
  if (!body.hidden) {
    const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
    if (job) {
      // Body width was 0 while hidden — now that it's visible, re-measure.
      requestAnimationFrame(() => updatePreviewTransform(card, job));
    }
  }
}

// ───── Per-card updates ──────────────────────────────────────────────────

// Swap the card title for an input. Commit -> PATCH {name}, which the server
// also writes through to the linked library row (see update_job).
function startTitleRename(card) {
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job || !["ready", "completed", "failed", "cancelled"].includes(job.status)) return;
  const titleEl = card.querySelector(".job-filename");
  if (!titleEl) return;
  const was = job.name || job.filename || "";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "job-filename job-filename-edit";
  input.value = was;
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const commit = (save) => {
    if (done) return;
    done = true;
    const val = input.value.trim();
    const span = document.createElement("span");
    span.className = "job-filename";
    input.replaceWith(span);
    const fresh = serverState.queue.find((j) => j.job_id === card.dataset.id) || job;
    updateCard(card, fresh);
    if (save && val && val !== was) queueCardUpdate(card, { name: val });
  };
  input.addEventListener("blur", () => commit(true));
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { input.value = was; input.blur(); }
  });
}

function updateCard(card, job) {
  const ctx = cardCtx.get(job.job_id) || {};

  // Track status transitions so we can auto-collapse a card once the next job
  // becomes active. Only flag the transition *into* a terminal state so a card
  // that's been sitting as "completed" on page load isn't surprise-collapsed.
  const prevStatus = ctx.lastSeenStatus;
  if (prevStatus && prevStatus !== job.status &&
      ["completed", "failed", "cancelled"].includes(job.status)) {
    ctx.finishedPendingCollapse = true;
  }
  ctx.lastSeenStatus = job.status;

  const filename = job.filename || "upload.svg";
  const titleEl = card.querySelector(".job-filename");
  // Null while a rename input is in its place — leave it be until the commit.
  if (titleEl) {
    titleEl.textContent = job.name || filename;
    // The name is truncated with an ellipsis in CSS so it can never push the
    // controls out of the card; hover restores it in full.
    titleEl.title = job.name || filename;
  }

  const paperLabel = formatPaperLabel(job);
  const stageCount = job.stages?.length || 0;
  const subParts = [paperLabel];
  const layerCount = (job.layer_selections || []).filter((s) => s.selected !== false).length;
  if (layerCount) {
    subParts.push(tn("job.layers", layerCount));
  }
  requestInk(card, job, () => {
    const fresh = serverState.queue.find((j) => j.job_id === job.job_id);
    if (fresh) updateCard(card, fresh);
  });
  previewStatus(card, job);
  const geomFootprint = inkFootprint(job);
  if (geomFootprint) {
    const u = effectiveDisplayUnit();
    subParts.push(`${formatLengthValue(geomFootprint.width, u)} × ${formatLengthValue(geomFootprint.height, u)} ${u}`);
  }
  renderDeltaOverlay(card, job, geomFootprint);
  if (job.estimated_total_seconds) subParts.push(formatDuration(Math.round(job.estimated_total_seconds)));
  // Surface the SVG-level pre-optimize state on queued cards so the user knows
  // a future "Plot" click won't be instant if their SVG is still in the
  // optimize queue.
  if (IDLE_JOB_STATUSES.includes(job.status) && job.optimize_svg) {
    const svgInfo = (serverState.svgs || {})[job.svg_id];
    if (svgInfo && svgInfo.status === "optimizing") subParts.push(t("job.optimizing_svg"));
    else if (svgInfo && svgInfo.status === "pending") subParts.push(t("job.waiting_optimize_svg"));
  }
  // The preview is fetched once (see createCardForJob) and reflects whatever
  // was effective at that moment. Refetch whenever the *effective* SVG the
  // machine would plot changes: a still-running background optimize
  // finishes (raw -> .opt.svg), or "Optimize SVG" gets toggled off/on
  // (.opt.svg <-> raw). "settled" means the background optimize (if any)
  // isn't still in flight, so GET /jobs/{id}/svg would resolve to its final
  // answer rather than a mid-flight raw fallback.
  if (ctx.svg) {
    const svgInfo = (serverState.svgs || {})[job.svg_id];
    const settled = !job.optimize_svg || !svgInfo || svgInfo.status === "ready" || svgInfo.status === "failed";
    const effectiveKind = job.optimize_svg && settled ? "optimized" : "raw";
    if (settled && ctx.previewEffectiveKind !== effectiveKind) {
      ctx.previewEffectiveKind = effectiveKind;
      reloadCardSvg(card, job);
    } else if (!settled) {
      ctx.previewEffectiveKind = null;
    }
  }
  // And the background-planning state, so the user knows whether the plot
  // click will be instant or still has to compute the estimate.
  if (IDLE_JOB_STATUSES.includes(job.status) &&
      (job.plan_status === "pending" || job.plan_status === "planning")) {
    subParts.push(job.plan_status === "planning" ? t("job.planning") : t("job.waiting_plan"));
  }
  // A background plan that failed used to be recorded and never shown, so the
  // first sign of trouble was the error after clicking Plot.
  if (IDLE_JOB_STATUSES.includes(job.status) && job.plan_status === "failed") {
    subParts.push(t("job.plan_failed"));
  }
  // Distinct from "failed": the estimate did not go wrong, it was stopped for
  // outgrowing the machine. The panel below the header says what to do about it.
  if (IDLE_JOB_STATUSES.includes(job.status) && job.plan_status === "too_complex") {
    subParts.push(t("job.plan_too_complex"));
  }
  card.querySelector(".job-sub").textContent = subParts.join(" · ");

  const pill = card.querySelector(".job-status-pill");
  pill.textContent = statusLabel(job.status);
  pill.className = `job-status-pill status ${job.status}`;

  const errorEl = card.querySelector(".job-error");
  const nudgeBtn = card.querySelector(".job-error-nudge-btn");
  if (job.error) {
    card.querySelector(".job-error-text").textContent = jobErrorText(job);
    errorEl.hidden = false;
    const hasHint = job.jog_hint_dx_mm != null && job.jog_hint_dy_mm != null;
    nudgeBtn.hidden = !hasHint;
    if (hasHint) {
      const u = effectiveDisplayUnit();
      nudgeBtn.textContent = t("card.nudge_back_btn", {
        dx: fmtLength(job.jog_hint_dx_mm, u),
        dy: fmtLength(job.jog_hint_dy_mm, u),
      });
      nudgeBtn.dataset.dx = job.jog_hint_dx_mm;
      nudgeBtn.dataset.dy = job.jog_hint_dy_mm;
    }
  } else {
    card.querySelector(".job-error-text").textContent = "";
    errorEl.hidden = true;
    nudgeBtn.hidden = true;
  }

  // Disable editing when job is active
  const activeBlocks = job.job_id === serverState.active_id &&
    !["ready", "completed", "failed", "cancelled"].includes(job.status);
  card.classList.toggle("active", job.job_id === serverState.active_id);
  card.classList.toggle("readonly", activeBlocks);
  card.querySelectorAll(".col-form input, .col-form select, .col-form button, .col-form textarea")
    .forEach((el) => { el.disabled = activeBlocks; });
  // The blanket pass above also re-enables the orientation buttons that the
  // machine auto-rotate policy locks (its selector doesn't know about that
  // lock); reassert it here so it isn't undone on every broadcast.
  applyMachineAutoRotateToCard(card, activeBlocks);

  // Auto-expand active card
  if (job.job_id === serverState.active_id && card.querySelector(".job-body").hidden) {
    toggleCardExpanded(card);
  }

  // Auto-collapse a just-finished card once another job is active.
  if (ctx.finishedPendingCollapse &&
      serverState.active_id && serverState.active_id !== job.job_id) {
    const body = card.querySelector(".job-body");
    if (!body.hidden) {
      body.hidden = true;
      card.classList.remove("expanded");
    }
    ctx.finishedPendingCollapse = false;
  }
  syncJobCardCaret(card);

  // Re-queue button visible when the job is in a terminal state AND either
  // it has actually been plotted before (started_at set) or it carries an
  // error — the latter covers a job rejected by a pre-flight check (e.g. the
  // artwork-bounds check in _run_job) before started_at is ever set, which
  // otherwise left a failed job with no way to retry from the card. The
  // started_at half of the condition still avoids the button flashing
  // visible for freshly-uploaded or just-PATCH-requeued jobs in the brief
  // window before the server broadcast lands.
  const requeueBtn = card.querySelector(".job-requeue");
  if (requeueBtn) {
    const isTerminal = ["completed", "failed", "cancelled"].includes(job.status);
    requeueBtn.hidden = !(isTerminal && (job.started_at || job.error));
  }

  cardCtx.set(job.job_id, ctx);

  setSegmentedValue(card.querySelector(".layer-mode"), job.layer_mode || "layer");

  // Preview + layers + stages + plot-info
  if (ctx.svg) {
    renderPreview(card, job);
    renderLayers(card, job);
  }
  renderStages(card, job);
  renderPlotInfo(card, job);
  renderMachineBoundsWarning(card, job);
  renderComplexityWarning(card, job);
  // renderLayers rebuilds the layer checkboxes and move buttons from scratch,
  // so the blanket disable pass above (which ran before them) never reached
  // these ones — leaving a plotting job's layers toggleable, with the change
  // shown as applied while the server refused it. Re-assert the lock now that
  // every control exists.
  card.querySelectorAll(".col-form input, .col-form select, .col-form button, .col-form textarea")
    .forEach((el) => { el.disabled = activeBlocks; });
  applyMachineAutoRotateToCard(card, activeBlocks);
  // Same reason as the two calls above: the blanket pass re-enables everything
  // it can see, so any lock narrower than "the job is running" has to be
  // re-asserted after it, every time.
  applyPreOptimizedLock(card, job);
}

// A source promoted out of a .opt.svg has already been through vpype. Running
// linesimplify over already-simplified paths only loses more of the original
// curve, so the server forces optimize_svg off (main.create_job / update_job)
// and the beginner panel is locked to match rather than showing a control
// that silently does nothing. Expert mode is left alone — typing further
// custom vpype commands on an already-optimized source is still meaningful,
// and the server only ever forces off optimize_svg, never optimize_mode.
//
// Real `disabled` attributes, not the existing `.disabled` class on
// .optimize-options: that one is `pointer-events: none`, which still allows
// keyboard focus, and it does not cover the master checkbox — which sits
// outside .optimize-options — or the section's reset button.
function applyPreOptimizedLock(card, job) {
  const section = card.querySelector("[data-section='optimize']");
  if (!section) return;
  const locked = !!job.pre_optimized;
  section.classList.toggle("locked", locked);
  const note = section.querySelector(".optimize-locked-note");
  if (note) note.hidden = !locked;
  const resetBtn = section.querySelector(".card-section-reset");
  if (resetBtn) resetBtn.disabled = locked;
  if (!locked) return;
  section.querySelectorAll(".optimize-beginner-panel input, .optimize-beginner-panel select, .optimize-beginner-panel button")
    .forEach((el) => { el.disabled = true; });
}

// The delta (a manual jog for the next job about to start, or an origin
// nudge for the active job paused mid-plot) that actually applies to this
// job right now, in mm — {dx: 0, dy: 0} if neither applies.
// The delta actually baked into the active job's physical run right now —
// null for any other job, including one that's merely queued next (see
// effectiveDeltaForJob, which also previews a manual jog for that case:
// aiming, not yet drawn). Both pieces stay in effect for every stage of the
// run, not just while paused: a manual jog becomes the run's physical zero
// the moment its first stage starts (nothing re-homes the carriage mid-run
// — see plot_worker.py, manual_jog is idle-only), and an origin nudge
// dialed in at a pause applies to every stage from then on (see
// nudge_origin / _run_staged_loop_impl), not only the pause itself.
function activeRunDelta(job) {
  if (job.job_id !== serverState.active_id) return null;
  return {
    dx: (serverState.manual_origin_offset_x_mm || 0) + (serverState.origin_nudge_x_mm || 0),
    dy: (serverState.manual_origin_offset_y_mm || 0) + (serverState.origin_nudge_y_mm || 0),
  };
}

function effectiveDeltaForJob(job) {
  const running = activeRunDelta(job);
  if (running) return running;
  if (serverState.status === "idle") {
    const firstReady = serverState.queue.find((j) => j.status === "ready");
    if (firstReady && firstReady.job_id === job.job_id) {
      return { dx: serverState.manual_origin_offset_x_mm || 0, dy: serverState.manual_origin_offset_y_mm || 0 };
    }
    // Nothing else is actually queued, and this is the job that was last
    // running (e.g. cancelled) — the manual jog is still physically applied
    // (nothing resets it on cancel), so keep showing it "as if queued"
    // rather than snapping to zero the instant the job stops being active.
    // Server-tracked (state.last_active_id), not just client memory, so
    // this survives a page reload after the cancel already happened.
    if (!firstReady && job.job_id === serverState.last_active_id) {
      return { dx: serverState.manual_origin_offset_x_mm || 0, dy: serverState.manual_origin_offset_y_mm || 0 };
    }
  }
  return { dx: 0, dy: 0 };
}

// Red dot + thin red outline on the preview: where the current delta (see
// effectiveDeltaForJob) puts the geometry's own origin corner, and the
// geometry's actual on-page footprint from there — the same numbers the
// server's pre-flight/nudge bounds check uses (see _delta_correction_mm),
// made visible instead of only surfacing as an error after the fact.
function renderDeltaOverlay(card, job, footprint) {
  const dot = card.querySelector(".delta-dot");
  const square = card.querySelector(".delta-square");
  if (!dot || !square) return;
  const w = job.paper_width_mm, h = job.paper_height_mm;
  const validPage = w > 0 && h > 0;

  // The dot marks the delta itself — (dx, dy) as a raw coordinate on the
  // page (0, 0 = the paper's own top-left, same as if there were no jog/
  // nudge at all) — independent of where the geometry sits, so it stays
  // meaningful even for a job with no geometry loaded yet.
  const { dx, dy } = effectiveDeltaForJob(job);
  dot.hidden = !validPage;
  if (validPage) {
    dot.style.left = `${(dx / w) * 100}%`;
    dot.style.top = `${(dy / h) * 100}%`;
  }

  // The square is the geometry's actual on-page footprint, delta included —
  // where the artwork will really be, unlinked from the dot above.
  if (!footprint || !validPage) {
    square.hidden = true;
    return;
  }
  const left = footprint.left + dx, top = footprint.top + dy;
  square.hidden = false;
  square.style.left = `${(left / w) * 100}%`;
  square.style.top = `${(top / h) * 100}%`;
  square.style.width = `${(footprint.width / w) * 100}%`;
  square.style.height = `${(footprint.height / h) * 100}%`;
}

// Advisory only: pyaxidraw clips anything past the travel bounds at plot time
// regardless — this is the earlier heads-up that it's going to.
//
// Measured against the *effective* working area from the server — the active
// machine profile's bed, the same figure the driver and the jog guards use
// (see plot_worker.machine_bounds_mm) — so the warning and the actual clip
// can't disagree about where the machine stops.
// Shown when the estimate was refused because it outgrew
// plot_worker.PREVIEW_RSS_LIMIT_MB. Deliberately not a bare failure: the
// server also works out which linemerge tolerance would bring the drawing back
// into range (app/svg_complexity.py), and that recommendation is the whole
// point of the panel. Nothing here changes the drawing — the user applies it
// themselves from the optimization panel, in beginner or expert mode.
function renderComplexityWarning(card, job) {
  const el = card.querySelector(".complexity-warning");
  if (!el) return;
  const blocked = job.plan_status === "too_complex";
  el.hidden = !blocked;
  if (!blocked) return;

  const ctx = cardCtx.get(job.job_id);
  const svg = ctx && ctx.svg;
  const strokes = svg && svg.subpath_count;
  const c = (svg && svg.complexity) || null;

  const parts = [];
  parts.push(`<strong>${t("card.complexity_blocked")}</strong>`);
  if (strokes) parts.push(t("card.complexity_strokes", { count: strokes.toLocaleString() }));

  if (c && c.recommended_tolerance_mm) {
    parts.push(t("card.complexity_gap", {
      mean: (c.mean_gap_mm ?? 0).toFixed(2),
      median: (c.median_gap_mm ?? 0).toFixed(2),
    }));
    parts.push(t("card.complexity_recommendation", {
      tolerance: c.recommended_tolerance_mm,
      percent: ((c.joinable_fraction ?? 0) * 100).toFixed(1),
    }));
  } else {
    // The analysis runs on a background worker and can land after the refusal.
    parts.push(t("card.complexity_analyzing"));
  }
  el.innerHTML = parts.join(" ");
}

function renderMachineBoundsWarning(card, job) {
  const el = card.querySelector(".machine-bounds-warning");
  if (!el) return;
  const bedW = appSettings.machine_effective_width_mm;
  const bedH = appSettings.machine_effective_height_mm;
  const exceeds = bedW > 0 && bedH > 0 &&
    (job.paper_width_mm > bedW + 0.5 || job.paper_height_mm > bedH + 0.5);
  el.hidden = !exceeds;
  if (exceeds) {
    const u = effectiveDisplayUnit();
    el.textContent = t("card.machine_bounds_warning", {
      width: formatLengthValue(bedW, u),
      height: formatLengthValue(bedH, u),
      unit: u,
    });
  }
}

function formatPaperLabel(job) {
  const u = effectiveDisplayUnit();
  if (job.paper_size_name) {
    const w = formatLengthValue(job.paper_width_mm, u);
    const h = formatLengthValue(job.paper_height_mm, u);
    return `${job.paper_size_name} (${w} × ${h} ${u})`;
  }
  const { preset, orientation } = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm);
  if (preset === "Custom") {
    const w = formatLengthValue(job.paper_width_mm, u);
    const h = formatLengthValue(job.paper_height_mm, u);
    return `${w}×${h} ${u}`;
  }
  return `${preset} ${orientation}`;
}

function renderPreview(card, job) {
  const ctx = cardCtx.get(job.job_id);
  if (!ctx || !ctx.svg) return;
  const previewEl = card.querySelector(".svg-preview");
  if (!previewEl.dataset.rendered) {
    previewEl.innerHTML = `<div class="paper"><div class="paper-margins" hidden></div><div class="paper-content">${ctx.svg.text}</div><div class="pen-cursor" hidden></div><div class="delta-square" hidden></div><div class="delta-dot" hidden></div></div>`;
    previewEl.dataset.rendered = "1";
  }
  card.querySelector(".svg-dims").textContent = `${formatDim(ctx.svg.width)} × ${formatDim(ctx.svg.height)}`;
  // Optional paper stock (API-set), right-aligned opposite the size.
  card.querySelector(".preview-paper-name").textContent = job.paper_name || "";
  updatePreviewTransform(card, job);
  syncPreviewLayers(card, job);
}

function dispatchValueChange(el) {
  if (!el) return;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function resetMargins(card) {
  for (const sel of [".margin-top", ".margin-right", ".margin-bottom", ".margin-left"]) {
    const el = card.querySelector(sel);
    if (el) { el.value = 0; dispatchValueChange(el); }
  }
}

function resetTransform(card) {
  const pairs = [
    [".transform-scale", "1.00"],
    [".transform-rotation", 0],
    [".transform-offset-x", 0],
    [".transform-offset-y", 0],
  ];
  for (const [sel, val] of pairs) {
    const el = card.querySelector(sel);
    if (el) { el.value = val; dispatchValueChange(el); }
  }
}

function applyOptimizeEnabledStyle(card) {
  const on = card.querySelector(".optimize").checked;
  const opts = card.querySelector("[data-section='optimize'] .optimize-options");
  if (opts) opts.classList.toggle("disabled", !on);
}

// Grows a textarea to fit its content instead of scrolling internally —
// overflow stays hidden (see .optimize-expert-cmd in style.css) so this is
// the only thing that ever changes its height.
function autoGrowTextarea(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}

// Beginner vs expert mode: which panel shows, and the expert boxes' content.
// Called from updateCard, so this also re-syncs after every WebSocket
// broadcast — safe for the boxes' live text because typing them goes through
// queueCardUpdate's immediate-update path (see the "input" handler below),
// which overlays serverState before updateCard ever reads job.* (see
// cardUpdateUnconfirmed / applyUnconfirmedEdits).
function applyOptimizeMode(card, job) {
  const mode = job.optimize_mode === "expert" ? "expert" : "beginner";
  setSegmentedValue(card.querySelector(".optimize-mode"), mode);
  card.querySelector(".optimize-beginner-panel").hidden = mode === "expert";
  card.querySelector(".optimize-expert-panel").hidden = mode !== "expert";
  for (const n of [1, 2, 3]) {
    const enabledEl = card.querySelector(`.optimize-expert-${n}-enabled`);
    const cmdEl = card.querySelector(`.optimize-expert-${n}-cmd`);
    enabledEl.checked = !!job[`optimize_expert_${n}_enabled`];
    cmdEl.value = job[`optimize_expert_${n}_cmd`] || "";
    cmdEl.disabled = !enabledEl.checked;
    autoGrowTextarea(cmdEl);
  }
}

function optimizeExpertUiRefs(card) {
  return {
    btn: card.querySelector(".optimize-expert-execute"),
    spinner: card.querySelector(".optimize-expert-spinner"),
    statusEl: card.querySelector(".optimize-expert-status"),
    logEl: card.querySelector(".optimize-expert-log"),
  };
}

function setOptimizeExpertStatus(card, msg, isError) {
  const { statusEl } = optimizeExpertUiRefs(card);
  statusEl.textContent = msg;
  statusEl.className = "optimize-expert-status muted" + (isError ? " error" : "");
}

// Polls GET .../optimize-expert/status on a self-rescheduling setTimeout
// chain (same shape as startUpdate's tick — waits for each fetch to resolve
// before scheduling the next, so a slow Pi never piles up overlapping polls)
// until the run finishes, then reloads the card's preview from the .opt.svg
// Execute just produced. Also the resume path after a page reload: the run
// lives in the server's queue, not the browser, so a reload just needs to
// start polling again, never to re-POST.
function pollOptimizeExpertStatus(card) {
  const { btn, spinner, logEl } = optimizeExpertUiRefs(card);
  const finish = (msg, isError) => {
    btn.disabled = false;
    spinner.hidden = true;
    setOptimizeExpertStatus(card, msg, isError);
  };
  const tick = async () => {
    if (!card.isConnected) return;  // job's card was removed while we polled
    let d;
    try {
      const r = await fetch(`/jobs/${card.dataset.id}/optimize-expert/status`, { cache: "no-store" });
      if (!r.ok) { setTimeout(tick, 1000); return; }
      d = await r.json();
    } catch (e) {
      setTimeout(tick, 1000);
      return;
    }
    if (d.log) {
      logEl.textContent = d.log;
      logEl.hidden = false;
      logEl.scrollTop = logEl.scrollHeight;
    }
    if (d.status === "running") { setTimeout(tick, 1000); return; }
    if (d.status === "done") {
      finish(t("optimize.expert_done"), false);
      const fresh = serverState.queue.find((j) => j.job_id === card.dataset.id);
      if (fresh) reloadCardSvg(card, fresh);
      return;
    }
    if (d.status === "error") {
      finish(t("optimize.expert_failed", { message: d.error || "" }), true);
    }
    // "idle" shouldn't normally appear mid-poll; nothing to report if it does.
  };
  tick();
}

async function runOptimizeExpert(card) {
  const { btn, spinner, logEl } = optimizeExpertUiRefs(card);
  const boxes = {};
  for (const n of [1, 2, 3]) {
    boxes[`optimize_expert_${n}_enabled`] = card.querySelector(`.optimize-expert-${n}-enabled`).checked;
    boxes[`optimize_expert_${n}_cmd`] = card.querySelector(`.optimize-expert-${n}-cmd`).value;
  }
  const hasCommand = [1, 2, 3].some((n) =>
    boxes[`optimize_expert_${n}_enabled`] && boxes[`optimize_expert_${n}_cmd`].trim());
  if (!hasCommand) {
    setOptimizeExpertStatus(card, t("optimize.expert_no_command"), true);
    return;
  }
  btn.disabled = true;
  spinner.hidden = false;
  setOptimizeExpertStatus(card, t("optimize.expert_running"), false);
  logEl.hidden = true;
  logEl.textContent = "";
  let res;
  try {
    res = await fetch(`/jobs/${card.dataset.id}/optimize-expert/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(boxes),
    });
  } catch (e) {
    btn.disabled = false;
    spinner.hidden = true;
    setOptimizeExpertStatus(card, t("optimize.expert_could_not_start", { message: e.message }), true);
    return;
  }
  if (!res.ok) {
    const msg = await readErr(res);
    btn.disabled = false;
    spinner.hidden = true;
    setOptimizeExpertStatus(card, t("optimize.expert_could_not_start", { message: msg }), true);
    return;
  }
  pollOptimizeExpertStatus(card);
}

// Called once at card creation. A run started before a page reload keeps
// going server-side (see app/optimize_expert_queue.py), so the only thing a
// fresh card is missing is knowing to poll for it.
async function resumeOptimizeExpertStatus(card, job) {
  if (job.optimize_mode !== "expert") return;
  try {
    const r = await fetch(`/jobs/${card.dataset.id}/optimize-expert/status`, { cache: "no-store" });
    if (!r.ok) return;
    const d = await r.json();
    if (d.status === "running") {
      const { btn, spinner } = optimizeExpertUiRefs(card);
      btn.disabled = true;
      spinner.hidden = false;
      setOptimizeExpertStatus(card, t("optimize.expert_running"), false);
      pollOptimizeExpertStatus(card);
    } else if (d.status === "done" || d.status === "error") {
      const { logEl } = optimizeExpertUiRefs(card);
      if (d.log) { logEl.textContent = d.log; logEl.hidden = false; }
      setOptimizeExpertStatus(card,
        d.status === "done" ? t("optimize.expert_done") : t("optimize.expert_failed", { message: d.error || "" }),
        d.status === "error");
    }
  } catch (e) {}
}

// Keep the master toggle consistent with the sub-options: if all four steps
// are off, the master can't do anything, so untick it.
function syncOptimizeMaster(card) {
  const subs = ["optimize-linemerge", "optimize-linesimplify", "optimize-linesort", "optimize-reloop"];
  const anyOn = subs.some((cls) => card.querySelector("." + cls).checked);
  const master = card.querySelector(".optimize");
  if (!anyOn && master.checked) master.checked = false;
  applyOptimizeEnabledStyle(card);
}

function resetOptimize(card) {
  card.querySelector(".optimize").checked = !!appSettings.optimize_svg_default;
  card.querySelector(".optimize-linemerge").checked = !!appSettings.optimize_svg_linemerge_default;
  card.querySelector(".optimize-linesimplify").checked = !!appSettings.optimize_svg_linesimplify_default;
  card.querySelector(".optimize-linesort").checked = !!appSettings.optimize_svg_linesort_default;
  card.querySelector(".optimize-reloop").checked = !!appSettings.optimize_svg_reloop_default;
  card.querySelector(".optimize-tolerance").value =
    (appSettings.optimize_svg_tolerance_default_mm ?? 0.10).toFixed(2);
  applyOptimizeEnabledStyle(card);
  queueCardUpdate(card);
}

function resetParameters(card) {
  const pairs = [
    [".speed-pendown", appSettings.speed_pendown_default],
    [".speed-penup", appSettings.speed_penup_default],
    [".accel", appSettings.acceleration_default],
    [".pen-pos-up", appSettings.pen_pos_up_default],
    [".pen-pos-down", appSettings.pen_pos_down_default],
  ];
  for (const [sel, val] of pairs) {
    const el = card.querySelector(sel);
    if (el) { el.value = val; dispatchValueChange(el); }
  }
}

// Clamp a number input's value to its own min/max attributes. type="number"
// only enforces the bounds via form validation, so a user can still type 200
// into a 1–110 field — this snaps it back on blur/enter. Returns true if the
// value was changed.
function clampNumberInput(el) {
  if (!el || el.type !== "number" || el.value === "") return false;
  const v = parseFloat(el.value);
  if (!isFinite(v)) return false;
  const min = el.min !== "" ? parseFloat(el.min) : -Infinity;
  const max = el.max !== "" ? parseFloat(el.max) : Infinity;
  const c = Math.max(min, Math.min(max, v));
  if (c !== v) {
    el.value = String(c);
    return true;
  }
  return false;
}

function updateSliderProgress(slider) {
  if (!slider) return;
  const min = parseFloat(slider.min);
  const max = parseFloat(slider.max);
  const val = parseFloat(slider.value);
  if (!isFinite(min) || !isFinite(max) || !isFinite(val) || max <= min) {
    slider.style.setProperty("--progress", "0%");
    return;
  }
  const pct = Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100));
  slider.style.setProperty("--progress", pct + "%");
}

// Parses a form field's numeric value, falling back only when it's genuinely
// invalid/empty — unlike `parseFloat(v) || fallback`, this doesn't also
// override a legitimate 0 (wrong for fields like contrast whose neutral
// value is 1, not 0).
function numOr(value, fallback) {
  const n = parseFloat(value);
  return Number.isFinite(n) ? n : fallback;
}

// Sets a number input and its paired "<id>-slider" range input to the same
// value (used for the camera settings modal's picture-tuning fields).
function setSliderNumber(baseId, value) {
  const number = $(baseId);
  const slider = $(`${baseId}-slider`);
  number.value = value;
  slider.value = value;
  updateSliderProgress(slider);
}

function applyOffsetBoundsToCard(card, paperW, paperH) {
  const ox = card.querySelector(".transform-offset-x");
  const oy = card.querySelector(".transform-offset-y");
  const oxS = card.querySelector(".transform-offset-x-slider");
  const oyS = card.querySelector(".transform-offset-y-slider");
  if (!ox || !oy) return;
  const w = Math.max(1, paperW || 0);
  const h = Math.max(1, paperH || 0);
  ox.min = -w; ox.max = w;
  oy.min = -h; oy.max = h;
  if (oxS) { oxS.min = -w; oxS.max = w; }
  if (oyS) { oyS.min = -h; oyS.max = h; }
  // Clamp any out-of-range values so PATCH stays consistent with the new bounds.
  const cx = parseFloat(ox.value) || 0;
  const cy = parseFloat(oy.value) || 0;
  if (cx < -w || cx > w) ox.value = Math.max(-w, Math.min(w, cx));
  if (cy < -h || cy > h) oy.value = Math.max(-h, Math.min(h, cy));
  if (oxS) { oxS.value = ox.value; updateSliderProgress(oxS); }
  if (oyS) { oyS.value = oy.value; updateSliderProgress(oyS); }
}

function pairSlider(card, numberSel, sliderSel) {
  const number = card.querySelector(numberSel);
  const slider = card.querySelector(sliderSel);
  if (!number || !slider) return;
  slider.value = number.value;
  updateSliderProgress(slider);
  slider.addEventListener("input", () => {
    if (number.value !== slider.value) {
      number.value = slider.value;
      number.dispatchEvent(new Event("input", { bubbles: true }));
    }
    updateSliderProgress(slider);
  });
  slider.addEventListener("change", () => {
    number.dispatchEvent(new Event("change", { bubbles: true }));
  });
  number.addEventListener("input", () => {
    if (slider.value !== number.value) slider.value = number.value;
    updateSliderProgress(slider);
  });
}

function readTransformFromCard(card) {
  return {
    transform_scale: parseFloat(card.querySelector(".transform-scale").value) || 1,
    transform_rotation_deg: parseFloat(card.querySelector(".transform-rotation").value) || 0,
    transform_offset_x_mm: parseFloat(card.querySelector(".transform-offset-x").value) || 0,
    transform_offset_y_mm: parseFloat(card.querySelector(".transform-offset-y").value) || 0,
  };
}

function updatePreviewTransform(card, job) {
  const previewEl = card.querySelector(".svg-preview");
  const paper = previewEl.querySelector(".paper");
  const content = previewEl.querySelector(".paper-content");
  const margins = previewEl.querySelector(".paper-margins");
  previewStatus(card, job);
  if (!paper || !content) return;
  const ctx = cardCtx.get(job.job_id);
  if (!ctx || !ctx.svg) return;

  const w = job.paper_width_mm, h = job.paper_height_mm;
  if (w <= 0 || h <= 0) return;
  paper.style.aspectRatio = `${w} / ${h}`;

  // Everything geometric comes from the server (see requestPlacement). Ask
  // for a fresh answer whenever the inputs have moved, and redraw when it
  // lands — during a slider drag this function runs against uncommitted
  // editor values, so without the callback the reply would arrive with
  // nothing to paint it. Re-entry terminates: the second pass finds the key
  // already current and requests nothing.
  requestPlacement(card, job, () => updatePreviewTransform(card, job));
  // Draw against the answer carried forward to the current transform, not the
  // raw cached one: a drag has to keep moving between replies, and waiting for
  // the round-trip is what made the artwork freeze until the mouse came up.
  const place = effectivePlacement(job);
  if (!place) return;

  const mt = job.margin_top_mm, mr = job.margin_right_mm;
  const mb = job.margin_bottom_mm, ml = job.margin_left_mm;

  // The document's unrotated, fit-scaled box: what the <svg> element is laid
  // out at before CSS rotates it.
  const layoutW = place.layout_width_mm, layoutH = place.layout_height_mm;
  // Its rotated, user-scaled extent: what it actually covers on the page.
  const bboxW = place.footprint_width_mm, bboxH = place.footprint_height_mm;
  const userScale = Math.max(0.01, Math.min(5, job.transform_scale ?? 1));

  // Zoom the whole sheet so anything hanging off the paper still shows.
  const contentLeft = place.center_x_mm - bboxW / 2;
  const contentTop = place.center_y_mm - bboxH / 2;
  const extentW = Math.max(w, contentLeft + bboxW) - Math.min(0, contentLeft);
  const extentH = Math.max(h, contentTop + bboxH) - Math.min(0, contentTop);

  const cs = getComputedStyle(previewEl);
  const padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
  const padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  const availW = previewEl.clientWidth - padX;
  const availH = previewEl.clientHeight - padY;
  if (availW <= 0 || availH <= 0) return;
  const mmToPx = Math.min(availW / extentW, availH / extentH);
  paper.style.width = `${w * mmToPx}px`;
  paper.style.height = `${h * mmToPx}px`;

  content.style.left = `${(ml / w) * 100}%`;
  content.style.top = `${(mt / h) * 100}%`;
  content.style.width = `${(layoutW / w) * 100}%`;
  content.style.height = `${(layoutH / h) * 100}%`;
  content.style.transformOrigin = "center center";
  // The element is laid out at its unrotated size anchored to the margin
  // corner, so its transform-origin sits at (ml + layoutW/2, mt + layoutH/2)
  // — not at the placement's centre. translate() is the only part of this
  // transform applied in unrotated screen space, so the difference goes here.
  //
  // While this job is actually running, its live delta folds in too (see
  // activeRunDelta): the same pure page-space translation the plotter is
  // physically applying right now, so the preview matches where the pen
  // really is rather than where the design was drawn. It snaps back the
  // moment the job stops being active.
  const runDelta = activeRunDelta(job);
  const dx = (place.center_x_mm - (ml + layoutW / 2)) + (runDelta ? runDelta.dx : 0);
  const dy = (place.center_y_mm - (mt + layoutH / 2)) + (runDelta ? runDelta.dy : 0);
  content.style.transform =
    `translate(${dx * mmToPx}px, ${dy * mmToPx}px) ` +
    `rotate(${place.rotation_deg}deg) scale(${userScale})`;

  const anyM = mt > 0 || mr > 0 || mb > 0 || ml > 0;
  margins.hidden = !anyM;
  margins.style.top = `${(mt / h) * 100}%`;
  margins.style.left = `${(ml / w) * 100}%`;
  margins.style.right = `${(mr / w) * 100}%`;
  margins.style.bottom = `${(mb / h) * 100}%`;
}

function syncPreviewLayers(card, job) {
  const svgEl = card.querySelector(".paper-content svg");
  if (!svgEl) return;
  const groups = Array.from(svgEl.children).filter(
    (el) => el.tagName.toLowerCase() === "g" && el.getAttribute("inkscape:groupmode") === "layer"
  );
  // `selected !== false` keeps backward-compat with old job records that
  // never carried an explicit selected flag.
  const selected = new Set(
    job.layer_selections.filter((s) => s.selected !== false).map((s) => s.index)
  );
  groups.forEach((g, i) => { g.style.display = selected.has(i) ? "" : "none"; });
}

function renderLayers(card, job) {
  const ctx = cardCtx.get(job.job_id);
  if (!ctx || !ctx.svg) return;
  ensureSvgColors(card, ctx);
  const ul = card.querySelector(".layers");
  // A rename is in progress — leave the list alone so a state broadcast
  // doesn't yank the input out from under the cursor. The commit re-renders.
  if (ul.querySelector(".layer-label-edit")) return;
  // Layer entries carry their own metadata (label, type) plus an optional
  // `selected` flag; entries with selected===false stay in the list so the
  // metadata is preserved across toggles. The array order IS the plot/
  // execution order (see _run_job in plot_worker.py), so we iterate
  // job.layer_selections directly rather than the SVG's native layer order —
  // that's what lets the move-up/down buttons reorder execution.
  const selected = new Set(
    job.layer_selections.filter((s) => s.selected !== false).map((s) => s.index)
  );
  const svgByIndex = new Map(ctx.svg.layers.map((l) => [l.index, l]));
  ul.innerHTML = "";
  const total = job.layer_selections.length;
  job.layer_selections.forEach((sel, i) => {
    const layer = svgByIndex.get(sel.index);
    const li = document.createElement("li");
    const checked = selected.has(sel.index);
    const displayLabel = sel.label || (layer && layer.label) || "";
    // Optional pen name (API-set), trailing the layer name in grey.
    const penName = sel.pen_name || "";
    const swatch = layerSwatch(
      sel.type,
      (ctx.svg.layerColors || {})[sel.index] || null,
      ctx.svg.pageColor || null,
    );
    const estSecs = layerEstimateSeconds(job, ctx, sel.index);
    li.innerHTML = `
      <label>
        <input type="checkbox" data-index="${sel.index}" ${checked ? "checked" : ""} />
        ${swatch}
        <span class="layer-label" title="${t("a11y.rename_layer")}" data-i18n-title="a11y.rename_layer">${escapeHtml(displayLabel)}${
          penName ? `<span class="layer-pen">${escapeHtml(penName)}</span>` : ""
        }</span>
      </label>
      ${estSecs != null ? `<span class="layer-time">${escapeHtml(formatHoursMinutes(estSecs))}</span>` : ""}
      <div class="layer-move">
        <button type="button" class="icon-btn layer-move-up" data-index="${sel.index}" ${i === 0 ? "disabled" : ""} title="${t("a11y.move_up")}" data-i18n-title="a11y.move_up">↑</button>
        <button type="button" class="icon-btn layer-move-down" data-index="${sel.index}" ${i === total - 1 ? "disabled" : ""} title="${t("a11y.move_down")}" data-i18n-title="a11y.move_down">↓</button>
      </div>`;
    ul.appendChild(li);
  });
  // Attach change/click handlers once
  if (!ul.dataset.wired) {
    ul.addEventListener("change", () => {
      const cur = serverState.queue.find((j) => j.job_id === card.dataset.id);
      const checkedIndices = new Set(
        Array.from(ul.querySelectorAll("input[type=checkbox]:checked"))
          .map((el) => parseInt(el.dataset.index))
      );
      // Walk the current (possibly reordered) selections list, not the SVG's
      // native layer order, so a checkbox toggle never undoes a reorder.
      // Deselected layers stay in the list with `selected: false`. Their
      // label/type and per-layer speed overrides survive a toggle off-and-on.
      const layers = (cur?.layer_selections || []).map((ovr) => {
        const l = svgByIndex.get(ovr.index);
        const sel = {
          index: ovr.index,
          label: ovr.label || (l && l.label) || "",
          selected: checkedIndices.has(ovr.index),
        };
        if (ovr.type) sel.type = ovr.type;
        if (ovr.pen_name) sel.pen_name = ovr.pen_name;
        // Carry through API-set per-layer speed overrides — there's no UI
        // control for them, so a checkbox toggle here must not drop them.
        for (const k of ["speed_pendown", "speed_penup", "acceleration"]) {
          if (ovr[k] != null) sel[k] = ovr[k];
        }
        return sel;
      });
      // Write back immediately so a second edit fired before this one's
      // debounced PATCH lands (e.g. rapid checkbox toggles or a toggle right
      // after a reorder) builds on this result instead of the stale
      // pre-edit snapshot still sitting in serverState.queue.
      if (cur) cur.layer_selections = layers;
      const selectedCount = layers.filter((l) => l.selected).length;
      card.querySelector(".multi-layer-options").hidden = selectedCount < 2;
      syncPreviewLayers(card, { ...job, layer_selections: layers });
      queueCardUpdate(card, { layer_selections: layers });
    });
    ul.addEventListener("click", (e) => {
      const btn = e.target.closest(".layer-move-up, .layer-move-down");
      if (!btn || btn.disabled) return;
      const delta = btn.classList.contains("layer-move-up") ? -1 : 1;
      moveLayer(card, parseInt(btn.dataset.index), delta);
    });
    // Double-click a row's name to rename it. Display-only: label lives on the
    // layer_selections entry and just re-labels the row / its plot stage.
    ul.addEventListener("dblclick", (e) => {
      const span = e.target.closest(".layer-label");
      if (!span || ul.querySelector(".layer-label-edit")) return;
      e.preventDefault();
      const li = span.closest("li");
      const idx = parseInt(li.querySelector("input[type=checkbox]").dataset.index);
      const cur = serverState.queue.find((j) => j.job_id === card.dataset.id);
      if (!cur || !["ready", "completed", "failed", "cancelled"].includes(cur.status)) return;
      const entry = (cur.layer_selections || []).find((s) => s.index === idx);
      if (!entry) return;
      const wasLabel = entry.label || "";
      const input = document.createElement("input");
      input.type = "text";
      input.className = "layer-label-edit";
      input.value = wasLabel;
      span.replaceWith(input);
      input.focus();
      input.select();
      let done = false;
      const commit = (save) => {
        if (done) return;
        done = true;
        const val = input.value.trim();
        input.remove();
        const next = (cur.layer_selections || []).map((s) =>
          s.index === idx ? { ...s, label: save && val ? val : wasLabel } : s);
        cur.layer_selections = next;
        renderLayers(card, { ...job, layer_selections: next });
        if (save && val && val !== wasLabel) queueCardUpdate(card, { layer_selections: next });
      };
      input.addEventListener("blur", () => commit(true));
      input.addEventListener("click", (ev) => ev.stopPropagation());
      input.addEventListener("keydown", (ev) => {
        ev.stopPropagation();
        if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
        else if (ev.key === "Escape") { input.value = wasLabel; input.blur(); }
      });
    });
    ul.dataset.wired = "1";
  }
  const selectedCount = job.layer_selections.filter((s) => s.selected !== false).length;
  card.querySelector(".multi-layer-options").hidden = selectedCount < 2;
}

function moveLayer(card, layerIndex, delta) {
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job) return;
  const layers = job.layer_selections.map((s) => ({ ...s }));
  const pos = layers.findIndex((s) => s.index === layerIndex);
  if (pos < 0) return;
  const newPos = pos + delta;
  if (newPos < 0 || newPos >= layers.length) return;
  [layers[pos], layers[newPos]] = [layers[newPos], layers[pos]];
  // Write back immediately — see the matching comment in the checkbox
  // handler above. Without this, clicking ↑/↓ repeatedly (faster than the
  // 150ms PATCH debounce) has each click reorder the same stale pre-edit
  // array instead of stacking on the previous click's result.
  job.layer_selections = layers;
  renderLayers(card, { ...job, layer_selections: layers });
  queueCardUpdate(card, { layer_selections: layers });
}

// Per-stage status is a fixed small set; translate the known ones and fall
// back to the raw value for anything unexpected.
const STAGE_STATUS_KEYS = { pending: 1, current: 1, done: 1 };
function stageStatusLabel(st) {
  return STAGE_STATUS_KEYS[st] ? t(`stage_status.${st}`) : st;
}

function renderStages(card, job) {
  const wrap = card.querySelector(".stages-wrap");
  const ol = card.querySelector(".stages");
  if (!job.stages || job.stages.length <= 1) { wrap.hidden = true; ol.innerHTML = ""; return; }
  wrap.hidden = false;
  ol.innerHTML = "";
  // Pull each layer's type so stage rows can echo the icon shown in the
  // layer list above. With pause_between_layers=true (the only case where
  // this list is rendered) each stage has exactly one layer, but joining
  // the icons copes if that ever changes.
  const typeByIndex = new Map(
    (job.layer_selections || []).map((s) => [s.index, s.type])
  );
  const ctx = cardCtx.get(job.job_id);
  const layerColors = (ctx && ctx.svg && ctx.svg.layerColors) || {};
  const pageColor = (ctx && ctx.svg && ctx.svg.pageColor) || null;
  job.stages.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = `stage ${s.status}`;
    const icons = (s.layer_indices || [])
      .map((idx) => layerSwatch(typeByIndex.get(idx), layerColors[idx] || null, pageColor))
      .join("");
    // Same trailing grey pen name as the layer list above.
    const penName = stagePenName(job, s);
    li.innerHTML = `<span class="stage-num">${i + 1}</span>
      ${icons}
      <span class="stage-label">${escapeHtml((s.labels || []).join(", "))}${
        penName ? `<span class="layer-pen">${escapeHtml(penName)}</span>` : ""
      }</span>
      <span class="stage-status">${escapeHtml(stageStatusLabel(s.status))}</span>`;
    ol.appendChild(li);
  });
}

function renderPlotInfo(card, job) {
  const el = card.querySelector(".plot-info");
  if (job.estimated_total_seconds == null) { el.hidden = true; return; }
  el.hidden = false;
  el.querySelector(".est-time").textContent = formatDuration(Math.round(job.estimated_total_seconds));
  el.querySelector(".pendown-dist").textContent = `${(job.distance_pendown_m || 0).toFixed(2)} m`;
  el.querySelector(".total-dist").textContent = `${(job.distance_total_m || 0).toFixed(2)} m`;
  el.querySelector(".pen-lifts").textContent = `${job.pen_lifts || 0}`;
}

function onPaperChange(card) {
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job) return;
  const ctx = cardCtx.get(job.job_id);
  const preset = card.querySelector(".paper-size").value;
  card.querySelector(".custom-dims").hidden = preset !== "Custom";
  applyMachineAutoRotateToCard(card);
  const { w, h, paper_size_name } = readPaperFromCard(card);

  const updates = {
    paper_width_mm: w,
    paper_height_mm: h,
    paper_size_name: paper_size_name,
    margin_top_mm: parseFloat(card.querySelector(".margin-top").value) || 0,
    margin_right_mm: parseFloat(card.querySelector(".margin-right").value) || 0,
    margin_bottom_mm: parseFloat(card.querySelector(".margin-bottom").value) || 0,
    margin_left_mm: parseFloat(card.querySelector(".margin-left").value) || 0,
  };

  applyOffsetBoundsToCard(card, w, h);
  Object.assign(updates, readTransformFromCard(card));

  // Auto-fit if not user-locked and content exceeds available area
  if (!ctx?.fitLocked && ctx?.svg?.width_mm && ctx?.svg?.height_mm) {
    const aW = updates.paper_width_mm - updates.margin_left_mm - updates.margin_right_mm;
    const aH = updates.paper_height_mm - updates.margin_top_mm - updates.margin_bottom_mm;
    if (ctx.svg.width_mm > aW || ctx.svg.height_mm > aH) {
      card.querySelector(".fit-content").checked = true;
    }
  }
  updates.fit_content = card.querySelector(".fit-content").checked;

  // Update custom inputs to match computed dims (useful if orientation toggled)
  card.querySelector(".paper-w").value = w;
  card.querySelector(".paper-h").value = h;

  queueCardUpdate(card, updates);
}

const cardUpdateTimers = new Map();
// Per card: {explicit, form}. `explicit` holds fields passed in directly
// (currently only layer_selections), `form` records that some edit wants the
// whole form re-read. Both have to survive coalescing — the last call used to
// win outright, so a layer toggle followed within the debounce window by any
// other edit was never sent, while the card went on showing it as applied.
const cardUpdatePending = new Map();

// Edits the user has made that the server has not echoed back yet.
//
// A state broadcast replaces `serverState` wholesale, and PATCHes are
// debounced 150ms, so a broadcast landing in that window used to revert the
// card to the pre-edit values. Reordering layers made that visible and then
// harmful: the panel would flick back to the old order, and because the next
// click reads its starting point out of `serverState`, a second click would
// build on the reverted array and lose the first move. Broadcasts are frequent
// exactly when layers get arranged — right after an upload, while the optimize
// and plan queues step the job through its statuses.
//
// So an edit stays here from the moment it is made until the PATCH carrying it
// comes back, and every broadcast is re-overlaid with it on the way in.
const cardUpdateUnconfirmed = new Map();   // job_id -> {field: value}

function rememberUnconfirmed(id, updates) {
  cardUpdateUnconfirmed.set(id, { ...(cardUpdateUnconfirmed.get(id) || {}), ...updates });
}

function applyUnconfirmedEdits() {
  if (!cardUpdateUnconfirmed.size) return;
  for (const job of serverState.queue || []) {
    const mine = cardUpdateUnconfirmed.get(job.job_id);
    if (mine) Object.assign(job, mine);
  }
}

function queueCardUpdate(card, immediateUpdates = null) {
  // Coalesce rapid updates into one PATCH per ~150ms per card
  const id = card.dataset.id;
  clearTimeout(cardUpdateTimers.get(id));
  const pending = cardUpdatePending.get(id) || { explicit: {}, form: false };
  if (immediateUpdates) {
    Object.assign(pending.explicit, immediateUpdates);
    rememberUnconfirmed(id, immediateUpdates);
  } else pending.form = true;
  cardUpdatePending.set(id, pending);
  cardUpdateTimers.set(id, setTimeout(() => {
    cardUpdateTimers.delete(id);
    cardUpdatePending.delete(id);
    sendCardUpdate(card, pending);
  }, 150));
}

// Pull one job back from the server and redraw its card. Used when a PATCH is
// refused: the card and serverState have both already been updated locally, so
// the only reliable "before" state is the server's.
async function revertCardToServer(card) {
  try {
    const res = await fetch(`/jobs/${card.dataset.id}`);
    if (!res.ok) return;
    const fresh = await res.json();
    const i = serverState.queue.findIndex((j) => j.job_id === fresh.job_id);
    if (i >= 0) serverState.queue[i] = fresh;
    updateCard(card, fresh);
  } catch (e) {}
}

async function sendCardUpdate(card, pending) {
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job) return;
  // A PATCH on a non-queued job re-queues it server-side. Hide the requeue
  // button immediately so the user doesn't see a stale "Plot again" ↻ between
  // the PATCH and the broadcast landing.
  const requeueBtn = card.querySelector(".job-requeue");
  if (requeueBtn && job.status !== "ready") requeueBtn.hidden = true;
  const updates = {};
  if (pending.form) {
    const { w, h, paper_size_name } = readPaperFromCard(card);
    updates.paper_width_mm = w;
    updates.paper_height_mm = h;
    updates.paper_size_name = paper_size_name;
    updates.margin_top_mm = parseFloat(card.querySelector(".margin-top").value) || 0;
    updates.margin_right_mm = parseFloat(card.querySelector(".margin-right").value) || 0;
    updates.margin_bottom_mm = parseFloat(card.querySelector(".margin-bottom").value) || 0;
    updates.margin_left_mm = parseFloat(card.querySelector(".margin-left").value) || 0;
    updates.fit_content = card.querySelector(".fit-content").checked;
    Object.assign(updates, readTransformFromCard(card));
    updates.speed_pendown = parseInt(card.querySelector(".speed-pendown").value);
    updates.speed_penup = parseInt(card.querySelector(".speed-penup").value);
    updates.acceleration = parseInt(card.querySelector(".accel").value);
    updates.pen_pos_up = parseInt(card.querySelector(".pen-pos-up").value);
    updates.pen_pos_down = parseInt(card.querySelector(".pen-pos-down").value);
    updates.pause_between_layers = card.querySelector(".pause-between-layers").checked;
    updates.delete_on_complete = card.querySelector(".delete-on-complete").checked;
    updates.disable_motors_on_complete = card.querySelector(".disable-motors-on-complete").checked;
    updates.record_plot = card.querySelector(".record-plot").checked;
    updates.optical_reg = card.querySelector(".optical-reg").checked;
    updates.record_mode = card.querySelector(".record-mode").value;
    updates.record_timelapse_interval_s = parseFloat(card.querySelector(".record-timelapse-interval").value) || 5;
    updates.record_speed_multiplier = parseFloat(card.querySelector(".record-speed-multiplier").value) || 4;
    updates.optimize_svg = card.querySelector(".optimize").checked;
    updates.optimize_svg_linemerge = card.querySelector(".optimize-linemerge").checked;
    updates.optimize_svg_linesimplify = card.querySelector(".optimize-linesimplify").checked;
    updates.optimize_svg_linesort = card.querySelector(".optimize-linesort").checked;
    updates.optimize_svg_reloop = card.querySelector(".optimize-reloop").checked;
    const tol = parseFloat(card.querySelector(".optimize-tolerance").value);
    if (isFinite(tol) && tol > 0) updates.optimize_svg_tolerance_mm = tol;
    updates.optimize_mode = getSegmentedValue(card.querySelector(".optimize-mode"), "beginner");
    for (const n of [1, 2, 3]) {
      updates[`optimize_expert_${n}_enabled`] = card.querySelector(`.optimize-expert-${n}-enabled`).checked;
      updates[`optimize_expert_${n}_cmd`] = card.querySelector(`.optimize-expert-${n}-cmd`).value;
    }
  }
  Object.assign(updates, pending.explicit);
  // An emptied number field parses to NaN, which JSON-encodes as null — and a
  // null speed or pen height written onto the job crashes the plot worker.
  // Leave the field out instead; the server keeps its current value.
  for (const [k, v] of Object.entries(updates)) {
    if (typeof v === "number" && !Number.isFinite(v)) delete updates[k];
  }
  if (!Object.keys(updates).length) return;
  // A rejected PATCH used to be indistinguishable from a saved one: the card
  // had already been redrawn from local state, so the screen showed settings
  // the machine was never told about. Surface the failure and pull the card
  // back to whatever the server actually holds.
  const errEl = card.querySelector(".job-save-error");
  const doPatch = () => fetch(`/jobs/${card.dataset.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  try {
    let res;
    try {
      res = await doPatch();
    } catch (e) {
      // A dropped/stalled connection (e.g. the Pi's event loop briefly
      // starved by a heavy vpype run) throws here identically to a real
      // network outage. It's usually transient, and the fallback below
      // reverts live user input (revertCardToServer) — worth one retry
      // before treating the edit as failed.
      res = await doPatch();
    }
    if (!res.ok) {
      const msg = await readErr(res);
      if (errEl) {
        errEl.textContent = t("card.save_failed", { message: msg });
        errEl.hidden = false;
      }
      // serverState was written to optimistically (see the layer handlers), so
      // it can't be trusted as the "before" state — re-read the job.
      cardUpdateUnconfirmed.delete(card.dataset.id);
      await revertCardToServer(card);
      return;
    }
    // The server now holds these values, so the next broadcast carries them
    // and the overlay has nothing left to protect.
    cardUpdateUnconfirmed.delete(card.dataset.id);
    if (errEl) { errEl.hidden = true; errEl.textContent = ""; }
    // Mirrors app/main.py's _persist_expert_defaults: the server just
    // remembered this box's text as the next new job's pre-fill, but
    // appSettings was only fetched once at page load, so a job created right
    // after this one would otherwise still see the old (or empty) default.
    for (const n of [1, 2, 3]) {
      const key = `optimize_expert_${n}_cmd`;
      if (key in updates) appSettings[`${key}_default`] = updates[key];
    }
    // A rename writes through to the library entry, and a mode switch swaps
    // the job's linked svg — neither reaches the browser via the state
    // broadcast (upload metadata isn't broadcast), so re-pull the library.
    if ("name" in updates || "layer_mode" in updates) loadLibrary();
  } catch (e) {
    cardUpdateUnconfirmed.delete(card.dataset.id);
    if (errEl) {
      errEl.textContent = t("card.save_failed", { message: e.message });
      errEl.hidden = false;
    }
    return;
  }
  // Refresh visuals locally right away (server will broadcast soon too)
  if (updates.paper_width_mm) updatePreviewTransform(card, { ...job, ...updates });
}

// A queueCardUpdate edit (e.g. a layer reorder) sits debounced for up to
// 150ms before its PATCH goes out. Starting the queue reads whatever's on the
// job record at that instant, so an edit made just before hitting Plot has to
// land first — otherwise the job can start with the pre-edit order/settings.
// Any action that immediately reads job state server-side should await this.
async function flushPendingCardUpdates() {
  const ids = [...cardUpdateTimers.keys()];
  await Promise.all(ids.map((id) => {
    clearTimeout(cardUpdateTimers.get(id));
    cardUpdateTimers.delete(id);
    const pending = cardUpdatePending.get(id);
    cardUpdatePending.delete(id);
    const card = queueList.querySelector(`[data-id="${id}"]`);
    return (pending && card) ? sendCardUpdate(card, pending) : Promise.resolve();
  }));
}

async function deleteJob(id) {
  const res = await fetch(`/jobs/${id}`, { method: "DELETE" });
  if (!res.ok) {
    topMessage.textContent = t("error.cannot_delete", { message: await readErr(res) });
    topMessage.className = "error";
  }
  // Deleting a job can free its drawing, so an in-use badge may have cleared.
  loadLibrary();
}

// Put a finished or cancelled job back to `ready`, with its stages and
// estimate reset. The card redraws from the broadcast that follows, which is
// what hides the button.
async function requeueJob(id) {
  try {
    const res = await fetch(`/jobs/${id}/requeue`, { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = t("error.requeue_failed", { message: e.message });
    topMessage.className = "error";
  }
}

// Applies a job-card's suggested jog correction (see job.jog_hint_dx_mm/
// jog_hint_dy_mm) via the same idle manual-jog endpoint as the top control
// panel's Move buttons, then re-queues the job it was correcting — one
// click both fixes the carriage position and puts the job back where
// pressing Plot works again. The error banner (and this button with it)
// disappears on its own once the requeue clears job.error.
async function nudgeBack(jobId, dxStr, dyStr) {
  const dx = parseFloat(dxStr) || 0;
  const dy = parseFloat(dyStr) || 0;
  try {
    const res = await fetch("/pen/jog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Pre-confirmed: this correction can legitimately land above/left of
      // the origin (the server aims for the design's own baseline overflow,
      // not for a non-negative delta), and the below-origin prompt exists to
      // catch aiming blind — the exact distance is printed on the button the
      // user just clicked, so there's nothing left to warn about.
      body: JSON.stringify({ dx_mm: dx, dy_mm: dy, confirm_below_origin: true }),
    });
    if (!res.ok) throw new Error(await readErr(res));
    const res2 = await fetch(`/jobs/${jobId}/requeue`, { method: "POST" });
    if (!res2.ok) throw new Error(await readErr(res2));
  } catch (e) {
    topMessage.textContent = t("error.request_failed", { message: e.message });
    topMessage.className = "error";
  }
}

// "Save As": download the job's processed drawing in the chosen format. The
// source is whatever GET /jobs/{id}/svg would serve (the vpype .opt.svg once
// optimization has run, else the raw upload) — exported in its own
// coordinates, not placed on the page. The fetch goes through a blob so a
// failed conversion shows in the card instead of navigating the tab to a JSON
// error body.
async function exportJob(card, id) {
  const sel = card.querySelector(".export-format");
  const btn = card.querySelector(".export-download");
  const errEl = card.querySelector(".export-error");
  const raw = sel.value;
  let params = raw === "png-transparent"
    ? "fmt=png&bg=transparent"
    : `fmt=${encodeURIComponent(raw)}`;
  if (getSegmentedValue(card.querySelector(".export-scope"), "optimized") === "placed") {
    params += "&placed=true";
  }
  errEl.hidden = true;
  btn.disabled = true;
  try {
    const res = await fetch(`/jobs/${id}/export?${params}`);
    if (!res.ok) {
      errEl.textContent = await readErr(res);
      errEl.hidden = false;
      return;
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/i);
    const name = m ? m[1] : `export.${raw}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    errEl.textContent = t("export.failed", { message: String(e) });
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

async function moveJob(id, delta) {
  const idx = serverState.queue.findIndex((j) => j.job_id === id);
  if (idx < 0) return;
  const newIndex = Math.max(0, Math.min(serverState.queue.length - 1, idx + delta));
  if (newIndex === idx) return;
  await fetch(`/jobs/${id}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_index: newIndex }),
  });
}

// ───── Top-level controls ────────────────────────────────────────────────

plotBtn.addEventListener("click", async () => {
  await flushPendingCardUpdates();
  postAction("/queue/start");
});
pauseBtn.addEventListener("click", () => postAction("/queue/pause"));
pausePenUpBtn.addEventListener("click", () => postAction("/queue/pause-at-pen-up"));
resumeBtn.addEventListener("click", () => postAction("/queue/resume"));
continueBtn.addEventListener("click", () => postAction("/queue/continue"));
calibrateBtn.addEventListener("click", () => postAction("/queue/calibrate"));
cancelBtn.addEventListener("click", () => postAction("/queue/cancel"));

// Redraw the last N mm of pen-down travel: rewind the paused plot's resume
// point and let it carry on, re-tracing a skipped line or a stretch a dry pen
// missed. Only shown while paused (see applyTopControls).
async function postRedraw(distanceMm) {
  try {
    const res = await fetch("/queue/redraw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ distance_mm: distanceMm }),
    });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = t("error.request_failed", { message: e.message });
    topMessage.className = "error";
  }
}
redrawBtn.addEventListener("click", () => postRedraw(parseFloat(redrawMm.value) || 50));
redrawControls.querySelectorAll(".redraw-preset").forEach((b) => {
  b.addEventListener("click", () => {
    redrawMm.value = b.dataset.mm;
    postRedraw(parseFloat(b.dataset.mm));
  });
});

// Standalone calibration-test library (calibration/ folder): only relevant
// at a pen-change pause. Re-fetched once per pause so a file dropped in
// while paused shows up without a page reload.
let calibrationFilesFetchedFor = null;
async function refreshCalibrationFiles() {
  try {
    const res = await fetch("/calibration/files");
    if (!res.ok) return;
    const { files } = await res.json();
    calibrationFileSelect.innerHTML = "";
    for (const f of files) {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      calibrationFileSelect.appendChild(opt);
    }
    calibrationFileRow.hidden = files.length === 0;
  } catch (e) {}
}
calibrationFileRunBtn.addEventListener("click", async () => {
  const filename = calibrationFileSelect.value;
  if (!filename) return;
  try {
    const res = await fetch("/queue/calibrate-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = t("error.request_failed", { message: e.message });
    topMessage.className = "error";
  }
});

async function postAction(path) {
  try {
    const res = await fetch(path, { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = t("error.request_failed", { message: e.message });
    topMessage.className = "error";
  }
}

// Pen up/down live outside #queue-controls (which hides when the queue is
// empty) so they get their own tiny message area instead of topMessage.
penUpBtn.addEventListener("click", () => postPenAction("/pen/up"));
penDownBtn.addEventListener("click", () => postPenAction("/pen/down"));
motorsEnableBtn.addEventListener("click", () => postPenAction("/motors/enable"));
motorsDisableBtn.addEventListener("click", () => postPenAction("/motors/disable"));

async function postPenAction(path) {
  penControlsMessage.textContent = "";
  penControlsMessage.className = "muted";
  try {
    const res = await fetch(path, { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    penControlsMessage.textContent = t("error.request_failed", { message: e.message });
    penControlsMessage.className = "error";
  }
}

// Manual recording controls — independent of any job's record_plot flag.
async function postCameraAction(path, body) {
  cameraControlsMessage.textContent = "";
  cameraControlsMessage.className = "muted";
  try {
    const opts = { method: "POST" };
    if (body) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    cameraControlsMessage.textContent = t("error.request_failed", { message: e.message });
    cameraControlsMessage.className = "error";
  }
}
cameraStartBtn.addEventListener("click", () => postCameraAction("/camera/recording/start", {}));
cameraPauseBtn.addEventListener("click", () => postCameraAction("/camera/recording/pause"));
cameraResumeBtn.addEventListener("click", () => postCameraAction("/camera/recording/resume"));
cameraStopBtn.addEventListener("click", () => postCameraAction("/camera/recording/stop"));

// Confirm/cancel prompt for a jog or nudge that lands above or left of the
// origin. The server allows it, but only on a second ask (see
// plot_worker.manual_jog): it aims part of the plot off the top-left of the
// paper, and past the origin the carriage may not have the travel to get
// there — worth a look before it moves, not worth refusing outright.
const belowOriginModal = $("below-origin-modal");
let belowOriginRetry = null;

function askBelowOrigin(retry) {
  belowOriginRetry = retry;
  belowOriginModal.hidden = false;
}
function closeBelowOrigin() {
  belowOriginModal.hidden = true;
  belowOriginRetry = null;
}
$("below-origin-cancel").addEventListener("click", closeBelowOrigin);
belowOriginModal.addEventListener("click", (e) => {
  if (e.target === belowOriginModal) closeBelowOrigin();
});
$("below-origin-confirm").addEventListener("click", () => {
  const retry = belowOriginRetry;
  closeBelowOrigin();
  if (retry) retry();
});

// Fine origin nudge — only meaningful at an awaiting_pen_change pause (see
// applyTopControls, which shows/hides #origin-nudge and updates the readouts
// from the broadcast state).
async function postNudge(dx, dy, confirmBelowOrigin) {
  try {
    const res = await fetch("/queue/nudge-origin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dx_mm: dx, dy_mm: dy, confirm_below_origin: !!confirmBelowOrigin }),
    });
    if (!res.ok) {
      const { code, text } = await readErrDetail(res);
      if (code === "nudge_below_origin") {
        return askBelowOrigin(() => postNudge(dx, dy, true));
      }
      throw new Error(text);
    }
    return true;
  } catch (e) {
    topMessage.textContent = t("error.request_failed", { message: e.message });
    topMessage.className = "error";
    return false;
  }
}

originNudge.querySelectorAll(".nudge-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const step = parseFloat(btn.dataset.step);
    if (btn.dataset.axis === "x") postNudge(step, 0, false);
    else postNudge(0, step, false);
  });
});

// Optical registration — a camera measurement at a pen-change pause that
// proposes a dx/dy, which the user confirms via the same postNudge() path as
// the manual nudge above. It never moves the carriage on its own.
let opticalRegDismissedSig = null;
// Signature of the reading already sent to nudge_origin, so it can never be
// applied twice (see the Apply handler).
let opticalRegAppliedSig = null;

function rerenderOpticalReg() {
  const active = serverState.active_id
    ? serverState.queue.find((j) => j.job_id === serverState.active_id)
    : null;
  renderOpticalReg(active, active ? active.status : "idle");
}

function opticalRegSig(r) {
  return `${r.status}|${r.dx_mm}|${r.dy_mm}|${r.probe_mm}`;
}

async function postOpticalRegMeasure(probeMm) {
  try {
    const res = await fetch("/queue/optical-reg/measure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(probeMm == null ? {} : { probe_mm: probeMm }),
    });
    if (!res.ok) throw new Error((await readErrDetail(res)).text);
  } catch (e) {
    topMessage.textContent = t("error.request_failed", { message: e.message });
    topMessage.className = "error";
  }
}

function renderOpticalReg(active, status) {
  const reg = serverState.optical_reg || { status: "idle" };
  const eligible = !!active
    && (status === "awaiting_pen_change" || status === "measuring_registration")
    && !!appSettings.camera_enabled
    && !!active.optical_reg
    // reg.ready: the run actually got its reference cross down. Without it
    // there is nothing to measure against, and measuring would draw probe
    // crosses on the artwork and report the gap between two of those.
    && !!reg.ready
    && (appSettings.optical_reg_mm_per_px || 0) > 0;
  opticalReg.hidden = !eligible;
  if (!eligible) return;

  const busy = reg.status === "measuring" || status === "measuring_registration";
  opticalRegMeasureBtn.disabled = busy;
  opticalRegWidenBtn.disabled = busy;
  opticalRegWidenBtn.hidden = !(reg.status === "measured" || reg.status === "failed");
  const dismissed = opticalRegDismissedSig === opticalRegSig(reg);

  if (busy) {
    opticalRegStatus.textContent = t("controls.optical_reg_measuring");
    opticalRegResult.hidden = true;
  } else if (reg.status === "measured" && !dismissed) {
    opticalRegStatus.textContent = "";
    opticalRegPreview.src = `/camera/optical-reg/preview?t=${Date.now()}`;
    opticalRegReadout.textContent = t("controls.optical_reg_readout", {
      dx: (reg.dx_mm ?? 0).toFixed(2),
      dy: (reg.dy_mm ?? 0).toFixed(2),
      conf: Math.round((reg.confidence || 0) * 100),
      probe: (reg.probe_mm ?? 0).toFixed(1),
    });
    opticalRegResult.hidden = false;
  } else if (reg.status === "failed" && !dismissed) {
    opticalRegStatus.textContent = reg.reason || t("controls.optical_reg_failed");
    opticalRegResult.hidden = true;
  } else {
    opticalRegStatus.textContent = "";
    opticalRegResult.hidden = true;
  }
}

opticalRegMeasureBtn.addEventListener("click", () => postOpticalRegMeasure(null));
opticalRegWidenBtn.addEventListener("click", () => {
  const reg = serverState.optical_reg || {};
  postOpticalRegMeasure(Math.max(0.4, (reg.probe_mm || 2) * 2));
});
opticalRegApplyBtn.addEventListener("click", async () => {
  const reg = serverState.optical_reg || {};
  const sig = opticalRegSig(reg);
  // The nudge is *relative* and the reading stays "measured" after it lands, so
  // a second click would apply the same correction a second time. Retire the
  // reading up front, by signature, so neither a double click nor a re-render
  // from the WebSocket can offer it again; the result card disappearing is also
  // the only confirmation the user gets that it went through. Put back on a
  // definite failure. (The below-origin confirmation hands off to a modal and
  // resolves undefined here — the reading stays retired either way, so
  // cancelling that prompt means measuring again.)
  if (reg.status !== "measured" || opticalRegAppliedSig === sig) return;
  opticalRegAppliedSig = sig;
  opticalRegDismissedSig = sig;
  rerenderOpticalReg();
  if ((await postNudge(reg.dx_mm || 0, reg.dy_mm || 0, false)) === false) {
    opticalRegAppliedSig = null;
    opticalRegDismissedSig = null;
    rerenderOpticalReg();
  }
});
opticalRegDismissBtn.addEventListener("click", () => {
  opticalRegDismissedSig = opticalRegSig(serverState.optical_reg || { status: "idle" });
  rerenderOpticalReg();
});

// Manual jog — idle-only (see applyTopControls, which enables/disables
// #jog-controls and updates the readouts from the broadcast state).
// Distinct from the fine origin nudge above, which only applies mid-plot
// to the active job's remaining stages.
//
// Success is shown by flashing the clicked button green for 2s; a denied
// command (bed-edge/artwork-bounds rejection, or any other failure) blinks
// it red twice instead, instead of a persistent confirmation message.
function flashJogResult(btn, ok) {
  btn.classList.remove("jog-flash-ok", "jog-flash-err-blink");
  void btn.offsetWidth; // restart the flash/blink if the same button is clicked again quickly
  clearTimeout(btn._jogFlashTimer);
  if (ok) {
    btn.classList.add("jog-flash-ok");
    btn._jogFlashTimer = setTimeout(() => btn.classList.remove("jog-flash-ok"), 2000);
  } else {
    btn.classList.add("jog-flash-err-blink");
    btn._jogFlashTimer = setTimeout(() => btn.classList.remove("jog-flash-err-blink"), 800);
  }
}

async function postJog(dx, dy, confirmBelowOrigin) {
  try {
    const res = await fetch("/pen/jog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dx_mm: dx, dy_mm: dy, confirm_below_origin: !!confirmBelowOrigin }),
    });
    if (!res.ok) {
      const { code, text } = await readErrDetail(res);
      // Not a failure — don't blink the button red for a move the user is
      // about to be asked about; the retry reports its own outcome.
      if (code === "jog_below_origin") {
        askBelowOrigin(() => postJog(dx, dy, true));
        return;
      }
      throw new Error(text);
    }
    flashJogResult(jogMoveBtn, true);
    jogXInput.value = "";
    jogYInput.value = "";
  } catch (e) {
    flashJogResult(jogMoveBtn, false);
  }
}

jogMoveBtn.addEventListener("click", () => {
  postJog(parseFloat(jogXInput.value) || 0, parseFloat(jogYInput.value) || 0, false);
});

// One press to the Move shortcut configured in Settings — an absolute spot
// measured from the origin, so a second press moves nothing (see
// plot_worker.manual_jog_shortcut). Whether arriving also declares that spot
// the new origin is part of that same setting, not decided here.
jogShortcutBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/pen/jog-shortcut", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    flashJogResult(jogShortcutBtn, true);
  } catch (e) {
    flashJogResult(jogShortcutBtn, false);
  }
});

jogHomeBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/pen/jog-home", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    flashJogResult(jogHomeBtn, true);
  } catch (e) {
    flashJogResult(jogHomeBtn, false);
  }
});

// Declare where the carriage sits now to be the page's top-left corner: the
// delta readout goes back to 0, 0 and nothing moves (see plot_worker.
// set_origin). From here on Return to Origin comes back to this spot.
jogOriginBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/pen/set-origin", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    flashJogResult(jogOriginBtn, true);
  } catch (e) {
    flashJogResult(jogOriginBtn, false);
  }
});

// Pen height live test — only takes effect while this card's job is the
// active one and paused at a pen-change (see set_live_pen_heights); harmless
// no-op otherwise (server rejects with 409, silently ignored here since the
// normal queueCardUpdate "change" listener already surfaces real save errors).
let penHeightDebounceTimer = null;
function applyLivePenHeight(card, which) {
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job || job.job_id !== serverState.active_id || job.status !== "awaiting_pen_change") return;
  clearTimeout(penHeightDebounceTimer);
  penHeightDebounceTimer = setTimeout(async () => {
    try {
      const body = { test: which };
      if (which === "up") body.pen_pos_up = parseInt(card.querySelector(".pen-pos-up").value);
      else body.pen_pos_down = parseInt(card.querySelector(".pen-pos-down").value);
      const res = await fetch("/queue/pen-height", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await readErr(res));
    } catch (e) {
      topMessage.textContent = t("error.request_failed", { message: e.message });
      topMessage.className = "error";
    }
  }, 300);
}

// Live speed/pen-height push while a plot is actively running (as opposed to
// applyLivePenHeight above, which only fires at an awaiting_pen_change pause
// and does a physical test move). The two gates are mutually exclusive by
// status, so both sets of listeners can safely share the pen-pos-up/down
// inputs. See set_live_plot_settings in plot_worker.py.
const liveSettingsDebounceTimers = {};
function applyLiveSetting(card, field, selector) {
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job || job.job_id !== serverState.active_id || job.status !== "plotting") return;
  clearTimeout(liveSettingsDebounceTimers[field]);
  liveSettingsDebounceTimers[field] = setTimeout(async () => {
    try {
      const val = parseInt(card.querySelector(selector).value, 10);
      if (!Number.isFinite(val)) return;
      const res = await fetch("/queue/live-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: val }),
      });
      if (!res.ok) throw new Error(await readErr(res));
    } catch (e) {
      topMessage.textContent = t("error.request_failed", { message: e.message });
      topMessage.className = "error";
    }
  }, 300);
}

// The pen loaded for a stage, or null when it's unknown. A stage normally
// holds a single layer (pause_between_layers is the only case where stages
// are shown), but a multi-layer stage still has one pen only if every one of
// its layers names the same one.
function stagePenName(job, stage) {
  if (!stage) return null;
  const penByIndex = new Map(
    (job.layer_selections || []).map((s) => [s.index, s.pen_name])
  );
  const pens = (stage.layer_indices || []).map((i) => penByIndex.get(i) || "");
  if (!pens.length || pens.some((p) => !p)) return null;
  return pens.every((p) => p === pens[0]) ? pens[0] : null;
}

// At an awaiting_pen_change pause `current_stage_index` already points at the
// *next* stage, so the one just finished is the entry before it. Name the pen
// to swap to when we know it — and say "if needed" when we don't know what's
// in the plotter right now (no pen on the finished stage).
function penChangeMessage(job) {
  const stages = job.stages || [];
  const nextPen = stagePenName(job, stages[job.current_stage_index]);
  if (!nextPen) return t("msg.awaiting_pen_change");
  const currentPen = stagePenName(job, stages[job.current_stage_index - 1]);
  if (!currentPen) return t("msg.awaiting_pen_change_to_optional", { pen: nextPen });
  if (currentPen === nextPen) return t("msg.awaiting_pen_change");
  return t("msg.awaiting_pen_change_to", { pen: nextPen });
}

function applyTopControls() {
  const s = serverState;
  const active = s.active_id ? s.queue.find((j) => j.job_id === s.active_id) : null;
  const status = active ? active.status : "idle";

  // Plot runs the topmost ready job, so it is offered whenever there is one
  // and nothing is currently running.
  plotBtn.hidden = !!active || !s.queue.some((j) => j.status === "ready");
  pauseBtn.hidden = !active || status !== "plotting";
  pausePenUpBtn.hidden = !active || status !== "plotting";
  const penUpPending = !!s.pause_at_pen_up_pending;
  pausePenUpBtn.textContent = penUpPending ? t("controls.pausing_pen_up") : t("controls.pause_pen_up");
  pausePenUpBtn.disabled = penUpPending;
  resumeBtn.hidden = !active || status !== "paused";
  redrawControls.hidden = !(active && status === "paused");
  continueBtn.hidden = !(active && status === "awaiting_pen_change");
  // Calibration button: visible only at a pen-change pause when this job has
  // at least one type='calibration' layer. Label switches singular/plural.
  const calLayers = active && status === "awaiting_pen_change"
    ? (active.layer_selections || []).filter((l) => l.type === "calibration")
    : [];
  calibrateBtn.hidden = calLayers.length === 0;
  calibrateBtn.textContent = calLayers.length > 1
    ? t("controls.calibrate_plural")
    : t("controls.calibrate");
  cancelBtn.hidden = !active;

  // Standalone calibration-file library: only relevant at a pen-change
  // pause. Fetched once per pause (tracked by job_id) rather than on every
  // state broadcast.
  if (active && status === "awaiting_pen_change") {
    if (calibrationFilesFetchedFor !== active.job_id) {
      calibrationFilesFetchedFor = active.job_id;
      refreshCalibrationFiles();
    }
  } else {
    calibrationFilesFetchedFor = null;
    calibrationFileRow.hidden = true;
  }

  // Fine origin nudge: only relevant at a pen-change pause.
  originNudge.hidden = !(active && status === "awaiting_pen_change");
  nudgeXReadout.textContent = (s.origin_nudge_x_mm ?? 0).toFixed(1);
  nudgeYReadout.textContent = (s.origin_nudge_y_mm ?? 0).toFixed(1);

  renderOpticalReg(active, status);

  // Manual jog: idle-only. A run ends with its own job, so between jobs the
  // machine really is idle and these stay available for re-aiming.
  const jogDisabled = s.status !== "idle";
  jogXInput.disabled = jogDisabled;
  jogYInput.disabled = jogDisabled;
  jogMoveBtn.disabled = jogDisabled;
  jogShortcutBtn.disabled = jogDisabled;
  jogHomeBtn.disabled = jogDisabled;
  jogOriginBtn.disabled = jogDisabled;
  jogXReadout.textContent = (s.manual_origin_offset_x_mm ?? 0).toFixed(1);
  jogYReadout.textContent = (s.manual_origin_offset_y_mm ?? 0).toFixed(1);

  // Pen up/down: only refused while a real plot_run is actively driving the
  // pen (mirrors plot_worker's _current_ad guard) — enabled otherwise,
  // including while idle or paused/awaiting_pen_change.
  const penBusy = !!active && ["plotting", "homing", "plotting_calibration"].includes(status);
  penUpBtn.disabled = penBusy;
  penDownBtn.disabled = penBusy;
  motorsEnableBtn.disabled = penBusy;
  motorsDisableBtn.disabled = penBusy;

  // Top status pill text
  if (!active) {
    statusEl.textContent = statusLabel("idle");
    statusEl.className = "status idle";
    topMessage.textContent = "";
  } else {
    statusEl.textContent = `${statusLabel(status)}${active.filename ? ` · ${active.filename}` : ""}`;
    statusEl.className = `status ${status}`;
    let msg = "";
    if (active.error) msg = t("msg.error_prefix", { error: jobErrorText(active) });
    else if (status === "awaiting_pen_change") msg = penChangeMessage(active);
    else if (status === "awaiting_optimize") msg = t("msg.awaiting_optimize");
    else if (status === "optimizing") msg = t("msg.optimizing");
    topMessage.textContent = msg;
    topMessage.className = active.error ? "error" : "muted";
  }

  // Shutdown button: disabled while the worker is busy so the Pi can't be
  // powered off mid-plot. Safe to shut down only when idle.
  const busy = !!s.active_id;
  shutdownBtn.disabled = busy;
  shutdownBtn.title = busy
    ? t("a11y.shutdown_busy")
    : t("a11y.shutdown");

  // Sticky progress bar. The denominator is progress_total_seconds (the one a
  // live speed change recalibrates), falling back to the plain estimate for
  // job records written before that field existed.
  const progressTotal = active
    ? (active.progress_total_seconds || active.estimated_total_seconds)
    : 0;
  if (active && active.status === "plotting" && active.plotting_started_at && progressTotal > 0) {
    queueProgress.hidden = false;
    renderProgressTicks(active, cardCtx.get(active.job_id), progressTotal);
    startSharedElapsed(active.plotting_started_at,
                       active.run_elapsed_seconds || 0, progressTotal);
  } else {
    queueProgress.hidden = true;
    stopSharedElapsed();
  }
}

function applyCameraControls() {
  if (!appSettings.camera_enabled) {
    cameraControls.hidden = true;
    return;
  }
  cameraControls.hidden = false;
  const recStatus = serverState.recording_status || "idle";
  cameraStartBtn.hidden = recStatus !== "idle";
  cameraPauseBtn.hidden = recStatus !== "recording";
  cameraResumeBtn.hidden = recStatus !== "paused";
  cameraStopBtn.hidden = recStatus === "idle";
  cameraRecordingIndicator.hidden = recStatus === "idle";
  cameraRecordingIndicator.className = `recording-indicator ${recStatus}`;
  cameraRecordingIndicator.textContent = recStatus === "recording"
    ? t("controls.recording_status_recording")
    : recStatus === "paused" ? t("controls.recording_status_paused") : "";
  cameraPreviewPausedOverlay.hidden = recStatus !== "paused";
}

// ───── Elapsed / progress timer ──────────────────────────────────────────

// `startedAt` marks the span the pen is plotting right now, which restarts at
// every layer and every resume; `bankedSecs` is everything this run already
// spent plotting before that (see run_elapsed_seconds in plot_worker.py).
// Adding them is what makes the bar measure the job instead of the layer.
function startSharedElapsed(startedAt, bankedSecs, estTotal) {
  stopSharedElapsed();
  const fill = queueProgress.querySelector(".progress-fill");
  const timeEl = queueProgress.querySelector(".progress-time");
  const render = () => {
    const secs = Math.max(0, Math.floor(bankedSecs + Date.now() / 1000 - startedAt));
    const pct = estTotal > 0 ? Math.min(100, (secs / estTotal) * 100) : 0;
    fill.style.width = `${pct}%`;
    const remaining = Math.max(0, estTotal - secs);
    timeEl.textContent = t("progress.remaining", { time: formatDuration(Math.round(remaining)) });
  };
  render();
  sharedElapsedTimer = setInterval(render, 1000);
}

// Marks where each layer change falls along the progress bar, at the
// cumulative share of estTotal the layers before it are expected to take
// (see layerEstimateSeconds). Skipped entirely if any selected layer's
// estimate is unavailable, rather than showing marks at the wrong spot.
function renderProgressTicks(job, ctx, estTotal) {
  const ticksEl = queueProgress.querySelector(".progress-ticks");
  const selected = (job.layer_selections || []).filter((s) => s.selected !== false);
  let cumulative = 0;
  const marks = [];
  for (let i = 0; i < selected.length - 1; i++) {
    const secs = layerEstimateSeconds(job, ctx, selected[i].index);
    if (secs == null) { marks.length = 0; break; }
    cumulative += secs;
    marks.push((cumulative / estTotal) * 100);
  }
  ticksEl.innerHTML = marks
    .map((pct) => `<div class="progress-tick" style="left:${pct}%"></div>`)
    .join("");
}

function stopSharedElapsed() {
  if (sharedElapsedTimer) { clearInterval(sharedElapsedTimer); sharedElapsedTimer = null; }
}

function formatDuration(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

// Hours:minutes only, for the compact per-layer estimate in the layer panel
// (see layerEstimateSeconds) — that number is an approximation, so it isn't
// worth the width formatDuration's seconds digit costs there.
function formatHoursMinutes(secs) {
  const totalMin = Math.round(secs / 60);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return `${h}:${String(m).padStart(2, "0")}`;
}

// Approximates one layer's share of the job's single simulated estimate by
// its share of the selected layers' total pen-down distance — cheap (no
// per-layer simulation) and good enough for an hours:minutes readout. Travel
// moves and pen-lift overhead aren't split out, so this is directional, not
// exact.
function layerEstimateSeconds(job, ctx, layerIndex) {
  const lengths = ctx && ctx.layerLengthsMm;
  const total = job.estimated_total_seconds;
  if (!lengths || total == null) return null;
  const selected = job.layer_selections.filter((s) => s.selected !== false).map((s) => s.index);
  if (!selected.includes(layerIndex)) return null;
  const totalLen = selected.reduce((sum, i) => sum + (lengths[i] || 0), 0);
  if (!totalLen) return null;
  return (lengths[layerIndex] || 0) / totalLen * total;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Inline SVG glyphs that approximate the SF Symbols the macOS companion app
// uses (waveform.path / character.text.justify / xmark.triangle.circle.square /
// scope).
const LAYER_TYPE_ICONS = {
  pattern: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 8 Q 3 3 5 8 T 9 8 T 13 8 T 15 8" /></svg>`,
  text: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><line x1="2" y1="4" x2="14" y2="4"/><line x1="2" y1="8" x2="14" y2="8"/><line x1="2" y1="12" x2="11" y2="12"/></svg>`,
  svg: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="3.2"/><rect x="7.5" y="7.5" width="6.5" height="6.5"/><polygon points="3,14 9,14 6,9"/></svg>`,
  calibration: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="4.5"/><line x1="8" y1="1.5" x2="8" y2="14.5"/><line x1="1.5" y1="8" x2="14.5" y2="8"/></svg>`,
  image: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3.5" width="12" height="9" rx="1.5"/><circle cx="5.5" cy="6.5" r="1.1"/><path d="M2.5 11.5 L6 8 L8.5 10.5 L10.5 8.5 L13.5 11.5"/></svg>`,
  map: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 2.5 L2 4 V13.5 L6 12 L10 13.5 L14 12 V2.5 L10 4 Z"/><line x1="6" y1="2.5" x2="6" y2="12"/><line x1="10" y1="4" x2="10" y2="13.5"/></svg>`,
  model: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.6 L14 5 V10 L8 13.4 L2 10 V5 Z"/><path d="M2 5 L8 8.4 L14 5"/><line x1="8" y1="8.4" x2="8" y2="13.4"/><path d="M8 1.6 V6.6 M8 6.6 L2 10 M8 6.6 L14 10"/></svg>`,
};
// Generic glyph for layer types we don't render a dedicated icon for yet
// (e.g. a not-yet-supported "image" layer) — a neutral rounded square.
const LAYER_TYPE_FALLBACK_ICON = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="2.5"/></svg>`;
// colorToHex / isPaintedColor / SWATCH_DRAW_SELECTOR / resolveLayerColor come
// from static/svg-colors.js, shared with static/draw-stream.js — see
// index.html for the script tag.

// Read the page + per-layer pen colors off the live preview SVG. Done once
// per SVG — getComputedStyle resolves CSS classes and inheritance, so the
// preview must already be in the DOM (renderPreview runs before renderLayers).
// The pen color is a layer's stroke color; the page color is the SVG's own
// CSS background. Results are cached on ctx.svg.
function ensureSvgColors(card, ctx) {
  if (!ctx || !ctx.svg || ctx.svg.colorsReady) return;
  const svgRoot = card.querySelector(".paper-content svg");
  if (!svgRoot) return;  // preview not in the DOM yet — retried next render
  const bg = getComputedStyle(svgRoot).backgroundColor;
  ctx.svg.pageColor = isPaintedColor(bg) ? colorToHex(bg) : null;
  // Walk top-level layer groups in the same order as fetchSvgMeta so the
  // running index lines up with ctx.svg.layers[].index.
  const layerColors = {};
  let index = 0;
  for (const g of svgRoot.children) {
    if (g.tagName.toLowerCase() !== "g") continue;
    if (g.getAttribute("inkscape:groupmode") !== "layer") continue;
    const c = resolveLayerColor(g);
    if (c) layerColors[index] = c;
    index++;
  }
  ctx.svg.layerColors = layerColors;
  ctx.svg.colorsReady = true;
}

// A layer color swatch: the type icon — or a plain dot when the layer has no
// type — drawn in the layer's pen color on a page-colored circle, mirroring
// how that pen looks on that paper. `penHex`/`pageHex` are "#rrggbb" or null;
// both fall back gracefully when the SVG carries no usable color.
function layerSwatch(type, penHex, pageHex) {
  const pen = penHex || "#9ca3af";   // neutral gray when no pen color found
  const page = pageHex || "#ffffff";
  const inner = (type && LAYER_TYPE_ICONS[type])
    ? LAYER_TYPE_ICONS[type]
    : type
      ? LAYER_TYPE_FALLBACK_ICON          // known-but-unsupported type
      : `<span class="layer-swatch-dot"></span>`;  // no type at all
  // Pen == page is an invisible plot; a faint halo keeps the icon legible.
  const faint = penHex && pageHex && penHex === pageHex ? " faint" : "";
  // Untranslated types (t() echoes the key back) fall back to the raw name.
  let typeLabel = "";
  if (type) {
    const key = `layer_type.${type}`;
    const tr = t(key);
    typeLabel = tr === key ? type : tr;
  }
  const title = penHex
    ? t("swatch.pen_color", { hex: penHex }) + (typeLabel ? ` · ${typeLabel}` : "")
    : typeLabel;
  return `<span class="layer-swatch${faint}" style="background:${page};color:${pen};"`
    + (title ? ` title="${escapeHtml(title)}"` : "")
    + `>${inner}</span>`;
}

// ───── Settings modal ────────────────────────────────────────────────────

const settingsBtn = $("settings-btn");
const settingsModal = $("settings-modal");
const settingsApiKey = $("settings-api-key");
const settingsApiKeyCopy = $("settings-api-key-copy");
const settingsPauseBetweenLayers = $("settings-pause-between-layers");
const settingsDeleteOnComplete = $("settings-delete-on-complete");
const settingsDisableMotorsOnComplete = $("settings-disable-motors-on-complete");
const settingsSpeedPendown = $("settings-speed-pendown");
const settingsSpeedPenup = $("settings-speed-penup");
const settingsAccel = $("settings-accel");
const settingsPenPosUp = $("settings-pen-pos-up");
const settingsPenPosDown = $("settings-pen-pos-down");
const settingsShortcutX = $("settings-shortcut-x");
const settingsShortcutY = $("settings-shortcut-y");
const settingsShortcutSetOrigin = $("settings-shortcut-set-origin");
const settingsMachineSelect = $("settings-machine-select");
const settingsMachineAdd = $("settings-machine-add");
const settingsMachineDelete = $("settings-machine-delete");
const settingsMachineName = $("settings-machine-name");
const settingsMachineWidth = $("settings-machine-width");
const settingsMachineHeight = $("settings-machine-height");
const settingsMachineAutoRotate = $("settings-machine-auto-rotate");
const settingsMachineSkew = $("settings-machine-skew");
const settingsMachineSkewAxis = $("settings-machine-skew-axis");
const settingsMachineSkewMode = $("settings-machine-skew-mode");
const settingsSkewSide = $("settings-skew-side");
const settingsSkewD1 = $("settings-skew-d1");
const settingsSkewD2 = $("settings-skew-d2");
const settingsSkewApply = $("settings-skew-apply");
const settingsSkewResult = $("settings-skew-result");
const settingsSkewClearance = $("settings-skew-clearance");
const settingsWebhookUrl = $("settings-webhook-url");
const settingsWebhookOnLayerComplete = $("settings-webhook-on-layer-complete");
const settingsWebhookOnJobComplete = $("settings-webhook-on-job-complete");
const settingsWebhookTest = $("settings-webhook-test");
const settingsWebhookMessage = $("settings-webhook-message");
const drawStreamSettingsBtn = $("draw-stream-settings-btn");
const drawStreamSettingsModal = $("draw-stream-settings-modal");
const drawStreamSettingsMessage = $("draw-stream-settings-message");
const settingsDrawStreamUrl = $("settings-draw-stream-url");
const settingsDrawStreamStrokeWidth = $("settings-draw-stream-stroke-width");
const settingsDrawStreamBackground = $("settings-draw-stream-background");
const settingsDrawStreamMaxResolution = $("settings-draw-stream-max-resolution");
const settingsDrawStreamBgThumb = $("settings-draw-stream-bg-thumb");
const settingsDrawStreamBgFile = $("settings-draw-stream-bg-file");
const settingsDrawStreamBgUpload = $("settings-draw-stream-bg-upload");
const settingsDrawStreamBgRemove = $("settings-draw-stream-bg-remove");
const settingsDrawStreamBgMessage = $("settings-draw-stream-bg-message");
const settingsOptimize = $("settings-optimize");
const settingsOptimizeLinemerge = $("settings-optimize-linemerge");
const settingsOptimizeLinesimplify = $("settings-optimize-linesimplify");
const settingsOptimizeLinesort = $("settings-optimize-linesort");
const settingsOptimizeReloop = $("settings-optimize-reloop");
const settingsOptimizeTolerance = $("settings-optimize-tolerance");
const settingsDisplayUnit = $("settings-display-unit");
const settingsLanguage = $("settings-language");
settingsBtn.addEventListener("click", openSettings);
// Closing without saving reverts a live language preview to the saved language.
function closeSettings(revertLang) {
  settingsModal.hidden = true;
  if (revertLang) I18N.revertLanguage();
}
$("settings-cancel").addEventListener("click", () => closeSettings(true));
settingsModal.addEventListener("click", (e) => { if (e.target === settingsModal) closeSettings(true); });
// Live-preview the language the moment it's picked; Save keeps it, Cancel reverts.
settingsLanguage?.addEventListener("change", () => I18N.previewLanguage(settingsLanguage.value));
{
  const clampOnLeaveSettings = (e) => {
    const el = e.target;
    if (el instanceof HTMLInputElement && el.type === "number") {
      if (clampNumberInput(el)) {
        // Settings sliders are wired to react to "input" — fire it so the
        // slider thumb snaps to the corrected value too.
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  };
  settingsModal.addEventListener("focusout", clampOnLeaveSettings);
  settingsModal.addEventListener("change", clampOnLeaveSettings, true);
}
$("settings-save").addEventListener("click", saveSettings);
settingsApiKeyCopy.addEventListener("click", async () => {
  if (!settingsApiKey.value) return;
  try { await navigator.clipboard.writeText(settingsApiKey.value); }
  catch { settingsApiKey.select(); document.execCommand("copy"); }
  settingsApiKeyCopy.textContent = t("common.copied");
  setTimeout(() => { settingsApiKeyCopy.textContent = t("common.copy"); }, 1200);
});

function applyAppSettings(data) {
  const prevUnit = effectiveDisplayUnit();
  appSettings = {
    plotter_model: data.plotter_model ?? appSettings.plotter_model,
    pause_between_layers_default: data.pause_between_layers_default ?? appSettings.pause_between_layers_default,
    delete_on_complete_default: data.delete_on_complete_default ?? appSettings.delete_on_complete_default,
    disable_motors_on_complete_default: data.disable_motors_on_complete_default ?? appSettings.disable_motors_on_complete_default,
    speed_pendown_default: data.speed_pendown_default ?? appSettings.speed_pendown_default,
    speed_penup_default: data.speed_penup_default ?? appSettings.speed_penup_default,
    acceleration_default: data.acceleration_default ?? appSettings.acceleration_default,
    pen_pos_up_default: data.pen_pos_up_default ?? appSettings.pen_pos_up_default,
    pen_pos_down_default: data.pen_pos_down_default ?? appSettings.pen_pos_down_default,
    optimize_svg_default: data.optimize_svg_default ?? appSettings.optimize_svg_default,
    optimize_svg_tolerance_default_mm: data.optimize_svg_tolerance_default_mm ?? appSettings.optimize_svg_tolerance_default_mm,
    optimize_svg_linemerge_default: data.optimize_svg_linemerge_default ?? appSettings.optimize_svg_linemerge_default,
    optimize_svg_linesimplify_default: data.optimize_svg_linesimplify_default ?? appSettings.optimize_svg_linesimplify_default,
    optimize_svg_linesort_default: data.optimize_svg_linesort_default ?? appSettings.optimize_svg_linesort_default,
    optimize_svg_reloop_default: data.optimize_svg_reloop_default ?? appSettings.optimize_svg_reloop_default,
    optimize_expert_1_cmd_default: data.optimize_expert_1_cmd_default ?? appSettings.optimize_expert_1_cmd_default,
    optimize_expert_2_cmd_default: data.optimize_expert_2_cmd_default ?? appSettings.optimize_expert_2_cmd_default,
    optimize_expert_3_cmd_default: data.optimize_expert_3_cmd_default ?? appSettings.optimize_expert_3_cmd_default,
    display_unit: data.display_unit ?? appSettings.display_unit,
    machine_custom_enabled: data.machine_custom_enabled ?? appSettings.machine_custom_enabled,
    machine_width_mm: data.machine_width_mm ?? appSettings.machine_width_mm,
    machine_height_mm: data.machine_height_mm ?? appSettings.machine_height_mm,
    machine_auto_rotate: data.machine_auto_rotate ?? appSettings.machine_auto_rotate,
    machine_effective_width_mm: data.machine_effective_width_mm ?? appSettings.machine_effective_width_mm,
    machine_effective_height_mm: data.machine_effective_height_mm ?? appSettings.machine_effective_height_mm,
    webhook_url: data.webhook_url ?? appSettings.webhook_url,
    webhook_on_layer_complete: data.webhook_on_layer_complete ?? appSettings.webhook_on_layer_complete,
    webhook_on_job_complete: data.webhook_on_job_complete ?? appSettings.webhook_on_job_complete,
    camera_enabled: data.camera_enabled ?? appSettings.camera_enabled,
    camera_resolution_width: data.camera_resolution_width ?? appSettings.camera_resolution_width,
    camera_resolution_height: data.camera_resolution_height ?? appSettings.camera_resolution_height,
    camera_fps: data.camera_fps ?? appSettings.camera_fps,
    camera_bitrate: data.camera_bitrate ?? appSettings.camera_bitrate,
    camera_af_mode: data.camera_af_mode ?? appSettings.camera_af_mode,
    camera_lens_position: data.camera_lens_position ?? appSettings.camera_lens_position,
    camera_af_speed: data.camera_af_speed ?? appSettings.camera_af_speed,
    camera_brightness: data.camera_brightness ?? appSettings.camera_brightness,
    camera_contrast: data.camera_contrast ?? appSettings.camera_contrast,
    camera_saturation: data.camera_saturation ?? appSettings.camera_saturation,
    camera_sharpness: data.camera_sharpness ?? appSettings.camera_sharpness,
    camera_ev: data.camera_ev ?? appSettings.camera_ev,
    camera_exposure_mode: data.camera_exposure_mode ?? appSettings.camera_exposure_mode,
    camera_shutter_us: data.camera_shutter_us ?? appSettings.camera_shutter_us,
    camera_awb_mode: data.camera_awb_mode ?? appSettings.camera_awb_mode,
    camera_gain: data.camera_gain ?? appSettings.camera_gain,
    camera_denoise: data.camera_denoise ?? appSettings.camera_denoise,
    camera_flicker_mode: data.camera_flicker_mode ?? appSettings.camera_flicker_mode,
    camera_hflip: data.camera_hflip ?? appSettings.camera_hflip,
    camera_vflip: data.camera_vflip ?? appSettings.camera_vflip,
    camera_output_folder: data.camera_output_folder ?? appSettings.camera_output_folder,
    camera_rclone_target: data.camera_rclone_target ?? appSettings.camera_rclone_target,
    camera_rclone_delete_local: data.camera_rclone_delete_local ?? appSettings.camera_rclone_delete_local,
    camera_retention_gb: data.camera_retention_gb ?? appSettings.camera_retention_gb,
    camera_recording_mode_default: data.camera_recording_mode_default ?? appSettings.camera_recording_mode_default,
    camera_timelapse_interval_s_default: data.camera_timelapse_interval_s_default ?? appSettings.camera_timelapse_interval_s_default,
    camera_speed_multiplier_default: data.camera_speed_multiplier_default ?? appSettings.camera_speed_multiplier_default,
    record_plot_default: data.record_plot_default ?? appSettings.record_plot_default,
    optical_reg_default: data.optical_reg_default ?? appSettings.optical_reg_default,
    optical_reg_mm_per_px: data.optical_reg_mm_per_px ?? appSettings.optical_reg_mm_per_px,
    optical_reg_cam_rotation_deg: data.optical_reg_cam_rotation_deg ?? appSettings.optical_reg_cam_rotation_deg,
    optical_reg_mark_x_mm: data.optical_reg_mark_x_mm ?? appSettings.optical_reg_mark_x_mm,
    optical_reg_mark_y_mm: data.optical_reg_mark_y_mm ?? appSettings.optical_reg_mark_y_mm,
    optical_reg_mark_size_mm: data.optical_reg_mark_size_mm ?? appSettings.optical_reg_mark_size_mm,
    optical_reg_probe_offset_mm: data.optical_reg_probe_offset_mm ?? appSettings.optical_reg_probe_offset_mm,
    draw_stream_enabled: data.draw_stream_enabled ?? appSettings.draw_stream_enabled,
    draw_stream_stroke_width_px: data.draw_stream_stroke_width_px ?? appSettings.draw_stream_stroke_width_px,
    draw_stream_background: data.draw_stream_background ?? appSettings.draw_stream_background,
    draw_stream_max_resolution_px: data.draw_stream_max_resolution_px ?? appSettings.draw_stream_max_resolution_px,
  };
  if (effectiveDisplayUnit() !== prevUnit) refreshUnitDependentDisplays();
  // applyMachineAutoRotateToCard only locks the orientation *button* visually;
  // without also re-running onPaperChange, a job's stored paper_width_mm/
  // paper_height_mm keeps whatever it was before this settings change, so the
  // UI can show e.g. "Landscape" locked in while the job would actually still
  // plot at its old portrait dimensions. Only do the full resync for jobs
  // still "ready" (editable) — anything else, PATCHing paper dims would
  // re-queue a finished job or fight an active plot, so just update the
  // visual lock there.
  cardEls.forEach((card, id) => {
    const job = serverState.queue.find((j) => j.job_id === id);
    if (job && IDLE_JOB_STATUSES.includes(job.status)) {
      onPaperChange(card);
    } else {
      applyMachineAutoRotateToCard(card);
    }
  });
  cameraSettingsBtn.hidden = !appSettings.camera_enabled;
  applyCameraControls();
  document.querySelectorAll(".camera-job-options").forEach((el) => {
    el.hidden = !appSettings.camera_enabled;
  });
  drawStreamSettingsBtn.hidden = !appSettings.draw_stream_enabled;
}

function refreshUnitDependentDisplays() {
  cardEls.forEach((card, id) => {
    relabelPaperOptions(card.querySelector(".paper-size"));
    const job = serverState.queue.find((j) => j.job_id === id);
    if (job) updateCard(card, job);
  });
}

async function loadAppSettings() {
  try {
    const res = await fetch("/settings");
    if (!res.ok) return;
    applyAppSettings(await res.json());
  } catch (e) {}
}

async function openSettings() {
  try {
    const res = await fetch("/settings");
    const data = await res.json();
    applyAppSettings(data);
    settingsApiKey.value = data.api_key || "";
    settingsPauseBetweenLayers.checked = data.pause_between_layers_default ?? true;
    settingsDeleteOnComplete.checked = data.delete_on_complete_default ?? false;
    settingsDisableMotorsOnComplete.checked = data.disable_motors_on_complete_default ?? false;
    settingsSpeedPendown.value = String(data.speed_pendown_default ?? 25);
    settingsSpeedPenup.value = String(data.speed_penup_default ?? 75);
    settingsAccel.value = String(data.acceleration_default ?? 75);
    settingsPenPosUp.value = String(data.pen_pos_up_default ?? 60);
    settingsPenPosDown.value = String(data.pen_pos_down_default ?? 30);
    settingsOptimize.checked = !!(data.optimize_svg_default ?? false);
    settingsOptimizeLinemerge.checked = data.optimize_svg_linemerge_default !== false;
    settingsOptimizeLinesimplify.checked = data.optimize_svg_linesimplify_default !== false;
    settingsOptimizeLinesort.checked = data.optimize_svg_linesort_default !== false;
    settingsOptimizeReloop.checked = data.optimize_svg_reloop_default !== false;
    settingsOptimizeTolerance.value = (data.optimize_svg_tolerance_default_mm ?? 0.10).toFixed(2);
    settingsDisplayUnit.value = data.display_unit || "auto";
    settingsShortcutX.value = String(data.move_shortcut_x_mm ?? 6);
    settingsShortcutY.value = String(data.move_shortcut_y_mm ?? 6);
    settingsShortcutSetOrigin.checked = !!data.move_shortcut_set_origin;
    if (settingsLanguage) settingsLanguage.value = I18N.getLanguage();
    applySettingsOptimizeEnabledStyle();
    loadMachineDraft(data);
    settingsWebhookUrl.value = data.webhook_url || "";
    settingsWebhookOnLayerComplete.checked = !!data.webhook_on_layer_complete;
    settingsWebhookOnJobComplete.checked = !!data.webhook_on_job_complete;
    settingsWebhookMessage.textContent = "";
    for (const sel of ["#settings-speed-pendown-slider", "#settings-speed-penup-slider",
                        "#settings-accel-slider", "#settings-pen-pos-up-slider",
                        "#settings-pen-pos-down-slider"]) {
      const s = document.querySelector(sel);
      const n = document.querySelector(sel.replace("-slider", ""));
      if (s && n) { s.value = n.value; updateSliderProgress(s); }
    }
  } catch (e) {}
  settingsModal.hidden = false;
}

async function saveSettings() {
  try {
    const tol = parseFloat(settingsOptimizeTolerance.value);
    captureMachineFields();
    const body = {
      machines: machineDraft,
      active_machine_id: machineDraftActiveId,
      pause_between_layers_default: settingsPauseBetweenLayers.checked,
      delete_on_complete_default: settingsDeleteOnComplete.checked,
      disable_motors_on_complete_default: settingsDisableMotorsOnComplete.checked,
      speed_pendown_default: parseInt(settingsSpeedPendown.value),
      speed_penup_default: parseInt(settingsSpeedPenup.value),
      acceleration_default: parseInt(settingsAccel.value),
      pen_pos_up_default: parseInt(settingsPenPosUp.value),
      pen_pos_down_default: parseInt(settingsPenPosDown.value),
      optimize_svg_default: settingsOptimize.checked,
      optimize_svg_tolerance_default_mm: isFinite(tol) && tol > 0 ? tol : 0.10,
      optimize_svg_linemerge_default: settingsOptimizeLinemerge.checked,
      optimize_svg_linesimplify_default: settingsOptimizeLinesimplify.checked,
      optimize_svg_linesort_default: settingsOptimizeLinesort.checked,
      optimize_svg_reloop_default: settingsOptimizeReloop.checked,
      display_unit: settingsDisplayUnit.value,
      move_shortcut_x_mm: parseFloat(settingsShortcutX.value) || 0,
      move_shortcut_y_mm: parseFloat(settingsShortcutY.value) || 0,
      move_shortcut_set_origin: settingsShortcutSetOrigin.checked,
      webhook_url: settingsWebhookUrl.value.trim(),
      webhook_on_layer_complete: settingsWebhookOnLayerComplete.checked,
      webhook_on_job_complete: settingsWebhookOnJobComplete.checked,
    };
    const res = await fetch("/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    applyAppSettings(await res.json());
    settingsModal.hidden = true;
    // Language was live-previewed on selection; persist the current choice.
    I18N.commitLanguage();
  } catch (e) {
    $("settings-message").textContent = t("settings.save_failed", { message: e.message });
    $("settings-message").className = "error";
  }
}

// Wire the settings-modal sliders (they're not inside a card, so createCardForJob doesn't touch them)
for (const base of ["settings-speed-pendown", "settings-speed-penup", "settings-accel",
                     "settings-pen-pos-up", "settings-pen-pos-down"]) {
  const number = $(base);
  const slider = $(base + "-slider");
  if (!number || !slider) continue;
  slider.addEventListener("input", () => {
    if (number.value !== slider.value) number.value = slider.value;
    updateSliderProgress(slider);
  });
  number.addEventListener("input", () => {
    if (slider.value !== number.value) slider.value = number.value;
    updateSliderProgress(slider);
  });
  updateSliderProgress(slider);
}

function resetSettingsJobOptions() {
  settingsPauseBetweenLayers.checked = true;
  settingsDeleteOnComplete.checked = false;
  settingsDisableMotorsOnComplete.checked = false;
}

function resetSettingsPenHeight() {
  const pairs = [["settings-pen-pos-up", 60], ["settings-pen-pos-down", 30]];
  for (const [id, val] of pairs) {
    const el = $(id);
    if (el) { el.value = val; el.dispatchEvent(new Event("input", { bubbles: true })); }
  }
}

settingsMachineAutoRotate.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => setSegmentedValue(settingsMachineAutoRotate, btn.dataset.val));
});

settingsMachineSkewAxis.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => {
    setSegmentedValue(settingsMachineSkewAxis, btn.dataset.val);
    renderSkewClearance();
  });
});

settingsMachineSkewMode.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => {
    setSegmentedValue(settingsMachineSkewMode, btn.dataset.val);
    renderSkewClearance();
  });
});

// ───── Machine profiles ──────────────────────────────────────────────────
//
// The modal edits a working copy of the machine list, not the live settings:
// switching between machines has to carry unsaved edits with it, and Cancel
// has to leave the real list alone. Nothing here reaches the server until
// Save — which is also why deleting a machine asks for no confirmation.
let machineDraft = [];
let machineDraftActiveId = "";
// Mirrors config.MACHINE_SKEW_DEG_MAX; the server clamps to the same bound.
const SKEW_DEG_MAX = 5;

function machineDraftEntry(id) {
  return machineDraft.find((m) => m.id === id) || null;
}

function renderMachineSelect() {
  settingsMachineSelect.innerHTML = "";
  for (const machine of machineDraft) {
    const opt = document.createElement("option");
    opt.value = machine.id;
    opt.textContent = machine.name || t("settings.machine.unnamed");
    settingsMachineSelect.appendChild(opt);
  }
  settingsMachineSelect.value = machineDraftActiveId;
  // The bed is the one thing every bounds check needs an answer from, so the
  // list can never be emptied.
  settingsMachineDelete.disabled = machineDraft.length < 2;
}

function loadMachineFields() {
  const machine = machineDraftEntry(machineDraftActiveId);
  if (!machine) return;
  settingsMachineName.value = machine.name;
  settingsMachineWidth.value = machine.width_mm;
  settingsMachineHeight.value = machine.height_mm;
  setSegmentedValue(settingsMachineAutoRotate, machine.auto_rotate || "off");
  settingsMachineSkew.value = machine.skew_deg ?? 0;
  setSegmentedValue(settingsMachineSkewAxis, machine.skew_true_axis || "x");
  setSegmentedValue(settingsMachineSkewMode, machine.skew_mode || "clip");
  renderSkewClearance();
}

// Fold whatever is in the fields back into the draft. Runs before anything
// that repoints the fields at a different machine, and again on save, so an
// edit is never lost to a switch.
function captureMachineFields() {
  const machine = machineDraftEntry(machineDraftActiveId);
  if (!machine) return;
  machine.name = settingsMachineName.value.trim() || machine.name;
  machine.width_mm = parseFloat(settingsMachineWidth.value) || machine.width_mm;
  machine.height_mm = parseFloat(settingsMachineHeight.value) || machine.height_mm;
  machine.auto_rotate = getSegmentedValue(settingsMachineAutoRotate, "off");
  // Not the `|| fallback` the dimensions use: 0 is both falsy and the value
  // this field holds on every machine that isn't skewed, so that pattern
  // would make "back to no correction" the one edit you can't save.
  const skew = parseFloat(settingsMachineSkew.value);
  machine.skew_deg = Number.isFinite(skew)
    ? Math.max(-SKEW_DEG_MAX, Math.min(SKEW_DEG_MAX, skew))
    : 0;
  machine.skew_true_axis = getSegmentedValue(settingsMachineSkewAxis, "x");
  machine.skew_mode = getSegmentedValue(settingsMachineSkewMode, "clip");
}

function loadMachineDraft(data) {
  machineDraft = (data.machines || []).map((m) => ({ ...m }));
  machineDraftActiveId = data.active_machine_id || "";
  if (!machineDraftEntry(machineDraftActiveId) && machineDraft.length) {
    machineDraftActiveId = machineDraft[0].id;
  }
  renderMachineSelect();
  loadMachineFields();
}

settingsMachineSelect.addEventListener("change", () => {
  captureMachineFields();
  machineDraftActiveId = settingsMachineSelect.value;
  loadMachineFields();
});

// Keep the dropdown label in step with the name field, so a rename is
// visible in the list it's meant to make searchable without a save first.
settingsMachineName.addEventListener("input", () => {
  const machine = machineDraftEntry(machineDraftActiveId);
  if (!machine) return;
  machine.name = settingsMachineName.value;
  const opt = [...settingsMachineSelect.options].find((o) => o.value === machine.id);
  if (opt) opt.textContent = machine.name || t("settings.machine.unnamed");
});

settingsMachineAdd.addEventListener("click", () => {
  captureMachineFields();
  // Start from the machine on screen: a second profile is usually a variant
  // of the one you're looking at, not of an arbitrary preset.
  const base = machineDraftEntry(machineDraftActiveId);
  const machine = {
    id: `m${Date.now().toString(36)}`,
    name: t("settings.machine.new_name"),
    width_mm: base ? base.width_mm : 430,
    height_mm: base ? base.height_mm : 297,
    auto_rotate: "off",
    // Skew belongs to the physical machine, not to the profile it was copied
    // from, so a new one starts unmeasured even when cloned from a
    // calibrated entry — inheriting it would silently misdescribe a
    // different plotter.
    skew_deg: 0,
    skew_true_axis: "x",
    skew_mode: "clip",
  };
  machineDraft.push(machine);
  machineDraftActiveId = machine.id;
  renderMachineSelect();
  loadMachineFields();
  settingsMachineName.focus();
  settingsMachineName.select();
});

// What correcting `skewDeg` costs, and where.
//
// Not accuracy: the correction is anchored at the page's origin corner, so
// artwork is drawn exactly where it was placed, at exactly its size (see
// app/axis_skew.py). What it costs is travel — a design `span` mm along the
// driving axis is commanded `span * tan(skew)` further across than it draws,
// and the driver clips commands at the page edge. `shrinkPct` mirrors
// axis_skew.absorb_scale for ink filling the whole bed; keeping the result
// centred costs twice the shrink that shoving it against one edge would, and
// buys an equal margin at both edges for it.
function skewClearanceMm(skewDeg, trueAxis, bedWidthMm, bedHeightMm) {
  const tan = Math.abs(Math.tan(skewDeg * Math.PI / 180));
  // The overrun runs along the axis that is *not* the true one, and grows
  // with travel along the true one — so the span and the dimension absorb
  // scales against swap together with trueAxis.
  const acrossY = trueAxis === "x";
  const span = acrossY ? bedHeightMm : bedWidthMm;
  const across = acrossY ? bedWidthMm : bedHeightMm;
  const growth = tan * span;
  return {
    growth,
    span,
    travelAxis: acrossY ? "Y" : "X",
    // Positive skew shears toward negative coordinates (skew_matrix's -tan
    // term), so commands overrun the left/top edge; negative, the other one.
    side: acrossY
      ? (skewDeg > 0 ? "left" : "right")
      : (skewDeg > 0 ? "top" : "bottom"),
    shrinkPct: across + growth > 0 ? (200 * growth) / (across + growth) : 0,
    marginEachSide: across + growth > 0 ? (across * growth) / (across + growth) : 0,
  };
}

// Restate the angle as the one number the user can act on: the margin the
// artwork has to leave at one page edge. Quoted for the two portrait paper
// sizes rather than for the bed, since that is the paper in front of them.
function renderSkewClearance() {
  const deg = parseFloat(settingsMachineSkew.value);
  if (!Number.isFinite(deg) || deg === 0) {
    settingsSkewClearance.textContent = t("settings.machine.skew_clearance_none");
    return;
  }
  const axis = getSegmentedValue(settingsMachineSkewAxis, "x");
  const a4 = skewClearanceMm(deg, axis, 210, 297);
  const a3 = skewClearanceMm(deg, axis, 297, 420);
  // Four literal lookups rather than one key concatenated from .side:
  // tests/test_i18n.py scrapes literal t() keys out of this file to prove
  // every string exists in every catalog, and a built-up key hides from it.
  const edges = {
    left: t("settings.machine.skew_edge_left"),
    right: t("settings.machine.skew_edge_right"),
    top: t("settings.machine.skew_edge_top"),
    bottom: t("settings.machine.skew_edge_bottom"),
  };
  let text = t("settings.machine.skew_clearance", {
    a4: a4.growth.toFixed(1),
    a3: a3.growth.toFixed(1),
    side: edges[a4.side],
  });
  if (getSegmentedValue(settingsMachineSkewMode, "clip") === "absorb") {
    // Shrink to fit's worst case is a bed-filling drawing, so that half stays
    // measured against the machine.
    const bed = skewClearanceMm(
      deg,
      axis,
      parseFloat(settingsMachineWidth.value) || 0,
      parseFloat(settingsMachineHeight.value) || 0,
    );
    text += " " + t("settings.machine.skew_clearance_absorb", {
      pct: bed.shrinkPct.toFixed(2),
      margin: bed.marginEachSide.toFixed(1),
    });
  }
  settingsSkewClearance.textContent = text;
}

settingsMachineSkew.addEventListener("input", renderSkewClearance);
settingsMachineWidth.addEventListener("input", renderSkewClearance);
settingsMachineHeight.addEventListener("input", renderSkewClearance);

// Turn two measured diagonals into the skew angle. A square commanded with
// side L comes off a skewed machine as a parallelogram whose diagonals differ
// by exactly d1² - d2² = 4L²·tan(skew), so the angle falls straight out of
// what a ruler can tell you. d1 is the top-left/bottom-right diagonal: when it
// is the longer one the machine drifts +x as it travels down the page, which
// is the positive direction here.
function skewAngleDeg(sideMm, d1Mm, d2Mm) {
  return Math.atan((d1Mm * d1Mm - d2Mm * d2Mm) / (4 * sideMm * sideMm)) * 180 / Math.PI;
}

settingsSkewApply.addEventListener("click", () => {
  const side = parseFloat(settingsSkewSide.value);
  const d1 = parseFloat(settingsSkewD1.value);
  const d2 = parseFloat(settingsSkewD2.value);
  if (!(side > 0) || !(d1 > 0) || !(d2 > 0)) {
    settingsSkewResult.textContent = t("settings.machine.skew_calc_incomplete");
    settingsSkewResult.className = "error";
    return;
  }
  const deg = skewAngleDeg(side, d1, d2);
  if (Math.abs(deg) > SKEW_DEG_MAX) {
    settingsSkewResult.textContent = t("settings.machine.skew_calc_too_large");
    settingsSkewResult.className = "error";
    return;
  }
  settingsMachineSkew.value = deg.toFixed(3);
  renderSkewClearance();
  settingsSkewResult.textContent = t("settings.machine.skew_calc_result", { deg: deg.toFixed(3) });
  settingsSkewResult.className = "muted";
});

settingsMachineDelete.addEventListener("click", () => {
  if (machineDraft.length < 2) return;
  const i = machineDraft.findIndex((m) => m.id === machineDraftActiveId);
  if (i < 0) return;
  machineDraft.splice(i, 1);
  machineDraftActiveId = machineDraft[Math.min(i, machineDraft.length - 1)].id;
  renderMachineSelect();
  loadMachineFields();
});

settingsWebhookTest.addEventListener("click", async () => {
  settingsWebhookMessage.textContent = "";
  settingsWebhookMessage.className = "muted";
  if (!settingsWebhookUrl.value.trim()) {
    settingsWebhookMessage.textContent = t("settings.notifications.url_required");
    settingsWebhookMessage.className = "error";
    return;
  }
  try {
    // Persist just the URL first (a partial PATCH, so it doesn't touch or
    // close anything else in the modal) so the server has what's typed.
    const patchRes = await fetch("/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ webhook_url: settingsWebhookUrl.value.trim() }),
    });
    if (!patchRes.ok) throw new Error(await readErr(patchRes));
    const res = await fetch("/webhook/test", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    settingsWebhookMessage.textContent = t("settings.notifications.test_sent");
  } catch (e) {
    settingsWebhookMessage.textContent = t("error.request_failed", { message: e.message });
    settingsWebhookMessage.className = "error";
  }
});

// ───── Camera settings modal ─────────────────────────────────────────────

cameraSettingsBtn.addEventListener("click", openCameraSettings);

function closeCameraSettings() {
  cameraSettingsModal.hidden = true;
  cameraPreviewFrame.src = "";
  stopRecordingsPolling();
  // The open player goes with the modal's markup; clearing this is what lets
  // polling start again the next time the modal is opened.
  recordingPreviewOpen = null;
}
$("camera-settings-cancel").addEventListener("click", closeCameraSettings);
cameraSettingsModal.addEventListener("click", (e) => {
  if (e.target === cameraSettingsModal) closeCameraSettings();
});

// Collapsible sections (Resolution & Bitrate, Recording, Livestream Address)
// need the same click-to-expand wiring settingsModal's sections get — without
// it they're stuck collapsed and their fields are unreachable.
cameraSettingsModal.querySelectorAll(".card-section-head").forEach((head) => {
  head.addEventListener("click", () => {
    head.parentElement.classList.toggle("collapsed");
    syncSectionCaret(head.parentElement);
  });
  syncSectionCaret(head.parentElement);
});

async function openCameraSettings() {
  cameraSettingsMessage.textContent = "";
  try {
    const res = await fetch("/settings");
    const data = await res.json();
    applyAppSettings(data);
    cameraResolutionWidth.value = appSettings.camera_resolution_width;
    cameraResolutionHeight.value = appSettings.camera_resolution_height;
    cameraFps.value = appSettings.camera_fps;
    cameraBitrate.value = appSettings.camera_bitrate;
    setSegmentedValue(cameraAfMode, appSettings.camera_af_mode);
    cameraLensPositionField.hidden = appSettings.camera_af_mode !== "manual";
    setSliderNumber("camera-lens-position", appSettings.camera_lens_position);
    setSegmentedValue(cameraAfSpeed, appSettings.camera_af_speed || "normal");
    setSliderNumber("camera-brightness", appSettings.camera_brightness ?? 0);
    setSliderNumber("camera-contrast", appSettings.camera_contrast ?? 1);
    setSliderNumber("camera-saturation", appSettings.camera_saturation ?? 1);
    setSliderNumber("camera-sharpness", appSettings.camera_sharpness ?? 1);
    setSliderNumber("camera-ev", appSettings.camera_ev ?? 0);
    cameraExposureMode.value = appSettings.camera_exposure_mode || "normal";
    cameraShutterUs.value = appSettings.camera_shutter_us ?? 0;
    setSliderNumber("camera-gain", appSettings.camera_gain ?? 0);
    cameraAwbMode.value = appSettings.camera_awb_mode || "auto";
    cameraDenoise.value = appSettings.camera_denoise || "off";
    cameraFlickerMode.value = appSettings.camera_flicker_mode || "off";
    cameraHflip.checked = !!appSettings.camera_hflip;
    cameraVflip.checked = !!appSettings.camera_vflip;
    cameraRecordPlotDefault.checked = !!appSettings.record_plot_default;
    cameraRecordingMode.value = appSettings.camera_recording_mode_default;
    cameraTimelapseInterval.value = appSettings.camera_timelapse_interval_s_default;
    cameraSpeedMultiplier.value = appSettings.camera_speed_multiplier_default;
    cameraOutputFolder.value = appSettings.camera_output_folder;
    cameraRcloneTarget.value = appSettings.camera_rclone_target || "";
    cameraRcloneDeleteLocal.checked = !!appSettings.camera_rclone_delete_local;
    cameraRetentionGb.value = appSettings.camera_retention_gb ?? 10;
    opticalRegMarkX.value = appSettings.optical_reg_mark_x_mm ?? 10;
    opticalRegMarkY.value = appSettings.optical_reg_mark_y_mm ?? 10;
    opticalRegMarkSize.value = appSettings.optical_reg_mark_size_mm ?? 3;
    opticalRegProbeOffset.value = appSettings.optical_reg_probe_offset_mm ?? 2;
    renderOpticalRegCalibrationStatus();

    const statusRes = await fetch("/camera/status");
    if (statusRes.ok) {
      const status = await statusRes.json();
      cameraRtspUrl.value = status.rtsp_url;
      cameraHlsUrl.value = status.hls_url;
      // MediaMTX's own WHEP viewer page defaults to native <video controls>,
      // whose hover-triggered bar darkens the picture right where you're
      // trying to judge brightness — turn it off.
      cameraPreviewFrame.src = status.webrtc_view_url + "?controls=false";
    }
  } catch (e) {}
  startRecordingsPolling();
  cameraSettingsModal.hidden = false;
}

async function saveCameraSettings() {
  try {
    const body = {
      camera_resolution_width: parseInt(cameraResolutionWidth.value) || 1920,
      camera_resolution_height: parseInt(cameraResolutionHeight.value) || 1080,
      camera_fps: parseInt(cameraFps.value) || 30,
      camera_bitrate: parseInt(cameraBitrate.value) || 5000000,
      camera_af_mode: getSegmentedValue(cameraAfMode, "continuous"),
      camera_lens_position: parseFloat(cameraLensPosition.value) || 0,
      camera_af_speed: getSegmentedValue(cameraAfSpeed, "normal"),
      camera_brightness: numOr(cameraBrightness.value, 0),
      camera_contrast: numOr(cameraContrast.value, 1),
      camera_saturation: numOr(cameraSaturation.value, 1),
      camera_sharpness: numOr(cameraSharpness.value, 1),
      camera_ev: numOr(cameraEv.value, 0),
      camera_exposure_mode: cameraExposureMode.value,
      camera_shutter_us: parseInt(cameraShutterUs.value) || 0,
      camera_gain: numOr(cameraGain.value, 0),
      camera_awb_mode: cameraAwbMode.value,
      camera_denoise: cameraDenoise.value,
      camera_flicker_mode: cameraFlickerMode.value,
      camera_hflip: cameraHflip.checked,
      camera_vflip: cameraVflip.checked,
      record_plot_default: cameraRecordPlotDefault.checked,
      camera_recording_mode_default: cameraRecordingMode.value,
      camera_timelapse_interval_s_default: parseFloat(cameraTimelapseInterval.value) || 5,
      camera_speed_multiplier_default: parseFloat(cameraSpeedMultiplier.value) || 4,
      camera_output_folder: cameraOutputFolder.value.trim() || "recordings",
      camera_rclone_target: cameraRcloneTarget.value.trim(),
      camera_rclone_delete_local: cameraRcloneDeleteLocal.checked,
      camera_retention_gb: Math.max(0, numOr(cameraRetentionGb.value, 10)),
      optical_reg_mark_x_mm: numOr(opticalRegMarkX.value, 10),
      optical_reg_mark_y_mm: numOr(opticalRegMarkY.value, 10),
      optical_reg_mark_size_mm: numOr(opticalRegMarkSize.value, 3),
      optical_reg_probe_offset_mm: numOr(opticalRegProbeOffset.value, 2),
    };
    const res = await fetch("/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    applyAppSettings(await res.json());
    closeCameraSettings();
  } catch (e) {
    cameraSettingsMessage.textContent = t("settings.save_failed", { message: e.message });
    cameraSettingsMessage.className = "error";
  }
}
$("camera-settings-save").addEventListener("click", saveCameraSettings);

// ───── Recordings on the Pi ──────────────────────────────────────────────
//
// The local recordings folder doubles as the upload queue's work list (see
// app/upload_queue.py), so this panel is also the answer to "did my plots
// make it to the cloud": with "delete local after upload" on, a row that is
// still here is a row whose upload has not landed yet, and it says why.
//
// Polled only while the camera settings modal is open — the healthy steady
// state is an empty list, which is not worth a request every two seconds from
// a tab nobody is looking at.

let recordingsTimer = null;
// Filename whose inline player is open. Polling stops while one is, because a
// re-render replaces the <video> and would restart playback from zero.
let recordingPreviewOpen = null;

function startRecordingsPolling() {
  stopRecordingsPolling();
  loadRecordings();
  recordingsTimer = setInterval(loadRecordings, 2000);
}

function stopRecordingsPolling() {
  if (recordingsTimer !== null) clearInterval(recordingsTimer);
  recordingsTimer = null;
}

async function loadRecordings() {
  if (recordingPreviewOpen) return;
  try {
    const res = await fetch("/camera/recordings");
    if (!res.ok) return;
    renderRecordings(await res.json());
  } catch (e) {
    console.warn("recordings fetch failed", e);
  }
}

function recordingStatusText(r) {
  switch (r.upload_status) {
    case "queued": return t("camera.recording.files_status_queued");
    case "uploading": return t("camera.recording.files_status_uploading", {
      percent: r.percent,
      done: formatBytes(r.uploaded_bytes),
      total: formatBytes(r.size_bytes),
    });
    case "uploaded": return t("camera.recording.files_status_uploaded");
    case "failed": return t("camera.recording.files_status_failed", {
      retry: formatDuration(r.retry_in_s),
      error: r.error || "",
    });
    case "local_only": return t("camera.recording.files_status_local_only");
    default: return t("camera.recording.files_status_idle");
  }
}

function renderFinalizeFailures(rows) {
  // A recording ffmpeg could not assemble is not in the list below — there is
  // no video. Its raw footage is still on the Pi, and this is the only place
  // that says so, so it has to name the folder it was kept in.
  cameraFinalizeFailures.innerHTML = "";
  for (const f of rows || []) {
    const row = document.createElement("div");
    row.className = "recording-row";
    row.innerHTML = `
      <div class="recording-line">
        <span class="recording-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
        <span class="recording-meta">${escapeHtml(formatBytes(f.size_bytes))}</span>
      </div>
      <div class="recording-line">
        <span class="recording-status error">${escapeHtml(t("camera.recording.finalize_failed", {
          error: f.error,
          path: f.path,
        }))}</span>
      </div>`;
    cameraFinalizeFailures.appendChild(row);
  }
}

function renderRecordings(data) {
  if (!data.rclone_target) {
    cameraRecordingsNote.textContent = t("camera.recording.files_no_target");
  } else if (!data.rclone_installed) {
    cameraRecordingsNote.textContent = t("camera.recording.files_no_rclone");
  } else {
    cameraRecordingsNote.textContent = data.delete_local
      ? t("camera.recording.files_delete_note")
      : t("camera.recording.files_keep_note");
  }
  if (data.free_bytes != null) {
    cameraRecordingsNote.textContent += " " + t("camera.recording.files_disk_free", {
      free: formatBytes(data.free_bytes),
    });
  }
  renderFinalizeFailures(data.failed_finalizes);

  cameraRecordingsList.innerHTML = "";
  const rows = data.recordings || [];
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "recordings-empty";
    empty.textContent = t("camera.recording.files_empty");
    cameraRecordingsList.appendChild(empty);
    return;
  }
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "recording-row";
    row.dataset.name = r.name;
    const failed = r.upload_status === "failed";
    const inFlight = r.upload_status === "uploading" || r.upload_status === "uploaded";
    const canUploadNow = data.rclone_target && !inFlight;
    const when = new Date(r.modified * 1000).toLocaleString();
    row.innerHTML = `
      <div class="recording-line">
        <span class="recording-name" title="${escapeHtml(r.name)}">${escapeHtml(r.name)}</span>
        <span class="recording-meta">${escapeHtml(formatBytes(r.size_bytes))} · ${escapeHtml(when)}</span>
      </div>
      ${inFlight ? `<div class="progress-bar"><div class="progress-fill" style="width:${r.percent}%"></div></div>` : ""}
      <div class="recording-line">
        <span class="recording-status${failed ? " error" : ""}">${escapeHtml(recordingStatusText(r))}</span>
        <span class="recording-actions">
          <button type="button" class="neutral recording-preview">${escapeHtml(t("camera.recording.files_preview"))}</button>
          ${canUploadNow ? `<button type="button" class="neutral recording-upload">${escapeHtml(t("camera.recording.files_upload_now"))}</button>` : ""}
          <button type="button" class="danger recording-delete">${escapeHtml(t("camera.recording.files_delete"))}</button>
        </span>
      </div>`;
    cameraRecordingsList.appendChild(row);
  }
}

// Delegated once, so the two-second re-render never stacks duplicate listeners.
cameraRecordingsList.addEventListener("click", async (ev) => {
  const row = ev.target.closest(".recording-row");
  if (!row) return;
  const name = row.dataset.name;

  if (ev.target.closest(".recording-preview")) {
    const open = row.querySelector(".recording-video");
    if (open) {
      open.remove();
      recordingPreviewOpen = null;
      startRecordingsPolling();
      return;
    }
    const video = document.createElement("video");
    video.className = "recording-video";
    video.controls = true;
    video.preload = "metadata";
    video.src = `/camera/recordings/${encodeURIComponent(name)}`;
    row.appendChild(video);
    recordingPreviewOpen = name;
    stopRecordingsPolling();
    return;
  }

  if (ev.target.closest(".recording-upload")) {
    await fetch(`/camera/recordings/${encodeURIComponent(name)}/upload`, { method: "POST" });
    loadRecordings();
    return;
  }

  if (ev.target.closest(".recording-delete")) {
    if (!confirm(t("camera.recording.files_confirm_delete", { name }))) return;
    if (recordingPreviewOpen === name) {
      recordingPreviewOpen = null;
      startRecordingsPolling();
    }
    await fetch(`/camera/recordings/${encodeURIComponent(name)}`, { method: "DELETE" });
    loadRecordings();
  }
});

cameraRecordingsRefresh.addEventListener("click", () => {
  // An explicit refresh rebuilds every row and so tears down an open player.
  // Forget it rather than leaving polling wedged off against a <video> that
  // no longer exists.
  recordingPreviewOpen = null;
  startRecordingsPolling();
});

function renderOpticalRegCalibrationStatus() {
  opticalRegCalibrateStatus.className = "muted";
  const mmpp = appSettings.optical_reg_mm_per_px || 0;
  if (mmpp > 0) {
    const w = appSettings.camera_resolution_width || 1920;
    const h = appSettings.camera_resolution_height || 1080;
    opticalRegCalibrateStatus.textContent = t("camera.optical.calibrated", {
      mm_per_px: mmpp.toFixed(4),
      rot: (appSettings.optical_reg_cam_rotation_deg || 0).toFixed(1),
      fov: (mmpp * Math.min(w, h)).toFixed(0),
    });
  } else {
    opticalRegCalibrateStatus.textContent = t("camera.optical.uncalibrated");
  }
}

opticalRegCalibrateBtn.addEventListener("click", async () => {
  opticalRegCalibrateBtn.disabled = true;
  opticalRegCalibrateStatus.textContent = t("camera.optical.calibrating");
  try {
    const res = await fetch("/optical-reg/calibrate", { method: "POST" });
    if (!res.ok) throw new Error((await readErrDetail(res)).text);
    const r = await res.json();
    appSettings.optical_reg_mm_per_px = r.mm_per_px;
    appSettings.optical_reg_cam_rotation_deg = r.cam_rotation_deg;
    renderOpticalRegCalibrationStatus();
  } catch (e) {
    opticalRegCalibrateStatus.textContent = t("settings.save_failed", { message: e.message });
    opticalRegCalibrateStatus.className = "error";
  } finally {
    opticalRegCalibrateBtn.disabled = false;
  }
});

// AF mode + live focus: PATCHes /camera/focus immediately (debounced for the
// slider) so the embedded preview reflects the change while framing a shot —
// this is the "live adjust focus" the camera settings modal exists for.
let focusDebounceTimer = null;
function applyLiveFocus() {
  const afMode = getSegmentedValue(cameraAfMode, "continuous");
  const lensPosition = parseFloat(cameraLensPosition.value) || 0;
  clearTimeout(focusDebounceTimer);
  focusDebounceTimer = setTimeout(async () => {
    try {
      const res = await fetch("/camera/focus", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ af_mode: afMode, lens_position: lensPosition }),
      });
      if (!res.ok) throw new Error(await readErr(res));
    } catch (e) {
      cameraSettingsMessage.textContent = t("error.request_failed", { message: e.message });
      cameraSettingsMessage.className = "error";
    }
  }, 200);
}
cameraAfMode.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => {
    setSegmentedValue(cameraAfMode, btn.dataset.val);
    cameraLensPositionField.hidden = btn.dataset.val !== "manual";
    applyLiveFocus();
  });
});
{
  const number = cameraLensPosition;
  const slider = $("camera-lens-position-slider");
  slider.addEventListener("input", () => {
    number.value = slider.value;
    updateSliderProgress(slider);
    applyLiveFocus();
  });
  number.addEventListener("input", () => {
    slider.value = number.value;
    updateSliderProgress(slider);
    applyLiveFocus();
  });
  updateSliderProgress(slider);
}

cameraAfSpeed.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => setSegmentedValue(cameraAfSpeed, btn.dataset.val));
});

// Picture-tuning fields: bidirectional slider/number sync, pushed live to the
// running camera the same way the focus slider is, so the preview reflects
// each change instead of only updating after Save. Each push restarts
// MediaMTX's camera pipeline (a brief stream dropout/reconnect), so the two
// delays below matter: sliders only push once per drag release rather than
// per drag tick, and typed number fields wait out a normal inter-keystroke
// pause instead of pushing after every digit.
const PICTURE_COMMIT_DEBOUNCE_MS = 150; // slider release / select / checkbox
const PICTURE_TYPE_DEBOUNCE_MS = 700;   // free-typed number field
let pictureDebounceTimer = null;
function applyLivePicture(delayMs = PICTURE_TYPE_DEBOUNCE_MS) {
  clearTimeout(pictureDebounceTimer);
  pictureDebounceTimer = setTimeout(async () => {
    try {
      const res = await fetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          camera_brightness: numOr(cameraBrightness.value, 0),
          camera_contrast: numOr(cameraContrast.value, 1),
          camera_saturation: numOr(cameraSaturation.value, 1),
          camera_sharpness: numOr(cameraSharpness.value, 1),
          camera_ev: numOr(cameraEv.value, 0),
          camera_exposure_mode: cameraExposureMode.value,
          camera_shutter_us: parseInt(cameraShutterUs.value) || 0,
          camera_gain: numOr(cameraGain.value, 0),
          camera_awb_mode: cameraAwbMode.value,
          camera_denoise: cameraDenoise.value,
          camera_flicker_mode: cameraFlickerMode.value,
          camera_hflip: cameraHflip.checked,
          camera_vflip: cameraVflip.checked,
        }),
      });
      if (!res.ok) throw new Error(await readErr(res));
      applyAppSettings(await res.json());
    } catch (e) {
      cameraSettingsMessage.textContent = t("error.request_failed", { message: e.message });
      cameraSettingsMessage.className = "error";
    }
  }, delayMs);
}
for (const baseId of ["camera-brightness", "camera-contrast", "camera-saturation",
                      "camera-sharpness", "camera-ev", "camera-gain"]) {
  const number = $(baseId);
  const slider = $(`${baseId}-slider`);
  slider.addEventListener("input", () => {
    number.value = slider.value;
    updateSliderProgress(slider);
  });
  slider.addEventListener("change", () => applyLivePicture(PICTURE_COMMIT_DEBOUNCE_MS));
  number.addEventListener("input", () => {
    slider.value = number.value;
    updateSliderProgress(slider);
    applyLivePicture(PICTURE_TYPE_DEBOUNCE_MS);
  });
}
cameraExposureMode.addEventListener("change", () => applyLivePicture(PICTURE_COMMIT_DEBOUNCE_MS));
cameraShutterUs.addEventListener("input", () => applyLivePicture(PICTURE_TYPE_DEBOUNCE_MS));
cameraAwbMode.addEventListener("change", () => applyLivePicture(PICTURE_COMMIT_DEBOUNCE_MS));
cameraDenoise.addEventListener("change", () => applyLivePicture(PICTURE_COMMIT_DEBOUNCE_MS));
cameraFlickerMode.addEventListener("change", () => applyLivePicture(PICTURE_COMMIT_DEBOUNCE_MS));
cameraHflip.addEventListener("change", () => applyLivePicture(PICTURE_COMMIT_DEBOUNCE_MS));
cameraVflip.addEventListener("change", () => applyLivePicture(PICTURE_COMMIT_DEBOUNCE_MS));

// ───── Draw-stream settings modal ────────────────────────────────────────

drawStreamSettingsBtn.addEventListener("click", openDrawStreamSettings);

function closeDrawStreamSettings() {
  drawStreamSettingsModal.hidden = true;
}
$("draw-stream-settings-cancel").addEventListener("click", closeDrawStreamSettings);
drawStreamSettingsModal.addEventListener("click", (e) => {
  if (e.target === drawStreamSettingsModal) closeDrawStreamSettings();
});
drawStreamSettingsModal.querySelectorAll(".card-section-head").forEach((head) => {
  head.addEventListener("click", () => {
    head.parentElement.classList.toggle("collapsed");
    syncSectionCaret(head.parentElement);
  });
  syncSectionCaret(head.parentElement);
});

async function openDrawStreamSettings() {
  drawStreamSettingsMessage.textContent = "";
  try {
    const res = await fetch("/settings");
    const data = await res.json();
    applyAppSettings(data);
    settingsDrawStreamUrl.value = `${location.origin}/draw-stream`;
    settingsDrawStreamStrokeWidth.value = String(data.draw_stream_stroke_width_px ?? 4);
    setSegmentedValue(settingsDrawStreamBackground, data.draw_stream_background === "white" ? "white" : "black");
    settingsDrawStreamMaxResolution.value = String(data.draw_stream_max_resolution_px ?? 2560);
    refreshDrawStreamBgThumb();
    settingsDrawStreamBgMessage.textContent = "";
  } catch (e) {}
  drawStreamSettingsModal.hidden = false;
}

async function saveDrawStreamSettings() {
  try {
    const body = {
      draw_stream_stroke_width_px: parseInt(settingsDrawStreamStrokeWidth.value) || 4,
      draw_stream_background: getSegmentedValue(settingsDrawStreamBackground, "black"),
      draw_stream_max_resolution_px: parseInt(settingsDrawStreamMaxResolution.value) || 2560,
    };
    const res = await fetch("/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await readErr(res));
    applyAppSettings(await res.json());
    closeDrawStreamSettings();
  } catch (e) {
    drawStreamSettingsMessage.textContent = t("settings.save_failed", { message: e.message });
    drawStreamSettingsMessage.className = "error";
  }
}
$("draw-stream-settings-save").addEventListener("click", saveDrawStreamSettings);

for (const [inputId, copyId] of [["camera-rtsp-url", "camera-rtsp-url-copy"],
                                  ["camera-hls-url", "camera-hls-url-copy"],
                                  ["settings-draw-stream-url", "settings-draw-stream-url-copy"]]) {
  const input = $(inputId);
  const btn = $(copyId);
  btn.addEventListener("click", async () => {
    if (!input.value) return;
    try { await navigator.clipboard.writeText(input.value); }
    catch { input.select(); document.execCommand("copy"); }
    btn.textContent = t("common.copied");
    setTimeout(() => { btn.textContent = t("common.copy"); }, 1200);
  });
}

settingsDrawStreamBackground.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => setSegmentedValue(settingsDrawStreamBackground, btn.dataset.val));
});

// Draw-stream background image: uploaded/removed immediately (not part of
// the batched settings Save), since it's a file on disk, not a _SETTINGS
// value — reflected via the /draw-stream/background GET/DELETE routes.
function refreshDrawStreamBgThumb() {
  const url = `/draw-stream/background?t=${Date.now()}`;
  const probe = new Image();
  probe.onload = () => {
    settingsDrawStreamBgThumb.src = url;
    settingsDrawStreamBgThumb.hidden = false;
    settingsDrawStreamBgRemove.hidden = false;
  };
  probe.onerror = () => {
    settingsDrawStreamBgThumb.hidden = true;
    settingsDrawStreamBgRemove.hidden = true;
  };
  probe.src = url;
}
settingsDrawStreamBgUpload.addEventListener("click", () => settingsDrawStreamBgFile.click());
settingsDrawStreamBgFile.addEventListener("change", async () => {
  const file = settingsDrawStreamBgFile.files[0];
  if (!file) return;
  settingsDrawStreamBgMessage.textContent = "";
  try {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/draw-stream/background", { method: "POST", body: form });
    if (!res.ok) throw new Error(await readErr(res));
    refreshDrawStreamBgThumb();
  } catch (e) {
    settingsDrawStreamBgMessage.textContent = t("error.request_failed", { message: e.message });
    settingsDrawStreamBgMessage.className = "error";
  }
  settingsDrawStreamBgFile.value = "";
});
settingsDrawStreamBgRemove.addEventListener("click", async () => {
  try {
    await fetch("/draw-stream/background", { method: "DELETE" });
    refreshDrawStreamBgThumb();
  } catch (e) {}
});

function resetSettingsDisplay() {
  settingsDisplayUnit.value = localeDefaultUnit();
}

function applySettingsOptimizeEnabledStyle() {
  const optsBody = settingsOptimize?.closest(".card-section-body");
  const opts = optsBody?.querySelector(".optimize-options");
  if (opts) opts.classList.toggle("disabled", !settingsOptimize.checked);
}

function syncSettingsOptimizeMaster() {
  const anyOn = settingsOptimizeLinemerge.checked
             || settingsOptimizeLinesimplify.checked
             || settingsOptimizeLinesort.checked
             || settingsOptimizeReloop.checked;
  if (!anyOn && settingsOptimize.checked) settingsOptimize.checked = false;
  applySettingsOptimizeEnabledStyle();
}

function resetSettingsOptimize() {
  settingsOptimize.checked = true;
  settingsOptimizeLinemerge.checked = true;
  settingsOptimizeLinesimplify.checked = true;
  settingsOptimizeLinesort.checked = true;
  settingsOptimizeReloop.checked = true;
  settingsOptimizeTolerance.value = (0.10).toFixed(2);
  applySettingsOptimizeEnabledStyle();
}

settingsOptimize?.addEventListener("change", () => {
  if (settingsOptimize.checked) {
    const anyOn = settingsOptimizeLinemerge.checked
               || settingsOptimizeLinesimplify.checked
               || settingsOptimizeLinesort.checked
               || settingsOptimizeReloop.checked;
    if (!anyOn) {
      settingsOptimizeLinemerge.checked = true;
      settingsOptimizeLinesimplify.checked = true;
      settingsOptimizeLinesort.checked = true;
      settingsOptimizeReloop.checked = true;
    }
  }
  applySettingsOptimizeEnabledStyle();
});
[settingsOptimizeLinemerge, settingsOptimizeLinesimplify,
 settingsOptimizeLinesort, settingsOptimizeReloop]
  .forEach((el) => el?.addEventListener("change", syncSettingsOptimizeMaster));

// Wire collapsible sections + reset button inside the Settings modal
function resetSettingsSpeed() {
  const pairs = [
    ["settings-speed-pendown", "settings-speed-pendown-slider", 25],
    ["settings-speed-penup", "settings-speed-penup-slider", 75],
    ["settings-accel", "settings-accel-slider", 75],
  ];
  for (const [numId, sliderId, val] of pairs) {
    const n = $(numId);
    const s = $(sliderId);
    if (n) n.value = val;
    if (s) { s.value = val; updateSliderProgress(s); }
  }
}

// ───── Shutdown modal ────────────────────────────────────────────────────

const shutdownBtn = $("shutdown-btn");
const shutdownModal = $("shutdown-modal");
const shutdownCancel = $("shutdown-cancel");
const shutdownConfirm = $("shutdown-confirm");
const shutdownMessage = $("shutdown-message");

function openShutdownModal() {
  shutdownMessage.textContent = "";
  shutdownMessage.className = "muted";
  shutdownConfirm.disabled = false;
  shutdownCancel.disabled = false;
  shutdownModal.hidden = false;
}
function closeShutdownModal() { shutdownModal.hidden = true; }

shutdownBtn.addEventListener("click", openShutdownModal);
shutdownCancel.addEventListener("click", closeShutdownModal);
shutdownModal.addEventListener("click", (e) => { if (e.target === shutdownModal) closeShutdownModal(); });
shutdownConfirm.addEventListener("click", async () => {
  shutdownConfirm.disabled = true;
  shutdownCancel.disabled = true;
  shutdownMessage.textContent = t("shutdown.sending");
  shutdownMessage.className = "muted";
  try {
    const res = await fetch("/system/shutdown", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    shutdownMessage.textContent = t("shutdown.sent");
  } catch (e) {
    shutdownMessage.textContent = t("shutdown.failed", { message: e.message });
    shutdownMessage.className = "error";
    shutdownConfirm.disabled = false;
    shutdownCancel.disabled = false;
  }
});

settingsModal.querySelectorAll(".card-section-head").forEach((head) => {
  head.addEventListener("click", (e) => {
    if (e.target.closest(".card-section-reset")) return;
    head.parentElement.classList.toggle("collapsed");
    syncSectionCaret(head.parentElement);
  });
  syncSectionCaret(head.parentElement);
});
settingsModal.querySelectorAll(".card-section-reset").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (btn.dataset.reset === "settings-speed") resetSettingsSpeed();
    else if (btn.dataset.reset === "settings-pen-height") resetSettingsPenHeight();
    else if (btn.dataset.reset === "settings-job-options") resetSettingsJobOptions();
    else if (btn.dataset.reset === "settings-optimize") resetSettingsOptimize();
    else if (btn.dataset.reset === "settings-display") resetSettingsDisplay();
  });
});

// ───── Pen cursor on active job's preview ────────────────────────────────

const PEN_POSITION_STATUSES = new Set([
  "plotting", "paused", "awaiting_pen_change", "plotting_calibration", "homing",
]);

function updatePenCursor(msg) {
  const active = serverState.active_id ? cardEls.get(serverState.active_id) : null;
  if (!active) return;
  const cursor = active.querySelector(".pen-cursor");
  const job = serverState.queue.find((j) => j.job_id === serverState.active_id);
  if (!cursor || !job) return;
  const w = job.paper_width_mm, h = job.paper_height_mm;
  // This fork never sets ad.options.auto_rotate, so it stays at pyaxidraw's
  // own default (False, see axidraw_conf.py) — the physical pen frame is
  // never rotated relative to the document, and phys_x/phys_y map straight
  // onto the document's own top-left-origin frame. That frame is anchored
  // wherever plot_setup() started (i.e. it excludes any jog/nudge — verified
  // empirically: phys_x stays within the artwork's own extent regardless of
  // an active manual jog), the same frame the *undelta-shifted* design sits
  // in — so add the same live delta applied to the preview content (see
  // updatePreviewTransform's runDelta) or the cursor drifts off the artwork
  // the moment there's an active jog/nudge.
  const runDelta = activeRunDelta(job) || { dx: 0, dy: 0 };
  const leftPct = ((msg.x_mm + runDelta.dx) / w) * 100;
  const topPct = ((msg.y_mm + runDelta.dy) / h) * 100;
  cursor.hidden = false;
  cursor.style.left = `${leftPct}%`;
  cursor.style.top = `${topPct}%`;
  cursor.classList.toggle("pen-down", !!msg.pen_down);
}

function hideAllPenCursors() {
  document.querySelectorAll(".pen-cursor").forEach((c) => { c.hidden = true; });
}

function applyPenCursor() {
  const job = serverState.active_id
    ? serverState.queue.find((j) => j.job_id === serverState.active_id)
    : null;
  if (!job || !PEN_POSITION_STATUSES.has(job.status)) {
    hideAllPenCursors();
    return;
  }
  const pos = serverState.last_pen_position;
  if (pos) updatePenCursor(pos);
}

// ───── WebSocket ─────────────────────────────────────────────────────────

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/state`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "state") {
      serverState = msg;
      // Before anything reads it: a broadcast is a full replacement, and an
      // edit still in flight has to survive one. See cardUpdateUnconfirmed.
      applyUnconfirmedEdits();
      renderQueue();
      applyTopControls();
      applyCameraControls();
      applyPenCursor();
    } else if (msg.type === "position") {
      updatePenCursor(msg);
    }
  };
  // Fixed 2s, no backoff: this is a plotter on a LAN with one client, and a
  // reconnect that lags behind a service restart is more annoying than the
  // handful of failed opens it costs.
  ws.onclose = () => setTimeout(connectWs, 2000);
}
connectWs();
loadAppSettings();
loadAppVersion();
loadUpdateStatus();
loadLibrary();

async function loadAppVersion() {
  try {
    const res = await fetch("/version");
    if (!res.ok) return;
    const data = await res.json();
    const el = $("app-version");
    if (el && data.version) el.textContent = data.version;
  } catch (e) {}
}

// ───── Update notice ─────────────────────────────────────────────────────

let updateStatus = null;
const updateBanner = $("update-banner");

// On a live language swap, applyStatic() handles all data-i18n markup; re-run
// the render paths that build text via t()/tn() so dynamic copy updates too.
I18N.onLanguageChange(() => {
  applyTopControls();
  applyCameraControls();
  cardEls.forEach((card, id) => {
    const job = serverState.queue.find((j) => j.job_id === id);
    if (job) updateCard(card, job);
  });
  if (updateStatus) renderUpdateStatus(updateStatus);
  renderSkewClearance();
});

function renderUpdateStatus(status) {
  updateStatus = status;

  // Self-update is compiled out on this fork (updates._UPDATES_DISABLED), and
  // the server says so via `enabled`. Without reading it, every field below is
  // indistinguishable from a healthy "you are up to date" answer — so the
  // banner, the pill and Check now all stayed live over a feature that can do
  // nothing, and pressing Check now returned a permanently reassuring result
  // that meant nothing. Hide the controls; keep the version line, which is
  // still true and still useful.
  //
  // Hidden rather than removed: `updateBanner` is captured once at load and
  // several handlers bind unconditionally, so removing the nodes would throw.
  const enabled = !status || status.enabled !== false;
  const actions = document.querySelector(".settings-update-actions");
  if (actions) actions.hidden = !enabled;
  const pillEl = $("settings-update-pill");
  if (pillEl) pillEl.hidden = !enabled;
  if (!enabled) {
    updateBanner.hidden = true;
    const curOnly = $("settings-current-version");
    if (curOnly) curOnly.textContent = status ? status.current : "";
    return;
  }

  // Header banner: only when there's a newer version the user hasn't skipped.
  const show = status && status.update_available && !status.skipped;
  if (show) {
    $("update-from").textContent = status.current;
    $("update-to").textContent = status.latest;
  }
  updateBanner.hidden = !show;

  // Settings "About & Updates" pill always reflects the latest known state.
  const cur = $("settings-current-version");
  if (cur) cur.textContent = status ? status.current : "";
  const pill = $("settings-update-pill");
  if (pill) {
    if (!status || status.error) {
      pill.textContent = t("update.check_failed");
      pill.className = "update-pill error";
    } else if (status.update_available) {
      pill.textContent = t("update.available_version", { version: status.latest });
      pill.className = "update-pill available";
    } else {
      pill.textContent = t("update.up_to_date");
      pill.className = "update-pill ok";
    }
  }
  // Settings "Update now" is available whenever there's a newer version —
  // including after the banner was skipped (the deferred-update path).
  const sUpd = $("settings-update-now");
  if (sUpd) sUpd.hidden = !(status && status.update_available);
}

async function loadUpdateStatus() {
  try {
    const res = await fetch("/update/status");
    if (!res.ok) return;
    renderUpdateStatus(await res.json());
  } catch (e) {}
}

$("update-skip-btn").addEventListener("click", async () => {
  if (!updateStatus || !updateStatus.latest) return;
  try {
    const res = await fetch("/update/skip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: updateStatus.latest }),
    });
    if (res.ok) renderUpdateStatus(await res.json());
  } catch (e) {}
});


$("settings-check-update").addEventListener("click", async () => {
  const btn = $("settings-check-update");
  btn.disabled = true;
  btn.textContent = t("settings.updates.checking");
  try {
    const res = await fetch("/update/check", { method: "POST" });
    if (res.ok) renderUpdateStatus(await res.json());
  } catch (e) {
  } finally {
    btn.disabled = false;
    btn.textContent = t("settings.updates.check_now");
  }
});

$("update-now-btn").addEventListener("click", () => startUpdate(false));
$("settings-update-now").addEventListener("click", () => {
  settingsModal.hidden = true;
  startUpdate(false);
});
$("update-progress-close").addEventListener("click", () => {
  $("update-progress-modal").hidden = true;
});

// Confirm dialog shown when the app folder has local changes that the update
// would overwrite. On confirm we retry the apply with force=true.
let dirtyConfirmCallback = null;
function openDirtyConfirm(files, onConfirm) {
  dirtyConfirmCallback = onConfirm;
  $("update-confirm-files").textContent = files.length ? files.join("\n") : t("update.unknown_files");
  $("update-confirm-modal").hidden = false;
}
$("update-confirm-cancel").addEventListener("click", () => {
  $("update-confirm-modal").hidden = true;
  dirtyConfirmCallback = null;
});
$("update-confirm-overwrite").addEventListener("click", () => {
  $("update-confirm-modal").hidden = true;
  const cb = dirtyConfirmCallback;
  dirtyConfirmCallback = null;
  if (cb) cb();
});

// Kick off an update and follow it to completion. The service restarts
// mid-flight, so we stream the wrapper's log and watch /version: when the app
// comes back reporting the target version, we reload.
async function startUpdate(dryRun, force = false) {
  const target = updateStatus && updateStatus.latest;
  const modal = $("update-progress-modal");
  const title = $("update-progress-title");
  const logEl = $("update-progress-log");
  const statusEl = $("update-progress-status");
  const closeBtn = $("update-progress-close");

  title.textContent = dryRun ? t("update.dryrun_title") : t("update.progress_title");
  logEl.textContent = "";
  statusEl.textContent = t("update.starting");
  statusEl.className = "muted";
  closeBtn.hidden = true;
  modal.hidden = false;

  let res;
  try {
    res = await fetch("/update/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: !!dryRun, force: !!force }),
    });
  } catch (e) {
    statusEl.textContent = t("update.could_not_start", { message: e.message });
    statusEl.className = "error";
    closeBtn.hidden = false;
    return;
  }
  if (!res.ok) {
    let detail = null;
    try { detail = (await res.json()).detail; } catch {}
    // Dirty working tree → offer to overwrite (unless we already forced).
    if (res.status === 409 && detail && typeof detail === "object"
        && detail.reason === "dirty" && !force) {
      modal.hidden = true;
      openDirtyConfirm(detail.files || [], () => startUpdate(dryRun, true));
      return;
    }
    const msg = apiErrText(detail) || res.statusText;
    statusEl.textContent = t("update.could_not_start", { message: msg });
    statusEl.className = "error";
    closeBtn.hidden = false;
    return;
  }

  const startedAt = Date.now();
  const TIMEOUT_MS = 5 * 60 * 1000;
  let sawDown = false;

  const finish = (msg, isError) => {
    statusEl.textContent = msg;
    statusEl.className = isError ? "error" : "muted";
    closeBtn.hidden = false;
  };

  const tick = async () => {
    // Stream the wrapper log (may fail while the service is restarting).
    try {
      const r = await fetch("/update/log", { cache: "no-store" });
      if (r.ok) {
        const d = await r.json();
        if (d.log) {
          logEl.textContent = d.log;
          logEl.scrollTop = logEl.scrollHeight;
        }
        if (/update DONE \(dry run\)/.test(d.log)) {
          finish(t("update.dryrun_done"), false);
          return;
        }
      } else {
        sawDown = true;
      }
    } catch (e) {
      sawDown = true; // service down during restart
    }

    // For a real update, completion = the app reappears on the new version.
    if (!dryRun) {
      try {
        const r = await fetch("/version", { cache: "no-store" });
        if (r.ok) {
          const d = await r.json();
          if (target && d.version === target) {
            finish(t("update.updated_reloading", { version: target }), false);
            setTimeout(() => location.reload(), 1500);
            return;
          }
          if (sawDown) statusEl.textContent = t("update.service_back");
        } else {
          sawDown = true;
        }
      } catch (e) {
        sawDown = true;
        statusEl.textContent = t("update.service_restarting");
      }
    }

    if (Date.now() - startedAt > TIMEOUT_MS) {
      finish(t("update.timeout"), true);
      return;
    }
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1200);
}

window.addEventListener("resize", () => {
  cardEls.forEach((card, id) => {
    const job = serverState.queue.find((j) => j.job_id === id);
    if (job) updatePreviewTransform(card, job);
  });
});
