/** 记一笔抽屉：AI 文字解析 + 手动记账 + 编辑模式。 */
import { app, type App } from "../app";
import { parseText } from "../domain/ai";
import { todayIso } from "../domain/dates";
import type { TransactionRow } from "../domain/types";
import { $, el, openSheet, closeSheet } from "./dom";
import { openConfirmPanel } from "./confirm";
import { categoryOptions } from "./home";

export function openEntry(ctx: App) {
  renderEntryBody(ctx, null);
  openSheet("#entry-sheet");
}

export function openEntryForEdit(ctx: App, row: TransactionRow) {
  renderEntryBody(ctx, row);
  openSheet("#entry-sheet");
}

function renderEntryBody(ctx: App, editing: TransactionRow | null) {
  const body = $("#entry-body");
  body.innerHTML = "";
  $("#entry-title").textContent = editing ? "编辑记录" : "记一笔";

  const dateInput = el("input", {
    id: "entry-date",
    type: "date",
    value: editing ? editing.date : todayIso(),
  });
  const textArea = el("textarea", {
    id: "entry-text",
    placeholder: "例如：午饭食堂 15\n奶茶 12\n昨天兼职 200\n聚餐 200 4人AA",
  });
  const aiBtn = el("button", { class: "btn primary", id: "entry-ai" }, ["智能解析并确认"]);
  const aiHint = el("p", { class: "muted small" }, [
    "断网或没有 API Key 时用下面的手动表单；也可以用输入法语音先转文字再点解析。",
  ]);

  const typeSel = el("select", { id: "entry-type" });
  for (const t of ["支出", "收入", "退款", "取现", "转账", "还款"]) {
    typeSel.append(el("option", { value: t }, [t]));
  }
  const amountInput = el("input", {
    id: "entry-amount",
    type: "number",
    inputmode: "decimal",
    step: "0.01",
    min: "0.01",
    placeholder: "金额（元）",
  });
  const catSel = el("select", { id: "entry-category" });
  const fillCats = () => {
    catSel.innerHTML = "";
    for (const c of categoryOptions(typeSel.value)) {
      catSel.append(el("option", { value: c }, [c]));
    }
  };
  typeSel.addEventListener("change", fillCats);
  fillCats();
  const merchantInput = el("input", { id: "entry-merchant", placeholder: "商家（可选）" });
  const noteInput = el("input", { id: "entry-note", placeholder: "备注（可选）" });
  const submitBtn = el("button", { class: "btn primary", id: "entry-submit" }, [
    editing ? "保存修改" : "确认入账",
  ]);

  if (editing) {
    typeSel.value = editing.type;
    fillCats();
    amountInput.value = String(editing.amount);
    catSel.value = editing.category;
    merchantInput.value = editing.merchant;
    noteInput.value = editing.note;
    textArea.value = "";
  }

  aiBtn.addEventListener("click", async () => {
    const text = textArea.value.trim();
    if (!text) {
      app.toast("请先输入记账内容", "error");
      return;
    }
    aiBtn.textContent = "解析中…";
    aiBtn.disabled = true;
    try {
      const result = await parseText(ctx.config, text, dateInput.value || todayIso());
      if (!result.items.length) {
        ctx.repo.storePending(text);
        await ctx.repo.save();
        app.toast(
          result.questions.length
            ? `没有识别出可入账条目：${result.questions.join("；")}`
            : "没有识别出可入账条目",
          "error",
        );
        return;
      }
      closeSheet("#entry-sheet");
      openConfirmPanel(ctx, result.items, result.questions, "文字");
    } catch (e) {
      ctx.repo.storePending(text);
      await ctx.repo.save();
      app.toast(`解析失败：${e instanceof Error ? e.message : String(e)}，原文已保留，可手动记账`, "error");
    } finally {
      aiBtn.textContent = "智能解析并确认";
      aiBtn.disabled = false;
    }
  });

  submitBtn.addEventListener("click", async () => {
    const amount = Number(amountInput.value);
    if (!amount || amount <= 0) {
      app.toast("请填写有效金额", "error");
      return;
    }
    if (editing) {
      const ok = ctx.repo.patchTransaction(editing.id, {
        date: dateInput.value,
        amount: Math.round(amount * 100) / 100,
        type: typeSel.value as TransactionRow["type"],
        category: catSel.value,
        merchant: merchantInput.value.trim(),
        note: noteInput.value.trim(),
      });
      if (!ok) {
        app.toast("记录不存在或已删除", "error");
        return;
      }
      await ctx.afterDataChange();
      app.toast("已保存修改", "success");
      closeSheet("#entry-sheet");
      return;
    }
    ctx.repo.addTransaction({
      date: dateInput.value || todayIso(),
      amount: Math.round(amount * 100) / 100,
      type: typeSel.value,
      category: catSel.value,
      merchant: merchantInput.value.trim(),
      note: noteInput.value.trim(),
      source: "手动",
    });
    await ctx.afterDataChange();
    app.toast("已记下", "success");
    closeSheet("#entry-sheet");
  });

  body.append(
    el("div", { class: "form-row" }, [el("label", {}, ["日期（可补记）"]), dateInput]),
  );
  if (!editing) {
    body.append(
      el("div", { class: "form-row" }, [el("label", {}, ["文字记账（支持多笔 / AA / 收入）"]), textArea]),
      aiBtn,
      aiHint,
      el("hr"),
    );
  }
  body.append(
    el("div", { class: "field-grid" }, [typeSel, amountInput]),
    el("div", { class: "form-row" }, [el("label", {}, ["分类"]), catSel]),
    el("div", { class: "form-row" }, [el("label", {}, ["商家"]), merchantInput]),
    el("div", { class: "form-row" }, [el("label", {}, ["备注"]), noteInput]),
    submitBtn,
  );
}
