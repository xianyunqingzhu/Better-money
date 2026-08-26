/** AI 解析确认面板：逐条可改可删；退款条目支持配对历史支出，确认后才入账。 */
import { app, type App } from "../app";
import type { ParsedItem } from "../domain/ai";
import { todayIso } from "../domain/dates";
import { VALID_TYPES, type TransactionRow } from "../domain/types";
import { $, el, openSheet, closeSheet, escapeHtml } from "./dom";
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

/** 从历史选择对应的花销（退款配对）。 */
function openRefundPicker(
  ctx: App,
  item: {
    merchant: string;
    amount: number;
    date: string;
  },
  onPick: (uuid: string) => void,
) {
  const body = $("#refund-pick-body");
  body.innerHTML = "";
  const search = el("input", { type: "search", placeholder: "搜索商家 / 备注", id: "refund-pick-search" });
  body.append(el("div", { class: "form-row" }, [search]));

  const list = el("div", {}, []);
  body.append(list);

  const candidates = ctx.repo.findRefundCandidates(item);
  const rows: (TransactionRow | { uuid: string; date: string; amount: number; merchant: string; note: string; category: string })[] =
    candidates.length
      ? candidates
      : ctx.repo
          .listTransactions(200)
          .filter((t) => t.type === "支出")
          .map((t) => ({ uuid: t.uuid, date: t.date, amount: t.amount, merchant: t.merchant, note: t.note, category: t.category }));

  const renderList = () => {
    const kw = search.value.trim();
    list.innerHTML = "";
    if (!rows.length) {
      list.innerHTML = '<div class="empty-state">没有可配对的支出记录。<br>可点「不配对」按独立退款记账。</div>';
      return;
    }
    const filtered = rows.filter(
      (r) => !kw || r.merchant.includes(kw) || r.note.includes(kw),
    );
    if (!filtered.length) {
      list.innerHTML = '<div class="empty-state">没有匹配的搜索结果。</div>';
      return;
    }
    for (const row of filtered) {
      const item = el("div", { class: "history-card", "data-uuid": row.uuid });
      item.append(
        el("div", { class: "history-head" }, [
          el("span", { class: "history-amount expense" }, [`−¥${row.amount.toFixed(2)}`]),
          el("span", { class: "muted small" }, [row.date]),
        ]),
        el("div", { class: "history-meta" }, [
          el("span", {}, [escapeHtml(row.merchant || "（无商家）")]),
          row.note ? el("span", {}, [escapeHtml(row.note)]) : el("span"),
        ]),
      );
      item.addEventListener("click", () => {
        onPick(row.uuid);
        closeSheet("#refund-pick-sheet");
      });
      list.append(item);
    }
  };
  search.addEventListener("input", renderList);
  renderList();
  openSheet("#refund-pick-sheet");
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

    // ---- 退款配对 ----
    let refundChoice = ""; // ""=自动匹配；uuid=手动选择；"__none__"=不配对
    if (item.type === "退款") {
      const pairingBox = el("div", { class: "refund-pairing" });
      const label = el("span", { class: "muted small" });
      const autoCandidates = ctx.repo.findRefundCandidates({
        merchant: item.merchant,
        amount: item.amount,
        date: item.date,
      });
      const describe = (c: { date: string; merchant: string; amount: number }) =>
        `将修正 ${c.date} ${c.merchant || "支出"} ¥${c.amount.toFixed(2)}`;
      const refreshLabel = () => {
        if (refundChoice === "__none__") label.textContent = "不配对，按独立退款记账";
        else if (refundChoice) {
          const chosen =
            autoCandidates.find((c) => c.uuid === refundChoice) ||
            ctx.repo
              .listTransactions(300)
              .filter((t) => t.type === "支出")
              .find((t) => t.uuid === refundChoice);
          label.textContent = chosen
            ? describe({ date: chosen.date, merchant: chosen.merchant, amount: chosen.amount })
            : "已选择配对（原支出可能已变化）";
        } else if (autoCandidates.length) {
          label.textContent = `将自动修正 ${describe(autoCandidates[0])}`;
        } else {
          label.textContent = "未找到对应的花销，可手动选择";
        }
      };
      refreshLabel();
      const pickBtn = el("button", { class: "btn ghost small" }, ["从历史选择"]);
      pickBtn.addEventListener("click", () => {
        openRefundPicker(
          ctx,
          {
            merchant: merchantInput.value.trim() || item.merchant,
            amount: Math.round(Number(amountInput.value || item.amount) * 100) / 100,
            date: dateInput.value || item.date,
          },
          (uuid) => {
            refundChoice = uuid;
            refreshLabel();
          },
        );
      });
      const noneBtn = el("button", { class: "btn ghost small" }, ["不配对"]);
      noneBtn.addEventListener("click", () => {
        refundChoice = "__none__";
        refreshLabel();
      });
      pairingBox.append(label, el("div", { class: "btn-row", style: "margin-top:6px" }, [pickBtn, noneBtn]));
      card.append(pairingBox);
    }

    (card as unknown as Record<string, unknown>)._collect = () => ({
      type: typeSel.value,
      amount: Math.round(Number(amountInput.value) * 100) / 100,
      date: dateInput.value || todayIso(),
      category: catSel.value,
      merchant: merchantInput.value.trim(),
      note: noteInput.value.trim(),
      estimated: estimated.querySelector("input")!.checked ? 1 : 0,
      line_items: item.line_items,
      refund_of: refundChoice,
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
    const result = ctx.repo.saveItems(collected, source);
    await ctx.afterDataChange();
    const pairedCount = result.saved.filter((s) => s.refund_paired).length;
    app.toast(
      pairedCount > 0
        ? `已入账 ${collected.length} 笔，其中 ${pairedCount} 笔已配对修正原支出`
        : `已入账 ${collected.length} 笔`,
      "success",
    );
    closeSheet("#confirm-sheet");
  };
  saveBtn.replaceWith(saveBtn.cloneNode(true));
  $("#confirm-save").addEventListener("click", handler);
}

export function closeConfirmPanel() {
  closeSheet("#confirm-sheet");
}
