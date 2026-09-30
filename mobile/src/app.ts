/** 应用状态与刷新编排。 */
import wasmUrl from "sql.js/dist/sql-wasm.wasm?url";
import { LedgerRepo } from "./db/repository";
import { todayIso } from "./domain/dates";
import type { AppConfig } from "./domain/types";
import { platformStorage } from "./platform/storage";
import { toast } from "./ui/dom";
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
    this.month = todayIso().slice(0, 7);
    this.config = this.repo.getConfig();
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
