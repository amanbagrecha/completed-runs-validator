const apiBase = document.body.dataset.apiBase || "/api";
const reviewApiBase = `${apiBase}/review`;

// Keep topping up the queue in the background while runs are still preparing and
// the queue has fallen below this many images, rather than waiting for it to
// drain to zero (which forced reviewers to refresh the page). ~2 runs' worth of
// buffer; the server caches further ahead (REVIEW_READY_IMAGE_BUFFER) so a dip
// below this is refilled before the reviewer reaches the end.
const QUEUE_LOW_WATER = 12;

const loadButton = document.querySelector("#loadButton");
const batchSelect = document.querySelector("#batchSelect");
const localityCategoryFilter = document.querySelector("#localityCategoryFilter");
const runFilter = document.querySelector("#runFilter");
const stateInput = document.querySelector("#stateInput");
const stateTabs = Array.from(document.querySelectorAll(".state-tab"));
const message = document.querySelector("#message");
const imageFrame = document.querySelector("#imageFrame");
const currentImage = document.querySelector("#currentImage");
const emptyState = document.querySelector("#emptyState");
const imageMeta = document.querySelector("#imageMeta");
const queueSummary = document.querySelector("#queueSummary");
const draftSummary = document.querySelector("#draftSummary");
const prevButton = document.querySelector("#prevButton");
const nextButton = document.querySelector("#nextButton");
const failButton = document.querySelector("#failButton");
const passButton = document.querySelector("#passButton");
const failOptions = document.querySelector("#failOptions");
const minorBugButton = document.querySelector("#minorBugButton");
const majorBugButton = document.querySelector("#majorBugButton");
const notesInput = document.querySelector("#notesInput");
const failValidation = document.querySelector("#failValidation");
const submitButton = document.querySelector("#submitButton");
const statsUpdatedAt = document.querySelector("#statsUpdatedAt");
const totalRunsStat = document.querySelector("#totalRunsStat");
const completedRunsStat = document.querySelector("#completedRunsStat");
const inProgressRunsStat = document.querySelector("#inProgressRunsStat");
const notStartedRunsStat = document.querySelector("#notStartedRunsStat");
const validatedImagesStat = document.querySelector("#validatedImagesStat");
const passImagesStat = document.querySelector("#passImagesStat");
const failImagesStat = document.querySelector("#failImagesStat");
const failRateStat = document.querySelector("#failRateStat");
const batchStatsBody = document.querySelector("#batchStatsBody");

let queue = [];
let currentIndex = -1;
let drafts = new Map();
let touchStartX = null;
let touchStartY = null;
let queueRefreshTimer = null;
let statsRefreshTimer = null;
let queueLoadToken = 0;

loadButton.addEventListener("click", () => {
  loadQueue().catch((error) => setMessage(error.message));
});
batchSelect.addEventListener("change", noteFilterChange);
localityCategoryFilter.addEventListener("change", noteFilterChange);
prevButton.addEventListener("click", () => moveCurrentImage(-1));
nextButton.addEventListener("click", () => moveCurrentImage(1));
failButton.addEventListener("click", () => markCurrentImage("fail"));
passButton.addEventListener("click", () => markCurrentImage("pass"));
minorBugButton.addEventListener("click", () => setFailSeverity("minor"));
majorBugButton.addEventListener("click", () => setFailSeverity("major"));
submitButton.addEventListener("click", () => {
  submitDrafts().catch((error) => {
    setMessage(error.message);
    updateDraftSummary();
  });
});

for (const tab of stateTabs) {
  tab.addEventListener("click", () => setState(tab.dataset.state || "unreviewed"));
}

notesInput.addEventListener("input", () => {
  const image = currentQueueItem();
  if (!image) {
    return;
  }
  setDraft(image.id, { notes: notesInput.value });
});

runFilter.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    loadQueue().catch((error) => setMessage(error.message));
  }
});
runFilter.addEventListener("input", noteFilterChange);

document.addEventListener("keydown", (event) => {
  if (event.key === "ArrowLeft") {
    moveCurrentImage(-1);
  } else if (event.key === "ArrowRight") {
    moveCurrentImage(1);
  }
});

imageFrame.addEventListener("touchstart", (event) => {
  const touch = event.changedTouches[0];
  touchStartX = touch.clientX;
  touchStartY = touch.clientY;
});

imageFrame.addEventListener("touchend", (event) => {
  if (touchStartX === null || touchStartY === null) {
    return;
  }
  const touch = event.changedTouches[0];
  const deltaX = touch.clientX - touchStartX;
  const deltaY = touch.clientY - touchStartY;
  touchStartX = null;
  touchStartY = null;
  if (Math.abs(deltaX) < 48 || Math.abs(deltaX) <= Math.abs(deltaY)) {
    return;
  }
  moveCurrentImage(deltaX < 0 ? 1 : -1);
});

applyQueryParams();
renderCurrentImage();
updateDraftSummary();
loadReviewStats().catch(() => renderStatsError());

async function loadQueue() {
  return refreshQueue({ background: false });
}

function renderCurrentImage() {
  const image = currentQueueItem();
  imageFrame.classList.toggle("has-image", Boolean(image));
  emptyState.hidden = Boolean(image);
  currentImage.hidden = !image;

  if (!image) {
    currentImage.removeAttribute("src");
    imageMeta.textContent = "";
    queueSummary.textContent = "";
    notesInput.value = "";
    notesInput.disabled = true;
    failButton.disabled = true;
    passButton.disabled = true;
    minorBugButton.disabled = true;
    majorBugButton.disabled = true;
    prevButton.disabled = true;
    nextButton.disabled = true;
    setDecisionButtons("", "");
    clearFailValidation();
    updateDraftSummary();
    return;
  }

  const draft = draftStateForImage(image);

  currentImage.src = image.file_url;
  currentImage.alt = `Validation image ${image.id}`;
  imageMeta.textContent = `${currentIndex + 1} of ${queue.length} · image ${image.id} · run ${image.run_id} · ${localityText(image)}`;
  queueSummary.textContent = "";
  notesInput.value = draft.notes;
  notesInput.disabled = false;
  failButton.disabled = false;
  passButton.disabled = false;
  minorBugButton.disabled = false;
  majorBugButton.disabled = false;
  prevButton.disabled = currentIndex <= 0;
  nextButton.disabled = currentIndex < 0 || currentIndex >= queue.length - 1;
  setDecisionButtons(draft.status, draft.severity);
  syncFailValidationState(draft);
  preloadAdjacentImages();
  updateDraftSummary();
}

function moveCurrentImage(delta) {
  const blockReason = validateCurrentImageBeforeLeaving();
  if (blockReason) {
    syncFailValidationState(draftStateForImage(currentQueueItem()));
    setMessage(blockReason);
    return;
  }
  if (!queue.length) {
    return;
  }
  const nextIndex = currentIndex + delta;
  if (nextIndex < 0 || nextIndex >= queue.length) {
    return;
  }
  currentIndex = nextIndex;
  renderCurrentImage();
}

function markCurrentImage(status) {
  const image = currentQueueItem();
  if (!image) {
    return;
  }
  const draft = draftStateForImage(image);
  if (status === "pass") {
    setDraft(image.id, {
      status: "pass",
      severity: "",
      notes: draft.notes,
    });
    setDecisionButtons("pass", "");
    clearFailValidation();
    updateDraftSummary();
    moveCurrentImage(1);
    return;
  }

  setDraft(image.id, {
    status: "fail",
    severity: draft.severity,
    notes: draft.notes,
  });
  setDecisionButtons("fail", draft.severity);
  failOptions.hidden = false;
  syncFailValidationState(draftStateForImage(image));
  notesInput.focus();
  updateDraftSummary();
}

function setFailSeverity(severity) {
  const image = currentQueueItem();
  if (!image) {
    return;
  }
  const draft = draftStateForImage(image);
  setDraft(image.id, {
    status: "fail",
    severity,
    notes: draft.notes,
  });
  setDecisionButtons("fail", severity);
  syncFailValidationState(draftStateForImage(image));
  notesInput.focus();
}

async function submitDrafts() {
  const items = collectDraftItems();
  if (!items.length) {
    setMessage("Mark one or more images before submitting drafts.");
    return;
  }

  submitButton.disabled = true;
  setMessage(`Saving ${items.length} draft${items.length === 1 ? "" : "s"}...`);
  try {
    const data = await apiFetch(`${reviewApiBase}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });

    applySubmittedDrafts(items);
    setMessage(reviewSubmitMessage(data.saved, data.completed_runs || 0));
    loadReviewStats().catch(() => renderStatsError());
    if (stateInput.value !== "all") {
      scheduleQueueRefresh();
    }
  } finally {
    updateDraftSummary();
  }
}

function applySubmittedDrafts(items) {
  const submittedById = new Map(items.map((item) => [Number(item.image_id), item]));
  const currentImageId = currentQueueItem()?.id ?? null;

  for (const image of queue) {
    const submitted = submittedById.get(Number(image.id));
    if (!submitted) {
      continue;
    }
    image.status = submitted.status;
    image.notes = submitted.notes || "";
  }

  for (const item of items) {
    drafts.delete(Number(item.image_id));
  }

  queue = queue.filter((image) => matchesCurrentState(image));
  if (!queue.length) {
    currentIndex = -1;
    renderCurrentImage();
    return;
  }

  if (currentImageId !== null) {
    const nextIndex = queue.findIndex((image) => image.id === currentImageId);
    if (nextIndex >= 0) {
      currentIndex = nextIndex;
    } else {
      currentIndex = Math.min(currentIndex, queue.length - 1);
    }
  } else {
    currentIndex = Math.min(Math.max(currentIndex, 0), queue.length - 1);
  }

  renderCurrentImage();
}

function collectDraftItems() {
  const items = [];
  for (const [imageId, draft] of drafts.entries()) {
    const image = queue.find((item) => item.id === imageId);
    if (!image) {
      continue;
    }
    const draftState = draftStateForImage(image);
    const status = draft.status || image.status || "";
    const notes = draft.notes ?? draftState.notes;
    const severity = draft.severity || draftState.severity;
    if (!status) {
      continue;
    }
    if (status === "fail") {
      if (!severity) {
        throw new Error("Choose Minor Bug or Major Bug for each failed image before submitting drafts.");
      }
      if (!notes.trim()) {
        throw new Error("Write notes for each failed image before submitting drafts.");
      }
    }
    const storedNotes = composeStoredReviewNotes(status, severity, notes);
    if (status === (image.status || "") && storedNotes === (image.notes || "")) {
      continue;
    }
    items.push({ image_id: imageId, status, notes: storedNotes });
  }
  return items;
}

function reviewSubmitMessage(savedCount, completedRuns) {
  if (completedRuns > 0) {
    return `Saved ${savedCount} draft${savedCount === 1 ? "" : "s"}. Completed ${completedRuns} run${completedRuns === 1 ? "" : "s"}.`;
  }
  return `Saved ${savedCount} draft${savedCount === 1 ? "" : "s"}.`;
}

function matchesCurrentState(image) {
  if (stateInput.value === "all") {
    return true;
  }
  if (stateInput.value === "unreviewed") {
    return !image.status;
  }
  if (stateInput.value === "submitted") {
    return Boolean(image.status);
  }
  return image.status === stateInput.value;
}

function setDraft(imageId, updates) {
  const image = queue.find((item) => item.id === imageId);
  if (!image) {
    return;
  }

  const current = drafts.get(imageId) || {};
  const persisted = parseStoredReviewState(image);
  const next = {
    status: current.status ?? persisted.status,
    severity: current.severity ?? persisted.severity,
    notes: current.notes ?? persisted.notes,
    ...updates,
  };

  if (
    (next.status || "") === persisted.status
    && (next.severity || "") === persisted.severity
    && next.notes === persisted.notes
  ) {
    drafts.delete(imageId);
  } else {
    drafts.set(imageId, next);
  }

  const currentImage = currentQueueItem();
  if (currentImage && currentImage.id === imageId) {
    syncFailValidationState(draftStateForImage(currentImage));
  }
  updateDraftSummary();
}

function currentQueueItem() {
  return currentIndex >= 0 ? queue[currentIndex] : null;
}

function preloadAdjacentImages() {
  for (const index of [currentIndex - 1, currentIndex + 1]) {
    const image = queue[index];
    if (!image) {
      continue;
    }
    const preloader = new Image();
    preloader.src = image.file_url;
  }
}

function setDecisionButtons(status, severity) {
  failOptions.hidden = status !== "fail";
  failButton.classList.toggle("decision-button-active", status === "fail");
  passButton.classList.toggle("decision-button-active", status === "pass");
  minorBugButton.classList.toggle("decision-button-active", severity === "minor");
  majorBugButton.classList.toggle("decision-button-active", severity === "major");
}

function setState(nextState) {
  stateInput.value = nextState;
  for (const tab of stateTabs) {
    tab.classList.toggle("state-tab-active", tab.dataset.state === nextState);
  }
  noteFilterChange();
}

function noteFilterChange() {
  queueLoadToken += 1;
  stopQueueRefresh();
  scheduleStatsRefresh();
  if (!loadButton.disabled) {
    setMessage("Filters changed. Load images to refresh.");
  }
}

async function refreshQueue({ background }) {
  const requestToken = background ? queueLoadToken : queueLoadToken + 1;
  if (!background) {
    queueLoadToken = requestToken;
    stopQueueRefresh();
    setLoadingState(true);
    setMessage("Loading images...");
    updateUrl();
  }

  const hadImages = queue.length > 0;
  try {
    const data = await apiFetch(`${reviewApiBase}/images?${currentQueryParams().toString()}`);
    if (requestToken !== queueLoadToken) {
      return;
    }

    applyQueueUpdate(data.images || [], { resetDrafts: !background });
    const pendingRuns = Number(data.pending_runs || 0);
    if (pendingRuns > 0 && queue.length <= QUEUE_LOW_WATER) {
      scheduleQueueRefresh();
    } else {
      stopQueueRefresh();
    }

    if (background) {
      if (!hadImages && queue.length) {
        setMessage(loadMessage(data, true));
      } else if (pendingRuns === 0) {
        setMessage(loadMessage(data, false));
      }
      return;
    }

    setMessage(loadMessage(data, false));
  } finally {
    if (!background && requestToken === queueLoadToken) {
      setLoadingState(false);
    }
  }
}

function applyQueueUpdate(nextQueue, { resetDrafts }) {
  const currentImageId = currentQueueItem()?.id ?? null;
  if (resetDrafts) {
    drafts = new Map();
  }

  queue = nextQueue;
  if (!queue.length) {
    currentIndex = -1;
    renderCurrentImage();
    return;
  }

  if (currentImageId !== null) {
    const nextIndex = queue.findIndex((image) => image.id === currentImageId);
    if (nextIndex >= 0) {
      currentIndex = nextIndex;
    } else if (currentIndex < 0 || currentIndex >= queue.length) {
      currentIndex = 0;
    } else {
      currentIndex = Math.min(currentIndex, queue.length - 1);
    }
  } else {
    currentIndex = 0;
  }

  renderCurrentImage();
}

function scheduleQueueRefresh() {
  stopQueueRefresh();
  const refreshToken = queueLoadToken;
  queueRefreshTimer = window.setTimeout(() => {
    if (refreshToken !== queueLoadToken) {
      return;
    }
    refreshQueue({ background: true }).catch(() => {
      if (refreshToken === queueLoadToken) {
        scheduleQueueRefresh();
      }
    });
  }, 1500);
}

function stopQueueRefresh() {
  if (queueRefreshTimer !== null) {
    window.clearTimeout(queueRefreshTimer);
    queueRefreshTimer = null;
  }
}

function currentQueryParams() {
  return new URLSearchParams({
    batch: batchSelect.value || "all",
    locality_category: localityCategoryFilter.value || "all",
    run_id: runFilter.value.trim(),
    state: stateInput.value,
  });
}

function statsQueryParams() {
  return new URLSearchParams({
    batch: batchSelect.value || "all",
    locality_category: localityCategoryFilter.value || "all",
    run_id: runFilter.value.trim(),
  });
}

function scheduleStatsRefresh() {
  if (statsRefreshTimer !== null) {
    window.clearTimeout(statsRefreshTimer);
  }
  statsRefreshTimer = window.setTimeout(() => {
    statsRefreshTimer = null;
    loadReviewStats().catch(() => renderStatsError());
  }, 250);
}

async function loadReviewStats() {
  const data = await apiFetch(`${reviewApiBase}/stats?${statsQueryParams().toString()}`);
  renderReviewStats(data);
}

function renderReviewStats(data) {
  const summary = data.summary || {};
  totalRunsStat.textContent = formatNumber(summary.total_runs);
  completedRunsStat.textContent = formatNumber(summary.completed_runs);
  inProgressRunsStat.textContent = formatNumber(summary.in_progress_runs);
  notStartedRunsStat.textContent = formatNumber(summary.not_started_runs);
  validatedImagesStat.textContent = formatNumber(summary.validated_images);
  passImagesStat.textContent = formatNumber(summary.pass_images);
  failImagesStat.textContent = formatNumber(summary.fail_images);
  failRateStat.textContent = formatPercent(summary.fail_rate);
  statsUpdatedAt.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  renderBatchStats(data.batches || []);
}

function renderBatchStats(batches) {
  batchStatsBody.innerHTML = "";
  if (!batches.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = "No runs match these filters.";
    tr.appendChild(td);
    batchStatsBody.appendChild(tr);
    return;
  }

  for (const batch of batches) {
    const tr = document.createElement("tr");
    appendStatCell(tr, batch.batch_name || "unknown", "batch-stat-name");
    appendStatCell(tr, formatNumber(batch.completed_runs));
    appendStatCell(tr, formatNumber(batch.in_progress_runs));
    appendStatCell(tr, formatNumber(batch.validated_images));
    tr.title = `${formatNumber(batch.total_runs)} runs · ${formatNumber(batch.pass_images)} pass · ${formatNumber(batch.fail_images)} fail · ${formatPercent(batch.fail_rate)} fail rate`;
    batchStatsBody.appendChild(tr);
  }
}

function appendStatCell(tr, text, className = "") {
  const td = document.createElement("td");
  if (className) {
    td.className = className;
  }
  td.textContent = text;
  tr.appendChild(td);
}

function renderStatsError() {
  statsUpdatedAt.textContent = "Stats unavailable";
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString();
}

function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function loadMessage(data, becameAvailableInBackground) {
  const pendingRuns = Number(data.pending_runs || 0);
  const skippedRuns = Number(data.skipped_runs || 0);
  if (!queue.length) {
    if (pendingRuns > 0) {
      return skippedRuns
        ? "Preparing images in the background. Some runs were skipped."
        : "Preparing images in the background.";
    }
    return skippedRuns ? "No images matched. Some runs were skipped." : "No images match these filters.";
  }
  if (pendingRuns > 0) {
    return becameAvailableInBackground
      ? "Images are ready. More runs will prepare as this queue is submitted."
      : "Images loaded. More runs will prepare as this queue is submitted.";
  }
  return skippedRuns ? "Images loaded. Some runs were skipped." : "Images loaded.";
}

function updateDraftSummary() {
  let items = [];
  try {
    items = collectDraftItems();
  } catch {
    items = Array.from(drafts.entries())
      .filter(([, draft]) => draft.status)
      .map(([imageId, draft]) => ({ image_id: imageId, status: draft.status }));
  }
  const passCount = items.filter((item) => item.status === "pass").length;
  const failCount = items.filter((item) => item.status === "fail").length;
  draftSummary.textContent = items.length
    ? `${items.length} drafts · ${failCount} fail · ${passCount} pass`
    : "0 drafts";
  submitButton.disabled = items.length === 0;
}

function applyQueryParams() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("batch")) {
    batchSelect.value = params.get("batch");
  }
  if (params.get("run_id")) {
    runFilter.value = params.get("run_id");
  }
  if (params.get("locality_category")) {
    localityCategoryFilter.value = params.get("locality_category");
  }
  if (params.get("state")) {
    setState(params.get("state"));
  }
  if (params.toString()) {
    loadQueue().catch((error) => setMessage(error.message));
  }
}

function updateUrl() {
  const params = new URLSearchParams({
    batch: batchSelect.value || "all",
    locality_category: localityCategoryFilter.value || "all",
    state: stateInput.value,
  });
  if (runFilter.value.trim()) {
    params.set("run_id", runFilter.value.trim());
  }
  window.history.replaceState(null, "", `?${params.toString()}`);
}

function setLoadingState(isLoading) {
  loadButton.disabled = isLoading;
  batchSelect.disabled = isLoading;
  localityCategoryFilter.disabled = isLoading;
  runFilter.disabled = isLoading;
  prevButton.disabled = isLoading || currentIndex <= 0;
  nextButton.disabled = isLoading || currentIndex < 0 || currentIndex >= queue.length - 1;
  notesInput.disabled = isLoading || !currentQueueItem();
  failButton.disabled = isLoading || !currentQueueItem();
  passButton.disabled = isLoading || !currentQueueItem();
  minorBugButton.disabled = isLoading || !currentQueueItem();
  majorBugButton.disabled = isLoading || !currentQueueItem();
  submitButton.disabled = isLoading || safeDraftItemCount() === 0;
  for (const tab of stateTabs) {
    tab.disabled = isLoading;
  }
  if (!isLoading) {
    renderCurrentImage();
  }
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    const detail = data && data.detail ? formatApiErrorDetail(data.detail) : response.statusText;
    throw new Error(detail);
  }
  return data;
}

function formatApiErrorDetail(detail) {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  }
  return JSON.stringify(detail);
}

function setMessage(text) {
  message.textContent = text;
}

function localityText(image) {
  const category = image.locality_category || "unknown locality";
  const name = image.locality_name || "unknown";
  const region = image.region_id ? ` · ${image.region_id}` : "";
  return `${category.replaceAll("_", " ")} · ${name}${region}`;
}

function validateCurrentImageBeforeLeaving() {
  const image = currentQueueItem();
  if (!image) {
    return "";
  }
  const draft = draftStateForImage(image);
  if (!draft || draft.status !== "fail") {
    return "";
  }
  if (!draft.severity) {
    return "Choose Minor Bug or Major Bug before going to the next image.";
  }
  if (!String(draft.notes || "").trim()) {
    return "Write notes for the failed image before going to the next image.";
  }
  return "";
}

function parseStoredReviewState(image) {
  const status = image.status || "";
  const rawNotes = image.notes || "";
  if (status !== "fail") {
    return { status, severity: "", notes: rawNotes };
  }
  if (rawNotes.startsWith("[major] ")) {
    return { status, severity: "major", notes: rawNotes.slice(8) };
  }
  if (rawNotes.startsWith("[minor] ")) {
    return { status, severity: "minor", notes: rawNotes.slice(8) };
  }
  return { status, severity: "", notes: rawNotes };
}

function draftStateForImage(image) {
  const persisted = parseStoredReviewState(image);
  const draft = drafts.get(image.id);
  if (!draft) {
    return persisted;
  }
  return {
    status: draft.status ?? persisted.status,
    severity: draft.severity ?? persisted.severity,
    notes: draft.notes ?? persisted.notes,
  };
}

function syncFailValidationState(draft) {
  if (!draft || draft.status !== "fail") {
    clearFailValidation();
    return;
  }

  if (!draft.severity) {
    showFailValidation("Select Minor Bug or Major Bug.", { highlightSeverity: true });
    return;
  }
  if (!String(draft.notes || "").trim()) {
    showFailValidation("Write notes for the failed image.", { highlightNotes: true });
    return;
  }
  clearFailValidation();
}

function showFailValidation(text, options = {}) {
  failValidation.textContent = text;
  failValidation.hidden = false;
  failOptions.classList.toggle("field-invalid", Boolean(options.highlightSeverity));
  notesInput.classList.toggle("field-invalid", Boolean(options.highlightNotes));
}

function clearFailValidation() {
  failValidation.textContent = "";
  failValidation.hidden = true;
  failOptions.classList.remove("field-invalid");
  notesInput.classList.remove("field-invalid");
}

function composeStoredReviewNotes(status, severity, notes) {
  if (status !== "fail") {
    return notes;
  }
  if (severity === "major") {
    return `[major] ${notes}`;
  }
  if (severity === "minor") {
    return `[minor] ${notes}`;
  }
  return notes;
}

function safeDraftItemCount() {
  try {
    return collectDraftItems().length;
  } catch {
    return drafts.size;
  }
}
