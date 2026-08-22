/** AI 解析确认面板：逐条可改可删，确认后才入账。 */
import { app, type App } from "../app";
import type { ParsedItem } from "../domain/ai";
import { todayIso } from "../domain/dates";
import { VALID_TYPES } from "../domain/types";
import { $, el, openSheet, closeSheet } from "./dom";
import { categoryOptions } from "./home";

export function openConfirmPanel(
  ctx: App,
  items: ParsedItem[],
  questions: string[],
  source: string,
) {
  renderConfirmPanel(ctx, items, questions, source);
  openSheet("#confirm-sheet");
}

function renderConfirmPanel(
  ctx: App,
  items: ParsedItem[],
  questions: string[],
  source: string,
) {
  const body = $("#confirm-body");
  body.innerHTML = "";
  $("#confirm-title").textContent = source === "截图/小票" ? "确认识别结果" : "确认入账";

  if (questions.length) {
    body.append(
      el("div", { class: "budget-alert warn", style: "margin-bottom:10px" }, [
        "待确认：" + questions.join("；"),
      ]),
    );
  }

  const rows: HTMLElement[] = [];
  for (const [index, item] of items.entries()) {
    const card = el("div", { class: "confirm-item", "data-index": String(index) });
    const typeSel = el("select", {}, []);
    for (const t of VALID_TYPES) typeSel.append(el("option", { value: t }, [t]));
    typeSel.value = item.type;
    const amountInput = el("input", {
      type: "number",
      inputmode: "decimal",
      step: "0.01",
      value: String(item.amount),
    });
    const dateInput = el("input", { type: "date", value: item.date || todayIso() });
    const catSel = el("select", {}, []);
    const fillCats = () => {
      catSel.innerHTML = "";
      for (const c of categoryOptions(typeSel.value)) {
        catSel.append(el("option", { value: c }, [c]));
      }
      if (!categoryOptions(typeSel.value).includes(item.category)) {
        catSel.append(el("option", { value: item.category }, [item.category]));
      }
      catSel.value = item.category;
    };
    typeSel.addEventListener("change", fillCats);
    fillCats();
    const merchantInput = el("input", { placeholder: "商家", value: item.merchant });
    const noteInput = el("input", { placeholder: "备注", value: item.note });
    const estimated = el("label", { class: "muted small" }, [
      el("input", { type: "checkbox", checked: item.estimated ? "checked" : "" }),
      " 估算",
    ]);
    const removeBtn = el("button", { class: "btn ghost small" }, ["删除"]);
    removeBtn.addEventListener("click", () => {
      card.remove();
      rows.splice(rows.indexOf(card), 1);
    });
    const head = el("div", { class: "confirm-item-head" }, [typeSel, amountInput, removeBtn]);
    const liInfo = (item.line_items || []).map((li) =>
      `${li.name} ×${li.qty}（${li.price}元）`).join("、");
    card.append(
      head,
      el("div", { class: "field-grid", style: "margin-top:8px" }, [dateInput, catSel]),
      el("div", { class: "field-grid", style: "margin-top:8px" }, [merchantInput, noteInput]),
      el("div", { class: "form-row", style: "margin-top:8px" }, [estimated]),
      liInfo ? el("p", { class: "muted small" }, [`单品：${liInfo}`]) : el("span"),
    );
    (card as unknown as Record<string, unknown>)._collect = () => ({
      type: typeSel.value,
      amount: Math.round(Number(amountInput.value) * 100) / 100,
      date: dateInput.value || todayIso(),
      category: catSel.value,
      merchant: merchantInput.value.trim(),
      note: noteInput.value.trim(),
      estimated: estimated.querySelector("input")!.checked ? 1 : 0,
      line_items: item.line_items,
    });
    rows.push(card);
    body.append(card);
  }

  const saveBtn = $("#confirm-save");
  const handler = async () => {
    const collected = rows
      .map((card) => (card as unknown as { _collect: () => ParsedItem })._collect())
      .filter((it) => it && it.amount > 0);
    if (!collected.length) {
      app.toast("没有可入账的条目", "error");
      return;
    }
    ctx.repo.saveItems(collected, source);
    await ctx.afterDataChange();
    app.toast(`已入账 ${collected.length} 笔`, "success");
    closeSheet("#confirm-sheet");
  };
  saveBtn.replaceWith(saveBtn.cloneNode(true));
  $("#confirm-save").addEventListener("click", handler);
}

export function closeConfirmPanel() {
  closeSheet("#confirm-sheet");
}
