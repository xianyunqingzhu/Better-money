/** AI 层（app/ai.py 的 TS 移植）：OpenAI 兼容接口 + 解析/识别 + 结果规整。 */
import { todayDesc } from "./dates";
import { EXPENSE_CATS, INCOME_CATS, VALID_TYPES } from "./types";

export class AIUnavailableError extends Error {}

export const AI_PROVIDER_BASES: Record<string, string> = {
  OpenAI: "https://api.openai.com/v1",
  DeepSeek: "https://api.deepseek.com",
  Qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  自定义: "",
};

const SYSTEM_PROMPT = `你是 Better-money 记账工具的解析器。用户用中文描述花销/收入，可能多行、多笔。
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
10. 只输出 JSON 对象本身，不要 markdown 代码块。`;

const VISION_SYSTEM_PROMPT = `你是 Better-money 记账工具的票据识别器。用户上传购物小票、支付截图、订单截图的照片。
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
9. 只输出 JSON 对象本身，不要 markdown 代码块。`;

const MIME_BY_EXT: Record<string, string> = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".webp": "image/webp",
  ".gif": "image/gif",
};

export interface ParsedItem {
  date: string;
  amount: number;
  type: string;
  category: string;
  merchant: string;
  note: string;
  estimated: number;
  line_items?: { name: string; qty: number; price: number }[];
}

export interface ParseResult {
  items: ParsedItem[];
  questions: string[];
}

function extractJson(content: string): unknown {
  let text = content.trim();
  if (text.startsWith("```")) {
    text = text.replace(/^```[a-zA-Z]*\s*|\s*```$/g, "");
  }
  try {
    return JSON.parse(text);
  } catch {
    const match = /\{.*\}/s.exec(text);
    if (match) return JSON.parse(match[0]);
    throw new Error("模型输出不是合法 JSON");
  }
}

function toFloat(value: unknown): number | null {
  let num: number;
  if (typeof value === "number") num = value;
  else if (typeof value === "string") {
    const s = value.replace(/[¥￥元块]/g, "").trim();
    if (!s) return null;
    num = Number(s);
  } else return null;
  if (!Number.isFinite(num) || num <= 0) return null;
  return num;
}

/** 校验并规整模型输出（app/ai.py _normalize 的移植）。 */
export function normalizeAiResult(data: unknown, recordDate: string): ParseResult {
  const root = (data && typeof data === "object" ? data : {}) as {
    items?: unknown[];
    questions?: unknown[];
  };
  const items: ParsedItem[] = [];
  for (const raw of root.items || []) {
    if (!raw || typeof raw !== "object") continue;
    const it = raw as Record<string, unknown>;
    const amount = toFloat(it.amount);
    if (amount === null) continue;
    const rawType = String(it.type);
    let type = (VALID_TYPES as readonly string[]).includes(rawType) ? rawType : "支出";
    let category = String(it.category || "其他");
    if (type === "收入" && !INCOME_CATS.includes(category)) category = "其他收入";
    else if ((type === "支出" || type === "退款") && !EXPENSE_CATS.includes(category)) category = "其他";
    else if (["取现", "转账", "还款"].includes(type)) category = "—";
    let date = String(it.date || recordDate);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) date = recordDate;
    const row: ParsedItem = {
      date,
      amount: Math.round(amount * 100) / 100,
      type,
      category,
      merchant: String(it.merchant || ""),
      note: String(it.note || ""),
      estimated: it.estimated ? 1 : 0,
    };
    const lis = it.line_items;
    if (Array.isArray(lis) && lis.length) {
      const clean: { name: string; qty: number; price: number }[] = [];
      for (const rawLi of lis) {
        if (!rawLi || typeof rawLi !== "object") continue;
        const li = rawLi as Record<string, unknown>;
        const name = String(li.name || "").trim();
        if (!name) continue;
        clean.push({
          name,
          qty: toFloat(li.qty) || 1,
          price: toFloat(li.price) || 0,
        });
      }
      if (clean.length) row.line_items = clean;
    }
    items.push(row);
  }
  const questions = (root.questions || []).map(String).filter(Boolean);
  return { items, questions };
}

export interface AiConfig {
  api_base: string;
  api_key: string;
  model_text: string;
  model_vision: string;
}

async function chat(
  cfg: AiConfig,
  model: string,
  messages: unknown[],
): Promise<string> {
  if (!cfg.api_key) throw new AIUnavailableError("未配置 API Key，请在「设置」页填写");
  if (!cfg.api_base) throw new AIUnavailableError("未填写 API Base");
  let response: Response;
  try {
    response = await fetch(`${cfg.api_base.replace(/\/+$/, "")}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${cfg.api_key}`,
      },
      body: JSON.stringify({
        model,
        messages,
        response_format: { type: "json_object" },
        temperature: 0,
      }),
    });
  } catch (e) {
    throw new AIUnavailableError(String(e));
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new AIUnavailableError(`AI 服务返回 ${response.status}：${detail.slice(0, 200)}`);
  }
  const data = (await response.json()) as { choices?: { message?: { content?: string } }[] };
  return data.choices?.[0]?.message?.content || "";
}

export async function testConnection(cfg: {
  api_base: string;
  api_key: string;
  model: string;
}): Promise<void> {
  await chat(
    { ...cfg, model_text: cfg.model, model_vision: cfg.model },
    cfg.model,
    [{ role: "user", content: "回复 OK" }],
  );
}

/** 文字解析。AI 不可用抛 AIUnavailableError。 */
export async function parseText(
  cfg: AiConfig,
  text: string,
  recordDate: string,
): Promise<ParseResult> {
  const content = await chat(cfg, cfg.model_text, [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: `${todayDesc(recordDate)}。以下是我的记账内容：\n${text}` },
  ]);
  return normalizeAiResult(extractJson(content), recordDate);
}

/** 总结生成：根据组装好的素材 prompt 直接让模型输出正文。 */
export async function chatSummary(cfg: AiConfig, prompt: string): Promise<string> {
  const content = await chat(cfg, cfg.model_text, [
    {
      role: "system",
      content:
        "你是 Better-money 的账本总结助手。根据给定的账本数据，按指定语气写一段 150~300 字的总结。" +
        "只输出总结正文，不要标题、不要 markdown、不要解释。",
    },
    { role: "user", content: prompt },
  ]);
  return content.trim();
}

/** 图片识别（小票/截图）。imageBytes 本地读取，直接发往用户配置的服务商。 */
export async function parseImage(
  cfg: AiConfig,
  imageBytes: Uint8Array,
  ext: string,
  textNote: string,
  recordDate: string,
): Promise<ParseResult> {
  const mime = MIME_BY_EXT[ext.toLowerCase()] || "image/jpeg";
  const b64 = bytesToB64(imageBytes);
  const note = textNote.trim() || "无";
  const content = await chat(cfg, cfg.model_vision, [
    { role: "system", content: VISION_SYSTEM_PROMPT },
    {
      role: "user",
      content: [
        { type: "text", text: `${todayDesc(recordDate)}。图片说明：${note}` },
        { type: "image_url", image_url: { url: `data:${mime};base64,${b64}` } },
      ],
    },
  ]);
  return normalizeAiResult(extractJson(content), recordDate);
}

function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}
