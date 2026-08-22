/** 总结页：区间选择、生成（覆盖确认）、列表、删除；账目变化后提示重新生成。 */
import { app, type App } from "../app";
import { chatSummary } from "../domain/ai";
import { addDays, todayIso } from "../domain/dates";
import { buildSummaryPrompt, gather, periodBounds } from "../domain/summaries";
import type { SummaryRow } from "../domain/types";
import { $, el, confirmDialog } from "./dom";

const TONES: Record<string, string> = {
  朋友: "像朋友聊天一样，口语化、真诚",
  毒舌: "毒舌吐槽风格，一针见血但善意",
  温柔: "温柔鼓励风格，包容不judge",
  老师: "像老师点评，客观中肯给建议",
};

const PRESETS: { label: string; type: "周" | "月"; anchor?: string; start?: string; end?: string }[] = [
  { label: "本周", type: "周" },
  { label: "上周", type: "周", anchor: addDays(todayIso(), -7) },
  { label: "本月", type: "月" },
  { label: "上月", type: "月", anchor: previousMonth(todayIso()) + "-01" },
];

function previousMonth(iso: string): string {
  const [year, month] = iso.slice(0, 7).split("-").map(Number);
  return month === 1 ? `${year - 1}-12` : `${year}-${String(month - 1).padStart(2, "0")}`;
}

export function renderSummaries(ctx: App) {
  const body = $("#summaries-body");
  if (body.dataset.bound !== "1") {
    body.dataset.bound = "1";
    body.innerHTML = `
      <div class="card">
        <h3>生成总结</h3>
        <div class="btn-row" id="summary-presets"></div>
        <div class="field-grid" style="margin-top:10px">
          <div class="form-row"><label>开始日期</label><input type="date" id="summary-start"></div>
          <div class="form-row"><label>结束日期</label><input type="date" id="summary-end"></div>
        </div>
        <p class="muted small" id="summary-ai-status"></p>
      </div>
      <div id="summary-list"></div>
    `;
    const presetBox = $("#summary-presets");
    for (const preset of PRESETS) {
      const btn = el("button", { class: "btn ghost", "data-preset": preset.label }, [preset.label]);
      btn.addEventListener("click", async () => {
        const anchor = preset.anchor || todayIso();
        const bounds = periodBounds(preset.type, anchor);
        ($("#summary-start") as HTMLInputElement).value = bounds.start;
        ($("#summary-end") as HTMLInputElement).value = bounds.end;
        await generateSummary(ctx, preset.type, bounds.start, bounds.end);
      });
      presetBox.append(btn);
    }
    const generateCustom = el("button", { class: "btn primary" }, ["生成自定义区间总结"]);
    generateCustom.addEventListener("click", async () => {
      const start = ($("#summary-start") as HTMLInputElement).value;
      const end = ($("#summary-end") as HTMLInputElement).value;
      if (!start || !end) {
        app.toast("请先选择起止日期", "error");
        return;
      }
      await generateSummary(ctx, "周", start, end);
    });
    presetBox.append(generateCustom);
  }
  renderSummaryList(ctx);
}

async function generateSummary(
  ctx: App,
  periodType: "周" | "月",
  start: string,
  end: string,
) {
  const status = $("#summary-ai-status");
  const existing = ctx.repo.listSummaries().find(
    (s) => s.period_type === periodType && s.period_start === start && s.period_end === end,
  );
  const overwrite = existing
    ? await confirmDialog("这个类型和区间已经有总结，是否覆盖重写？")
    : false;
  if (existing && !overwrite) return;

  const g = gather(
    ctx.config,
    periodType,
    start,
    end,
    ctx.repo.allTransactions(),
    ctx.repo.allGoals(),
    ctx.repo.allWins(),
  );
  const prompt = buildSummaryPrompt(g, ctx.config.tone, TONES[ctx.config.tone] || "");
  status.textContent = "生成中…（需要网络）";
  try {
    const content = await chatSummary(ctx.config, prompt);
    ctx.repo.upsertSummary({
      period_type: periodType,
      period_start: start,
      period_end: end,
      content,
      overwrite,
    });
    await ctx.afterDataChange();
    renderSummaries(ctx);
    app.toast("总结已生成", "success");
  } catch (e) {
    app.toast(`生成失败：${e instanceof Error ? e.message : String(e)}`, "error");
  } finally {
    status.textContent = "";
  }
}

function renderSummaryList(ctx: App) {
  const list = $("#summary-list");
  list.innerHTML = "";
  const rows = ctx.repo.listSummaries();
  if (!rows.length) {
    list.innerHTML = '<div class="empty-state">还没有总结。<br>选一个区间生成第一份吧。</div>';
    return;
  }
  for (const row of rows) {
    list.append(summaryCard(ctx, row));
  }
}

function summaryCard(ctx: App, row: SummaryRow): HTMLElement {
  const card = el("div", { class: "card" });
  const head = el("div", { class: "goal-card-head" });
  head.append(
    el("span", { class: "goal-card-name" }, [`${row.period_type}总结`]),
    el("span", { class: "muted small" }, [`${row.period_start} ~ ${row.period_end}`]),
  );
  const content = el("div", { style: "white-space:pre-wrap; margin-top:8px" }, [row.content]);
  const expired = el("p", { class: "muted small" }, [
    row.expired ? "⚠️ 账目已更新，建议重新生成（不会自动调用 AI）" : "",
  ]);
  const actions = el("div", { class: "btn-row", style: "margin-top:10px" });
  const regen = el("button", { class: "btn ghost" }, ["重新生成"]);
  regen.addEventListener("click", () =>
    generateSummary(ctx, row.period_type, row.period_start, row.period_end),
  );
  const del = el("button", { class: "btn ghost" }, ["删除"]);
  del.addEventListener("click", async () => {
    if (!(await confirmDialog("删除这份总结？（不影响账目）"))) return;
    ctx.repo.deleteSummary(row.id);
    await ctx.afterDataChange();
    renderSummaries(ctx);
  });
  actions.append(regen, del);
  card.append(head, content, expired, actions);
  return card;
}
