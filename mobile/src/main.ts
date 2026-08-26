/** 应用启动与事件接线。 */
import { app } from "./app";
import { parseImage } from "./domain/ai";
import { todayIso } from "./domain/dates";
import { readImageBytes, pickImages, takePhoto } from "./platform/images";
import { $, el, openSheet, closeSheet, openOverlay } from "./ui/dom";
import { openEntry } from "./ui/entry";
import { openConfirmPanel } from "./ui/confirm";
import { openOverlayGoals } from "./ui/goals";
import { openHistoryOverlay } from "./ui/insights";
import { renderSummaries } from "./ui/summaries";
import { openSettingsOverlay } from "./ui/settings";
import type { PickedImage } from "./platform/images";

async function bootstrap() {
  try {
    await app.init();
  } catch (e) {
    const appEl = $("#app");
    appEl.innerHTML = `<div class="empty-state" style="padding-top:80px">初始化失败：${
      e instanceof Error ? e.message : String(e)
    }</div>`;
    return;
  }
  bindEvents();
  await app.refreshAll();
  handleOnboarding();
}

function bindEvents() {
  // 两页滑动
  const pages = $("#pages");
  pages.addEventListener("scroll", () => {
    // 滑动到第二页后允许其内部滚动；页面本身不锁定
  });

  // 手动记账
  $("#manual-entry-btn").addEventListener("click", () => openEntry(app));

  // 第二页入口
  $("#go-entry-goals").addEventListener("click", () => openOverlayGoals(app));
  $("#go-entry-summaries").addEventListener("click", () => {
    openOverlay("#overlay-summaries");
    renderSummaries(app);
  });
  $("#go-entry-history").addEventListener("click", () => openHistoryOverlay(app));
  $("#go-entry-settings").addEventListener("click", () => openSettingsOverlay(app));

  // 图片入口
  $("#img-camera").addEventListener("click", () => startImageFlow("camera"));
  $("#img-gallery").addEventListener("click", () => startImageFlow("gallery"));

  // 抽屉与全屏返回按钮
  document.querySelectorAll("[data-close-sheet]").forEach((node) => {
    node.addEventListener("click", () => closeSheet(`#${(node as HTMLElement).dataset.closeSheet}`));
  });
  document.querySelectorAll("[data-close-overlay]").forEach((node) => {
    node.addEventListener("click", () => {
      openOverlay; // 占位保持引用（关闭用全局函数）
      const id = (node as HTMLElement).dataset.closeOverlay!;
      $(`#${id}`).classList.add("hidden");
      document.body.classList.remove("overlay-open");
    });
  });
}

let pendingImages: PickedImage[] = [];
let imageFlowDateDir = todayIso();

async function startImageFlow(source: "camera" | "gallery") {
  imageFlowDateDir = todayIso();
  pendingImages = [];
  try {
    if (source === "camera") {
      const photo = await takePhoto(imageFlowDateDir);
      if (photo) pendingImages.push(photo);
    } else {
      pendingImages.push(...(await pickImages(imageFlowDateDir, 10)));
    }
  } catch (e) {
    if (e instanceof Error && /cancel/i.test(e.message)) return;
    app.toast(`图片获取失败：${e instanceof Error ? e.message : String(e)}（可检查相机/相册权限）`, "error");
    return;
  }
  if (!pendingImages.length) return;
  renderImageSheet();
  openSheet("#image-sheet");
}

/** 追加照片：拍照或相册均保留已有选择，直到 10 张上限。 */
async function addMoreImages(source: "camera" | "gallery") {
  const remaining = 10 - pendingImages.length;
  if (remaining <= 0) {
    app.toast("最多 10 张，可先移除一些再添加", "error");
    return;
  }
  try {
    if (source === "camera") {
      const photo = await takePhoto(imageFlowDateDir);
      if (photo) pendingImages.push(photo);
    } else {
      pendingImages.push(...(await pickImages(imageFlowDateDir, remaining)));
    }
  } catch (e) {
    if (e instanceof Error && /cancel/i.test(e.message)) return;
    app.toast(`图片获取失败：${e instanceof Error ? e.message : String(e)}`, "error");
    return;
  }
  renderImageSheet();
}

function renderImageSheet() {
  const body = $("#image-sheet-body");
  body.innerHTML = "";
  const grid = el("div", { class: "thumb-grid" });
  for (const image of pendingImages) {
    const wrap = el("div", { class: "thumb-wrap" });
    const img = el("img", { class: "thumb", "data-path": image.path });
    wrap.append(img);
    const removeBtn = el("button", { class: "thumb-remove", type: "button" }, ["✕"]);
    removeBtn.addEventListener("click", () => {
      pendingImages = pendingImages.filter((i) => i.path !== image.path);
      renderImageSheet();
    });
    wrap.append(removeBtn);
    grid.append(wrap);
  }
  const addBtn = $("#image-add-more") as HTMLButtonElement;
  addBtn.textContent = pendingImages.length >= 10 ? "已达上限（10 张）" : "＋ 添加照片";
  addBtn.disabled = pendingImages.length >= 10;
  body.append(
    el("p", { class: "muted small" }, [`已选 ${pendingImages.length} / 10 张（相册支持长按多选）`]),
    grid,
    el("div", { class: "form-row", style: "margin-top:10px" }, [
      el("label", {}, ["日期（图片上没有时间时使用）"]),
      el("input", { type: "date", id: "image-date", value: todayIso() }),
    ]),
    el("div", { class: "form-row" }, [
      el("label", {}, ["补充说明（AA / 请客等，可选）"]),
      el("input", { type: "text", id: "image-note", placeholder: "例如：4人AA / 我请客" }),
    ]),
  );
  // 加载缩略图
  Promise.all(
    pendingImages.map(async (image) => {
      const bytes = await readImageBytes(image.path);
      const node = grid.querySelector(`[data-path="${image.path}"]`) as HTMLImageElement | null;
      if (node && bytes) {
        node.src = URL.createObjectURL(new Blob([bytes as unknown as BlobPart], { type: "image/jpeg" }));
      }
    }),
  ).catch(() => undefined);
}

async function recognizeImages() {
  const date = ($("#image-date") as HTMLInputElement).value || todayIso();
  const note = ($("#image-note") as HTMLInputElement).value.trim();
  const button = $("#image-recognize") as HTMLButtonElement;
  button.textContent = "识别中…";
  button.disabled = true;
  const items: Awaited<ReturnType<typeof parseImage>>["items"] = [];
  const questions: string[] = [];
  let failed = 0;
  let firstError = "";
  for (const image of pendingImages) {
    try {
      const bytes = await readImageBytes(image.path);
      if (!bytes) {
        failed += 1;
        continue;
      }
      const ext = "." + (image.path.split(".").pop() || "jpg");
      const result = await parseImage(app.config, bytes, ext, note, date);
      items.push(...result.items);
      questions.push(...result.questions);
    } catch (e) {
      failed += 1;
      if (!firstError) firstError = e instanceof Error ? e.message : String(e);
    }
  }
  button.textContent = "开始识别";
  button.disabled = false;
  closeSheet("#image-sheet");
  if (!items.length) {
    app.toast(
      `识别失败：${firstError || "没有识别出条目"}（图片已保留在本机，可重试或手动记账）`,
      "error",
    );
    return;
  }
  if (failed > 0) {
    app.toast(`有 ${failed} 张识别失败，已保留在本机`, "error");
  }
  openConfirmPanel(app, items, questions, "截图/小票");
  pendingImages = [];
}

async function handleOnboarding() {
  if (!app.config.onboarding_completed) {
    // 首次启动：提示一次，随即标记完成，之后不再出现
    app.toast("首次使用：可在「设置」里配置初始余额、预算与 AI Key", "info");
    await app.repo.updateConfig({ onboarding_completed: true });
    await app.repo.save();
    app.config = app.repo.getConfig();
  }
}

$("#image-recognize").addEventListener("click", recognizeImages);
$("#image-add-more").addEventListener("click", () => addMoreImages("gallery"));

bootstrap().catch((e) => console.error(e));
