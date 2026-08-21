/* Better-money 前端逻辑（M1：手动记账 + 看板 + 设置） */

const CATS = {
  '支出': ['餐饮', '奶茶咖啡', '交通', '学习', '购物', '娱乐', '生活', '其他'],
  '收入': ['兼职', '红包', '家里给', '其他收入'],
  '退款': ['餐饮', '奶茶咖啡', '交通', '学习', '购物', '娱乐', '生活', '其他'],
  '取现': ['—'],
  '转账': ['—'],
  '还款': ['—'],
};

const $ = (s) => document.querySelector(s);

function showToast(message, type = 'info') {
  let box = document.getElementById('toast-box');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast-box';
    document.body.appendChild(box);
  }
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  toast.textContent = message;
  box.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

async function requestJson(url, options = {}) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || '请求失败');
    err.data = data;
    throw err;
  }
  return data;
}

/* ---------- 记账 ---------- */

function fillCategories() {
  const type = $('#tx-type').value;
  const sel = $('#tx-category');
  sel.innerHTML = CATS[type].map((c) => `<option value="${c}">${c}</option>`).join('');
}

async function submitTx() {
  const amount = parseFloat($('#tx-amount').value);
  if (!amount || amount <= 0) { alert('请填写有效金额'); return; }
  const payload = {
    date: $('#tx-date').value,
    amount,
    type: $('#tx-type').value,
    category: $('#tx-category').value,
    merchant: $('#tx-merchant').value.trim(),
    note: $('#tx-note').value.trim(),
    source: '手动',
  };
  const res = await fetch('/api/transactions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (data.ok) {
    $('#tx-amount').value = ''; $('#tx-merchant').value = ''; $('#tx-note').value = '';
    refresh();
  } else {
    alert('记账失败：' + (data.error || '未知错误'));
  }
}

async function delTx(id) {
  if (!confirm('删除这条记录？')) return;
  await fetch(`/api/transactions/${id}`, { method: 'DELETE' });
  refresh();
}

/* ---------- 智能记账（M2） ---------- */

function showAiBanner(msg) {
  const b = $('#ai-banner');
  b.textContent = 'AI 不可用：' + msg + '（可继续手动记账，解决后自动恢复）';
  b.classList.remove('hidden');
}

function hideAiBanner() {
  $('#ai-banner').classList.add('hidden');
}

async function aiSubmit() {
  const text = $('#ai-text').value.trim();
  if (!text) { alert('先写下今天的花销，例如：午饭食堂 15'); return; }
  const btn = $('#ai-submit');
  btn.disabled = true; btn.textContent = '解析中…';
  try {
    const res = await fetch('/api/parse_text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, date: $('#tx-date').value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (data.error === 'ai_unavailable') {
        showAiBanner(data.message || '请检查网络或 API 配置');
        $('#ai-result').innerHTML = '<div class="res-err">这次没解析成功，原文已保留，AI 恢复后可重试；也可先手动记账。</div>';
      } else {
        $('#ai-result').innerHTML = '<div class="res-err">解析失败（' + (data.message || '未知原因') + '），可重试或手动记账</div>';
      }
      return;
    }
    hideAiBanner();
    let html = '';
    if (data.saved > 0) {
      $('#ai-text').value = '';
      html += '<div class="res-ok">已入账 ' + data.saved + ' 笔</div>';
      refresh();
    } else if (data.message) {
      html += '<div class="res-err">' + data.message + '</div>';
    }
    if (data.skipped && data.skipped.length) {
      html += '<div class="res-warn">跳过 ' + data.skipped.length + ' 笔疑似重复（日期+金额+商家相同）</div>';
    }
    if (data.questions && data.questions.length) {
      html += '<div class="res-q">需要补充：' + data.questions.join('<br>') + '</div>';
    }
    $('#ai-result').innerHTML = html;
  } catch (e) {
    showAiBanner('网络错误');
    $('#ai-result').innerHTML = '<div class="res-err">请求失败，请检查本地服务是否在运行</div>';
  } finally {
    btn.disabled = false; btn.textContent = '智能解析入账';
  }
}

/* ---------- 截图/小票上传 + CSV 导入 + 确认面板（M3） ---------- */

const TYPE_ORDER = ['支出', '收入', '退款', '取现', '转账', '还款'];
let confirmData = [];     // 当前确认面板的原始 items（含 line_items）
let confirmSource = '确认面板';
let confirmPreviousFocus = null;

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

async function uploadImages() {
  const files = $('#img-file').files;
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('note', $('#img-note').value.trim());
  fd.append('date', $('#tx-date').value);
  const btn = $('#img-btn');
  btn.disabled = true; btn.textContent = '识别中…';
  try {
    const res = await fetch('/api/upload_images', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (data.error === 'ai_unavailable') {
        showAiBanner(data.message || '请检查网络或 API 配置');
        $('#ai-result').innerHTML = '<div class="res-err">图片已保存，AI 恢复后可重试；也可手动记账。</div>';
      } else {
        $('#ai-result').innerHTML = '<div class="res-err">识别失败：' + (data.message || '未知原因') + '</div>';
      }
      return;
    }
    hideAiBanner();
    let extra = '';
    if (data.failed) extra = `<div class="res-warn">${data.failed} 张图片识别失败，其余已识别。</div>`;
    if (data.items && data.items.length) {
      confirmSource = '小票';
      openConfirm(data.items, data.questions || []);
      $('#ai-result').innerHTML = extra + '<div class="res-ok">识别出 ' + data.items.length + ' 条，请在确认面板核对</div>';
    } else {
      $('#ai-result').innerHTML = extra + '<div class="res-err">没识别出可入账的条目，可手动记账</div>';
      if (data.questions && data.questions.length) {
        $('#ai-result').innerHTML += '<div class="res-q">需要补充：' + data.questions.join('<br>') + '</div>';
      }
    }
  } catch (e) {
    showAiBanner('网络错误');
  } finally {
    btn.disabled = false; btn.textContent = '上传截图 / 小票';
    $('#img-file').value = '';
  }
}

async function importCsv() {
  const f = $('#csv-file').files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  const btn = $('#csv-btn');
  btn.disabled = true; btn.textContent = '解析中…';
  try {
    const res = await fetch('/api/import_csv', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $('#ai-result').innerHTML = '<div class="res-err">导入失败：' + (data.message || '未知原因') + '</div>';
      return;
    }
    if (data.items && data.items.length) {
      confirmSource = 'CSV导入';
      const extra = data.skipped_rows ? `（已跳过 ${data.skipped_rows} 行无关记录）` : '';
      openConfirm(data.items, []);
      $('#ai-result').innerHTML = '<div class="res-ok">解析出 ' + data.items.length + ' 条待确认' + extra + '</div>';
    } else {
      $('#ai-result').innerHTML = '<div class="res-err">' + (data.message || '没有解析出交易行') + '</div>';
    }
  } catch (e) {
    showAiBanner('网络错误');
  } finally {
    btn.disabled = false; btn.textContent = '导入账单 CSV';
    $('#csv-file').value = '';
  }
}

function syncCatSelect(sel, type, current) {
  const opts = CATS[type] || CATS['支出'];
  sel.innerHTML = opts.map((c) => `<option value="${c}" ${c === current ? 'selected' : ''}>${c}</option>`).join('');
}

function confirmRowHtml(it, i) {
  const typeOpts = TYPE_ORDER
    .map((t) => `<option value="${t}" ${t === it.type ? 'selected' : ''}>${t}</option>`)
    .join('');
  return `<tr data-i="${i}">
    <td><input class="cf-date" value="${esc(it.date)}"></td>
    <td><select class="cf-type">${typeOpts}</select></td>
    <td><select class="cf-cat"></select></td>
    <td><input class="cf-merchant" value="${esc(it.merchant)}"></td>
    <td><input class="cf-amount" type="number" step="0.01" min="0" value="${it.amount}"></td>
    <td><input class="cf-note" value="${esc(it.note)}"></td>
    <td><button class="del-btn cf-del" title="删除此行" aria-label="删除此行">&times;</button></td>
  </tr>`;
}

function openConfirm(items, questions) {
  confirmPreviousFocus = document.activeElement;
  confirmData = items;
  const tbody = $('#confirm-table tbody');
  tbody.innerHTML = items.map((it, i) => confirmRowHtml(it, i)).join('');
  tbody.querySelectorAll('tr').forEach((tr) => {
    const typeSel = tr.querySelector('.cf-type');
    const catSel = tr.querySelector('.cf-cat');
    syncCatSelect(catSel, typeSel.value, items[+tr.dataset.i].category);
    typeSel.addEventListener('change', () => syncCatSelect(catSel, typeSel.value, ''));
    tr.querySelector('.cf-del').addEventListener('click', () => tr.remove());
  });
  const q = $('#confirm-questions');
  if (questions && questions.length) {
    q.innerHTML = '需要补充：' + questions.join('<br>');
    q.classList.remove('hidden');
  } else {
    q.classList.add('hidden');
  }
  $('#confirm-count').textContent = `（${items.length} 条，可修改后确认）`;
  $('#confirm-modal').classList.remove('hidden');
  requestAnimationFrame(() => $('#confirm-close').focus());
}

function closeConfirm() {
  $('#confirm-modal').classList.add('hidden');
  if (confirmPreviousFocus && typeof confirmPreviousFocus.focus === 'function') {
    confirmPreviousFocus.focus();
  }
  confirmPreviousFocus = null;
}

async function confirmSave() {
  const rows = [...$('#confirm-table tbody').querySelectorAll('tr')];
  const items = [];
  rows.forEach((tr) => {
    const amount = parseFloat(tr.querySelector('.cf-amount').value);
    if (!(amount > 0)) return;
    const orig = confirmData[+tr.dataset.i] || {};
    const it = {
      date: tr.querySelector('.cf-date').value.trim(),
      amount,
      type: tr.querySelector('.cf-type').value,
      category: tr.querySelector('.cf-cat').value,
      merchant: tr.querySelector('.cf-merchant').value.trim(),
      note: tr.querySelector('.cf-note').value.trim(),
    };
    if (orig.line_items) it.line_items = orig.line_items;
    items.push(it);
  });
  if (!items.length) { alert('没有有效条目（金额需大于 0）'); return; }
  const res = await fetch('/api/confirm_items', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items, source: confirmSource }),
  });
  const data = await res.json();
  closeConfirm();
  let html = '<div class="res-ok">已入账 ' + data.saved + ' 笔</div>';
  if (data.skipped && data.skipped.length) {
    html += '<div class="res-warn">跳过 ' + data.skipped.length + ' 笔疑似重复（日期+金额+商家相同）</div>';
  }
  $('#ai-result').innerHTML = html;
  refresh();
}

/* ---------- 图表看板（M4） ---------- */

const PALETTE = ['#147d64', '#d68b3c', '#426da9', '#70589c', '#c84f3b',
  '#2f918c', '#bd6d51', '#7b8a84'];
const CHART_TEXT = '#687a72';
const CHART_LINE = '#e3ebe7';
const CHART_AXIS = '#9aaba3';
const REDUCE_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const tooltipStyle = {
  backgroundColor: '#17241f', borderWidth: 0, padding: [8, 10],
  textStyle: { color: '#ffffff', fontSize: 12 },
};
const categoryAxisStyle = {
  axisLine: { lineStyle: { color: CHART_LINE } },
  axisTick: { show: false },
  axisLabel: { color: CHART_TEXT, fontSize: 10 },
};
const valueAxisStyle = {
  axisLine: { show: false }, axisTick: { show: false },
  axisLabel: { color: CHART_TEXT, fontSize: 10 },
  splitLine: { lineStyle: { color: CHART_LINE, type: 'dashed' } },
};
let charts = {};
let currentMonth = '';

function initCharts() {
  ['chart-cat', 'chart-daily', 'chart-week'].forEach((id) => {
    charts[id] = echarts.init(document.getElementById(id));
  });
  window.addEventListener('resize', () => Object.values(charts).forEach((c) => c.resize()));
}

async function loadMonths() {
  const list = await (await fetch('/api/months')).json();
  const now = new Date();
  const cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  const months = new Set([cur, ...list]);
  const sel = $('#month-select');
  sel.innerHTML = [...months].sort().reverse()
    .map((m) => `<option value="${m}" ${m === currentMonth ? 'selected' : ''}>${m}</option>`)
    .join('');
}

async function loadStats() {
  const month = currentMonth || '';
  const s = await (await fetch('/api/stats?month=' + month)).json();

  // 分类占比（环形）
  const hasCat = s.category && s.category.length;
  charts['chart-cat'].setOption({
    animation: !REDUCE_MOTION,
    tooltip: { ...tooltipStyle, trigger: 'item', formatter: '{b}：¥{c}（{d}%）' },
    legend: { bottom: 0, type: 'scroll', itemWidth: 8, itemHeight: 8, textStyle: { color: CHART_TEXT, fontSize: 11 } },
    color: PALETTE,
    series: [{
      type: 'pie', radius: ['48%', '70%'], center: ['50%', '43%'],
      itemStyle: { borderColor: '#ffffff', borderWidth: 3, borderRadius: 4 },
      label: { formatter: '{b}\n{d}%', color: CHART_TEXT, fontSize: 10, lineHeight: 15 },
      labelLine: { lineStyle: { color: CHART_AXIS } },
      emphasis: { scaleSize: 4 },
      data: hasCat ? s.category : [],
    }],
    graphic: hasCat ? [] : [{
      type: 'text', left: 'center', top: 'middle',
      style: { text: '本月暂无支出记录', fill: CHART_AXIS, fontSize: 12 },
    }],
  }, true);

  // 近30天趋势（折线）
  charts['chart-daily'].setOption({
    animation: !REDUCE_MOTION,
    tooltip: { ...tooltipStyle, trigger: 'axis', valueFormatter: (value) => `¥${Number(value).toFixed(2)}` },
    grid: { left: 46, right: 12, top: 24, bottom: 26 },
    xAxis: {
      type: 'category',
      data: s.daily.map((d) => d.date.slice(5)),
      ...categoryAxisStyle,
      axisLabel: { ...categoryAxisStyle.axisLabel, interval: 4 },
    },
    yAxis: { type: 'value', ...valueAxisStyle },
    series: [{
      type: 'line', smooth: true, symbol: 'none',
      data: s.daily.map((d) => d.value),
      lineStyle: { color: PALETTE[0], width: 2.5 },
      areaStyle: { color: 'rgba(20,125,100,.10)' },
      itemStyle: { color: PALETTE[0] },
    }],
  }, true);

  // 近8周对比（柱状）
  charts['chart-week'].setOption({
    animation: !REDUCE_MOTION,
    tooltip: { ...tooltipStyle, trigger: 'axis', valueFormatter: (value) => `¥${Number(value).toFixed(2)}` },
    grid: { left: 46, right: 12, top: 24, bottom: 26 },
    xAxis: {
      type: 'category', data: s.weekly.map((w) => w.label),
      ...categoryAxisStyle,
    },
    yAxis: { type: 'value', ...valueAxisStyle },
    series: [{
      type: 'bar', data: s.weekly.map((w) => w.value),
      itemStyle: { color: PALETTE[2], borderRadius: [5, 5, 2, 2] },
      barMaxWidth: 24,
    }],
  }, true);

  // 多目标进度列表
  const goals = await (await fetch('/api/goals')).json();
  renderGoalProgressList(goals);
}

function renderGoalProgressList(goals) {
  const box = $('#goal-progress-list');
  const visible = goals.filter((g) => ['冷静期', '进行中', '已暂停'].includes(g.status));
  if (!visible.length) {
    box.innerHTML = '<p class="goal-empty">暂无目标<br>去目标清单创建第一个计划</p>';
    return;
  }
  box.innerHTML = visible.map((g, i) => {
    const saved = Number(g.saved) || 0;
    const target = Number(g.price) || 0;
    const remaining = Math.max(0, target - saved);
    const pct = target > 0 ? Math.min(100, saved / target * 100) : 0;
    const paused = g.status === '已暂停';
    const isFirst = i === 0;
    return '<div class="goal-progress-item' + (paused ? ' paused' : '') + '">' +
      '<div class="goal-progress-head">' +
        '<span class="goal-progress-name">' + esc(g.name) + '</span>' +
        (isFirst ? '<span class="tag income">当前优先目标</span>' : '') +
        '<span class="goal-progress-status">' + g.status + '</span>' +
      '</div>' +
      '<div class="goal-progress-bar"><div style="width:' + pct.toFixed(2) + '%"></div></div>' +
      '<div class="goal-progress-meta">' +
        '<span class="goal-amount">已存 <b>¥' + saved.toFixed(2) + '</b></span>' +
        '<span class="goal-amount">需要 <b>¥' + target.toFixed(2) + '</b></span>' +
        '<span class="goal-amount">还差 <b>¥' + remaining.toFixed(2) + '</b></span>' +
        '<span class="goal-pct">' + Math.round(pct) + '%</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

/* ---------- 周/月总结（M5） ---------- */

function localISO(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function weekRange(offsetWeeks) {
  const now = new Date();
  const monday = new Date(now);
  monday.setDate(now.getDate() - ((now.getDay() + 6) % 7) + offsetWeeks * 7);
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);
  return [localISO(monday), localISO(sunday)];
}

function monthRange(offsetMonths) {
  const now = new Date();
  const first = new Date(now.getFullYear(), now.getMonth() + offsetMonths, 1);
  const last = new Date(now.getFullYear(), now.getMonth() + offsetMonths + 1, 0);
  return [localISO(first), localISO(last)];
}

const SUMMARY_PRESETS = {
  this_week: () => weekRange(0),
  last_week: () => weekRange(-1),
  this_month: () => monthRange(0),
  last_month: () => monthRange(-1),
};

// 重新生成意图：非空表示针对该总结 id 覆盖写作
let summaryOverwriteId = null;

function openSummaryModal(preset = 'this_week') {
  $('#summary-modal').classList.remove('hidden');
  $('#summary-modal-title').textContent = summaryOverwriteId !== null ? '重新生成总结' : '生成总结';
  $('#summary-submit').textContent = summaryOverwriteId !== null ? '重新写作' : '开始写作';
  if (preset !== 'custom') {
    const [start, end] = SUMMARY_PRESETS[preset]();
    $('#summary-start').value = start;
    $('#summary-end').value = end;
  } else {
    $('#summary-start').focus();
  }
  hideSummaryRangeError();
}

function closeSummaryModal() {
  $('#summary-modal').classList.add('hidden');
  summaryOverwriteId = null;
}

function summaryType() {
  const el = document.querySelector('input[name="summary-type"]:checked');
  return el ? el.value : '周';
}

function validateSummaryRange(start, end) {
  if (!start || !end) return '请选择开始和结束日期';
  if (start > end) return '开始日期不能晚于结束日期';
  const days = Math.round((new Date(end) - new Date(start)) / 86400000) + 1;
  if (days > 366) return '区间最长 366 天';
  return '';
}

function showSummaryRangeError(message) {
  const box = $('#summary-range-error');
  box.textContent = message;
  box.classList.remove('hidden');
}

function hideSummaryRangeError() {
  $('#summary-range-error').classList.add('hidden');
}

async function loadSummaries() {
  const list = await (await fetch('/api/summaries')).json();
  const box = $('#summary-list');
  if (!list.length) {
    box.innerHTML = '<p class="todo-note">还没有总结，点上面的「生成总结」选择区间，生成第一篇吧。</p>';
  } else {
    box.innerHTML = list.map(renderSummary).join('');
    box.querySelectorAll('[data-summary-act]').forEach((b) =>
      b.addEventListener('click', () => {
        const id = parseInt(b.dataset.summaryId, 10);
        if (b.dataset.summaryAct === 'delete') deleteSummary(id);
        else regenerateSummary(id);
      }));
  }
  // 轻提示：有账但本周总结缺失
  const hasWeek = list.some((s) => s.period_type === '周' && s.period_start === weekRange(0)[0]);
  const txs = await (await fetch('/api/transactions?limit=1')).json();
  const hint = $('#summary-hint');
  if (!hasWeek && txs.length) {
    hint.textContent = '本周有记账记录但还没有周总结，点「生成总结」看看吧。';
    hint.classList.remove('hidden');
  } else {
    hint.classList.add('hidden');
  }
}

function renderSummary(s) {
  const paras = s.content.split('\n').filter((l) => l.trim())
    .map((p) => `<p>${esc(p)}</p>`).join('');
  const img = s.image_path
    ? `<img src="/api/summary_image/${s.id}" class="summary-img" alt="总结配图">` : '';
  const badge = s.expired
    ? '<span class="tag" style="color:#b45309;background:#fef3c7">账目已修改，可能过期</span>' : '';
  const actions = `<div class="summary-actions">
      <button type="button" class="mini" data-summary-act="regen" data-summary-id="${s.id}">重新生成</button>
      <button type="button" class="mini danger" data-summary-act="delete" data-summary-id="${s.id}">删除</button>
    </div>`;
  return `<div class="summary-card">
    <div class="summary-head">
      <b>${s.period_type === '周' ? '周总结' : '月总结'}（${s.period_start} ~ ${s.period_end}）</b>
      ${badge}
      <span class="muted">${s.created_at}</span>
      ${actions}
    </div>
    ${img}
    <div class="summary-body">${paras}</div>
  </div>`;
}

async function submitSummary(forceOverwrite = false) {
  const start = $('#summary-start').value;
  const end = $('#summary-end').value;
  const error = validateSummaryRange(start, end);
  if (error) { showSummaryRangeError(error); return; }
  hideSummaryRangeError();
  const type = summaryType();
  const overwrite = forceOverwrite || summaryOverwriteId !== null;
  const btn = $('#summary-submit');
  btn.disabled = true; btn.textContent = '写作中…';
  try {
    const res = await fetch('/api/summaries/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ period_type: type, period_start: start, period_end: end, overwrite }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (data.error === 'summary_exists' && !forceOverwrite) {
        summaryOverwriteId = data.summary_id;
        const label = type === '周' ? '周总结' : '月总结';
        if (confirm(`这个区间已经有${label}（${start} ~ ${end}）。\n是否覆盖原总结，重新写作？`)) {
          await submitSummary(true);
        }
        return;
      }
      if (data.error === 'ai_unavailable') {
        showAiBanner(data.message || '请检查网络或 API 配置');
        alert('生成失败：' + (data.message || 'AI 不可用'));
      } else {
        alert('生成失败：' + (data.message || '未知原因'));
      }
      return;
    }
    hideAiBanner();
    closeSummaryModal();
    showToast(data.overwritten ? '总结已覆盖更新' : '总结已生成', 'success');
    await loadSummaries();
  } catch (e) {
    showAiBanner('网络错误');
    alert('生成失败：网络错误');
  } finally {
    btn.disabled = false;
    btn.textContent = summaryOverwriteId !== null ? '重新写作' : '开始写作';
  }
}

async function regenerateSummary(id) {
  const list = await (await fetch('/api/summaries')).json().catch(() => []);
  const s = list.find((x) => x.id === id);
  if (!s) { showToast('总结已经不存在', 'error'); return; }
  summaryOverwriteId = id;
  const typeEl = document.querySelector(`input[name="summary-type"][value="${s.period_type}"]`);
  if (typeEl) typeEl.checked = true;
  openSummaryModal('custom');
  $('#summary-start').value = s.period_start;
  $('#summary-end').value = s.period_end;
}

async function deleteSummary(id) {
  const list = await (await fetch('/api/summaries')).json().catch(() => []);
  const s = list.find((x) => x.id === id);
  if (!s) { showToast('总结已经不存在', 'error'); return; }
  const label = s.period_type === '周' ? '周总结' : '月总结';
  if (!confirm(`删除这份${label}（${s.period_start} ~ ${s.period_end}）？\n账目、目标和余额都不会受影响。`)) return;
  const res = await fetch(`/api/summaries/${id}`, { method: 'DELETE' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { showToast(data.message || '删除失败', 'error'); return; }
  showToast(data.message || '总结已删除', 'success');
  await loadSummaries();
}

/* ---------- 目标清单（M6） ---------- */

async function loadGoals() {
  const [goals, wins] = await Promise.all([
    (await fetch('/api/goals')).json(),
    (await fetch('/api/savings_wins')).json(),
  ]);
  const box = $('#goal-list');
  if (!goals.length) {
    box.innerHTML = '<p class="todo-note">还没有目标。在上面写下想买的东西，会自动进入冷静期帮你拦截冲动消费。</p>';
  } else {
    box.innerHTML = goals.map((g) => goalCard(g)).join('');
  }
  const winsBox = $('#wins-box');
  winsBox.innerHTML = wins.total > 0
    ? `本月通过「冷静期」忍住了 <b>${wins.count}</b> 次冲动消费，共省下 <b>¥${wins.total.toFixed(2)}</b>`
    : '';
}

function goalCard(g) {
  const pct = g.price > 0 ? Math.min(100, (g.saved / g.price) * 100) : 0;
  const statusCls = {
    '冷静期': 'tag', '进行中': 'tag income', '已暂停': 'tag',
    '已达成': 'tag done', '已放弃': 'tag',
  }[g.status] || 'tag';
  const today = new Date().toISOString().slice(0, 10);
  let cdActions = '';
  if (g.status === '冷静期' && g.cooldown_until && g.cooldown_until <= today) {
    cdActions = `<div class="cooldown-actions">冷静期已过，还想要吗？
      <button class="ghost" data-act="want" data-id="${g.id}">还想要</button>
      <button class="ghost" data-act="pass" data-id="${g.id}">不要了（省下 ¥${g.price.toFixed(2)}）</button>
    </div>`;
  }
  const ops = [];
  ops.push(`<button class="mini" title="上移" data-act="up" data-id="${g.id}">↑</button>`);
  ops.push(`<button class="mini" title="下移" data-act="down" data-id="${g.id}">↓</button>`);
  ops.push(`<button class="mini" data-act="edit" data-id="${g.id}">编辑</button>`);
  if (g.status === '进行中') ops.push(`<button class="mini" data-act="pause" data-id="${g.id}">暂停</button>`);
  if (g.status === '已暂停') ops.push(`<button class="mini" data-act="resume" data-id="${g.id}">恢复</button>`);
  if (['冷静期', '进行中', '已暂停'].includes(g.status)) {
    ops.push(`<button class="mini" data-act="achieve" data-id="${g.id}">达成</button>`);
    ops.push(`<button class="mini" data-act="abandon" data-id="${g.id}">放弃</button>`);
  }
  ops.push(`<button class="mini danger" data-act="delete" data-id="${g.id}">删除</button>`);
  return `<div class="goal-card">
    <div class="goal-top">
      <b>${esc(g.name)}</b>
      <span class="${statusCls}">${g.status}</span>
      ${g.expected_date ? `<span class="muted">期望 ${g.expected_date}</span>` : ''}
      ${g.status === '冷静期' && g.cooldown_until ? `<span class="muted">冷静期至 ${g.cooldown_until}</span>` : ''}
      <span class="goal-ops">${ops.join('')}</span>
    </div>
    <div class="goal-bar"><div style="width:${pct}%"></div></div>
    <div class="goal-meta">已存 ¥${g.saved.toFixed(2)} / ¥${g.price.toFixed(2)}（${Math.round(pct)}%）${g.note ? ' · ' + esc(g.note) : ''}</div>
    ${cdActions}
  </div>`;
}

async function addGoal() {
  const name = $('#g-name').value.trim();
  const price = parseFloat($('#g-price').value);
  if (!name || !(price > 0)) { alert('填写名称和价格'); return; }
  const r = await fetch('/api/goals', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name, price,
      expected_date: $('#g-expected').value,
      note: $('#g-note').value.trim(),
    }),
  });
  const d = await r.json();
  if (d.ok) {
    $('#g-name').value = ''; $('#g-price').value = '';
    $('#g-expected').value = ''; $('#g-note').value = '';
    await loadGoals();
    await loadStats();
  } else {
    alert(d.message || '添加失败');
  }
}

async function editGoal(id) {
  const goals = await (await fetch('/api/goals')).json();
  const g = goals.find((x) => x.id === id);
  if (!g) return;
  const name = prompt('名称', g.name);
  if (name === null) return;
  const price = prompt('价格（元）', g.price);
  if (price === null) return;
  const expected = prompt('期望日期（YYYY-MM-DD，可留空）', g.expected_date || '');
  if (expected === null) return;
  const note = prompt('备注/链接（可留空）', g.note || '');
  if (note === null) return;
  const saved = prompt('已存金额（手动调拨入口，可改）', g.saved);
  if (saved === null) return;
  const payload = {
    name, price: parseFloat(price), expected_date: expected, note,
    saved: parseFloat(saved) || 0,
  };
  if (!payload.name || !(payload.price > 0)) { alert('名称和价格必填'); return; }
  const r = await fetch(`/api/goals/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const d = await r.json();
  if (d.ok) { await loadGoals(); await loadStats(); } else { alert(d.message || '保存失败'); }
}

async function goalAction(id, act) {
  if (act === 'delete') {
    const goal = (await (await fetch('/api/goals')).json()).find((g) => g.id === id);
    if (!goal) { showToast('目标已经不存在', 'error'); return; }
    const message = `删除“${goal.name}”目标？\n其中规划的 ¥${Number(goal.saved).toFixed(2)} 将不再归属于任何目标，但不会改变账本余额。`;
    if (!confirm(message)) return;
    const response = await fetch(`/api/goals/${id}`, { method: 'DELETE' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { showToast(data.message || '删除失败', 'error'); return; }
    await Promise.all([loadGoals(), loadStats(), loadSummary()]);
    return;
  }
  if (act === 'abandon' && !confirm('放弃这个目标？已存金额清零（钱回到可用余额池）。')) return;
  if (act === 'pause' && !confirm('暂停这个目标？已存金额会保留（冻结）。')) return;
  if (act === 'achieve') {
    act = confirm('达成目标！\n\n「确定」= 我已买下它（自动记一笔支出，账实一致）\n「取消」= 钱够了但先不买（冻结，继续攒下一个）')
      ? 'achieve_buy' : 'achieve_freeze';
  }
  if (act === 'edit') { await editGoal(id); return; }
  const r = await fetch(`/api/goals/${id}/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: act }),
  });
  const d = await r.json().catch(() => ({}));
  if (!d.ok) { alert(d.message || '操作失败'); return; }
  await loadGoals();
  await loadStats();
}

function bindGoalList() {
  $('#goal-list').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (btn) goalAction(parseInt(btn.dataset.id, 10), btn.dataset.act);
  });
}

/* ---------- 对账（M6） ---------- */

async function loadReconcile() {
  const r = await (await fetch('/api/reconcile')).json();
  $('#r-ledger').textContent = '¥' + r.ledger_balance.toFixed(2);
}

async function doReconcile() {
  const actual = parseFloat($('#r-actual').value);
  if (isNaN(actual)) { alert('输入真实余额'); return; }
  const r = await fetch('/api/reconcile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actual, note: '' }),
  });
  const d = await r.json();
  if (d.ok) {
    $('#r-result').textContent = `已校准：差额 ${d.diff >= 0 ? '+' : ''}¥${d.diff.toFixed(2)} 记为调整`;
    $('#r-actual').value = '';
    await loadReconcile();
    await refresh();
  } else {
    alert('校准失败');
  }
}

/* ---------- 历史明细（M7） ---------- */

let histMonth = '';

async function loadHistory() {
  const months = await (await fetch('/api/months')).json();
  const now = new Date();
  const cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  const set = new Set([cur, ...months]);
  if (!histMonth || !set.has(histMonth)) histMonth = cur;
  const sel = $('#hist-month');
  sel.innerHTML = [...set].sort().reverse()
    .map((m) => `<option value="${m}" ${m === histMonth ? 'selected' : ''}>${m}</option>`)
    .join('');

  const txs = await (await fetch('/api/transactions?limit=2000')).json();
  const rows = txs.filter((t) => t.date.startsWith(histMonth));
  const tbody = $('#hist-table tbody');
  tbody.innerHTML = rows.map(histRow).join('');
  $('#hist-count').textContent = `共 ${rows.length} 笔`;
  tbody.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', () => delTx(parseInt(b.dataset.del, 10))));
  tbody.querySelectorAll('[data-edit]').forEach((b) =>
    b.addEventListener('click', () => editTx(parseInt(b.dataset.edit, 10))));

  const pending = await (await fetch('/api/pending')).json();
  const pb = $('#pending-box');
  if (pending.length) {
    pb.innerHTML = `<b>有 ${pending.length} 条 AI 解析失败的内容已保留（不会丢）：</b><ul>${pending.map((p) =>
      `<li>${esc(p.raw_text || p.image_path || '（图片）')} <button class="mini danger" data-pdel="${p.id}">删除</button></li>`
    ).join('')}</ul>`;
    pb.classList.remove('hidden');
    pb.querySelectorAll('[data-pdel]').forEach((b) =>
      b.addEventListener('click', async () => {
        await fetch('/api/pending/' + b.dataset.pdel, { method: 'DELETE' });
        loadHistory();
      }));
  } else {
    pb.classList.add('hidden');
  }
}

function histRow(t) {
  const isIn = t.type === '收入' || t.type === '退款';
  const cls = isIn ? 'amt-in' : 'amt-out';
  const sign = isIn ? '+' : '−';
  const est = t.estimated ? ' <span class="tag warn-tag">≈估算</span>' : '';
  return `<tr>
    <td>${t.date}</td>
    <td><span class="${t.type === '收入' ? 'tag income' : 'tag'}">${t.type}</span></td>
    <td>${t.category}</td>
    <td>${t.merchant || '—'}</td>
    <td class="${cls}">${sign}¥${t.amount.toFixed(2)}</td>
    <td>${esc(t.note || '')}${est}</td>
    <td>${t.source}</td>
    <td><button class="mini" data-edit="${t.id}">编辑</button> <button class="mini danger" data-del="${t.id}">删除</button></td>
  </tr>`;
}

async function editTx(id) {
  const txs = await (await fetch('/api/transactions?limit=2000')).json();
  const t = txs.find((x) => x.id === id);
  if (!t) return;
  const date = prompt('日期（YYYY-MM-DD）', t.date);
  if (date === null) return;
  const amount = prompt('金额（元）', t.amount);
  if (amount === null) return;
  const type = prompt('类型（支出/收入/退款/取现/转账/还款）', t.type);
  if (type === null) return;
  const category = prompt('分类', t.category);
  if (category === null) return;
  const merchant = prompt('商家', t.merchant || '');
  if (merchant === null) return;
  const note = prompt('备注', t.note || '');
  if (note === null) return;
  const r = await fetch(`/api/transactions/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      date, amount: parseFloat(amount), type, category, merchant, note,
    }),
  });
  const d = await r.json();
  if (d.ok) { await loadHistory(); await refresh(); } else { alert(d.message || '保存失败'); }
}

/* ---------- 看板 ---------- */

async function refresh() {
  await Promise.all([loadSummary(), loadTransactions(), loadMonths(), loadStats()]);
}

async function loadSummary() {
  const s = await (await fetch('/api/summary')).json();
  $('#c-balance').textContent = '¥' + s.balance.toFixed(2);
  const exp = $('#c-expense');
  exp.textContent = '¥' + s.month_expense.toFixed(2);
  $('#c-income').textContent = '¥' + s.month_income.toFixed(2);
  $('#c-budget').textContent = '¥' + s.monthly_budget.toFixed(2);
  const sp = $('#c-spendable');
  sp.textContent = (s.today_spendable < 0 ? '-' : '') + '¥' + Math.abs(s.today_spendable).toFixed(2);
  sp.classList.toggle('neg', s.today_spendable < 0);

  // 预算预警：>=80% 黄色，>=100% 红色
  const ratio = s.budget_ratio ?? 0;
  exp.classList.toggle('neg', ratio >= 1);
  exp.classList.toggle('warn-ratio', ratio >= 0.8 && ratio < 1);
  const banner = $('#budget-banner');
  if (s.monthly_budget > 0 && ratio >= 0.8) {
    if (ratio >= 1) {
      const over = s.month_expense - s.monthly_budget;
      banner.innerHTML = `本月预算已超支 ¥${over.toFixed(2)}！建议暂停奶茶/娱乐等非必需消费，月末总结会给出复盘。`;
    } else {
      banner.innerHTML = `本月预算已用 ${Math.round(ratio * 100)}%，剩余 ${s.days_left} 天日均需控制在 ¥${Math.max(s.today_spendable, 0).toFixed(2)} 内。`;
    }
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }
}

async function loadTransactions() {
  const list = await (await fetch('/api/transactions?limit=50')).json();
  const tbody = $('#recent tbody');
  tbody.innerHTML = list.map((t) => {
    const isIn = t.type === '收入' || t.type === '退款';
    const cls = isIn ? 'amt-in' : 'amt-out';
    const sign = isIn ? '+' : '−';
    const tagCls = t.type === '收入' ? 'tag income' : 'tag';
    return `<tr>
      <td>${t.date}</td>
      <td><span class="${tagCls}">${t.type}</span></td>
      <td>${t.category}</td>
      <td>${t.merchant || '—'}</td>
      <td class="${cls}">${sign}¥${t.amount.toFixed(2)}</td>
      <td>${t.note || ''}</td>
      <td><button class="del-btn" title="删除" aria-label="删除 ${t.date} 的${t.category}记录" data-id="${t.id}">&times;</button></td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('.del-btn').forEach((b) =>
    b.addEventListener('click', () => delTx(b.dataset.id)));
}

/* ---------- 设置 ---------- */

const AI_PROVIDER_BASES = {
  'OpenAI': 'https://api.openai.com/v1',
  'DeepSeek': 'https://api.deepseek.com',
  'Qwen': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  '自定义': '',
};

async function loadSettings() {
  const s = await (await fetch('/api/settings')).json();
  $('#s-initial-display').textContent =
    '¥' + Number(s.initial_balance || 0).toFixed(2) +
    '（起始 ' + (s.initial_balance_date || '—') + '）';
  $('#s-budget').value = s.monthly_budget;
  $('#s-ratio').value = s.auto_save_ratio;
  $('#s-tone').value = s.tone;
  $('#s-ai-provider').value = s.ai_provider || '自定义';
  $('#s-api-base').value = s.api_base;
  $('#s-api-key').value = s.api_key;
  $('#s-model').value = s.model_text;
  await Promise.all([loadAdjustments(), loadLatestBackup()]);
}

async function saveSettings() {
  const payload = {
    monthly_budget: parseFloat($('#s-budget').value) || 0,
    auto_save_ratio: parseFloat($('#s-ratio').value) || 0,
    tone: $('#s-tone').value,
    ai_provider: $('#s-ai-provider').value,
    api_base: $('#s-api-base').value.trim(),
    api_key: $('#s-api-key').value.trim(),
    model_text: $('#s-model').value.trim(),
  };
  const data = await requestJson('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).catch((e) => e.data);
  if (data && data.ok) {
    showToast('设置已保存', 'success');
    refresh();
  } else {
    alert((data && data.message) || '保存失败');
  }
}

function bindAiProvider(selectSel, baseSel) {
  $(selectSel).addEventListener('change', () => {
    const base = AI_PROVIDER_BASES[$(selectSel).value];
    if (base) $(baseSel).value = base;
  });
}

async function testAiConnection(prefix) {
  const base = $(`#${prefix}-api-base`).value.trim();
  const key = $(`#${prefix}-ai-key`).value.trim();
  const model = $(`#${prefix}-model`).value.trim();
  const result = $(`#${prefix}-ai-result`);
  result.textContent = '连接测试中…';
  const data = await requestJson('/api/settings/test-ai', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_base: base, api_key: key, model: model }),
  }).catch((e) => e.data);
  if (data && data.ok) {
    result.textContent = '连接成功 ✓';
  } else {
    result.textContent = (data && data.message) || '连接失败';
  }
}

/* ---------- 对账调整历史 ---------- */

async function loadAdjustments() {
  const list = await (await fetch('/api/adjustments')).json().catch(() => []);
  const box = $('#adjustment-list');
  if (!box) return;
  if (!list.length) {
    box.innerHTML = '<p class="todo-note">还没有对账调整。</p>';
    return;
  }
  box.innerHTML = list.map((a) => {
    const diffCls = Number(a.diff) >= 0 ? 'amt-in' : 'amt-out';
    const sign = Number(a.diff) >= 0 ? '+' : '';
    const state = a.reversed_by_id
      ? '<span class="tag">已撤销</span>'
      : `<button class="mini" data-reverse-id="${a.id}">撤销</button>`;
    return `<div class="adjustment-item">
      <span class="muted">${a.date}</span>
      <span class="${diffCls}">${sign}¥${Number(a.diff).toFixed(2)}</span>
      <span>${esc(a.note || '')}</span>
      ${state}
    </div>`;
  }).join('');
  box.querySelectorAll('[data-reverse-id]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('撤销这笔对账调整？账本余额会回到校准之前。')) return;
      const data = await requestJson(`/api/adjustments/${b.dataset.reverseId}/reverse`, {
        method: 'POST',
      }).catch((e) => e.data);
      if (data && data.ok) {
        showToast('已撤销调整', 'success');
        await Promise.all([loadAdjustments(), loadReconcile(), refresh()]);
      } else {
        alert((data && data.message) || '撤销失败');
      }
    }));
}

/* ---------- 备份控件 ---------- */

async function loadLatestBackup() {
  const box = $('#s-latest-backup');
  if (!box) return;
  const list = await (await fetch('/api/backups')).json().catch(() => []);
  const exportLink = $('#b-export');
  if (list.length) {
    box.textContent = list[0].filename + '（' + list[0].manifest.created_at + '）';
    exportLink.href = '/api/backups/export?filename=' + encodeURIComponent(list[0].filename);
  } else {
    box.textContent = '还没有备份';
    exportLink.removeAttribute('href');
  }
}

async function createBackupNow() {
  const data = await requestJson('/api/backups/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ include_images: true }),
  }).catch((e) => e.data);
  if (data && data.filename) {
    showToast('已创建备份：' + data.filename, 'success');
    await loadLatestBackup();
  } else {
    alert((data && data.message) || '备份失败');
  }
}

async function restoreBackupFile(file) {
  if (!confirm('恢复备份会覆盖当前账本数据（恢复前会自动先做一次安全备份）。确定继续？')) return;
  const form = new FormData();
  form.append('file', file);
  const res = await fetch('/api/backups/restore', { method: 'POST', body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { alert(data.message || '恢复失败'); return; }
  showToast('备份恢复完成', 'success');
  await Promise.all([loadSettings(), refresh()]);
}

async function openDataFolder() {
  const data = await requestJson('/api/system/open-data-folder', { method: 'POST' })
    .catch((e) => e.data);
  if (!data || !data.ok) {
    showToast((data && data.message) || '无法打开数据文件夹', 'error');
  }
}

async function shutdownService() {
  if (!confirm('退出服务后需要重新运行启动脚本才能再用。确定退出？')) return;
  const runtime = await (await fetch('/api/runtime')).json();
  if (!runtime.control_available) {
    showToast('开发者模式：请在终端按 Ctrl+C 停止服务', 'info');
    return;
  }
  const res = await fetch('/api/control/shutdown', {
    method: 'POST',
    headers: { 'X-Better-Money-Token': runtime.session_token },
  });
  if (res.ok) {
    showToast('服务已退出，可以关闭这个页面了', 'success');
  } else {
    showToast('退出失败', 'error');
  }
}

/* ---------- 更正初始余额（受保护） ---------- */

async function openCorrectModal() {
  const s = await (await fetch('/api/settings')).json();
  $('#correct-date').value = s.initial_balance_date || localISO(new Date());
  $('#correct-amount').value = Number(s.initial_balance || 0);
  $('#correct-modal').classList.remove('hidden');
}

function closeCorrectModal() {
  $('#correct-modal').classList.add('hidden');
}

async function correctInitialBalance() {
  const amount = parseFloat($('#correct-amount').value);
  const dateStr = $('#correct-date').value;
  if (isNaN(amount) || !dateStr) { alert('请填写起始日期和余额'); return; }
  const data = await requestJson('/api/settings/initial-balance', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initial_balance: amount, initial_balance_date: dateStr }),
  }).catch((e) => e.data);
  if (data && data.ok) {
    closeCorrectModal();
    showToast(`初始余额已更正为 ¥${amount.toFixed(2)}（已自动创建安全备份）`, 'success');
    await Promise.all([loadSettings(), refresh()]);
  } else {
    alert((data && data.message) || '更正失败');
  }
}

/* ---------- 首次引导 ---------- */

let onboardStepNo = 1;
let onboardMigration = null;

function showOnboarding() {
  $('#onboarding-modal').classList.remove('hidden');
  onboardStep(1);
}

function onboardStep(n) {
  onboardStepNo = n;
  for (let i = 1; i <= 4; i++) {
    document.getElementById('onboard-step-' + i).classList.toggle('hidden', i !== n);
  }
  $('#onboard-back').classList.toggle('hidden', n === 1);
  $('#onboard-next').classList.toggle('hidden', n === 4);
  $('#onboard-done').classList.toggle('hidden', n !== 4);
  $('#onboard-skip-ai').classList.toggle('hidden', n !== 4);
  document.querySelectorAll('[data-onboard-step-ind]').forEach((el) => {
    el.classList.toggle('active',
      parseInt(el.dataset.onboardStepInd, 10) === n);
  });
}

function onboardNext() {
  if (onboardStepNo === 1) {
    if (!$('#onboard-initial-date').value) {
      $('#onboard-initial-date').value = localISO(new Date());
    }
    onboardStep(2);
  } else if (onboardStepNo === 2) {
    if (!$('#onboard-initial-date').value) { alert('请选择起始日期'); return; }
    onboardStep(3);
  } else if (onboardStepNo === 3) {
    onboardStep(4);
  }
}

async function submitOnboarding(skipAi = false) {
  const payload = {
    monthly_budget: parseFloat($('#onboard-budget').value) || 0,
    auto_save_ratio: parseFloat($('#onboard-ratio').value) || 0,
  };
  if (!skipAi) {
    payload.ai_provider = $('#onboard-ai-provider').value;
    payload.api_base = $('#onboard-api-base').value.trim();
    payload.api_key = $('#onboard-ai-key').value.trim();
    payload.model_text = $('#onboard-model').value.trim();
  }
  payload.onboarding_completed = true;
  const saved = await requestJson('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).catch((e) => e.data);
  if (!saved || !saved.ok) { alert((saved && saved.message) || '保存失败'); return; }

  const initial = parseFloat($('#onboard-initial-balance').value);
  if (!isNaN(initial) && initial > 0) {
    const balance = await requestJson('/api/settings/initial-balance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        initial_balance: initial,
        initial_balance_date: $('#onboard-initial-date').value,
      }),
    }).catch((e) => e.data);
    if (!balance || !balance.ok) { alert((balance && balance.message) || '初始余额保存失败'); return; }
  }
  $('#onboarding-modal').classList.add('hidden');
  showToast('欢迎开始使用 Better-money，先记下第一笔吧', 'success');
  await Promise.all([loadSettings(), refresh()]);
}

async function loadOnboardingState() {
  const s = await (await fetch('/api/settings')).json();
  if (!s.onboarding_completed) showOnboarding();
  return s;
}

async function onboardPickFolder() {
  const picked = await requestJson('/api/migration/select-folder', { method: 'POST' })
    .catch((e) => e.data);
  if (!picked || picked.cancelled || !picked.path) return;
  const info = await requestJson('/api/migration/inspect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_path: picked.path }),
  }).catch((e) => e.data);
  if (!info || !info.transaction_count && !info.goal_count && !info.source_path) {
    alert((info && info.message) || '所选文件夹不是可迁移的 Better Money 数据');
    return;
  }
  onboardMigration = { path: picked.path, info };
  $('#onboard-migrate-info').textContent =
    `找到 ${info.transaction_count} 笔交易、${info.goal_count} 个目标；` +
    `建议起始日期 ${info.suggested_initial_balance_date}，起始余额 ${info.initial_balance} 元。`;
  $('#onboard-do-import').classList.remove('hidden');
}

async function onboardDoImport() {
  if (!onboardMigration) return;
  if (!confirm('确认迁移？迁移前会自动创建安全备份。')) return;
  const result = await requestJson('/api/migration/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_path: onboardMigration.path,
      initial_balance_date: onboardMigration.info.suggested_initial_balance_date,
    }),
  }).catch((e) => e.data);
  if (!result || !result.source_path) { alert((result && result.message) || '迁移失败'); return; }
  $('#onboard-migrate-info').textContent =
    `迁移完成：${result.transaction_count} 笔交易、${result.goal_count} 个目标。`;
  $('#onboard-initial-date').value = result.suggested_initial_balance_date;
  $('#onboard-initial-balance').value = result.initial_balance;
  onboardStep(2);
}

async function restoreBackupIntoOnboarding(file) {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch('/api/backups/restore', { method: 'POST', body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { alert(data.message || '恢复失败'); return; }
  $('#onboard-restore-info').textContent = '恢复完成！';
  onboardStep(2);
}

/* ---------- 标签切换 ---------- */

function bindTabs() {
  const tabs = [...document.querySelectorAll('.tab')];
  tabs.forEach((btn, index) => {
    btn.addEventListener('click', () => {
      tabs.forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
      tabs.forEach((b) => b.setAttribute('aria-selected', 'false'));
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      document.getElementById(btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'summaries') loadSummaries();
      if (btn.dataset.tab === 'goals') loadGoals();
      if (btn.dataset.tab === 'settings') loadReconcile();
      if (btn.dataset.tab === 'history') loadHistory();
    });
    btn.addEventListener('keydown', (e) => {
      let nextIndex = null;
      if (e.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
      if (e.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (e.key === 'Home') nextIndex = 0;
      if (e.key === 'End') nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      e.preventDefault();
      tabs[nextIndex].focus();
      tabs[nextIndex].click();
    });
  });
}

/* ---------- 初始化 ---------- */

async function init() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const today = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  $('#tx-date').value = today;
  fillCategories();
  initCharts();
  $('#tx-type').addEventListener('change', fillCategories);
  $('#tx-submit').addEventListener('click', submitTx);
  $('#ai-submit').addEventListener('click', aiSubmit);
  $('#img-btn').addEventListener('click', () => $('#img-file').click());
  $('#csv-btn').addEventListener('click', () => $('#csv-file').click());
  $('#img-file').addEventListener('change', uploadImages);
  $('#csv-file').addEventListener('change', importCsv);
  $('#confirm-save').addEventListener('click', confirmSave);
  $('#confirm-cancel').addEventListener('click', closeConfirm);
  $('#confirm-close').addEventListener('click', closeConfirm);
  $('#confirm-modal').addEventListener('click', (e) => {
    if (e.target === $('#confirm-modal')) closeConfirm();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('#confirm-modal').classList.contains('hidden')) closeConfirm();
    if (!$('#summary-modal').classList.contains('hidden')) closeSummaryModal();
  });
  $('#month-select').addEventListener('change', (e) => {
    currentMonth = e.target.value;
    loadStats();
  });
  $('#gen-summary').addEventListener('click', () => {
    summaryOverwriteId = null;
    openSummaryModal('this_week');
  });
  document.querySelectorAll('.preset-btn').forEach((b) =>
    b.addEventListener('click', () => openSummaryModal(b.dataset.preset)));
  $('#summary-close').addEventListener('click', closeSummaryModal);
  $('#summary-cancel').addEventListener('click', closeSummaryModal);
  $('#summary-submit').addEventListener('click', () => submitSummary());
  $('#summary-modal').addEventListener('click', (e) => {
    if (e.target === $('#summary-modal')) closeSummaryModal();
  });
  $('#g-add').addEventListener('click', addGoal);
  bindGoalList();
  $('#r-do').addEventListener('click', doReconcile);
  $('#hist-month').addEventListener('change', (e) => {
    histMonth = e.target.value;
    loadHistory();
  });
  $('#s-save').addEventListener('click', saveSettings);
  $('#s-test-ai').addEventListener('click', () => testAiConnection('s'));
  bindAiProvider('#s-ai-provider', '#s-api-base');
  bindAiProvider('#onboard-ai-provider', '#onboard-api-base');
  $('#correct-initial').addEventListener('click', openCorrectModal);
  $('#correct-close').addEventListener('click', closeCorrectModal);
  $('#correct-cancel').addEventListener('click', closeCorrectModal);
  $('#correct-save').addEventListener('click', correctInitialBalance);
  $('#correct-modal').addEventListener('click', (e) => {
    if (e.target === $('#correct-modal')) closeCorrectModal();
  });
  $('#b-create').addEventListener('click', createBackupNow);
  $('#b-restore').addEventListener('click', () => $('#b-restore-file').click());
  $('#b-restore-file').addEventListener('change', (e) => {
    if (e.target.files[0]) restoreBackupFile(e.target.files[0]);
    e.target.value = '';
  });
  $('#b-open-folder').addEventListener('click', openDataFolder);
  $('#s-shutdown').addEventListener('click', shutdownService);
  // 首次引导
  $('#onboard-new').addEventListener('click', () => {
    $('#onboard-migrate-box').classList.add('hidden');
    $('#onboard-restore-box').classList.add('hidden');
  });
  $('#onboard-migrate').addEventListener('click', () => {
    $('#onboard-migrate-box').classList.remove('hidden');
    $('#onboard-restore-box').classList.add('hidden');
  });
  $('#onboard-restore').addEventListener('click', () => {
    $('#onboard-restore-box').classList.remove('hidden');
    $('#onboard-migrate-box').classList.add('hidden');
  });
  $('#onboard-pick-folder').addEventListener('click', onboardPickFolder);
  $('#onboard-do-import').addEventListener('click', onboardDoImport);
  $('#onboard-pick-zip').addEventListener('click', () => $('#onboard-restore-file').click());
  $('#onboard-restore-file').addEventListener('change', (e) => {
    if (e.target.files[0]) restoreBackupIntoOnboarding(e.target.files[0]);
    e.target.value = '';
  });
  $('#onboard-next').addEventListener('click', onboardNext);
  $('#onboard-back').addEventListener('click', () => {
    if (onboardStepNo > 1) onboardStep(onboardStepNo - 1);
  });
  $('#onboard-done').addEventListener('click', () => submitOnboarding(false));
  $('#onboard-skip-ai').addEventListener('click', () => submitOnboarding(true));
  $('#onboard-test-ai').addEventListener('click', () => testAiConnection('onboard'));
  bindTabs();
  await loadSettings();
  await loadOnboardingState();
  await refresh();
  const health = await (await fetch('/api/health')).json();
  if (health.ai_configured) {
    $('#ai-note').textContent = 'AI 已就绪：支持多笔记账、收入识别、AA 分摊（如「聚餐 200 4人AA」）、日期补记（如「昨天午饭 15」）。';
  } else {
    $('#ai-note').textContent = '尚未配置 AI：到「设置」页填 API Key 后即可智能记账；当前可用下方手动记账。';
  }
}

init();
