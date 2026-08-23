/** 设置页：账本、AI、数据与共享、完整备份、版本与帮助。 */
import { Share } from "@capacitor/share";
import { Directory, Filesystem } from "@capacitor/filesystem";
import JSZip from "jszip";
import { app, type App } from "../app";
import { AI_PROVIDER_BASES, testConnection } from "../domain/ai";
import {
  applyImport,
  exportSharePackage,
  parseSharePackage,
  previewImport,
  type ImportDecisions,
  type ImportPreview,
  type SharePackage,
  ShareError,
} from "../domain/share";
import { DB_FILE } from "../db/database";
import { pickZipFile } from "../platform/images";
import { isNative } from "../platform/storage";
import { $, el, confirmDialog, escapeHtml, fmtMoney, openSheet, closeSheet } from "./dom";

export function openSettingsOverlay(ctx: App) {
  $("#overlay-settings").classList.remove("hidden");
  document.body.classList.add("overlay-open");
  renderSettings(ctx);
}

export function renderSettings(ctx: App) {
  const body = $("#settings-body");
  if (body.dataset.bound !== "1") {
    body.dataset.bound = "1";
    $("#settings-save").addEventListener("click", () => saveSettings(ctx));
  }
  body.innerHTML = "";
  const cfg = ctx.getConfig();
  const status = ctx.repo.syncStatus();

  // ---- 账本 ----
  const ledgerGroup = el("div", { class: "settings-group" });
  ledgerGroup.append(el("h3", {}, ["账本"]));
  const initialBalance = el("input", {
    id: "set-initial-balance", type: "number", inputmode: "decimal", step: "0.01",
    value: String(cfg.initial_balance),
  });
  const initialDate = el("input", {
    id: "set-initial-date", type: "date", value: cfg.initial_balance_date || "",
  });
  const budget = el("input", {
    id: "set-budget", type: "number", inputmode: "decimal", step: "0.01", min: "0",
    value: String(cfg.monthly_budget),
  });
  const ratio = el("input", {
    id: "set-ratio", type: "number", inputmode: "decimal", step: "0.05", min: "0", max: "1",
    value: String(cfg.auto_save_ratio),
  });
  const cooldown = el("input", {
    id: "set-cooldown", type: "number", inputmode: "numeric", min: "0",
    value: String(cfg.cooldown_days),
  });
  const deviceName = el("input", { id: "set-device-name", value: cfg.device_name || "手机" });
  ledgerGroup.append(
    el("div", { class: "form-row" }, [el("label", {}, ["初始余额（元）"]), initialBalance]),
    el("div", { class: "form-row" }, [el("label", {}, ["初始余额日期"]), initialDate]),
    el("div", { class: "form-row" }, [el("label", {}, ["月预算（元，0 表示不设预算）"]), budget]),
    el("div", { class: "form-row" }, [el("label", {}, ["收入自动存比例（0~1）"]), ratio]),
    el("div", { class: "form-row" }, [el("label", {}, ["冷静期天数"]), cooldown]),
    el("div", { class: "form-row" }, [el("label", {}, ["本机设备名称"]), deviceName]),
  );
  body.append(ledgerGroup);

  // ---- AI ----
  const aiGroup = el("div", { class: "settings-group" });
  aiGroup.append(el("h3", {}, ["AI（文字解析 / 图片识别）"]));
  const provider = el("select", { id: "set-provider" });
  for (const name of ["DeepSeek", "OpenAI", "Qwen", "自定义"]) {
    provider.append(el("option", { value: name }, [name]));
  }
  provider.value = AI_PROVIDER_BASES[cfg.ai_provider] !== undefined ? cfg.ai_provider : "自定义";
  const apiBase = el("input", { id: "set-api-base", type: "text", value: cfg.api_base });
  const apiKey = el("input", { id: "set-api-key", type: "password", value: cfg.api_key, placeholder: "sk-..." });
  const modelText = el("input", { id: "set-model-text", value: cfg.model_text });
  const modelVision = el("input", { id: "set-model-vision", value: cfg.model_vision });
  const testBtn = el("button", { class: "btn ghost", id: "set-test-ai" }, ["测试连接"]);
  const testResult = el("p", { class: "muted small", id: "set-ai-result" });
  provider.addEventListener("change", () => {
    const base = AI_PROVIDER_BASES[provider.value];
    if (base) apiBase.value = base;
  });
  testBtn.addEventListener("click", async () => {
    const base = apiBase.value.trim();
    const key = apiKey.value.trim();
    const model = modelText.value.trim();
    testResult.textContent = `连接测试中…（${base || "未填 Base"} · ${model || "未填模型"}）`;
    try {
      await testConnection({ api_base: base, api_key: key, model });
      testResult.textContent = "连接成功 ✓";
    } catch (e) {
      testResult.textContent = `连接失败：${e instanceof Error ? e.message : String(e)}`;
    }
  });
  aiGroup.append(
    el("div", { class: "form-row" }, [el("label", {}, ["服务商"]), provider]),
    el("div", { class: "form-row" }, [el("label", {}, ["API Base"]), apiBase]),
    el("div", { class: "form-row" }, [el("label", {}, ["API Key（只保存在本机）"]), apiKey]),
    el("div", { class: "form-row" }, [el("label", {}, ["文本模型"]), modelText]),
    el("div", { class: "form-row" }, [el("label", {}, ["视觉模型"]), modelVision]),
    el("div", { class: "btn-row" }, [testBtn, testResult]),
    el("p", { class: "muted small" }, [
      "提示：Key 属于哪家服务商，就把 Base 和模型填成哪家的（与电脑端「设置 → AI」保持一致即可）。",
    ]),
  );
  body.append(aiGroup);

  // ---- 数据与共享 ----
  const shareGroup = el("div", { class: "settings-group" });
  shareGroup.append(el("h3", {}, ["数据与共享（与电脑端交换账本）"]));
  shareGroup.append(
    el("p", { class: "muted small" }, [
      status.pending_changes > 0
        ? `本机「${status.device_name}」：有 ${status.pending_changes} 处待同步变化`
        : `本机「${status.device_name}」：已同步` +
          (status.last_import_at ? ` · 上次导入 ${status.last_import_at.slice(5, 16)}` : ""),
    ]),
  );
  const exportBtn = el("button", { class: "btn ghost", id: "share-export-btn" }, ["导出共享包（发到电脑）"]);
  const importBtn = el("button", { class: "btn ghost", id: "share-import-btn" }, ["导入共享包（来自电脑）"]);
  exportBtn.addEventListener("click", () => exportShare(ctx));
  importBtn.addEventListener("click", () => importShare(ctx));
  shareGroup.append(el("div", { class: "btn-row" }, [exportBtn, importBtn]));
  const events = ctx.repo.listSyncEvents(8);
  if (events.length) {
    shareGroup.append(el("h3", { style: "margin-top:14px" }, ["最近同步记录"]));
    for (const event of events) {
      const when = event.direction === "export" ? event.exported_at : event.imported_at;
      shareGroup.append(
        el("div", { class: "win-row" }, [
          el("span", { class: "muted small" }, [`${event.direction === "export" ? "导出" : "导入"} ${when}`]),
          el("span", { class: "small" }, [
            `${event.result || ""}${event.conflict_dates ? `（冲突：${event.conflict_dates}）` : ""}`,
          ]),
        ]),
      );
    }
  }
  shareGroup.append(
    el("p", { class: "muted small" }, [
      "共享包不含 API Key、总结正文与图片原件；重复导入不会产生重复记录。",
    ]),
  );
  body.append(shareGroup);

  // ---- 完整备份 ----
  const backupGroup = el("div", { class: "settings-group" });
  backupGroup.append(el("h3", {}, ["完整备份（换机 / 恢复）"]));
  const includeImages = el("input", { id: "set-backup-images", type: "checkbox" });
  const includeLabel = el("label", { class: "small", style: "display:flex;gap:6px;align-items:center" }, [
    includeImages, " 包含图片原件（默认不包含，体积更小）",
  ]);
  const backupBtn = el("button", { class: "btn ghost", id: "backup-create" }, ["创建完整备份 ZIP"]);
  const restoreBtn = el("button", { class: "btn ghost", id: "backup-restore" }, ["恢复完整备份 ZIP"]);
  backupBtn.addEventListener("click", () => createFullBackup(ctx, includeImages.checked));
  restoreBtn.addEventListener("click", () => restoreFullBackup(ctx));
  backupGroup.append(includeLabel, el("div", { class: "btn-row" }, [backupBtn, restoreBtn]));
  body.append(backupGroup);

  // ---- 版本与帮助 ----
  const aboutGroup = el("div", { class: "settings-group" });
  aboutGroup.append(el("h3", {}, ["版本与帮助"]));
  aboutGroup.append(
    el("p", { class: "muted small" }, [
      "Better-money 1.1.1 · 手机端独立本地账本，不依赖云端。",
    ]),
    el("p", { class: "muted small" }, [
      "GitHub Releases（APK 与 SHA-256 校验值）：",
      el("br"),
      el("a", { href: "https://github.com/xianyunqingzhu/Better-money/releases", target: "_blank", rel: "noopener" }, [
        "https://github.com/xianyunqingzhu/Better-money/releases",
      ]),
    ]),
    el("p", { class: "muted small" }, [
      "数据全部保存在本机应用目录：数据库、图片原件与备份。卸载应用会清空数据，换机前请先导出完整备份。",
    ]),
  );
  body.append(aboutGroup);
}

async function saveSettings(ctx: App) {
  const provider = ($("#set-provider") as HTMLSelectElement).value;
  const patch = {
    initial_balance: Math.round(Number(($("#set-initial-balance") as HTMLInputElement).value || 0) * 100) / 100,
    initial_balance_date: ($("#set-initial-date") as HTMLInputElement).value,
    monthly_budget: Math.round(Number(($("#set-budget") as HTMLInputElement).value || 0) * 100) / 100,
    auto_save_ratio: Number(($("#set-ratio") as HTMLInputElement).value || 0),
    cooldown_days: Number(($("#set-cooldown") as HTMLInputElement).value || 7),
    device_name: ($("#set-device-name") as HTMLInputElement).value.trim() || "手机",
    ai_provider: provider,
    api_base: ($("#set-api-base") as HTMLInputElement).value.trim(),
    api_key: ($("#set-api-key") as HTMLInputElement).value.trim(),
    model_text: ($("#set-model-text") as HTMLInputElement).value.trim(),
    model_vision: ($("#set-model-vision") as HTMLInputElement).value.trim(),
  };
  await ctx.repo.updateConfig(patch as never);
  await ctx.repo.save();
  await ctx.refreshAll();
  app.toast("设置已保存", "success");
}

// ---------- 共享 ----------

async function exportShare(ctx: App) {
  try {
    const result = await exportSharePackage(ctx.repo);
    if (isNative()) {
      await Filesystem.writeFile({
        path: `backups/${result.filename}`,
        directory: Directory.Data,
        data: bytesToB64(result.zip),
        recursive: true,
      });
      await Filesystem.writeFile({
        path: `share-tmp/${result.filename}`,
        directory: Directory.Cache,
        data: bytesToB64(result.zip),
        recursive: true,
      });
      await Share.share({
        title: "Better-money 共享包",
        text: "Better-money 共享包：请在电脑端「设置 → 数据与共享」导入。",
      }).catch(() => undefined);
      app.toast("共享包已导出（已保存到应用备份目录）", "success");
    } else {
      const blob = new Blob([result.zip as unknown as BlobPart], { type: "application/zip" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result.filename;
      a.click();
      URL.revokeObjectURL(url);
    }
    await ctx.repo.save();
    await ctx.refreshAll();
  } catch (e) {
    app.toast(`导出失败：${e instanceof Error ? e.message : String(e)}`, "error");
  }
}

function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

let pendingPackage: SharePackage | null = null;

async function importShare(ctx: App) {
  try {
    const file = await pickZipFile();
    if (!file) return;
    pendingPackage = await parseSharePackage(file.bytes);
    const preview = previewImport(ctx.repo, pendingPackage);
    renderSharePreview(ctx, preview);
    openSheet("#share-sheet");
  } catch (e) {
    app.toast(
      e instanceof ShareError ? e.message : `导入失败：${e instanceof Error ? e.message : String(e)}`,
      "error",
    );
  }
}

function renderSharePreview(ctx: App, preview: ImportPreview) {
  const box = $("#share-preview");
  box.innerHTML = "";
  const pkg = preview.package;
  box.append(
    el("p", { class: "muted small" }, [
      `来自「${pkg.device_name}」（${pkg.platform === "mobile" ? "手机" : "电脑"}）· ` +
        `交易 ${pkg.counts.transactions} 笔、目标 ${pkg.counts.goals} 个 · ${pkg.date_min || "—"} ~ ${pkg.date_max || "—"}`,
    ]),
  );
  const changes: string[] = [];
  const s = preview.summary;
  if (s.add_transactions) changes.push(`新增交易 ${s.add_transactions}`);
  if (s.modify_transactions) changes.push(`修改交易 ${s.modify_transactions}`);
  if (s.delete_transactions) changes.push(`删除交易 ${s.delete_transactions}`);
  if (s.add_goals + s.modify_goals + s.delete_goals) changes.push(`目标变化 ${s.add_goals + s.modify_goals + s.delete_goals}`);
  box.append(el("p", { class: "small" }, [
    changes.length ? "将自动合并：" + changes.join("、") : "没有需要合并的变化。",
  ]));

  if (s.conflict_days.length || s.goal_conflicts.length) {
    box.append(el("p", { class: "small", style: "color:var(--warn)" }, ["存在需要人工处理的冲突。"]));
  }

  if (preview.settings.conflict) {
    box.append(
      el("div", { class: "share-day" }, [
        el("p", { class: "small", style: "margin-top:0" }, [
          `公开账本设置不同：本机 预算 ${preview.settings.local.monthly_budget} / 包内 预算 ${preview.settings.package.monthly_budget}`,
        ]),
        el("label", { class: "small" }, [
          el("input", { type: "radio", name: "share-settings", value: "keep_local", checked: "checked" }),
          " 保留本机设置（推荐）",
        ]),
        el("label", { class: "small", style: "margin-left:12px" }, [
          el("input", { type: "radio", name: "share-settings", value: "apply_package" }),
          " 应用包内设置",
        ]),
        el("p", { class: "muted small", style: "margin-bottom:0" }, [
          "「应用包内设置」只导入月预算、自动存比例与冷静期天数；" +
            "初始余额与起始日期不会自动导入（首次导入完成后如需采用会单独询问你）。",
        ]),
      ]),
    );
  }

  for (const day of s.conflict_days) {
    const dayBox = el("div", { class: "share-day", "data-day": day.date });
    const actions = el("div", { class: "share-day-head" });
    actions.append(el("b", {}, [day.date]));
    const modes: { mode: "keep_local" | "keep_peer" | "merge"; label: string }[] = [
      { mode: "keep_local", label: "保留本机这一天" },
      { mode: "keep_peer", label: "使用包内这一天" },
      { mode: "merge", label: "合并两边" },
    ];
    for (const [i, m] of modes.entries()) {
      const b = el("button", { class: `btn small ghost share-mode${i === 0 ? " active" : ""}`, "data-mode": m.mode }, [m.label]);
      b.addEventListener("click", () => {
        dayBox.querySelectorAll(".share-mode").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        itemsBox.classList.toggle("hidden", m.mode !== "merge");
      });
      actions.append(b);
    }
    dayBox.append(actions);
    const itemsBox = el("div", { class: "hidden" });
    for (const item of day.items) {
      const kindText =
        item.kind === "delete_vs_modify" ? "删除 vs 修改" :
        item.kind === "resurrect" ? "复活 vs 删除" : "两边都改过";
      const localText = item.local
        ? `${item.local.type} ${fmtMoney(Number(item.local.amount))} ${item.local.category}${item.local.merchant ? " @ " + item.local.merchant : ""}`
        : "本机：已删除";
      const peerText = item.peer
        ? `${item.peer.type} ${fmtMoney(Number(item.peer.amount))} ${item.peer.category}${item.peer.merchant ? " @ " + item.peer.merchant : ""}`
        : "包内：已删除";
      const select = el("select", { class: "share-item-choice", "data-uuid": item.uuid });
      for (const [value, label] of [["local", "保留本机"], ["peer", "采用包内"], ["drop", "都不保留"]] as const) {
        select.append(el("option", { value }, [label]));
      }
      itemsBox.append(
        el("div", { class: "share-item" }, [
          el("div", { class: "share-item-side" }, [`本机：${escapeHtml(localText)}`]),
          el("div", { class: "share-item-side" }, [`包内：${escapeHtml(peerText)}`]),
          el("div", { class: "muted small" }, [kindText]),
          select,
        ]),
      );
    }
    dayBox.append(itemsBox);
    box.append(dayBox);
  }

  for (const item of s.goal_conflicts) {
    const kindText =
      item.kind === "delete_vs_modify" ? "删除 vs 修改（必须选择）" :
      item.kind === "resurrect" ? "复活 vs 删除" : "两边都改过";
    const localText = item.local ? `${item.local.name} · 已存 ${fmtMoney(Number(item.local.saved))}（${item.local.status}）` : "本机：已删除";
    const peerText = item.peer ? `${item.peer.name} · 已存 ${fmtMoney(Number(item.peer.saved))}（${item.peer.status}）` : "包内：已删除";
    const select = el("select", { class: "share-goal-choice", "data-uuid": item.uuid });
    for (const [value, label] of [["local", "保留本机"], ["peer", "采用包内"], ["drop", "都不保留"]] as const) {
      const option = el("option", { value }, [label]);
      if (value === item.suggested) option.selected = true;
      select.append(option);
    }
    box.append(
      el("div", { class: "share-day" }, [
        el("div", { class: "share-item", style: "border-top:none;margin-top:0" }, [
          el("div", { class: "share-item-side" }, [`本机目标：${escapeHtml(localText)}`]),
          el("div", { class: "share-item-side" }, [`包内目标：${escapeHtml(peerText)}`]),
          el("div", { class: "muted small" }, [kindText]),
          select,
        ]),
      ]),
    );
  }

  for (const dupe of s.dupes) {
    box.append(
      el("div", { class: "share-day" }, [
        el("p", { class: "small", style: "margin-top:0" }, [
          `疑似重复：${dupe.date} ${fmtMoney(dupe.amount)} ${escapeHtml(dupe.merchant)}（本机已有相同记录）`,
        ]),
        el("label", { class: "small" }, [
          el("input", { type: "checkbox", class: "share-dupe-drop", "data-uuid": dupe.uuid }),
          " 丢弃包内这笔",
        ]),
      ]),
    );
  }

  const applyBtn = $("#share-apply");
  applyBtn.replaceWith(applyBtn.cloneNode(true));
  $("#share-apply").addEventListener("click", async () => {
    await doApplyShare(ctx);
  });
}

async function doApplyShare(ctx: App) {
  if (!pendingPackage) return;
  const decisions: ImportDecisions = {
    days: {},
    goals: {},
    dupes: {},
  };
  const settingsRadio = document.querySelector<HTMLInputElement>('input[name="share-settings"]:checked');
  decisions.settings = (settingsRadio?.value || "keep_local") as "keep_local" | "apply_package";
  document.querySelectorAll(".share-day[data-day]").forEach((dayEl) => {
    const date = (dayEl as HTMLElement).dataset.day!;
    const modeBtn = dayEl.querySelector(".share-mode.active") as HTMLElement | null;
    const mode = (modeBtn?.dataset.mode || "keep_local") as "keep_local" | "keep_peer" | "merge";
    const items: Record<string, "local" | "peer" | "drop"> = {};
    dayEl.querySelectorAll<HTMLSelectElement>(".share-item-choice").forEach((sel) => {
      items[sel.dataset.uuid!] = sel.value as "local" | "peer" | "drop";
    });
    decisions.days![date] = { mode, items };
  });
  document.querySelectorAll<HTMLSelectElement>(".share-goal-choice").forEach((sel) => {
    decisions.goals![sel.dataset.uuid!] = sel.value as "local" | "peer" | "drop";
  });
  document.querySelectorAll<HTMLInputElement>(".share-dupe-drop:checked").forEach((cb) => {
    decisions.dupes![cb.dataset.uuid!] = "drop";
  });
  try {
    // 首次导入：本机还没设初始余额日期，而包内有参考值时，单独询问是否采用
    const hadNoInitialDate = !ctx.config.initial_balance_date;
    const pkg = pendingPackage;
    const result = await applyImport(ctx.repo, pendingPackage, decisions);
    pendingPackage = null;
    closeSheet("#share-sheet");
    await ctx.afterDataChange();
    if (hadNoInitialDate && pkg.settings.initial_balance_date) {
      const adopted = await confirmDialog(
        `是否采用包内的初始余额 ¥${fmtMoney(pkg.settings.initial_balance)}（起始 ` +
          `${pkg.settings.initial_balance_date}）作为本机初始余额？\n\n` +
          "不采用的话，请稍后在「设置 → 账本」里手动填写，否则余额计算会不完整。",
      );
      if (adopted) {
        await ctx.repo.updateConfig({
          initial_balance: pkg.settings.initial_balance,
          initial_balance_date: pkg.settings.initial_balance_date,
        });
        await ctx.repo.save();
        await ctx.refreshAll();
        app.toast("已设置初始余额，余额已按电脑端口径计算", "success");
      }
    }
    renderSettings(ctx);
    app.toast(
      `导入完成：新增 ${result.add_transactions}、修改 ${result.modify_transactions}、删除 ${result.delete_transactions}`,
      "success",
    );
  } catch (e) {
    app.toast(e instanceof ShareError ? e.message : `导入失败：${String(e)}`, "error");
  }
}

// ---------- 完整备份 ----------

async function createFullBackup(ctx: App, includeImages: boolean) {
  try {
    const zip = new JSZip();
    const manifest = {
      format: "better-money-backup-mobile",
      format_version: 1,
      app_version: "1.1.1",
      schema_version: 3,
      created_at: new Date().toISOString(),
      includes_images: includeImages,
    };
    zip.file("manifest.json", JSON.stringify(manifest, null, 2));
    zip.file(DB_FILE, ctx.repo.db.raw().export());
    const cfg = ctx.getConfig();
    const sanitized = { ...cfg };
    delete (sanitized as { api_key?: string }).api_key;
    zip.file("data/config.json", JSON.stringify(sanitized, null, 2));
    if (includeImages && isNative()) {
      try {
        const entries = await Filesystem.readdir({ path: "images", directory: Directory.Data });
        for (const entry of entries.files) {
          const relative = `images/${entry.name}`;
          const content = await Filesystem.readFile({ path: relative, directory: Directory.Data });
          zip.file(`data/${relative}`, atob(content.data as string));
        }
      } catch {
        /* 无图片目录 */
      }
    }
    const bytes = await zip.generateAsync({ type: "uint8array", compression: "DEFLATE" });
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "").replace(/(\d{8})(\d{6})/, "$1-$2");
    const filename = `better-money-backup-${stamp}.zip`;
    if (isNative()) {
      await Filesystem.writeFile({
        path: `backups/${filename}`,
        directory: Directory.Data,
        data: bytesToB64(bytes),
        recursive: true,
      });
      await Share.share({ title: "Better-money 完整备份", text: "完整备份 ZIP（恢复用）" }).catch(() => undefined);
      app.toast(`完整备份已创建：${filename}`, "success");
    } else {
      const blob = new Blob([bytes as unknown as BlobPart], { type: "application/zip" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    }
  } catch (e) {
    app.toast(`备份失败：${e instanceof Error ? e.message : String(e)}`, "error");
  }
}

async function restoreFullBackup(ctx: App) {
  try {
    const file = await pickZipFile();
    if (!file) return;
    const zip = await JSZip.loadAsync(file.bytes);
    const manifestEntry = zip.file("manifest.json");
    if (!manifestEntry) throw new ShareError("这不是 Better-money 完整备份（缺少 manifest）");
    const manifest = JSON.parse(await manifestEntry.async("string"));
    if (manifest.format !== "better-money-backup-mobile") {
      throw new ShareError("这不是手机端完整备份；共享包请用「导入共享包」");
    }
    const dbEntry = zip.file(DB_FILE);
    if (!dbEntry) throw new ShareError("备份缺少数据库文件");
    if (!(await confirmDialog("恢复完整备份将覆盖本机当前全部账本数据，且不可撤销。确认继续？"))) return;
    const dbBytes = await dbEntry.async("uint8array");
    const { platformStorage } = await import("../platform/storage");
    const storage = platformStorage();
    await storage.write(DB_FILE, dbBytes);
    const configEntry = zip.file("data/config.json");
    if (configEntry) {
      await storage.write("data/config.json", await configEntry.async("uint8array"));
    }
    await ctx.reload();
    await ctx.refreshAll();
    app.toast("备份已恢复", "success");
  } catch (e) {
    app.toast(`恢复失败：${e instanceof Error ? e.message : String(e)}`, "error");
  }
}
