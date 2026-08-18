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
  ['chart-cat', 'chart-daily', 'chart-week', 'chart-goal'].forEach((id) => {
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

  // 目标进度环
  const goals = await (await fetch('/api/goals')).json();
  const active = goals.find((g) => ['冷静期', '进行中'].includes(g.status))
    || goals.find((g) => g.status === '已暂停');
  if (active && active.price > 0) {
    const pct = Math.min(100, (active.saved / active.price) * 100);
    charts['chart-goal'].setOption({
      animation: !REDUCE_MOTION,
      title: {
        text: active.name,
        subtext: `已存 ¥${active.saved.toFixed(2)} / ¥${active.price.toFixed(2)}`,
        left: 'center', top: '4%',
        textStyle: { color: '#34463f', fontSize: 13, fontWeight: 600 },
        subtextStyle: { fontSize: 11, color: CHART_TEXT },
      },
      series: [{
        type: 'gauge', startAngle: 90, endAngle: -270, min: 0, max: 100,
        pointer: { show: false },
        progress: { show: true, roundCap: true, width: 12, itemStyle: { color: PALETTE[0] } },
        axisLine: { roundCap: true, lineStyle: { width: 12, color: [[1, '#e8efec']] } },
        axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
        detail: { formatter: '{value}%', color: '#17241f', fontSize: 19, fontWeight: 700, offsetCenter: [0, '62%'] },
        data: [{ value: Math.round(pct * 10) / 10 }],
      }],
    }, true);
  } else {
    charts['chart-goal'].setOption({
      title: { show: false }, series: [],
      graphic: [{
        type: 'text', left: 'center', top: 'middle',
        style: { text: '暂无目标\n去目标清单创建第一个计划', fill: CHART_AXIS, fontSize: 12, lineHeight: 20, textAlign: 'center' },
      }],
    }, true);
  }
}

/* ---------- 周/月总结（M5） ---------- */

function currentMondayISO() {
  const d = new Date();
  const monday = new Date(d);
  monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return monday.toISOString().slice(0, 10);
}

async function loadSummaries() {
  const list = await (await fetch('/api/summaries')).json();
  const box = $('#summary-list');
  if (!list.length) {
    box.innerHTML = '<p class="todo-note">还没有总结，点上面按钮生成第一篇吧。</p>';
  } else {
    box.innerHTML = list.map(renderSummary).join('');
  }
  // 轻提示：有账但本周总结缺失
  const hasWeek = list.some((s) => s.period_type === '周' && s.period_start === currentMondayISO());
  const txs = await (await fetch('/api/transactions?limit=1')).json();
  const hint = $('#summary-hint');
  if (!hasWeek && txs.length) {
    hint.textContent = '本周有记账记录但还没有周总结，点「生成本周总结」看看吧。';
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
  return `<div class="summary-card">
    <div class="summary-head">
      <b>${s.period_type === '周' ? '周总结' : '月总结'}（${s.period_start} ~ ${s.period_end}）</b>
      ${badge}
      <span class="muted">${s.created_at}</span>
    </div>
    ${img}
    <div class="summary-body">${paras}</div>
  </div>`;
}

async function genSummary(type) {
  const btn = type === '周' ? $('#gen-week') : $('#gen-month');
  const label = type === '周' ? '生成本周总结' : '生成本月总结';
  btn.disabled = true; btn.textContent = '写作中…';
  try {
    const res = await fetch('/api/summaries/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ period_type: type }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (data.error === 'ai_unavailable') {
        showAiBanner(data.message || '请检查网络或 API 配置');
        alert('生成失败：' + (data.message || 'AI 不可用'));
      } else {
        alert('生成失败：' + (data.message || '未知原因'));
      }
      return;
    }
    hideAiBanner();
    await loadSummaries();
  } catch (e) {
    showAiBanner('网络错误');
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
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
  if (act === 'delete' && !confirm('删除这个目标？')) return;
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

async function loadSettings() {
  const s = await (await fetch('/api/settings')).json();
  $('#s-initial').value = s.initial_balance;
  $('#s-budget').value = s.monthly_budget;
  $('#s-ratio').value = s.auto_save_ratio;
  $('#s-tone').value = s.tone;
  $('#s-api-base').value = s.api_base;
  $('#s-api-key').value = s.api_key;
}

async function saveSettings() {
  const payload = {
    initial_balance: parseFloat($('#s-initial').value) || 0,
    monthly_budget: parseFloat($('#s-budget').value) || 0,
    auto_save_ratio: parseFloat($('#s-ratio').value) || 0,
    tone: $('#s-tone').value,
    api_base: $('#s-api-base').value.trim(),
    api_key: $('#s-api-key').value.trim(),
  };
  const res = await (await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })).json();
  alert(res.ok ? '已保存' : '保存失败');
  refresh();
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
    if (e.key === 'Escape' && !$('#confirm-modal').classList.contains('hidden')) closeConfirm();
  });
  $('#month-select').addEventListener('change', (e) => {
    currentMonth = e.target.value;
    loadStats();
  });
  $('#gen-week').addEventListener('click', () => genSummary('周'));
  $('#gen-month').addEventListener('click', () => genSummary('月'));
  $('#g-add').addEventListener('click', addGoal);
  bindGoalList();
  $('#r-do').addEventListener('click', doReconcile);
  $('#hist-month').addEventListener('change', (e) => {
    histMonth = e.target.value;
    loadHistory();
  });
  $('#s-save').addEventListener('click', saveSettings);
  bindTabs();
  await loadSettings();
  await refresh();
  const health = await (await fetch('/api/health')).json();
  if (health.ai_configured) {
    $('#ai-note').textContent = 'AI 已就绪：支持多笔记账、收入识别、AA 分摊（如「聚餐 200 4人AA」）、日期补记（如「昨天午饭 15」）。';
  } else {
    $('#ai-note').textContent = '尚未配置 AI：到「设置」页填 API Key 后即可智能记账；当前可用下方手动记账。';
  }
}

init();
