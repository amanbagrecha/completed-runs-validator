const batchSelect = document.querySelector("#batchSelect");
const runFilter = document.querySelector("#runFilter");
const statusFilter = document.querySelector("#statusFilter");
const limitSelect = document.querySelector("#limitSelect");
const pageInput = document.querySelector("#pageInput");
const loadRunsButton = document.querySelector("#loadRunsButton");
const prevButton = document.querySelector("#prevButton");
const nextButton = document.querySelector("#nextButton");
const syncButton = document.querySelector("#syncButton");
const runsTableBody = document.querySelector("#runsTableBody");
const runCount = document.querySelector("#runCount");
const draftCount = document.querySelector("#draftCount");
const submitButton = document.querySelector("#submitButton");
const message = document.querySelector("#message");
const zoomDialog = document.querySelector("#zoomDialog");
const zoomImage = document.querySelector("#zoomImage");
const zoomCaption = document.querySelector("#zoomCaption");
const zoomPrevButton = document.querySelector("#zoomPrevButton");
const zoomNextButton = document.querySelector("#zoomNextButton");
const closeZoomButton = document.querySelector("#closeZoomButton");
const apiBase = document.body.dataset.apiBase || "/api";

let currentOffset = 0;
let currentTotal = 0;
let currentPage = 1;
let totalPages = 0;
let visibleRuns = [];
let visibleImages = new Map();
let visibleImageOffsets = new Map();
let draftSelections = new Map();
let zoomImages = [];
let zoomIndex = 0;

loadRunsButton.addEventListener("click", () => loadRuns(pageNumber()));
prevButton.addEventListener("click", () => loadRuns(Math.max(1, currentPage - 1)));
nextButton.addEventListener("click", () => loadRuns(currentPage + 1));
syncButton.addEventListener("click", syncMetadata);
submitButton.addEventListener("click", submitValidation);
zoomPrevButton.addEventListener("click", () => moveZoom(-1));
zoomNextButton.addEventListener("click", () => moveZoom(1));
closeZoomButton.addEventListener("click", () => zoomDialog.close());
zoomDialog.addEventListener("click", (event) => {
  if (event.target === zoomDialog) {
    zoomDialog.close();
  }
});

document.addEventListener("keydown", (event) => {
  if (!zoomDialog.open) {
    return;
  }
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    moveZoom(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    moveZoom(1);
  }
});

for (const input of [batchSelect, statusFilter, limitSelect]) {
  input.addEventListener("change", resetTable);
}

pageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    loadRuns(pageNumber());
  }
});

runFilter.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    pageInput.value = "1";
    loadRuns(1);
  }
});

applyQueryParams();

async function syncMetadata() {
  setMessage("Syncing Google Sheet and S3 metadata...");
  syncButton.disabled = true;
  try {
    const data = await apiFetch(`${apiBase}/sync`, { method: "POST" });
    setMessage(`Sync complete: indexed ${data.indexed_runs}/${data.sheet_runs} sheet runs. Missing in S3: ${data.missing_in_s3}.`);
  } finally {
    syncButton.disabled = false;
  }
}

async function loadRuns(page) {
  const batch = batchSelect.value;
  if (!batch) {
    setMessage("Select a batch first.");
    return;
  }

  currentPage = Math.max(1, page || 1);
  currentOffset = (currentPage - 1) * pageLimit();
  pageInput.value = String(currentPage);
  visibleRuns = [];
  visibleImages = new Map();
  visibleImageOffsets = new Map();
  draftSelections = new Map();
  updateSubmitState();
  setMessage("Loading run rows...");
  renderLoadingRows();
  updateUrl();

  const params = new URLSearchParams({
    batch,
    run_id: runFilter.value.trim(),
    status: statusFilter.value,
    limit: String(pageLimit()),
    page: String(currentPage),
  });
  const data = await apiFetch(`${apiBase}/runs?${params.toString()}`);
  currentTotal = data.total;
  currentPage = data.page;
  totalPages = data.total_pages;
  currentOffset = data.offset;
  pageInput.value = String(currentPage);
  visibleRuns = data.runs;
  renderRunTable(data.runs);
  updatePager();

  if (!data.runs.length) {
    setMessage(`No runs found for ${selectedBatchLabel()}.`);
    return;
  }

  setMessage(`Loading and caching images for ${data.runs.length} visible runs...`);
  const errorCount = await loadImagesForVisibleRuns(data.runs);
  if (errorCount) {
    setMessage(`Loaded ${data.runs.length} runs with ${errorCount} image row errors. Details are shown in the affected rows.`);
  } else {
    setMessage(`Loaded ${data.runs.length} runs. Click images to zoom. Submit saves selected visible validations.`);
  }
}

function renderRunTable(runs) {
  runsTableBody.innerHTML = "";
  if (!runs.length) {
    runsTableBody.innerHTML = '<tr><td colspan="5" class="empty">No runs found.</td></tr>';
    runCount.textContent = "0 found";
    return;
  }

  for (const run of runs) {
    const tr = document.createElement("tr");
    tr.id = rowId(run.run_id);
    tr.className = `run-row status-${run.status}`;
    tr.dataset.runId = run.run_id;
    tr.innerHTML = `
      <td class="run-cell">
        <div class="run-id-badge">${escapeHtml(run.run_id)}</div>
        <div class="run-stats">
          <span class="status-label">${escapeHtml(run.status)}</span>
          <span>batch ${escapeHtml(run.batch_name)}</span>
          <span class="validation-count-label">${run.validated_images}/${run.image_target_count} validated</span>
          <span class="image-count-label">${run.selected_images}/${run.image_target_count} images loaded</span>
          <span>count ${run.sheet_count ?? "n/a"}</span>
          <span>${escapeHtml(run.vehicle_type || "no vehicle")}</span>
        </div>
        <div class="run-image-controls">
          <button type="button" data-action="prev-run-images">‹</button>
          <span class="image-window-label">0/0</span>
          <button type="button" data-action="next-run-images">›</button>
          <button type="button" data-action="refresh-images">Refresh Images</button>
        </div>
      </td>
      ${[0, 1, 2].map((index) => `<td class="image-slot" data-view-index="${index}"><div class="skeleton">Caching...</div></td>`).join("")}
      <td class="run-actions">
        <button type="button" data-action="pass-all">Pass Run</button>
        <button type="button" data-action="fail-all" class="danger">Fail Run</button>
      </td>
    `;
    tr.querySelector('[data-action="prev-run-images"]').addEventListener("click", () => moveRunImages(run.run_id, -3));
    tr.querySelector('[data-action="next-run-images"]').addEventListener("click", () => moveRunImages(run.run_id, 3));
    tr.querySelector('[data-action="refresh-images"]').addEventListener("click", () => refreshRunImages(run.run_id));
    tr.querySelector('[data-action="pass-all"]').addEventListener("click", () => setRunStatus(run.run_id, "pass"));
    tr.querySelector('[data-action="fail-all"]').addEventListener("click", () => setRunStatus(run.run_id, "fail"));
    runsTableBody.appendChild(tr);
  }
  runCount.textContent = `${currentTotal} found · page ${currentPage}/${totalPages || 1} · showing ${currentOffset + 1}-${Math.min(currentOffset + runs.length, currentTotal)}`;
}

async function loadImagesForVisibleRuns(runs) {
  let errorCount = 0;
  let nextIndex = 0;
  const workerCount = Math.min(1, runs.length);

  async function loadNextRunImage() {
    const index = nextIndex;
    nextIndex += 1;
    if (index >= runs.length) {
      return;
    }
    const run = runs[index];
    const tr = getRunRow(run.run_id);
    try {
      const data = await apiFetch(`${apiBase}/runs/${encodeURIComponent(run.run_id)}/images`);
      visibleImages.set(run.run_id, data.images);
      visibleImageOffsets.set(run.run_id, 0);
      renderImagesForRun(tr, data.images);
    } catch (error) {
      errorCount += 1;
      if (tr) {
        tr.querySelectorAll(".image-slot").forEach((slot) => {
          slot.innerHTML = `<div class="image-error">${escapeHtml(error.message)}</div>`;
        });
      }
    }
    updateSubmitState();
    await loadNextRunImage();
  }

  await Promise.all(Array.from({ length: workerCount }, () => loadNextRunImage()));
  return errorCount;
}

function renderImagesForRun(tr, images) {
  if (!tr) {
    return;
  }

  const runId = tr.dataset.runId;
  const offset = visibleImageOffsets.get(runId) || 0;
  for (let viewIndex = 0; viewIndex < 3; viewIndex += 1) {
    const slot = tr.querySelector(`.image-slot[data-view-index="${viewIndex}"]`);
    if (!slot) {
      continue;
    }
    const image = images[offset + viewIndex];
    if (!image) {
      slot.innerHTML = '<div class="empty image-empty">No image loaded.</div>';
      continue;
    }
    const draftSelection = draftSelections.get(image.id);
    const checkedStatus = draftSelection?.status || image.status;
    const notesValue = draftSelection?.notes ?? image.notes ?? "";
    slot.innerHTML = `
      <div class="thumb-card" data-image-id="${image.id}">
        <img src="${image.file_url}" alt="Run image ${image.image_index + 1}" loading="lazy">
        <div class="thumb-name" title="${escapeHtml(image.member_name)}">${escapeHtml(imageBasename(image.member_name))}</div>
        <div class="thumb-cache">${escapeHtml(cachedText(image))}</div>
        <div class="thumb-actions">
          <label><input type="radio" name="status-${image.id}" value="pass" ${checkedStatus === "pass" ? "checked" : ""}> Pass</label>
          <label><input type="radio" name="status-${image.id}" value="fail" ${checkedStatus === "fail" ? "checked" : ""}> Fail</label>
        </div>
        <label class="thumb-notes-label">
          Notes
          <textarea name="notes-${image.id}" rows="3" placeholder="Optional notes">${escapeHtml(notesValue)}</textarea>
        </label>
      </div>
    `;
    const img = slot.querySelector("img");
    img.addEventListener("click", () => openZoomForRun(runId, image.id));
    slot.querySelectorAll(`input[name="status-${image.id}"]`).forEach((input) => input.addEventListener("change", () => {
      setDraftSelection(image.id, { status: input.value });
      markRunDraft(tr);
      updateSubmitState();
    }));
    const notesField = slot.querySelector(`textarea[name="notes-${image.id}"]`);
    notesField.addEventListener("input", () => {
      setDraftSelection(image.id, { notes: notesField.value });
      markRunDraft(tr);
      updateSubmitState();
    });
  }
  updateRunImageControls(tr, images);
}

function updateRunImageControls(tr, images) {
  const runId = tr.dataset.runId;
  const offset = visibleImageOffsets.get(runId) || 0;
  const start = images.length ? offset + 1 : 0;
  const end = Math.min(offset + 3, images.length);
  const label = tr.querySelector(".image-window-label");
  const prevButton = tr.querySelector('[data-action="prev-run-images"]');
  const nextButton = tr.querySelector('[data-action="next-run-images"]');
  const imageCountLabel = tr.querySelector(".image-count-label");
  if (label) {
    label.textContent = `${start}-${end}/${images.length}`;
  }
  if (prevButton) {
    prevButton.disabled = offset <= 0;
  }
  if (nextButton) {
    nextButton.disabled = offset + 3 >= images.length;
  }
  if (imageCountLabel) {
    imageCountLabel.textContent = `${images.length} images loaded`;
  }
}

function moveRunImages(runId, delta) {
  const images = visibleImages.get(runId) || [];
  const tr = getRunRow(runId);
  if (!images.length || !tr) {
    return;
  }
  const current = visibleImageOffsets.get(runId) || 0;
  const maxOffset = Math.max(0, images.length - 3);
  const nextOffset = Math.max(0, Math.min(maxOffset, current + delta));
  visibleImageOffsets.set(runId, nextOffset);
  renderImagesForRun(tr, images);
}

async function refreshRunImages(runId) {
  const tr = getRunRow(runId);
  if (!tr) {
    return;
  }
  const button = tr.querySelector('[data-action="refresh-images"]');
  if (button) {
    button.disabled = true;
    button.textContent = "Loading...";
  }
  setMessage(`Loading 3 more images for ${runId}...`);
  try {
    const data = await apiFetch(`${apiBase}/runs/${encodeURIComponent(runId)}/refresh-images`, { method: "POST" });
    visibleImages.set(runId, data.images);
    visibleImageOffsets.set(runId, Math.max(0, data.images.length - 3));
    renderImagesForRun(tr, data.images);
    setMessage(`${runId} now has ${data.images.length} cached images. Use ‹ / › to browse them.`);
  } catch (error) {
    setMessage(`Could not refresh images for ${runId}: ${error.message}`);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Refresh Images";
    }
  }
}

function setRunStatus(runId, status) {
  const images = visibleImages.get(runId) || [];
  const tr = getRunRow(runId);
  if (!images.length) {
    setMessage(`Images for ${runId} are still loading. Try again in a moment.`);
    return;
  }

  for (const image of images) {
    const input = document.querySelector(`input[name="status-${image.id}"][value="${status}"]`);
    setDraftSelection(image.id, { status });
    if (input) {
      input.checked = true;
    }
  }
  if (tr) {
    markRunDraft(tr);
    setDraftRunStatus(tr, status);
    setDraftValidationCount(tr, images.length);
  }
  updateSubmitState();
  setMessage(`${runId} all ${images.length} loaded images marked ${status} in draft. Click Submit Visible Validations to save.`);
}

async function submitValidation() {
  const items = collectValidationItems();
  if (!items.length) {
    setMessage("Choose pass/fail for at least one visible image before submitting.");
    return;
  }

  submitButton.disabled = true;
  setMessage(`Saving ${items.length} validation results...`);
  try {
    const data = await apiFetch(`${apiBase}/validations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    applySubmittedValidations(items);
    setMessage(`Saved ${data.saved} validation results.`);
  } catch (error) {
    setMessage(`Could not save validations: ${error.message}`);
    updateSubmitState();
  }
}

function applySubmittedValidations(items) {
  const submittedByImageId = new Map(items.map((item) => [Number(item.image_id), item]));
  for (const [runId, images] of visibleImages.entries()) {
    let touched = false;
    for (const image of images) {
      const submitted = submittedByImageId.get(Number(image.id));
      if (submitted) {
        image.status = submitted.status;
        image.notes = submitted.notes || "";
        touched = true;
      }
    }
    if (!touched) {
      continue;
    }
    const tr = getRunRow(runId);
    if (tr) {
      setSavedRunStatus(tr, images);
      renderImagesForRun(tr, images);
    }
  }
  draftSelections.clear();
  updateSubmitState();
}

function collectValidationItems() {
  return Array.from(draftSelections.entries())
    .filter(([, draft]) => draft.status)
    .map(([imageId, draft]) => ({ image_id: imageId, status: draft.status, notes: draft.notes || "" }));
}

function updateSubmitState() {
  const count = collectValidationItems().length;
  draftCount.textContent = `${count} selected`;
  submitButton.disabled = count === 0;
}

function updatePager() {
  prevButton.disabled = currentPage <= 1;
  nextButton.disabled = totalPages === 0 || currentPage >= totalPages;
}

function resetTable() {
  currentOffset = 0;
  currentTotal = 0;
  currentPage = 1;
  totalPages = 0;
  pageInput.value = "1";
  visibleRuns = [];
  visibleImages = new Map();
  visibleImageOffsets = new Map();
  draftSelections = new Map();
  runCount.textContent = "";
  runsTableBody.innerHTML = '<tr><td colspan="5" class="empty">Click Load Runs.</td></tr>';
  prevButton.disabled = true;
  nextButton.disabled = true;
  updateSubmitState();
}

function renderLoadingRows() {
  runsTableBody.innerHTML = '<tr><td colspan="5" class="empty">Loading...</td></tr>';
}

function markRunDraft(tr) {
  tr.classList.add("draft");
}

function setDraftRunStatus(tr, status) {
  tr.classList.remove("status-pending", "status-partial", "status-pass", "status-fail", "draft-pass", "draft-fail");
  tr.classList.add(`draft-${status}`);
  const label = tr.querySelector(".status-label");
  if (label) {
    label.textContent = `${status} draft`;
  }
}

function setDraftValidationCount(tr, count) {
  const label = tr.querySelector(".validation-count-label");
  if (label) {
    label.textContent = `${count}/${count} validated draft`;
  }
}

function setSavedRunStatus(tr, images) {
  const validatedCount = images.filter((image) => image.status).length;
  const hasFail = images.some((image) => image.status === "fail");
  const status = hasFail ? "fail" : validatedCount >= images.length ? "pass" : validatedCount > 0 ? "partial" : "pending";

  tr.classList.remove(
    "draft",
    "draft-pass",
    "draft-fail",
    "status-pending",
    "status-partial",
    "status-pass",
    "status-fail",
  );
  tr.classList.add(`status-${status}`);

  const statusLabel = tr.querySelector(".status-label");
  const validationLabel = tr.querySelector(".validation-count-label");
  if (statusLabel) {
    statusLabel.textContent = status;
  }
  if (validationLabel) {
    validationLabel.textContent = `${validatedCount}/${images.length} validated`;
  }
}

function setDraftSelection(imageId, updates) {
  const existing = draftSelections.get(imageId);
  const next = {
    status: existing?.status,
    notes: existing?.notes ?? "",
    ...updates,
  };

  if (!next.status && !next.notes) {
    draftSelections.delete(imageId);
    return;
  }

  draftSelections.set(imageId, next);
}

function openZoomForRun(runId, imageId) {
  zoomImages = visibleImages.get(runId) || [];
  zoomIndex = Math.max(0, zoomImages.findIndex((image) => image.id === imageId));
  renderZoomImage();
  zoomDialog.showModal();
}

function moveZoom(direction) {
  if (!zoomImages.length) {
    return;
  }
  const nextIndex = zoomIndex + direction;
  if (nextIndex < 0 || nextIndex >= zoomImages.length) {
    return;
  }
  zoomIndex = nextIndex;
  renderZoomImage();
}

function renderZoomImage() {
  const image = zoomImages[zoomIndex];
  if (!image) {
    return;
  }
  zoomImage.src = image.file_url;
  zoomImage.alt = imageBasename(image.member_name);
  zoomCaption.textContent = `${zoomIndex + 1}/${zoomImages.length} · ${imageBasename(image.member_name)}`;
  zoomPrevButton.disabled = zoomIndex === 0;
  zoomNextButton.disabled = zoomIndex === zoomImages.length - 1;
}

function pageLimit() {
  return Number(limitSelect.value || 10);
}

function imageBasename(name) {
  return String(name || "").split("/").pop() || String(name || "");
}

function cachedText(image) {
  return image.cached_at ? "cached locally" : "cache pending";
}

function pageNumber() {
  const parsed = Number(pageInput.value || 1);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 1;
}

function selectedBatchLabel() {
  return batchSelect.value === "all" ? "all batches" : batchSelect.value;
}

function applyQueryParams() {
  const params = new URLSearchParams(window.location.search);
  setIfPresent(batchSelect, params.get("batch"));
  setIfPresent(runFilter, params.get("run_id"));
  setIfPresent(statusFilter, params.get("status"));
  setIfPresent(limitSelect, params.get("limit"));
  setIfPresent(pageInput, params.get("page"));
  if (batchSelect.value) {
    loadRuns(pageNumber()).catch((error) => setMessage(error.message));
  }
}

function updateUrl() {
  const params = new URLSearchParams();
  params.set("batch", batchSelect.value);
  if (runFilter.value.trim()) {
    params.set("run_id", runFilter.value.trim());
  }
  params.set("status", statusFilter.value);
  params.set("limit", String(pageLimit()));
  params.set("page", String(currentPage));
  window.history.replaceState(null, "", `?${params.toString()}`);
}

function setIfPresent(element, value) {
  if (value !== null && value !== "") {
    element.value = value;
  }
}

function rowId(runId) {
  return `run-${runId.replaceAll("_", "-")}`;
}

function getRunRow(runId) {
  for (const row of runsTableBody.querySelectorAll("tr[data-run-id]")) {
    if (row.dataset.runId === runId) {
      return row;
    }
  }
  return null;
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
    const detail = data && data.detail ? data.detail : response.statusText;
    throw new Error(detail);
  }
  return data;
}

function setMessage(text) {
  message.textContent = text;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
