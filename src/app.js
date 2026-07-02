const steps = document.querySelectorAll(".step");
const screens = document.querySelectorAll(".screen");
const nextButtons = document.querySelectorAll("[data-next]");
const subpageButtons = document.querySelectorAll("[data-subpage]");
const sidepanelButtons = document.querySelectorAll("[data-sidepanel]");
const modalButtons = document.querySelectorAll("[data-open-modal]");
const closeModalButtons = document.querySelectorAll("[data-close-modal]");
const backButton = document.querySelector(".back-btn");
const backendProjects = document.querySelectorAll(".backend-project[data-project-type]");
const sourceEmptyState = document.getElementById("source-empty-state");
const sourceTabs = document.getElementById("source-tabs");
const projectLibrary = document.getElementById("project-library");
const selectedSourceState = document.getElementById("selected-source-state");
const selectedSourceLabel = document.getElementById("selected-source-label");
const selectedSourceTitle = document.getElementById("selected-source-title");
const selectedSourceMeta = document.getElementById("selected-source-meta");
const pickLocalSource = document.getElementById("pick-local-source");
const pickSingleVideoSource = document.getElementById("pick-single-video-source");
const backendSourceState = document.getElementById("backend-source-state");
const uploadDropzone = document.getElementById("upload-dropzone");
const uploadDropTitle = document.getElementById("upload-drop-title");
const uploadDropSubtitle = document.getElementById("upload-drop-subtitle");
const uploadListPanel = document.getElementById("upload-list-panel");
const uploadListCount = document.getElementById("upload-list-count");
const sourceSummary = document.getElementById("source-summary");
const analysisChoice = document.getElementById("analysis-choice");
const homeConfigRow = document.getElementById("home-config-row");
const analysisChoiceTip = document.getElementById("analysis-choice-tip");
const homeNormalAnalysis = document.getElementById("home-normal-analysis");
const homeAigcAnalysis = document.getElementById("home-aigc-analysis");
const footerUploadAction = document.getElementById("footer-upload-action");
const selectBackendProject = document.getElementById("select-backend-project");
const targetCountrySelect = document.getElementById("target-country-select");
const paramSummary = document.getElementById("param-summary");
const confirmSourceLabel = document.getElementById("confirm-source-label");
const confirmProjectTitle = document.getElementById("confirm-project-title");
const confirmEpisodeCount = document.getElementById("confirm-episode-count");
const confirmTargetCountry = document.getElementById("confirm-target-country");
const confirmFrameRatio = document.getElementById("confirm-frame-ratio");
const confirmResolution = document.getElementById("confirm-resolution");
const confirmStyleType = document.getElementById("confirm-style-type");
const confirmModelPreference = document.getElementById("confirm-model-preference");
const confirmStartUpload = document.getElementById("confirm-start-upload");
const sourceUploadInfo = document.getElementById("source-upload-info");
const sourceUploadCount = document.getElementById("source-upload-count");
const sourceAnalysisChoice = document.getElementById("source-analysis-choice");
const sourceAnalysisChoiceTip = document.getElementById("source-analysis-choice-tip");
const sourceNormalAnalysis = document.getElementById("source-normal-analysis");
const sourceAigcAnalysis = document.getElementById("source-aigc-analysis");
const storyProgressLabel = document.getElementById("story-progress-label");
const storyProgressBar = document.getElementById("story-progress-bar");
const assetProgressLabel = document.getElementById("asset-progress-label");
const assetProgressBar = document.getElementById("asset-progress-bar");
const assetProgressCopy = document.getElementById("asset-progress-copy");
const analysisModeCopy = document.getElementById("analysis-mode-copy");
const analysisCompleteAction = document.getElementById("analysis-complete-action");

let selectedSource = null;
let selectedBackendType = "ai";
let selectedLocalUploadMode = "folder";
let uploadTimers = [];
let analysisTimer = null;

function showScreen(id, updateHash = true) {
  const activeStep = id === "source" || id === "analysis" ? "home" : id;
  document.body.dataset.screen = id;
  screens.forEach((screen) => screen.classList.toggle("active", screen.id === id));
  steps.forEach((step) => step.classList.toggle("active", step.dataset.screen === activeStep));
  if (updateHash) history.replaceState(null, "", `#${id}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showSubpage(target) {
  const panel = document.getElementById(target);
  if (!panel) return;
  const screen = panel.closest(".screen") || document;
  const parentTab = target
    .replace("-detail", "-overview")
    .replace("asset-role-overview", "asset-role-overview")
    .replace("asset-scene-overview", "asset-scene-overview")
    .replace("asset-prop-overview", "asset-prop-overview");
  screen.querySelectorAll(".sub-panel").forEach((item) => item.classList.toggle("active", item.id === target));
  screen.querySelectorAll("[data-subpage]").forEach((item) => item.classList.toggle("active", item.dataset.subpage === target || item.dataset.subpage === parentTab));
  history.replaceState(null, "", `#${screen.id}:${target}`);
}

function showSidepanel(target) {
  const panel = document.getElementById(target);
  if (!panel) return;
  const sidebar = panel.closest(".asset-sidebar") || document;
  const screen = panel.closest(".screen") || document;
  sidebar.querySelectorAll(".asset-panel").forEach((item) => item.classList.toggle("active", item.id === target));
  sidebar.querySelectorAll("[data-sidepanel]").forEach((item) => item.classList.toggle("active", item.dataset.sidepanel === target));
  history.replaceState(null, "", `#${screen.id}:${target}`);
}

function openModal(id, updateHash = true) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  const currentScreen = document.body.dataset.screen || "home";
  if (updateHash) history.replaceState(null, "", `#${currentScreen}:${id}`);
}

function closeModal(updateHash = true) {
  document.querySelectorAll(".modal-overlay.open").forEach((modal) => {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  });
  if (updateHash) history.replaceState(null, "", `#${document.body.dataset.screen || "home"}`);
}

steps.forEach((step) => step.addEventListener("click", () => showScreen(step.dataset.screen)));
nextButtons.forEach((button) => button.addEventListener("click", () => {
  if (button.classList.contains("disabled")) return;
  showScreen(button.dataset.next);
}));
subpageButtons.forEach((button) => button.addEventListener("click", () => showSubpage(button.dataset.subpage)));
sidepanelButtons.forEach((button) => button.addEventListener("click", () => showSidepanel(button.dataset.sidepanel)));
modalButtons.forEach((button) => button.addEventListener("click", () => openModal(button.dataset.openModal)));
closeModalButtons.forEach((button) => button.addEventListener("click", closeModal));
backendProjects.forEach((card) => {
  card.addEventListener("click", () => {
    backendProjects.forEach((item) => item.classList.toggle("active", item === card));
    selectedBackendType = card.dataset.projectType || "ai";
  });
});

function clearUploadTimers() {
  uploadTimers.forEach((timer) => clearTimeout(timer));
  uploadTimers = [];
}

function showSourceMode(mode) {
  sourceTabs.querySelectorAll("[data-source-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.sourceMode === mode);
  });
  sourceEmptyState.hidden = mode !== "local";
  backendSourceState.hidden = mode !== "backend";
  selectedSourceState.hidden = true;
  footerUploadAction.classList.add("disabled");
  selectedSource = null;
}

function resetUploadFlow() {
  clearUploadTimers();
  clearInterval(analysisTimer);
  selectedSource = null;
  selectedLocalUploadMode = "folder";
  closeModal(false);
  sourceTabs.hidden = false;
  projectLibrary.hidden = false;
  showSourceMode("local");
  selectedSourceState.hidden = true;
  uploadDropzone.hidden = true;
  uploadListPanel.hidden = true;
  sourceSummary.hidden = true;
  analysisChoice.hidden = true;
  homeConfigRow.hidden = false;
  footerUploadAction.classList.add("disabled");
  document.querySelectorAll("[data-upload-row]").forEach((item) => { item.textContent = "等待上传"; });
  document.querySelectorAll("[data-upload-dot]").forEach((item) => { item.className = "dot blue"; });
  document.querySelectorAll("[data-upload-actions]").forEach((item) => { item.innerHTML = "<a>暂停</a>"; });
  document.querySelectorAll("[data-source-upload-row]").forEach((item) => { item.textContent = "等待上传"; });
  document.querySelectorAll("[data-source-upload-row]").forEach((item) => { item.closest("tr").hidden = false; });
  document.querySelectorAll("[data-source-upload-dot]").forEach((item) => { item.className = "dot blue"; });
  document.querySelectorAll("[data-source-upload-actions]").forEach((item) => { item.innerHTML = "<a>暂停</a>"; });
  uploadListCount.textContent = "全部 40　已上传 0　上传中 0　失败 0";
  sourceUploadCount.textContent = "全部 40　已上传 0　上传中 0　失败 0";
  sourceUploadInfo.textContent = "正在上传原片视频。上传完成后，再选择分析方式进入独立分析页面。";
  sourceAnalysisChoice.hidden = true;
  uploadDropTitle.textContent = "正在上传《六年后再遇首富前男友》原片视频";
  uploadDropSubtitle.textContent = "上传完成后再选择分析方式";
  storyProgressLabel.textContent = "0%";
  assetProgressLabel.textContent = "0%";
  storyProgressBar.style.width = "0%";
  assetProgressBar.style.width = "0%";
  analysisCompleteAction.classList.add("disabled");
  showScreen("home");
}

function setSelectedSource(source, backendType = null, localUploadMode = "folder") {
  selectedSource = source;
  selectedBackendType = backendType || selectedBackendType;
  selectedLocalUploadMode = localUploadMode;
  sourceTabs.hidden = false;
  projectLibrary.hidden = false;
  sourceEmptyState.hidden = true;
  backendSourceState.hidden = true;
  selectedSourceState.hidden = false;
  uploadDropzone.hidden = true;
  uploadListPanel.hidden = true;
  sourceSummary.hidden = true;
  analysisChoice.hidden = true;
  homeConfigRow.hidden = false;
  footerUploadAction.classList.remove("disabled");
  document.querySelectorAll("[data-upload-row]").forEach((item) => { item.textContent = "等待上传"; });
  document.querySelectorAll("[data-source-upload-row]").forEach((item) => { item.closest("tr").hidden = false; });
  uploadListCount.textContent = "全部 40　已上传 0　上传中 0　失败 0";
  if (source === "local") {
    selectedSourceLabel.textContent = "本地视频";
    selectedSourceTitle.textContent = "六年后再遇首富前男友";
    selectedSourceMeta.textContent = selectedLocalUploadMode === "single"
      ? "本地单个视频 · 1 集 · 1 个视频文件"
      : "本地文件夹 · 40 集 · 40 个视频文件";
  } else {
    const isAi = selectedBackendType === "ai";
    selectedSourceLabel.textContent = isAi ? "后台项目 · AI 剧" : "后台项目 · 真人剧";
    selectedSourceTitle.textContent = isAi ? "六年后再遇首富前男友" : "六年后再遇首富前男友-真人投放版";
    selectedSourceMeta.textContent = isAi
      ? "剧 ID：DRM-20260629-0916 · 40 集 · 可在上传后关联 AIGC 资产"
      : "剧 ID：DRM-20260621-0448 · 40 集 · 上传后走普通分析";
  }
}

function configureAnalysisChoice() {
  const canUseAigc = selectedSource === "backend" && selectedBackendType === "ai";
  analysisChoiceTip.textContent = canUseAigc
    ? "该后台项目为 AI 剧，上传完成后可关联 AIGC 资产资料，也可以选择普通分析。"
    : "当前素材无可复用 AIGC 资产，上传完成后走普通视频分析。";
  homeAigcAnalysis.classList.toggle("primary", canUseAigc);
  homeAigcAnalysis.classList.toggle("ghost", !canUseAigc);
  homeAigcAnalysis.classList.toggle("disabled", !canUseAigc);
  homeNormalAnalysis.classList.toggle("primary", !canUseAigc);
  homeNormalAnalysis.classList.toggle("ghost", canUseAigc);
  sourceAnalysisChoiceTip.textContent = canUseAigc
    ? "该后台项目为 AI 剧，上传完成后可关联 AIGC 资产资料，也可以选择普通分析。"
    : "当前素材无可复用 AIGC 资产，上传完成后走普通视频分析。";
  sourceAigcAnalysis.classList.toggle("primary", canUseAigc);
  sourceAigcAnalysis.classList.toggle("ghost", !canUseAigc);
  sourceAigcAnalysis.classList.toggle("disabled", !canUseAigc);
  sourceNormalAnalysis.classList.toggle("primary", !canUseAigc);
  sourceNormalAnalysis.classList.toggle("ghost", canUseAigc);
}

function getSelectedEpisodeCount() {
  return selectedSource === "local" && selectedLocalUploadMode === "single" ? 1 : 40;
}

function updateUploadConfirmModal() {
  const params = (paramSummary?.textContent || "16:9 · 720p · 写实通用")
    .split("·")
    .map((item) => item.trim());
  const episodeMatch = selectedSourceMeta.textContent.match(/(\d+)\s*集/);
  confirmSourceLabel.textContent = selectedSourceLabel.textContent || "未选择";
  confirmProjectTitle.textContent = selectedSourceTitle.textContent || "未选择项目";
  confirmEpisodeCount.textContent = episodeMatch ? `${episodeMatch[1]} 集` : "待确认";
  confirmTargetCountry.textContent = targetCountrySelect?.value || "美国";
  confirmFrameRatio.textContent = params[0] || "16:9";
  confirmResolution.textContent = params[1] || "720p";
  confirmStyleType.textContent = params[2] || "写实通用";
  confirmModelPreference.textContent = selectedSource === "backend" && selectedBackendType === "ai"
    ? "AIGC 资产优先复用 + 默认视频生成模型"
    : "默认视频生成模型";
}

function simulateUpload() {
  clearUploadTimers();
  showScreen("source");
  const totalEpisodes = getSelectedEpisodeCount();
  sourceAnalysisChoice.hidden = true;
  sourceUploadInfo.textContent = `正在上传《${selectedSourceTitle.textContent}》原片视频。上传完成后，再选择分析方式。`;
  const allRows = Array.from(document.querySelectorAll("[data-source-upload-row]"));
  const allDots = Array.from(document.querySelectorAll("[data-source-upload-dot]"));
  const allActionCells = Array.from(document.querySelectorAll("[data-source-upload-actions]"));
  allRows.forEach((row, index) => { row.closest("tr").hidden = index >= totalEpisodes; });
  const rows = allRows.slice(0, totalEpisodes);
  const dots = allDots.slice(0, totalEpisodes);
  const actionCells = allActionCells.slice(0, totalEpisodes);
  rows.forEach((row) => { row.textContent = "等待上传"; });
  dots.forEach((dot) => { dot.className = "dot blue"; });
  actionCells.forEach((cell) => { cell.innerHTML = "<a>暂停</a>"; });
  sourceUploadCount.textContent = `全部 ${totalEpisodes}　已上传 0　上传中 ${Math.min(totalEpisodes, 7)}　失败 0`;
  rows.forEach((row, index) => {
    uploadTimers.push(setTimeout(() => { row.textContent = `上传${35 + index * 9}%`; }, 250 + index * 130));
    uploadTimers.push(setTimeout(() => {
      if (totalEpisodes > 1 && index === 3) {
        row.textContent = "上传失败";
        dots[index].className = "dot red";
        actionCells[index].innerHTML = "<a data-retry-upload>重新上传</a><a class=\"danger\">删除</a>";
        return;
      }
      row.textContent = "上传成功";
      dots[index].className = "dot green";
      actionCells[index].innerHTML = "<a>重新上传</a><a class=\"danger\">删除</a>";
    }, 1200 + index * 180));
  });
  uploadTimers.push(setTimeout(() => {
    if (totalEpisodes === 1) {
      sourceUploadCount.textContent = "全部 1　已上传 1　上传中 0　失败 0";
      sourceUploadInfo.textContent = `《${selectedSourceTitle.textContent}》原片视频已上传完成，现在可以选择分析方式。`;
      sourceAnalysisChoice.hidden = false;
      configureAnalysisChoice();
      return;
    }
    sourceUploadCount.textContent = "全部 40　已上传 39　上传中 0　失败 1";
    sourceUploadInfo.textContent = `《${selectedSourceTitle.textContent}》有 1 集上传失败，请重新上传失败剧集后再选择分析方式。`;
  }, 2300));
}

function completeUploadAfterRetry() {
  const row = document.querySelector("[data-source-upload-row='4']");
  const dot = document.querySelector("[data-source-upload-dot='4']");
  const cell = document.querySelector("[data-source-upload-actions='4']");
  row.textContent = "重新上传中 45%";
  dot.className = "dot blue";
  cell.innerHTML = "<a>暂停</a>";
  sourceUploadCount.textContent = "全部 40　已上传 39　上传中 1　失败 0";
  setTimeout(() => {
    row.textContent = "上传成功";
    dot.className = "dot green";
    cell.innerHTML = "<a>重新上传</a><a class=\"danger\">删除</a>";
    sourceUploadCount.textContent = "全部 40　已上传 40　上传中 0　失败 0";
    sourceUploadInfo.textContent = `《${selectedSourceTitle.textContent}》原片视频已上传完成，现在可以选择分析方式。`;
    sourceAnalysisChoice.hidden = false;
    configureAnalysisChoice();
  }, 900);
}

function runAnalysis(mode) {
  const trigger = document.activeElement;
  if (trigger?.classList?.contains("disabled")) return;
  clearInterval(analysisTimer);
  closeModal(false);
  showScreen("analysis");
  const isAigc = mode === "aigc";
  analysisModeCopy.textContent = isAigc
    ? "系统正在关联 AIGC 资产资料，并同步分析剧情和镜头。"
    : "系统正在执行普通视频分析，通过抽帧、视觉理解和 OCR 解析资源。";
  assetProgressCopy.textContent = isAigc
    ? "优先复用 AIGC 平台中的原资产图和原资产描述，未命中的内容再通过视频分析补齐。"
    : "通过视频抽帧、视觉理解和 OCR 分析角色、场景、道具与画面文字。";
  analysisCompleteAction.classList.add("disabled");
  let story = 0;
  let asset = 0;
  const render = () => {
    storyProgressLabel.textContent = `${Math.min(story, 100)}%`;
    assetProgressLabel.textContent = `${Math.min(asset, 100)}%`;
    storyProgressBar.style.width = `${Math.min(story, 100)}%`;
    assetProgressBar.style.width = `${Math.min(asset, 100)}%`;
    if (story >= 100 && asset >= 100) {
      clearInterval(analysisTimer);
      analysisCompleteAction.classList.remove("disabled");
      analysisModeCopy.textContent = "分析完成，可以进入设定确认资产。";
    }
  };
  render();
  analysisTimer = setInterval(() => {
    story += 8;
    asset += isAigc ? 10 : 7;
    render();
  }, 180);
}

pickLocalSource?.addEventListener("click", () => setSelectedSource("local", null, "folder"));
pickSingleVideoSource?.addEventListener("click", () => setSelectedSource("local", null, "single"));
document.querySelectorAll("[data-source-mode]").forEach((button) => {
  button.addEventListener("click", () => showSourceMode(button.dataset.sourceMode || "local"));
});
selectBackendProject?.addEventListener("click", () => {
  const active = document.querySelector(".backend-project.active");
  setSelectedSource("backend", active?.dataset.projectType || "ai");
  closeModal();
});
footerUploadAction?.addEventListener("click", () => {
  if (footerUploadAction.classList.contains("disabled")) return;
  updateUploadConfirmModal();
  openModal("upload-confirm-modal");
});
confirmStartUpload?.addEventListener("click", () => {
  closeModal(false);
  simulateUpload();
});
document.addEventListener("click", (event) => {
  if (event.target.matches("[data-retry-upload]")) completeUploadAfterRetry();
});
document.querySelectorAll("[data-run-analysis]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.classList.contains("disabled")) return;
    runAnalysis(button.dataset.runAnalysis || "normal");
  });
});
backButton?.addEventListener("click", resetUploadFlow);

document.addEventListener("click", (event) => {
  document.querySelectorAll(".select-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
  document.querySelectorAll(".param-sheet[open]").forEach((sheet) => {
    if (!sheet.contains(event.target)) sheet.removeAttribute("open");
  });
  if (event.target.classList.contains("modal-overlay")) closeModal();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});

function applyHashRoute() {
  const route = location.hash.replace("#", "");
  if (!route) return;
  const [screenId, subpageId] = route.split(":");
  if (screenId && document.getElementById(screenId)) {
    showScreen(screenId, false);
    if (subpageId) {
      const target = document.getElementById(subpageId);
      if (target?.classList.contains("modal-overlay")) openModal(subpageId, false);
      else {
        closeModal(false);
        if (target?.classList.contains("asset-panel")) showSidepanel(subpageId);
        else showSubpage(subpageId);
      }
    } else {
      closeModal(false);
    }
  }
}

applyHashRoute();
window.addEventListener("hashchange", applyHashRoute);
