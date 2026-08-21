"""周/月总结生成：收集真实素材 → LLM 写小作文 → 可选生图配图。

反模板化策略（设计文档第 6 节）：
- 喂给模型的是一份「素材数据块」（流水、分类、对比、预算、目标），不是写作框架；
- 每次生成带上"上次总结的开头"，要求开头句式明显不同；
- 要求引用具体事件（日期星期+商家+金额），先肯定再点 1-2 个问题，结尾一条可执行建议。
"""
import base64
import uuid
from datetime import date, timedelta

from openai import OpenAI

from app import db
from app.config import load_config
from app.ai import AIUnavailableError
from app.paths import get_paths

TONE_DESC = {
    "朋友": "语气轻松自然，像室友聊天，可以开无伤大雅的小玩笑。",
    "毒舌": "犀利吐槽、一针见血，但出发点是真心希望对方攒下钱。",
    "温柔": "以鼓励为主，委婉提醒问题，多给情绪价值。",
    "老师": "客观理性，条理清晰，像班主任点评，指出问题并给出方法。",
}

SYSTEM_PROMPT = """你是 Better-money 记账应用的「记账搭子」，现在给用户写一份{period}总结。语气档位：{tone}。{tone_desc}

硬性要求：
1. 完全基于用户消息里的【素材数据】写作，引用的数字必须与数据一致，绝对不许编造。
2. 必须具体引用至少 2 个真实事件（日期或星期 + 商家 + 金额），例如「周二食堂那笔 15 块」。
3. 禁止「时光飞逝」「本周总结如下」这类模板化开头和客套话；开头要自然、有变化。
4. 上次总结的开头是「{last_opening}」，你这次的开头句式必须和它明显不同。
5. 结构：先肯定这{period}做得好的地方 → 再点出 1-2 个最值得改的问题 → 最后给一条下{period}具体、可执行的小建议（一句话能照着做）。
6. 全文 {min_len}-{max_len} 字。像朋友面对面说话，不要列表罗列流水账，不要 markdown 标题、不要小标题、不要 emoji 堆砌。
7. 如果本期没有任何支出/收入记录：写一段 80 字以内的俏皮话提醒对方记账，不要硬写总结。
只输出正文本身，不要任何其他内容。"""


def period_range(period_type: str, anchor: date):
    """周（周一起算）/ 月 的起止日期。"""
    if period_type == "周":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    start = anchor.replace(day=1)
    nxt = (date(start.year + 1, 1, 1) if start.month == 12
           else date(start.year, start.month + 1, 1))
    return start, nxt - timedelta(days=1)


def _sum_expr(col: str) -> str:
    return f"SUM(CASE WHEN type='支出' THEN {col} ELSE -{col} END)"


def gather(period_type: str, start: date, end: date) -> dict:
    """从账本收集素材：流水、分类、对比、预算、目标。"""
    conn = db.get_conn()
    s, e = start.isoformat(), end.isoformat()

    def one(sql, *a):
        r = conn.execute(sql, a).fetchone()
        return float(r[0] or 0)

    expense = one(
        f"SELECT {_sum_expr('amount')} FROM transactions "
        "WHERE type IN ('支出','退款') AND date BETWEEN ? AND ?", s, e)
    income = one(
        "SELECT SUM(amount) FROM transactions WHERE type='收入' AND date BETWEEN ? AND ?", s, e)
    txs = conn.execute(
        "SELECT * FROM transactions WHERE type IN ('支出','退款','收入') "
        "AND date BETWEEN ? AND ? ORDER BY date, id", (s, e)).fetchall()
    cat_rows = conn.execute(
        f"SELECT category, {_sum_expr('amount')} AS t FROM transactions "
        "WHERE type IN ('支出','退款') AND date BETWEEN ? AND ? "
        "GROUP BY category HAVING t > 0 ORDER BY t DESC", (s, e)).fetchall()
    merchant_rows = conn.execute(
        "SELECT merchant, SUM(amount) AS t FROM transactions "
        "WHERE type='支出' AND merchant <> '' AND date BETWEEN ? AND ? "
        "GROUP BY merchant ORDER BY t DESC LIMIT 5", (s, e)).fetchall()

    big = [t for t in txs if t["type"] == "支出" and t["amount"] >= 100]
    incomes = [t for t in txs if t["type"] == "收入"]
    days = {}
    for t in txs:
        if t["type"] in ("支出", "退款"):
            days[t["date"]] = days.get(t["date"], 0) + (t["amount"] if t["type"] == "支出" else -t["amount"])
    estimated_n = sum(1 for t in txs if t["estimated"])

    # 上一周期对比
    length = (end - start).days + 1
    prev_start, prev_end = start - timedelta(days=length), start - timedelta(days=1)
    prev_expense = one(
        f"SELECT {_sum_expr('amount')} FROM transactions "
        "WHERE type IN ('支出','退款') AND date BETWEEN ? AND ?",
        prev_start.isoformat(), prev_end.isoformat())
    prev_cat_rows = conn.execute(
        f"SELECT category, {_sum_expr('amount')} AS t FROM transactions "
        "WHERE type IN ('支出','退款') AND date BETWEEN ? AND ? "
        "GROUP BY category HAVING t > 0", (prev_start.isoformat(), prev_end.isoformat())).fetchall()

    goals = conn.execute(
        "SELECT name, price, saved, status FROM goals ORDER BY priority, id").fetchall()
    cfg = load_config()
    budget = float(cfg["monthly_budget"] or 0)
    anchor_month_start = start.replace(day=1).isoformat()
    month_spent = one(
        f"SELECT {_sum_expr('amount')} FROM transactions "
        "WHERE type IN ('支出','退款') AND date >= ?", anchor_month_start)
    month_income_total = one(
        "SELECT SUM(amount) FROM transactions WHERE type='收入' AND date >= ?",
        anchor_month_start)
    days_in_month = (date(start.year + 1, 1, 1) - timedelta(days=1)).day if start.month == 12 \
        else (date(start.year, start.month + 1, 1) - timedelta(days=1)).day
    wins_row = conn.execute(
        "SELECT SUM(amount) AS t, COUNT(*) AS c FROM savings_wins "
        "WHERE date BETWEEN ? AND ?", (s, e)).fetchone()
    conn.close()

    savings_rate = None
    if month_income_total > 0:
        savings_rate = (month_income_total - month_spent) / month_income_total

    return {
        "period_type": period_type, "start": s, "end": e,
        "expense": round(expense, 2), "income": round(income, 2),
        "tx_count": len([t for t in txs if t["type"] != "退款"]),
        "cats": [(r["category"], round(r["t"], 2)) for r in cat_rows],
        "top_merchants": [(r["merchant"], round(r["t"], 2)) for r in merchant_rows],
        "big": big, "incomes": incomes, "days": days,
        "estimated_n": estimated_n,
        "prev_expense": round(prev_expense, 2),
        "prev_cats": [(r["category"], round(r["t"], 2)) for r in prev_cat_rows],
        "budget": budget, "month_spent": round(month_spent, 2),
        "days_in_month": days_in_month,
        "goals": [(g["name"], g["price"], g["saved"], g["status"]) for g in goals],
        "wins_total": round(float(wins_row["t"] or 0), 2),
        "wins_count": int(wins_row["c"] or 0),
        "savings_rate": savings_rate,
    }


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def build_data_block(ctx: dict) -> str:
    """把素材压缩成给模型的「数据块」。"""
    lines = [f"时间范围: {ctx['start']} ~ {ctx['end']}",
             f"总支出: {_fmt(ctx['expense'])} 元（退款已冲减）｜总收入: {_fmt(ctx['income'])} 元｜支出笔数: {ctx['tx_count']}"]
    if ctx["cats"]:
        lines.append("分类支出: " + ", ".join(f"{c} {_fmt(v)}" for c, v in ctx["cats"]))
    if ctx["top_merchants"]:
        lines.append("常去的商家: " + ", ".join(f"{m} {_fmt(v)}" for m, v in ctx["top_merchants"]))
    if ctx["days"]:
        lines.append("每日支出: " + ", ".join(f"{d}({_fmt(v)})" for d, v in sorted(ctx["days"].items())))
    if ctx["big"]:
        lines.append("大额支出(>=100元): " + ", ".join(
            f"{t['date']} {t['merchant']} {_fmt(t['amount'])}（备注: {t['note'] or '无'}）" for t in ctx["big"]))
    else:
        lines.append("大额支出(>=100元): 无")
    if ctx["incomes"]:
        lines.append("收入明细: " + ", ".join(
            f"{t['date']} {t['category']} {_fmt(t['amount'])}" for t in ctx["incomes"]))
    diff = ctx["expense"] - ctx["prev_expense"]
    prev_label = "上周" if ctx["period_type"] == "周" else "上月"
    lines.append(f"对比: {prev_label}总支出 {_fmt(ctx['prev_expense'])}；本期比{prev_label}{'多花' if diff > 0 else '少花'} {_fmt(abs(diff))}")
    if ctx["prev_cats"]:
        prev_map = dict(ctx["prev_cats"])
        lines.append("分类变化: " + ", ".join(
            f"{c} {prev_label} {_fmt(prev_map.get(c, 0))} -> 本期 {_fmt(v)}" for c, v in ctx["cats"]))
    lines.append(f"预算: 月预算 {_fmt(ctx['budget'])}，本月已花 {_fmt(ctx['month_spent'])}，"
                 f"日均参考 {_fmt(ctx['budget'] / max(ctx['days_in_month'], 1))}")
    if ctx["goals"]:
        lines.append("目标: " + ", ".join(
            f"{n} 已存 {_fmt(s)}/{_fmt(p)}（{status}）" for n, p, s, status in ctx["goals"]))
    else:
        lines.append("目标: 暂无")
    if ctx["estimated_n"]:
        lines.append(f"备注: 有 {ctx['estimated_n']} 笔估算金额待核对")
    if ctx["wins_total"] > 0:
        lines.append(f"冷静期战绩: 本期忍住了 {ctx['wins_count']} 次冲动消费，省下 {_fmt(ctx['wins_total'])} 元")
    if ctx["savings_rate"] is not None:
        lines.append(f"本月储蓄率: {ctx['savings_rate'] * 100:.0f}%")
    return "\n".join(lines)


def _client():
    cfg = load_config()
    if not cfg.get("api_key"):
        raise AIUnavailableError("未配置 API Key，请在「设置」页填写")
    return OpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"]), cfg["model_text"]


def _last_opening() -> str:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT content FROM summaries ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
    conn.close()
    if row and row["content"]:
        return row["content"][:80].replace("\n", " ")
    return "（还没有过总结）"


def _gen_image(period_type: str) -> str:
    """可选配图：失败不影响总结正文。"""
    cfg = load_config()
    if not cfg.get("image_gen_enabled") or not cfg.get("api_key"):
        return ""
    try:
        client = OpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"])
        prompt = (f"可爱扁平插画，主题：学生{period_type}末记账总结，"
                  "元素：小猪存钱罐、奶茶、书本、校园，清新马卡龙配色，画面无文字")
        resp = client.images.generate(model=cfg["model_image"], prompt=prompt,
                                      size="1024x1024", n=1)
        b64 = resp.data[0].b64_json if resp.data else ""
        if not b64:
            return ""
        d = get_paths().images_dir / "summaries"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{uuid.uuid4().hex}.png"
        p.write_bytes(base64.b64decode(b64))
        return str(p)
    except Exception:
        return ""


def _upsert(period_type: str, start: str, end: str, content: str, image_path: str) -> int:
    conn = db.get_conn()
    existing = conn.execute(
        "SELECT id FROM summaries WHERE period_type = ? AND period_start = ? "
        "AND period_end = ?",
        (period_type, start, end)).fetchone()
    if existing:
        conn.execute(
            "UPDATE summaries SET content = ?, image_path = ?, expired = 0, "
            "created_at = ? WHERE id = ?",
            (content, image_path, db.now_str(), existing["id"]))
        summary_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO summaries(period_type, period_start, period_end, content, image_path, expired, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (period_type, start, end, content, image_path, db.now_str()))
        summary_id = cur.lastrowid
    conn.commit()
    conn.close()
    return summary_id


def generate(period_type: str, start: date, end: date) -> tuple[str, str, int]:
    """生成指定区间的总结 → (正文, 配图路径, 记录 id)。AI 不可用抛 AIUnavailableError。

    周/月只决定写作风格与篇幅，区间可以是任意起止日期。
    """
    ctx = gather(period_type, start, end)
    cfg = load_config()
    tone = str(cfg.get("tone") or "朋友")
    min_len, max_len = (200, 400) if period_type == "周" else (400, 800)
    system = SYSTEM_PROMPT.format(
        period=period_type, tone=tone, tone_desc=TONE_DESC.get(tone, TONE_DESC["朋友"]),
        last_opening=_last_opening(), min_len=min_len, max_len=max_len,
    )
    client, model = _client()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": "【素材数据】\n" + build_data_block(ctx)},
            ],
            temperature=0.8,
        )
    except Exception as e:
        raise AIUnavailableError(str(e)) from e
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise AIUnavailableError("模型返回空内容")
    image_path = _gen_image(period_type)
    summary_id = _upsert(period_type, start.isoformat(), end.isoformat(), content, image_path)
    return content, image_path, summary_id
