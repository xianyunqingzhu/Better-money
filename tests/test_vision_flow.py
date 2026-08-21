"""M3 端到端自测：图片识别（确认面板）+ CSV 导入 + 去重 + 单品明细落库。"""
import base64
import atexit
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.paths import get_paths

BASE = os.environ.get("BETTER_MONEY_TEST_BASE_URL", "http://127.0.0.1:8642")
SCRIPT_DIR = Path(__file__).resolve().parent

# 共享持久客户端：Windows 上每新建一次 httpx 客户端都要重建 SSL 上下文
# （即使访问 http），证书库枚举偶发卡顿；复用一个客户端只建一次。
_client = httpx.Client(base_url=BASE, timeout=30)

# 生成 1x1 PNG 作为测试图片
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
with tempfile.NamedTemporaryFile(
    mode="wb", prefix="better-money-xiaopiao-", suffix=".png", delete=False
) as generated_image:
    generated_image.write(PNG_1PX)
    GENERATED_IMAGE = Path(generated_image.name)
atexit.register(GENERATED_IMAGE.unlink, missing_ok=True)

# 1) 图片上传识别（带 AA 说明）
with GENERATED_IMAGE.open("rb") as image_file:
    r = _client.post(
        "/api/upload_images",
        files=[("files", ("xiaopiao.png", image_file, "image/png"))],
        data={"note": "4人AA", "date": "2026-08-17"},
    )
print("upload:", r.status_code)
data = r.json()
print(json.dumps(data, ensure_ascii=False)[:400])
assert r.status_code == 200, data
assert len(data["items"]) == 2
assert any(i.get("line_items") for i in data["items"]), "应包含单品明细"
assert data["items"][0]["amount"] == 50.0, "AA 后应记 50"

# 2) 确认面板：用户修改第一笔金额后确认入账
data["items"][0]["amount"] = 66.0
r2 = _client.post("/api/confirm_items", json={"items": data["items"]})
d2 = r2.json()
print("confirm:", json.dumps(d2, ensure_ascii=False)[:200])
assert d2["saved"] == 2, d2

conn = sqlite3.connect(get_paths().db_path)
n_li = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
print("line_items in db:", n_li)
assert n_li == 3, "应有 3 条单品明细"
conn.close()

# 3) CSV 导入（微信账单格式）
with (SCRIPT_DIR / "sample_bill.csv").open("rb") as csv_file:
    r3 = _client.post(
        "/api/import_csv",
        files=[("file", ("bill.csv", csv_file, "text/csv"))],
    )
d3 = r3.json()
print("csv parse:", json.dumps(d3, ensure_ascii=False))
assert r3.status_code == 200 and len(d3["items"]) == 3, d3
assert d3["items"][0]["type"] == "支出" and d3["items"][0]["amount"] == 12.5
assert d3["items"][1]["type"] == "收入" and d3["items"][1]["category"] == "红包"
assert d3["items"][2]["type"] == "转账" and d3["items"][2]["category"] == "—"

# 4) 确认 CSV 条目入账
r4 = _client.post("/api/confirm_items", json={"items": d3["items"]})
assert r4.json()["saved"] == 3, r4.json()
print("csv confirm ok")

# 5) 重复确认 → 全部跳过
r5 = _client.post("/api/confirm_items", json={"items": d3["items"]})
assert r5.json()["saved"] == 0 and r5.json()["skipped"], r5.json()
print("dedup ok")

print("M3 E2E ALL OK")
