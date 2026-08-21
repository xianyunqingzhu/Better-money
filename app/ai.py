"""AI 层：可切换供应商的 LLM 客户端。

所有供应商走 OpenAI 兼容接口：OpenAI / DeepSeek / Qwen（阿里云百炼）都支持。
在设置页改 api_base / api_key / model_text / model_vision 即可切换，无需改代码。
"""
import base64
import json
import re
from datetime import date
from pathlib import Path

from openai import BadRequestError, OpenAI

from app.config import load_config


class AIUnavailableError(Exception):
    """网络、认证或 API 服务不可用（前端亮红色横幅，用户自行解决）。"""


AI_PROVIDERS = {
    "OpenAI": "https://api.openai.com/v1",
    "DeepSeek": "https://api.deepseek.com",
    "Qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "自定义": "",
}


def test_connection(api_base: str, api_key: str, model: str) -> None:
    """Verify one-token chat completion against an unsaved draft; never logs the key."""
    if not api_key.strip():
        raise AIUnavailableError("未填写 API Key")
    if not api_base.strip():
        raise AIUnavailableError("未填写 API Base")
    if not model.strip():
        raise AIUnavailableError("未填写模型名称")
    client = OpenAI(
        api_key=api_key, base_url=api_base, timeout=10.0, max_retries=0)
    try:
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复 OK"}],
            max_tokens=4,
        )
    except Exception as e:
        raise AIUnavailableError(str(e)) from e


EXPENSE_CATS = ["餐饮", "奶茶咖啡", "交通", "学习", "购物", "娱乐", "生活", "其他"]
INCOME_CATS = ["兼职", "红包", "家里给", "其他收入"]
VALID_TYPES = {"支出", "收入", "退款", "取现", "转账", "还款"}

SYSTEM_PROMPT = """你是 Better-money 记账工具的解析器。用户用中文描述花销/收入，可能多行、多笔。
你的任务：把文字解析成结构化记账条目。只输出一个 JSON 对象，不要输出任何其他内容。

输出格式：
{"items":[{"date":"YYYY-MM-DD","amount":数字,"type":"支出","category":"分类","merchant":"商家","note":"备注","estimated":0}],"questions":["需要向用户澄清的问题"]}

规则：
1. 一行可能含多笔，按语义拆分（「食堂15和奶茶12」→ 两笔）。
2. 支出分类只能是：餐饮、奶茶咖啡、交通、学习、购物、娱乐、生活、其他。
3. 收入分类只能是：兼职、红包、家里给、其他收入。识别信号：「兼职」「工资」「收到红包」
   「家里给」「爸妈给」→ type=收入；「退了XX」→ type=退款（category 用原商品所属分类）。
4. AA：文字含「N人AA」「N个人A」→ amount=总价÷N（保留两位小数，除不尽的四舍五入）；
   「AA后我付了X」→ amount=X；note 保留原价信息，如「4人AA，原价200」。
5. 时间：未指明时间用当天日期；「昨天」「前天」换算为具体日期；用户提示中会给出今天的日期和星期。
6. 金额：统一为数字（元），支持「15块」「¥15」「15.5」；「大概30」→ amount=30 且 estimated=1；
   完全没有金额信息 → 该笔不放入 items，放入 questions，如「买了瓶水，多少钱？」。
7. merchant 从文字提取（食堂、罗森、淘宝、超市等）；提取不出留空字符串 ""。
8. 取现、转账给家人、还钱给朋友 → type 分别为 取现/转账/还款，category 用「—」。
9. 无法归类的支出 → category=其他。
10. 只输出 JSON 对象本身，不要 markdown 代码块。"""

VISION_SYSTEM_PROMPT = """你是 Better-money 记账工具的票据识别器。用户上传购物小票、支付截图、订单截图的照片。
识别图片中的消费信息。只输出一个 JSON 对象，不要输出任何其他内容。

输出格式：
{"items":[{"date":"YYYY-MM-DD","amount":数字,"type":"支出","category":"分类","merchant":"商家","note":"备注","estimated":0,"line_items":[{"name":"商品名","qty":数量,"price":单价}]}],"questions":["需要向用户澄清的问题"]}

规则：
1. amount 取实付总额（折扣后、优惠后），优先看「合计 / 实付 / 总计 / 应收 / 支付金额」。
2. date 取小票上的收银时间/支付时间；图片上没有时间则用用户提供的日期。
3. category 只能从：餐饮、奶茶咖啡、交通、学习、购物、娱乐、生活、其他 中选择。
4. 一张小票若明显包含多类商品（如超市小票：牛奶→餐饮、纸巾→生活、笔→学习），
   拆成多笔 items，每笔 category 对应商品类别，line_items 放该笔包含的商品。
5. 小票上的商品简称还原为常用名（如「蒙牛纯牛奶250ml」→「牛奶」）。
6. merchant 取商家/店名/收款方名称。
7. 识别不出的信息不要瞎编；关键信息（金额）识别不出 → 该笔进 questions，
   如「小票总金额看不清，实付多少钱？」。
8. 用户可能附文字说明（如「4人AA」「AA后我付了50」）→ 按说明计算实际承担金额，
   note 保留原价信息；「我请客」→ 记全额。
9. 只输出 JSON 对象本身，不要 markdown 代码块。"""

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}


def _extract_json(content: str):
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("模型输出不是合法 JSON")


def _to_float(v):
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        s = v.replace("¥", "").replace("￥", "").replace("元", "").replace("块", "").strip()
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    return f if f > 0 else None


def _client(model_key: str):
    cfg = load_config()
    if not cfg.get("api_key"):
        raise AIUnavailableError("未配置 API Key，请在「设置」页填写")
    return OpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"]), cfg[model_key]


def _chat(client, model, messages):
    try:
        return client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_object"}, temperature=0,
        )
    except BadRequestError:
        # 某些兼容服务不支持 json_object，去掉后重试
        return client.chat.completions.create(
            model=model, messages=messages, temperature=0,
        )


def _today_desc(record_date: str) -> str:
    try:
        d = date.fromisoformat(record_date)
    except ValueError:
        d = date.today()
    return f"今天是 {d.isoformat()} {WEEKDAYS[d.weekday()]}"


def _normalize(data: dict, record_date: str) -> dict:
    """校验并规整模型输出：金额、类型、分类、日期、单品明细。"""
    items = []
    for it in data.get("items", []):
        if not isinstance(it, dict):
            continue
        amount = _to_float(it.get("amount"))
        if amount is None:
            continue
        itype = it.get("type") if it.get("type") in VALID_TYPES else "支出"
        cat = str(it.get("category") or "其他")
        if itype == "收入" and cat not in INCOME_CATS:
            cat = "其他收入"
        elif itype in ("支出", "退款") and cat not in EXPENSE_CATS:
            cat = "其他"
        elif itype in ("取现", "转账", "还款"):
            cat = "—"
        d_str = str(it.get("date") or record_date)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d_str):
            d_str = record_date
        row = {
            "date": d_str,
            "amount": round(amount, 2),
            "type": itype,
            "category": cat,
            "merchant": str(it.get("merchant") or ""),
            "note": str(it.get("note") or ""),
            "estimated": 1 if it.get("estimated") else 0,
        }
        lis = it.get("line_items")
        if isinstance(lis, list) and lis:
            clean = []
            for li in lis:
                if not isinstance(li, dict):
                    continue
                name = str(li.get("name") or "").strip()
                if not name:
                    continue
                clean.append({
                    "name": name,
                    "qty": _to_float(li.get("qty")) or 1,
                    "price": _to_float(li.get("price")) or 0,
                })
            if clean:
                row["line_items"] = clean
        items.append(row)
    questions = [str(q) for q in (data.get("questions") or []) if str(q)]
    return {"items": items, "questions": questions}


def parse_text(text: str, record_date: str) -> dict:
    """解析记账文字 → {items, questions}。AI 不可用时抛 AIUnavailableError。"""
    client, model = _client("model_text")
    try:
        resp = _chat(client, model, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{_today_desc(record_date)}。以下是我的记账内容：\n{text}"},
        ])
    except Exception as e:  # 网络 / 认证 / 限流等
        raise AIUnavailableError(str(e)) from e
    return _normalize(_extract_json(resp.choices[0].message.content or ""), record_date)


def parse_image(image_path: str, text_note: str, record_date: str) -> dict:
    """识别小票/截图图片 → {items, questions}。AI 不可用时抛 AIUnavailableError。"""
    client, model = _client("model_vision")
    p = Path(image_path)
    b64 = base64.b64encode(p.read_bytes()).decode()
    mime = MIME_BY_EXT.get(p.suffix.lower(), "image/jpeg")
    note = text_note.strip() or "无"
    try:
        resp = _chat(client, model, [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"{_today_desc(record_date)}。图片说明：{note}"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]},
        ])
    except Exception as e:
        raise AIUnavailableError(str(e)) from e
    return _normalize(_extract_json(resp.choices[0].message.content or ""), record_date)
