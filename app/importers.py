"""账单 CSV/Excel 解析：微信支付 / 支付宝导出格式，纯规则解析，不依赖 AI。"""
import csv
import io
import re

# 支付宝「交易分类」→ 本系统分类
ALIPAY_CAT_MAP = {
    "餐饮美食": "餐饮", "饮料甜品": "奶茶咖啡", "交通出行": "交通",
    "学习与教育": "学习", "服饰装扮": "购物", "日用百货": "生活",
    "数码电器": "购物", "休闲娱乐": "娱乐", "文化休闲": "娱乐",
    "运动户外": "购物", "医疗健康": "生活", "生活服务": "生活",
    "住房物业": "生活", "通讯物流": "生活",
}

HEADER_KEYS = {
    "time": ("交易时间", "时间"),
    "peer": ("交易对方", "对方"),
    "goods": ("商品说明", "商品"),
    "io": ("收/支", "收支"),
    "amount": ("金额",),
    "type": ("交易类型",),
    "cat": ("交易分类",),
}


def _find_columns(header):
    idx = {}
    for name, keys in HEADER_KEYS.items():
        for k in keys:
            for i, h in enumerate(header):
                s = str(h).strip()
                if s.startswith(k):
                    idx[name] = i
                    break
            if name in idx:
                break
    return idx


def _norm_date(s: str) -> str:
    s = str(s).strip().split(" ")[0].split("T")[0]
    s = s.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def _amount(s) -> float | None:
    s = str(s).replace("¥", "").replace("￥", "").replace("元", "").replace(",", "").strip()
    try:
        f = float(s)
    except ValueError:
        return None
    return round(f, 2) if f > 0 else None


def parse_rows(rows) -> tuple[list[dict], int]:
    """rows: 二维列表（首行为表头）。返回 (items, 跳过行数)。"""
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise ValueError("空文件")
    idx = _find_columns(rows[0])
    if "time" not in idx or "amount" not in idx:
        raise ValueError("缺少「交易时间」或「金额」列，请确认是微信/支付宝导出的账单")

    items, skipped = [], 0
    for r in rows[1:]:
        if len(r) <= max(idx.values()):
            skipped += 1
            continue
        io_flag = str(r[idx["io"]]).strip() if "io" in idx else "支出"
        if io_flag == "收入":
            itype = "收入"
        elif io_flag == "支出":
            itype = "支出"
        else:  # 「/」「不计收支」等
            skipped += 1
            continue
        amount = _amount(r[idx["amount"]])
        if amount is None:
            skipped += 1
            continue
        ttype = str(r[idx["type"]]).strip() if "type" in idx else ""
        alipay_cat = str(r[idx["cat"]]).strip() if "cat" in idx else ""
        if itype == "收入":
            cat = "红包" if "红包" in ttype else "其他收入"
        elif "提现" in ttype:
            itype, cat = "取现", "—"
        elif "转账" in ttype:
            itype, cat = "转账", "—"
        else:
            cat = ALIPAY_CAT_MAP.get(alipay_cat, "其他") if alipay_cat else "其他"
        items.append({
            "date": _norm_date(r[idx["time"]]),
            "amount": amount,
            "type": itype,
            "category": cat,
            "merchant": str(r[idx["peer"]]).strip() if "peer" in idx else "",
            "note": str(r[idx["goods"]]).strip() if "goods" in idx else "",
            "estimated": 0,
        })
    return items, skipped


def parse_bill_csv(text: str) -> tuple[list[dict], int]:
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    return parse_rows(list(reader))
