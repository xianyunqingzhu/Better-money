"""Mock OpenAI 兼容服务器：没有真实 API Key 时用来测试解析/总结流程。

路由：/v1/images/generations → 返回 1x1 PNG；/v1/chat/completions →
- 系统提示含「总结/搭子」→ 返回总结小作文纯文本
- 多模态消息 → VISION_MOCK JSON
- 其余 → TEXT_MOCK JSON
"""
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

TEXT_MOCK = {
    "items": [
        {"date": "2026-08-16", "amount": 200.0, "type": "收入", "category": "兼职", "merchant": "", "note": "昨天兼职", "estimated": 0},
        {"date": "2026-08-17", "amount": 15.0, "type": "支出", "category": "餐饮", "merchant": "食堂", "note": "午饭", "estimated": 0},
        {"date": "2026-08-17", "amount": 12.0, "type": "支出", "category": "奶茶咖啡", "merchant": "奶茶店", "note": "", "estimated": 0},
        {"date": "2026-08-17", "amount": 50.0, "type": "支出", "category": "餐饮", "merchant": "聚餐", "note": "4人AA，原价200", "estimated": 0},
        {"date": "2026-08-17", "amount": 30.0, "type": "支出", "category": "学习", "merchant": "打印店", "note": "", "estimated": 1},
    ],
    "questions": ["打印资料具体多少钱？"],
}

VISION_MOCK = {
    "items": [
        {"date": "2026-08-17", "amount": 50.0, "type": "支出", "category": "餐饮", "merchant": "某火锅店",
         "note": "4人AA，原价200", "estimated": 0,
         "line_items": [{"name": "火锅套餐", "qty": 1, "price": 200.0}]},
        {"date": "2026-08-17", "amount": 45.5, "type": "支出", "category": "生活", "merchant": "超市",
         "note": "超市小票", "estimated": 0,
         "line_items": [{"name": "纸巾", "qty": 2, "price": 10.0}, {"name": "牛奶", "qty": 1, "price": 25.5}]},
    ],
    "questions": [],
}

SUMMARY_TEXT = (
    "周一食堂那 15 块吃得朴素，值得表扬；周四那杯 12 块的奶茶其实可以省。\n"
    "这周一共只花了 27 块，比上周少了快一半，攒钱的气势起来了。\n"
    "下周试试把奶茶换成白水，一杯就是 12 块，一个月下来够买半本书。"
)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        payload = json.loads(body)

        if "images/generations" in self.path:
            resp = json.dumps({
                "created": 1,
                "data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}],
            })
        else:
            msgs = payload.get("messages", [])
            sys_text = msgs[0].get("content", "") if msgs else ""
            is_vision = any(isinstance(m.get("content"), list) for m in msgs)
            if "搭子" in sys_text or "总结" in sys_text:
                content = SUMMARY_TEXT
            elif is_vision:
                content = json.dumps(VISION_MOCK, ensure_ascii=False)
            else:
                content = json.dumps(TEXT_MOCK, ensure_ascii=False)
            resp = json.dumps({
                "id": "mock",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }, ensure_ascii=False)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(resp.encode("utf-8"))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("mock openai on http://127.0.0.1:8001/v1")
    HTTPServer(("127.0.0.1", 8001), Handler).serve_forever()
