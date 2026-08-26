/** 数据行与配置类型（字段与桌面端 SQLite 表一致）。 */

export interface TransactionRow {
  id: number;
  date: string; // YYYY-MM-DD
  amount: number; // 元，2 位小数
  type: "支出" | "收入" | "退款" | "取现" | "转账" | "还款";
  category: string;
  merchant: string;
  note: string;
  source: string;
  estimated: number; // 0/1
  created_at: string;
  updated_at: string;
  uuid: string;
  device_id: string;
  deleted_at: string;
  last_synced_at: string;
  refund_of: string; // 退款配对：指向被修正的原支出 uuid；'' = 独立退款
}

export interface LineItemRow {
  id: number;
  transaction_id: number;
  name: string;
  qty: number;
  price: number;
  uuid: string;
  updated_at: string;
}

export interface GoalRow {
  id: number;
  name: string;
  price: number;
  saved: number;
  priority: number;
  status: "冷静期" | "进行中" | "已暂停" | "已达成" | "已放弃";
  cooldown_until: string;
  expected_date: string;
  note: string;
  created_at: string;
  achieved_at: string;
  uuid: string;
  device_id: string;
  updated_at: string;
  deleted_at: string;
  last_synced_at: string;
}

export interface SavingsWinRow {
  id: number;
  goal_name: string;
  amount: number;
  date: string;
  created_at: string;
  uuid: string;
  device_id: string;
  updated_at: string;
  deleted_at: string;
  last_synced_at: string;
}

export interface SummaryRow {
  id: number;
  period_type: "周" | "月";
  period_start: string;
  period_end: string;
  content: string;
  image_path: string;
  expired: number;
  created_at: string;
}

export interface AdjustmentRow {
  id: number;
  date: string;
  diff: number;
  note: string;
  created_at: string;
  reverses_adjustment_id: number | null;
}

export interface PendingItemRow {
  id: number;
  raw_text: string;
  image_path: string;
  created_at: string;
}

export interface TombstoneRow {
  id: number;
  uuid: string;
  kind: "transaction" | "goal" | "savings_win";
  deleted_at: string;
  device_id: string;
  synced: number;
}

export interface SyncEventRow {
  id: number;
  package_id: string;
  source_device: string;
  exported_at: string;
  imported_at: string;
  direction: "import" | "export";
  result: string;
  conflict_dates: string;
  decisions: string;
}

export interface AppConfig {
  ai_provider: string;
  api_base: string;
  api_key: string;
  model_text: string;
  model_vision: string;
  initial_balance: number;
  monthly_budget: number;
  auto_save_ratio: number;
  tone: string;
  cooldown_days: number;
  initial_balance_date: string;
  onboarding_completed: boolean;
  device_id: string;
  device_name: string;
}

export const DEFAULT_CONFIG: AppConfig = {
  ai_provider: "DeepSeek",
  api_base: "https://api.deepseek.com",
  api_key: "",
  model_text: "deepseek-v4-pro",
  model_vision: "deepseek-v4-flash-vision-exp",
  initial_balance: 0,
  monthly_budget: 0,
  auto_save_ratio: 0.3,
  tone: "朋友",
  cooldown_days: 7,
  initial_balance_date: "",
  onboarding_completed: false,
  device_id: "",
  device_name: "手机",
};

export const VALID_TYPES = ["支出", "收入", "退款", "取现", "转账", "还款"] as const;
export const EXPENSE_CATS = ["餐饮", "奶茶咖啡", "交通", "学习", "购物", "娱乐", "生活", "其他"];
export const INCOME_CATS = ["兼职", "红包", "家里给", "其他收入"];
