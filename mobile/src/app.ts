/** 应用状态与刷新编排。 */
import wasmUrl from "sql.js/dist/sql-wasm.wasm?url";
import { LedgerRepo } from "./db/repository";
import { todayIso } from "./domain/dates";
import type { AppConfig } from "./domain/types";
import { platformStorage } from "./platform/storage";
import { $, toast } from "./ui/dom";
import { renderHome } from "./ui/home";
import { renderHistory, renderInsights } from "./ui/insights";
import { renderGoalsOverlay, renderGoalProgress } from "./ui/goals";
import { renderSummaries } from "./ui/summaries";

export class App {
  repo!: LedgerRepo;
  config!: AppConfig;
  month = todayIso().slice(0, 7);
  initialized = false;

  async init() {
    this.repo = await LedgerRepo.open(platformStorage(), wasmUrl);
    this.config = this.repo.getConfig();
    this.initialized = true;
  }

  async reload() {
    this.repo.db.close();
    this.repo = await LedgerRepo.open(platformStorage(), wasmUrl);
    this.config = this.repo.getConfig();
  }

  getConfig(): AppConfig {
    return this.config;
  }

  async refreshAll() {
    if (!this.initialized) return;
    this.config = this.repo.getConfig();
    renderSyncPill(this);
    renderHome(this);
    renderInsights(this);
    renderGoalProgress(this);
  }

  async afterDataChange() {
    await this.repo.save();
    await this.refreshAll();
    renderGoalsOverlay(this);
    renderSummaries(this);
    renderHistory(this);
  }

  toast(message: string, type: "info" | "success" | "error" = "info") {
    toast(message, type);
  }
}

export const app = new App();

export function renderSyncPill(ctx: App) {
  const pill = $("#sync-pill");
  const status = ctx.repo.syncStatus();
  const last = status.last_import_at
    ? `上次导入 ${status.last_import_at.slice(5, 16)}`
    : "尚未导入";
  if (status.pending_changes > 0) {
    pill.textContent = `待同步 ${status.pending_changes}`;
    pill.classList.add("dirty");
  } else {
    pill.textContent = `已同步 · ${last}`;
    pill.classList.remove("dirty");
  }
}
