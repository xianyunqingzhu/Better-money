/** 共享同步包（app/sync.py 的 TS 移植）：导出、校验、预览差异与按天冲突合并。 */
import JSZip from "jszip";
import { localNowSql, uuidHex } from "../db/migrations";
import type { LedgerRepo } from "../db/repository";
import { toCents } from "./money";
import type { AppConfig, GoalRow, SavingsWinRow, TransactionRow } from "./types";

export const SHARE_FORMAT = "better-money-share";
export const SHARE_FORMAT_VERSION = 1;
export const MIN_SHARE_SCHEMA_VERSION = 3;
export const SHARE_SCHEMA_VERSION = 3;
export const MAX_PACKAGE_UNCOMPRESSED = 50 * 1024 * 1024;

export const MEMBERS = [
  "manifest.json",
  "transactions.json",
  "line_items.json",
  "goals.json",
  "savings_wins.json",
  "settings_public.json",
  "tombstones.json",
] as const;

export const PUBLIC_SETTING_KEYS = [
  "initial_balance",
  "initial_balance_date",
  "monthly_budget",
  "auto_save_ratio",
  "cooldown_days",
] as const;

const TS_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const UUID_RE = /^[0-9a-fA-F]{32}$/;

export const VALID_TYPES_SET = new Set(["支出", "收入", "退款", "取现", "转账", "还款"]);

export class ShareError extends Error {}

export async function sha256Hex(data: Uint8Array): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    data as unknown as BufferSource,
  );
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------- 数值辅助 ----------

export function moneyStr(value: number | string): string {
  return (toCents(value) / 100).toFixed(2);
}

function moneyNum(value: unknown, fieldName: string): number {
  if (typeof value === "boolean") throw new ShareError(`${fieldName} 金额无效: ${value}`);
  const text = String(value ?? "").trim();
  const num = Number(text);
  if (text === "" || !Number.isFinite(num)) {
    throw new ShareError(`${fieldName} 金额无效: ${value}`);
  }
  return Math.round(num * 100) / 100;
}

function require_(condition: boolean, message: string): void {
  if (!condition) throw new ShareError(message);
}

function checkTimestamp(item: Record<string, unknown>, key: string): void {
  const value = String(item[key] ?? "");
  if (!TS_RE.test(value)) throw new ShareError(`时间字段 ${key} 无效：${value}`);
}

// ---------- 包结构 ----------

export interface SharePackage {
  manifest: {
    format: string;
    format_version: number;
    app_version: string;
    schema_version: number;
    device_id: string;
    device_name: string;
    platform: string;
    created_at: string;
    date_min: string;
    date_max: string;
    counts: Record<string, number>;
    includes: Record<string, boolean>;
  };
  transactions: TransactionRow[];
  lineItems: Map<string, { uuid: string; name: string; qty: number; price: number; updated_at: string }[]>;
  goals: GoalRow[];
  savingsWins: SavingsWinRow[];
  tombstones: { uuid: string; kind: string; deleted_at: string; device_id: string }[];
  settings: {
    initial_balance: number;
    initial_balance_date: string;
    monthly_budget: number;
    auto_save_ratio: number;
    cooldown_days: number;
  };
}

function parseList(value: unknown, name: string): Record<string, unknown>[] {
  require_(Array.isArray(value), `${name} 必须是数组`);
  const seen = new Set<string>();
  const result: Record<string, unknown>[] = [];
  for (const item of value as unknown[]) {
    require_(item !== null && typeof item === "object", `${name} 含非对象元素`);
    const uuid = String((item as Record<string, unknown>).uuid ?? "");
    require_(UUID_RE.test(uuid), `${name} 含无效 uuid：${uuid}`);
    require_(!seen.has(uuid), `${name} 含重复 uuid：${uuid}`);
    seen.add(uuid);
    result.push(item as Record<string, unknown>);
  }
  return result;
}

export function parseSharePackage(zipBytes: Uint8Array): Promise<SharePackage> {
  return parseSharePackageAsync(zipBytes);
}

export async function parseSharePackageAsync(zipBytes: Uint8Array): Promise<SharePackage> {
  let zip: JSZip;
  try {
    zip = await JSZip.loadAsync(zipBytes);
  } catch {
    throw new ShareError("共享包无法解析：不是有效的 ZIP 文件");
  }
  const names = new Set<string>();
  let total = 0;
  for (const [name, entry] of Object.entries(zip.files)) {
    if (entry.dir) continue;
    const normalized = name.replace(/\\/g, "/");
    if (!(MEMBERS as readonly string[]).includes(normalized)) {
      throw new ShareError(`共享包内包含未知文件：${normalized}`);
    }
    if (names.has(normalized)) throw new ShareError(`共享包内文件重复：${normalized}`);
    names.add(normalized);
    const permissions = entry.unixPermissions;
    if (typeof permissions === "number" && (permissions & 0o170000) === 0o120000) {
      throw new ShareError("共享包内包含符号链接，已拒绝导入");
    }
    const internals = entry as unknown as { _data?: { uncompressedSize?: number } };
    total += internals._data?.uncompressedSize ?? 0;
    if (total > MAX_PACKAGE_UNCOMPRESSED) {
      throw new ShareError("共享包解压后体积超过上限");
    }
  }
  const missing = (MEMBERS as readonly string[]).filter((m) => !names.has(m));
  if (missing.length) {
    throw new ShareError(`共享包缺少文件：${missing.join("、")}`);
  }

  const sections: Record<string, unknown> = {};
  for (const name of MEMBERS) {
    try {
      sections[name] = JSON.parse(await zip.file(name)!.async("string"));
    } catch {
      throw new ShareError(`共享包文件 ${name} 不是合法 JSON`);
    }
  }

  const manifest = sections["manifest.json"] as Record<string, unknown>;
  require_(manifest && typeof manifest === "object", "manifest 必须是 JSON 对象");
  require_(
    manifest.format === SHARE_FORMAT,
    "这不是 Better-money 共享包（format 不符）；完整备份请用「完整备份」功能",
  );
  require_(
    manifest.format_version === SHARE_FORMAT_VERSION,
    `不支持的共享包格式版本：${manifest.format_version}`,
  );
  const schema = manifest.schema_version as number;
  require_(
    typeof schema === "number" && MIN_SHARE_SCHEMA_VERSION <= schema && schema <= SHARE_SCHEMA_VERSION,
    `不支持的共享包数据版本：${schema}`,
  );
  require_(
    UUID_RE.test(String(manifest.device_id ?? "")),
    "共享包缺少有效的来源设备标识",
  );

  const txList = parseList(sections["transactions.json"], "transactions.json");
  const liList = parseList(sections["line_items.json"], "line_items.json");
  const goalList = parseList(sections["goals.json"], "goals.json");
  const winList = parseList(sections["savings_wins.json"], "savings_wins.json");
  const tombList = parseList(sections["tombstones.json"], "tombstones.json");
  const settings = sections["settings_public.json"] as Record<string, unknown>;
  require_(settings && typeof settings === "object", "settings_public 必须是对象");

  const txUuids = new Set<string>();
  const transactions: TransactionRow[] = [];
  for (const item of txList) {
    checkTimestamp(item, "created_at");
    checkTimestamp(item, "updated_at");
    require_(DATE_RE.test(String(item.date ?? "")), `交易日期无效：${item.date}`);
    require_(
      VALID_TYPES_SET.has(String(item.type)),
      `交易类型无效：${item.type}`,
    );
    txUuids.add(String(item.uuid));
    transactions.push({
      id: 0,
      date: String(item.date),
      amount: moneyNum(item.amount, "交易金额"),
      type: String(item.type) as TransactionRow["type"],
      category: String(item.category ?? "其他"),
      merchant: String(item.merchant ?? ""),
      note: String(item.note ?? ""),
      source: String(item.source ?? "手动"),
      estimated: Number(item.estimated) || 0,
      created_at: String(item.created_at),
      updated_at: String(item.updated_at),
      uuid: String(item.uuid),
      device_id: String(item.device_id ?? ""),
      deleted_at: "",
      last_synced_at: "",
    });
  }

  const lineItems = new Map<string, { uuid: string; name: string; qty: number; price: number; updated_at: string }[]>();
  for (const item of liList) {
    checkTimestamp(item, "updated_at");
    require_(
      txUuids.has(String(item.transaction_uuid)),
      `单品明细引用了不存在的交易：${item.transaction_uuid}`,
    );
    const entry = {
      uuid: String(item.uuid),
      name: String(item.name ?? ""),
      qty: moneyNum(item.qty ?? 1, "单品数量"),
      price: moneyNum(item.price ?? 0, "单品单价"),
      updated_at: String(item.updated_at),
    };
    const key = String(item.transaction_uuid);
    if (!lineItems.has(key)) lineItems.set(key, []);
    lineItems.get(key)!.push(entry);
  }

  const goals: GoalRow[] = [];
  for (const item of goalList) {
    checkTimestamp(item, "created_at");
    checkTimestamp(item, "updated_at");
    goals.push({
      id: 0,
      name: String(item.name ?? ""),
      price: moneyNum(item.price, "目标price"),
      saved: moneyNum(item.saved ?? 0, "目标saved"),
      priority: Number(item.priority) || 100,
      status: String(item.status ?? "冷静期") as GoalRow["status"],
      cooldown_until: String(item.cooldown_until ?? ""),
      expected_date: String(item.expected_date ?? ""),
      note: String(item.note ?? ""),
      created_at: String(item.created_at),
      achieved_at: String(item.achieved_at ?? ""),
      uuid: String(item.uuid),
      device_id: String(item.device_id ?? ""),
      updated_at: String(item.updated_at),
      deleted_at: "",
      last_synced_at: "",
    });
  }

  const wins: SavingsWinRow[] = [];
  for (const item of winList) {
    checkTimestamp(item, "created_at");
    checkTimestamp(item, "updated_at");
    require_(DATE_RE.test(String(item.date ?? "")), `省下的钱日期无效：${item.date}`);
    wins.push({
      id: 0,
      goal_name: String(item.goal_name ?? ""),
      amount: moneyNum(item.amount, "省下的钱金额"),
      date: String(item.date),
      created_at: String(item.created_at),
      uuid: String(item.uuid),
      device_id: String(item.device_id ?? ""),
      updated_at: String(item.updated_at),
      deleted_at: "",
      last_synced_at: "",
    });
  }

  const liveUuids = new Set<string>([
    ...txUuids,
    ...goals.map((g) => g.uuid),
    ...wins.map((w) => w.uuid),
  ]);
  const tombstones: { uuid: string; kind: string; deleted_at: string; device_id: string }[] = [];
  for (const item of tombList) {
    checkTimestamp(item, "deleted_at");
    require_(
      ["transaction", "goal", "savings_win"].includes(String(item.kind)),
      `tombstone 类型无效：${item.kind}`,
    );
    require_(
      !liveUuids.has(String(item.uuid)),
      `tombstone 与数据文件冲突：${item.uuid}`,
    );
    tombstones.push({
      uuid: String(item.uuid),
      kind: String(item.kind),
      deleted_at: String(item.deleted_at),
      device_id: String(item.device_id ?? ""),
    });
  }

  for (const key of PUBLIC_SETTING_KEYS) {
    require_(key in settings, `共享设置缺少字段：${key}`);
  }
  const settingsNorm = {
    initial_balance: moneyNum(settings.initial_balance, "初始余额"),
    initial_balance_date: String(settings.initial_balance_date ?? ""),
    monthly_budget: moneyNum(settings.monthly_budget, "月预算"),
    auto_save_ratio: moneyNum(settings.auto_save_ratio, "自动存比例"),
    cooldown_days: Number(settings.cooldown_days) || 7,
  };
  if (settingsNorm.initial_balance_date && !DATE_RE.test(settingsNorm.initial_balance_date)) {
    throw new ShareError("共享设置里初始余额日期无效");
  }

  return {
    manifest: manifest as SharePackage["manifest"],
    transactions,
    lineItems,
    goals,
    savingsWins: wins,
    tombstones,
    settings: settingsNorm,
  };
}

// ---------- 导出 ----------

export interface ExportResult {
  filename: string;
  packageId: string;
  zip: Uint8Array;
  counts: { transactions: number; goals: number; savings_wins: number; line_items: number; tombstones: number };
}

export async function exportSharePackage(repo: LedgerRepo): Promise<ExportResult> {
  const db = repo.db;
  const cfg = repo.getConfig();
  const deviceId = repo.localDeviceId();
  const localNow = localNowSql();

  // 1. 读一致性快照（不写库）
  const liveTx = db.query<TransactionRow>(
    "SELECT * FROM transactions WHERE deleted_at = '' ORDER BY date, id",
  );
  const txIds = new Set(liveTx.map((t) => t.id));
  const txUuidById = new Map(liveTx.map((t) => [t.id, t.uuid]));
  const lineItems = db
    .query<{ id: number; transaction_id: number; name: string; qty: number; price: number; uuid: string; updated_at: string }>(
      "SELECT * FROM line_items",
    )
    .filter((li) => txIds.has(li.transaction_id));
  const goals = db.query<GoalRow>(
    "SELECT * FROM goals WHERE deleted_at = '' ORDER BY priority, id",
  );
  const wins = db.query<SavingsWinRow>(
    "SELECT * FROM savings_wins WHERE deleted_at = '' ORDER BY date, id",
  );
  const tombstones = db.query<{ uuid: string; kind: string; deleted_at: string; device_id: string }>(
    "SELECT * FROM sync_tombstones ORDER BY deleted_at, id",
  );

  const txDates = [...new Set(liveTx.map((t) => t.date).filter((d) => DATE_RE.test(d)))].sort();
  const manifest = {
    format: SHARE_FORMAT,
    format_version: SHARE_FORMAT_VERSION,
    app_version: "1.1.0",
    schema_version: SHARE_SCHEMA_VERSION,
    device_id: deviceId,
    device_name: cfg.device_name || "手机",
    platform: "mobile",
    created_at: new Date().toISOString().slice(0, 19) + "+08:00",
    date_min: txDates[0] || "",
    date_max: txDates[txDates.length - 1] || "",
    counts: {
      transactions: liveTx.length,
      goals: goals.length,
      savings_wins: wins.length,
      line_items: lineItems.length,
      tombstones: tombstones.length,
    },
    includes: {
      transactions: true,
      goals: true,
      savings_wins: true,
      images: false,
      summaries: false,
      api_key: false,
    },
  };
  const payload: Record<string, string> = {
    "manifest.json": JSON.stringify(manifest, null, 2),
    "transactions.json": JSON.stringify(
      liveTx.map((t) => ({
        uuid: t.uuid,
        device_id: t.device_id,
        date: t.date,
        amount: moneyStr(t.amount),
        type: t.type,
        category: t.category,
        merchant: t.merchant,
        note: t.note,
        source: t.source,
        estimated: Number(t.estimated) || 0,
        created_at: t.created_at,
        updated_at: t.updated_at,
      })),
      null,
      2,
    ),
    "line_items.json": JSON.stringify(
      lineItems.map((li) => ({
        uuid: li.uuid,
        transaction_uuid: txUuidById.get(li.transaction_id) || "",
        name: li.name,
        qty: moneyStr(li.qty),
        price: moneyStr(li.price),
        updated_at: li.updated_at,
      })),
      null,
      2,
    ),
    "goals.json": JSON.stringify(
      goals.map((g) => ({
        uuid: g.uuid,
        device_id: g.device_id,
        name: g.name,
        price: moneyStr(g.price),
        saved: moneyStr(g.saved),
        priority: g.priority,
        status: g.status,
        cooldown_until: g.cooldown_until,
        expected_date: g.expected_date,
        note: g.note,
        created_at: g.created_at,
        achieved_at: g.achieved_at,
        updated_at: g.updated_at,
      })),
      null,
      2,
    ),
    "savings_wins.json": JSON.stringify(
      wins.map((w) => ({
        uuid: w.uuid,
        device_id: w.device_id,
        goal_name: w.goal_name,
        amount: moneyStr(w.amount),
        date: w.date,
        created_at: w.created_at,
        updated_at: w.updated_at,
      })),
      null,
      2,
    ),
    "settings_public.json": JSON.stringify(
      {
        initial_balance: moneyStr(cfg.initial_balance),
        initial_balance_date: cfg.initial_balance_date || "",
        monthly_budget: moneyStr(cfg.monthly_budget),
        auto_save_ratio: moneyStr(cfg.auto_save_ratio),
        cooldown_days: cfg.cooldown_days || 7,
      },
      null,
      2,
    ),
    "tombstones.json": JSON.stringify(
      tombstones.map((t) => ({
        uuid: t.uuid,
        kind: t.kind,
        deleted_at: t.deleted_at,
        device_id: t.device_id,
      })),
      null,
      2,
    ),
  };

  // 2. 生成 ZIP 并自校验（失败不回写任何同步标记）
  const zip = new JSZip();
  for (const [name, content] of Object.entries(payload)) {
    zip.file(name, content);
  }
  const zipBytes = await zip.generateAsync({
    type: "uint8array",
    compression: "DEFLATE",
    compressionOptions: { level: 6 },
  });
  const packageId = await sha256Hex(zipBytes);
  await parseSharePackageAsync(zipBytes); // 自校验：结构/字段/数量

  const stamp = localNow.replace(/[-: ]/g, "").replace(/(\d{8})(\d{6})/, "$1-$2");
  const filename = `better-money-share-${deviceId.slice(0, 8)}-${stamp}.zip`;

  // 3. 事务性登记同步标记与导出事件
  db.transaction(() => {
    for (const table of ["transactions", "goals", "savings_wins"]) {
      db.run(`UPDATE ${table} SET last_synced_at = updated_at WHERE deleted_at = ''`);
      db.run(
        `UPDATE ${table} SET last_synced_at = deleted_at
         WHERE deleted_at <> '' AND last_synced_at < deleted_at`,
      );
    }
    db.run("UPDATE sync_tombstones SET synced = 1");
    db.run("UPDATE sync_devices SET last_export_at = ? WHERE device_id = ?", [
      localNow,
      deviceId,
    ]);
    db.run(
      `INSERT INTO sync_events(package_id, source_device, exported_at, imported_at,
        direction, result, conflict_dates, decisions)
       VALUES (?, ?, ?, '', 'export', ?, '', '')`,
      [
        packageId,
        deviceId,
        localNow,
        `导出 ${manifest.counts.transactions} 笔交易、${manifest.counts.goals} 个目标`,
      ],
    );
  });
  await repo.save();
  return { filename, packageId, zip: zipBytes, counts: manifest.counts };
}

// ---------- 差异计算 ----------

export interface DiffEntry {
  uuid: string;
  kind: "both_changed" | "delete_vs_modify" | "resurrect";
  local: TransactionRow | null;
  peer: TransactionRow | null;
  deleted_at?: string;
}

export interface GoalConflictEntry {
  uuid: string;
  kind: "both_changed" | "delete_vs_modify" | "resurrect";
  suggested: "local" | "peer";
  local: GoalRow | null;
  peer: GoalRow | null;
  deleted_at?: string;
}

export interface ShareDiff {
  addTx: TransactionRow[];
  modTx: TransactionRow[];
  delTx: { uuid: string; deleted_at: string }[];
  keepLocalTx: number;
  dayConflicts: Map<string, DiffEntry[]>;
  addGoals: GoalRow[];
  modGoals: GoalRow[];
  delGoals: { uuid: string; deleted_at: string }[];
  goalConflicts: GoalConflictEntry[];
  addWins: SavingsWinRow[];
  modWins: SavingsWinRow[];
  delWins: { uuid: string; deleted_at: string }[];
  newTombstones: { uuid: string; kind: string; deleted_at: string; device_id: string }[];
  dupes: { uuid: string; date: string; amount: number; merchant: string; local_uuid: string }[];
}

function changed(row: { updated_at?: string; last_synced_at?: string }): boolean {
  return (row.updated_at || "") !== (row.last_synced_at || "");
}

function addDayConflict(diff: ShareDiff, day: string, entry: DiffEntry) {
  const key = DATE_RE.test(day) ? day : "未知日期";
  if (!diff.dayConflicts.has(key)) diff.dayConflicts.set(key, []);
  diff.dayConflicts.get(key)!.push(entry);
}

export function diffAgainstLocal(repo: LedgerRepo, pkg: SharePackage): ShareDiff {
  const db = repo.db;
  const diff: ShareDiff = {
    addTx: [],
    modTx: [],
    delTx: [],
    keepLocalTx: 0,
    dayConflicts: new Map(),
    addGoals: [],
    modGoals: [],
    delGoals: [],
    goalConflicts: [],
    addWins: [],
    modWins: [],
    delWins: [],
    newTombstones: [],
    dupes: [],
  };

  const localTx = new Map(
    db.query<TransactionRow>("SELECT * FROM transactions").map((r) => [r.uuid, r]),
  );
  const localGoals = new Map(
    db.query<GoalRow>("SELECT * FROM goals").map((r) => [r.uuid, r]),
  );
  const localWins = new Map(
    db.query<SavingsWinRow>("SELECT * FROM savings_wins").map((r) => [r.uuid, r]),
  );
  const localTombs = new Map(
    db
      .query<{ uuid: string; kind: string; deleted_at: string; device_id: string }>(
        "SELECT * FROM sync_tombstones",
      )
      .map((r) => [r.uuid, r]),
  );

  // 交易
  for (const peer of pkg.transactions) {
    const row = localTx.get(peer.uuid);
    const tomb = localTombs.get(peer.uuid);
    if (!row || row.deleted_at) {
      if (row && (row.last_synced_at || "") >= peer.updated_at) continue; // 已就保持删除决策
      if (tomb && (tomb.deleted_at || "") >= peer.updated_at) continue; // 本机删除更新
      if (row) {
        addDayConflict(diff, peer.date, { uuid: peer.uuid, kind: "delete_vs_modify", local: null, peer });
      } else if (tomb) {
        addDayConflict(diff, peer.date, { uuid: peer.uuid, kind: "resurrect", local: null, peer });
      } else {
        diff.addTx.push(peer);
      }
    } else if (changed(row) && peer.updated_at !== row.last_synced_at) {
      addDayConflict(diff, peer.date, { uuid: peer.uuid, kind: "both_changed", local: row, peer });
    } else if (peer.updated_at !== row.last_synced_at) {
      diff.modTx.push(peer);
    } else if (changed(row)) {
      diff.keepLocalTx += 1;
    }
  }
  for (const tomb of pkg.tombstones.filter((t) => t.kind === "transaction")) {
    const row = localTx.get(tomb.uuid);
    const ltomb = localTombs.get(tomb.uuid);
    if (row && !row.deleted_at) {
      if (changed(row) && row.updated_at > tomb.deleted_at) {
        addDayConflict(diff, row.date, {
          uuid: tomb.uuid,
          kind: "delete_vs_modify",
          local: row,
          peer: null,
          deleted_at: tomb.deleted_at,
        });
      } else {
        diff.delTx.push({ uuid: tomb.uuid, deleted_at: tomb.deleted_at });
      }
    } else if (!ltomb) {
      diff.newTombstones.push(tomb);
    } else if (tomb.deleted_at > ltomb.deleted_at) {
      diff.newTombstones.push(tomb);
    }
  }

  // 疑似重复（仅针对包内新增交易）
  const liveLocal = [...localTx.values()].filter((r) => !r.deleted_at);
  for (const peer of diff.addTx) {
    const hit = liveLocal.find(
      (row) =>
        row.date === peer.date &&
        Math.round(row.amount * 100) === Math.round(peer.amount * 100) &&
        row.merchant === peer.merchant &&
        row.uuid !== peer.uuid,
    );
    if (hit) {
      diff.dupes.push({
        uuid: peer.uuid,
        date: peer.date,
        amount: peer.amount,
        merchant: peer.merchant,
        local_uuid: hit.uuid,
      });
    }
  }

  // 目标
  for (const peer of pkg.goals) {
    const row = localGoals.get(peer.uuid);
    const tomb = localTombs.get(peer.uuid);
    if (!row || row.deleted_at) {
      if (tomb && (tomb.deleted_at || "") >= peer.updated_at) continue;
      if (row) {
        diff.goalConflicts.push({ uuid: peer.uuid, kind: "delete_vs_modify", suggested: "peer", local: null, peer });
      } else if (tomb) {
        diff.goalConflicts.push({ uuid: peer.uuid, kind: "resurrect", suggested: "local", local: null, peer });
      } else {
        diff.addGoals.push(peer);
      }
    } else if (changed(row) && peer.updated_at !== row.last_synced_at) {
      diff.goalConflicts.push({
        uuid: peer.uuid,
        kind: "both_changed",
        suggested: peer.updated_at >= row.updated_at ? "peer" : "local",
        local: row,
        peer,
      });
    } else if (peer.updated_at !== row.last_synced_at) {
      diff.modGoals.push(peer);
    } else if (changed(row)) {
      diff.keepLocalTx += 1;
    }
  }
  for (const tomb of pkg.tombstones.filter((t) => t.kind === "goal")) {
    const row = localGoals.get(tomb.uuid);
    const ltomb = localTombs.get(tomb.uuid);
    if (row && !row.deleted_at) {
      if (changed(row) && row.updated_at > tomb.deleted_at) {
        diff.goalConflicts.push({
          uuid: tomb.uuid,
          kind: "delete_vs_modify",
          suggested: "local",
          local: row,
          peer: null,
          deleted_at: tomb.deleted_at,
        });
      } else {
        diff.delGoals.push({ uuid: tomb.uuid, deleted_at: tomb.deleted_at });
      }
    } else if (!ltomb) {
      diff.newTombstones.push(tomb);
    } else if (tomb.deleted_at > ltomb.deleted_at) {
      diff.newTombstones.push(tomb);
    }
  }

  // 省下的钱：按时间戳自动处理
  for (const peer of pkg.savingsWins) {
    const row = localWins.get(peer.uuid);
    const tomb = localTombs.get(peer.uuid);
    if (!row || row.deleted_at) {
      if (tomb && (tomb.deleted_at || "") >= peer.updated_at) continue;
      if (!row && !tomb) diff.addWins.push(peer);
      else if (row && row.updated_at < peer.updated_at) diff.modWins.push(peer);
    } else if (peer.updated_at > row.updated_at) {
      diff.modWins.push(peer);
    }
  }
  for (const tomb of pkg.tombstones.filter((t) => t.kind === "savings_win")) {
    const row = localWins.get(tomb.uuid);
    const ltomb = localTombs.get(tomb.uuid);
    if (row && !row.deleted_at) {
      if (row.updated_at > tomb.deleted_at) continue;
      diff.delWins.push({ uuid: tomb.uuid, deleted_at: tomb.deleted_at });
    } else if (!ltomb) {
      diff.newTombstones.push(tomb);
    } else if (tomb.deleted_at > ltomb.deleted_at) {
      diff.newTombstones.push(tomb);
    }
  }

  return diff;
}

// ---------- 预览 ----------

export function localPublicSettings(cfg: AppConfig) {
  return {
    initial_balance: Math.round(toCents(cfg.initial_balance)) / 100,
    initial_balance_date: cfg.initial_balance_date || "",
    monthly_budget: Math.round(toCents(cfg.monthly_budget)) / 100,
    auto_save_ratio: Math.round(toCents(cfg.auto_save_ratio)) / 100,
    cooldown_days: cfg.cooldown_days || 7,
  };
}

export interface ImportPreview {
  package: {
    device_id: string;
    device_name: string;
    platform: string;
    created_at: string;
    counts: Record<string, number>;
    date_min: string;
    date_max: string;
  };
  settings: {
    local: ReturnType<typeof localPublicSettings>;
    package: SharePackage["settings"];
    conflict: boolean;
  };
  summary: {
    add_transactions: number;
    modify_transactions: number;
    delete_transactions: number;
    keep_local_transactions: number;
    add_goals: number;
    modify_goals: number;
    delete_goals: number;
    add_savings_wins: number;
    modify_savings_wins: number;
    delete_savings_wins: number;
    new_tombstones: number;
    conflict_days: {
      date: string;
      items: {
        uuid: string;
        kind: string;
        local: Record<string, unknown> | null;
        peer: Record<string, unknown> | null;
      }[];
    }[];
    goal_conflicts: {
      uuid: string;
      kind: string;
      suggested: string;
      local: Record<string, unknown> | null;
      peer: Record<string, unknown> | null;
    }[];
    dupes: { uuid: string; date: string; amount: string; merchant: string; local_uuid: string }[];
  };
}

function txPublic(row: TransactionRow): Record<string, unknown> {
  return {
    uuid: row.uuid,
    date: row.date,
    amount: moneyStr(row.amount),
    type: row.type,
    category: row.category,
    merchant: row.merchant,
    note: row.note,
    source: row.source,
    estimated: row.estimated,
    updated_at: row.updated_at,
  };
}

function goalPublic(row: GoalRow): Record<string, unknown> {
  return {
    uuid: row.uuid,
    name: row.name,
    price: moneyStr(row.price),
    saved: moneyStr(row.saved),
    status: row.status,
    updated_at: row.updated_at,
  };
}

export function previewImport(repo: LedgerRepo, pkg: SharePackage): ImportPreview {
  const diff = diffAgainstLocal(repo, pkg);
  const cfg = repo.getConfig();
  const localSettings = localPublicSettings(cfg);
  const settingsConflict = PUBLIC_SETTING_KEYS.some(
    (key) => localSettings[key] !== pkg.settings[key],
  );
  const txDates = [...new Set(pkg.transactions.map((t) => t.date))].sort();
  return {
    package: {
      device_id: pkg.manifest.device_id,
      device_name: pkg.manifest.device_name || "另一台设备",
      platform: pkg.manifest.platform || "",
      created_at: pkg.manifest.created_at || "",
      counts: pkg.manifest.counts,
      date_min: txDates[0] || "",
      date_max: txDates[txDates.length - 1] || "",
    },
    settings: {
      local: localSettings,
      package: pkg.settings,
      conflict: settingsConflict,
    },
    summary: {
      add_transactions: diff.addTx.length,
      modify_transactions: diff.modTx.length,
      delete_transactions: diff.delTx.length,
      keep_local_transactions: diff.keepLocalTx,
      add_goals: diff.addGoals.length,
      modify_goals: diff.modGoals.length,
      delete_goals: diff.delGoals.length,
      add_savings_wins: diff.addWins.length,
      modify_savings_wins: diff.modWins.length,
      delete_savings_wins: diff.delWins.length,
      new_tombstones: diff.newTombstones.length,
      conflict_days: [...diff.dayConflicts.entries()]
        .sort(([a], [b]) => (a < b ? -1 : 1))
        .map(([date, items]) => ({
          date,
          items: items.map((item) => ({
            uuid: item.uuid,
            kind: item.kind,
            local: item.local ? txPublic(item.local) : null,
            peer: item.peer ? txPublic(item.peer) : null,
          })),
        })),
      goal_conflicts: diff.goalConflicts.map((item) => ({
        uuid: item.uuid,
        kind: item.kind,
        suggested: item.suggested,
        local: item.local ? goalPublic(item.local) : null,
        peer: item.peer ? goalPublic(item.peer) : null,
      })),
      dupes: diff.dupes.map((d) => ({
        uuid: d.uuid,
        date: d.date,
        amount: moneyStr(d.amount),
        merchant: d.merchant,
        local_uuid: d.local_uuid,
      })),
    },
  };
}

// ---------- 应用合并 ----------

export interface ImportDecisions {
  settings?: "keep_local" | "apply_package";
  days?: Record<string, { mode: "keep_local" | "keep_peer" | "merge"; items?: Record<string, "local" | "peer" | "drop"> }>;
  goals?: Record<string, "local" | "peer" | "drop">;
  dupes?: Record<string, "drop">;
}

export interface ImportResult {
  add_transactions: number;
  modify_transactions: number;
  delete_transactions: number;
  add_goals: number;
  modify_goals: number;
  delete_goals: number;
  add_savings_wins: number;
  modify_savings_wins: number;
  delete_savings_wins: number;
  conflict_days_resolved: number;
}

function validateDecisions(diff: ShareDiff, decisions: ImportDecisions): void {
  const dayDecisions = decisions.days || {};
  for (const [day, items] of diff.dayConflicts) {
    const mode = dayDecisions[day]?.mode;
    if (!["keep_local", "keep_peer", "merge"].includes(mode || "")) {
      throw new ShareError(`日期 ${day} 的冲突还没有处理`);
    }
    if (mode === "merge") {
      const itemDecisions = dayDecisions[day]?.items || {};
      for (const item of items) {
        if (!["local", "peer", "drop"].includes(itemDecisions[item.uuid] || "")) {
          throw new ShareError(`日期 ${day} 的合并模式缺少逐条处理结果`);
        }
      }
    }
  }
  const goalDecisions = decisions.goals || {};
  for (const item of diff.goalConflicts) {
    if (item.kind === "delete_vs_modify") {
      if (!["local", "peer", "drop"].includes(goalDecisions[item.uuid] || "")) {
        throw new ShareError("目标「删除与修改」冲突还没有处理");
      }
    } else if (item.kind === "resurrect") {
      if (!["local", "peer"].includes(goalDecisions[item.uuid] || "")) {
        throw new ShareError("目标「复活与删除」冲突还没有处理");
      }
    }
  }
  if (decisions.settings && !["keep_local", "apply_package"].includes(decisions.settings)) {
    throw new ShareError("设置冲突的决策无效");
  }
}

export async function applyImport(
  repo: LedgerRepo,
  pkg: SharePackage,
  decisions: ImportDecisions,
): Promise<ImportResult> {
  if (decisions.settings === "apply_package") {
    await repo.updateConfig({
      initial_balance: pkg.settings.initial_balance,
      initial_balance_date: pkg.settings.initial_balance_date,
      monthly_budget: pkg.settings.monthly_budget,
      auto_save_ratio: pkg.settings.auto_save_ratio,
      cooldown_days: pkg.settings.cooldown_days,
    });
  }

  const db = repo.db;
  const diff = diffAgainstLocal(repo, pkg);
  validateDecisions(diff, decisions);

  const result: ImportResult = {
    add_transactions: 0,
    modify_transactions: 0,
    delete_transactions: 0,
    add_goals: 0,
    modify_goals: 0,
    delete_goals: 0,
    add_savings_wins: 0,
    modify_savings_wins: 0,
    delete_savings_wins: 0,
    conflict_days_resolved: diff.dayConflicts.size,
  };

  db.transaction(() => {
    const now = localNowSql();
    const affectedDates = new Set<string>();

    const insertTx = (peer: TransactionRow, items: { uuid: string; name: string; qty: number; price: number; updated_at: string }[]) => {
      db.run(
        `INSERT INTO transactions(date, amount, type, category, merchant, note, source,
          estimated, created_at, updated_at, uuid, device_id, deleted_at, last_synced_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)`,
        [
          peer.date, peer.amount, peer.type, peer.category, peer.merchant, peer.note,
          peer.source, peer.estimated, peer.created_at, peer.updated_at, peer.uuid,
          peer.device_id, peer.updated_at,
        ],
      );
      const txId = db.queryOne<{ id: number }>("SELECT last_insert_rowid() AS id")!.id;
      for (const item of items) {
        db.run(
          `INSERT INTO line_items(transaction_id, name, qty, price, uuid, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)`,
          [txId, item.name, item.qty, item.price, item.uuid, item.updated_at],
        );
      }
    };
    const updateTx = (peer: TransactionRow, items: { uuid: string; name: string; qty: number; price: number; updated_at: string }[]) => {
      db.run(
        `UPDATE transactions SET date = ?, amount = ?, type = ?, category = ?, merchant = ?,
          note = ?, source = ?, estimated = ?, updated_at = ?, last_synced_at = ?
         WHERE uuid = ?`,
        [
          peer.date, peer.amount, peer.type, peer.category, peer.merchant, peer.note,
          peer.source, peer.estimated, peer.updated_at, peer.updated_at, peer.uuid,
        ],
      );
      const txId = db.queryOne<{ id: number }>(
        "SELECT id FROM transactions WHERE uuid = ?",
        [peer.uuid],
      )!.id;
      db.run("DELETE FROM line_items WHERE transaction_id = ?", [txId]);
      for (const item of items) {
        db.run(
          `INSERT INTO line_items(transaction_id, name, qty, price, uuid, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)`,
          [txId, item.name, item.qty, item.price, item.uuid, item.updated_at],
        );
      }
    };
    const reviveTx = (peer: TransactionRow, items: { uuid: string; name: string; qty: number; price: number; updated_at: string }[]) => {
      db.run(
        `UPDATE transactions SET date = ?, amount = ?, type = ?, category = ?, merchant = ?,
          note = ?, source = ?, estimated = ?, created_at = ?, updated_at = ?, device_id = ?,
          deleted_at = '', last_synced_at = ?
         WHERE uuid = ? AND deleted_at <> ''`,
        [
          peer.date, peer.amount, peer.type, peer.category, peer.merchant, peer.note,
          peer.source, peer.estimated, peer.created_at, peer.updated_at, peer.device_id,
          peer.updated_at, peer.uuid,
        ],
      );
      const applied = (db.query<{ n: number }>("SELECT changes() AS n")[0]?.n || 0) > 0;
      db.run("DELETE FROM sync_tombstones WHERE uuid = ?", [peer.uuid]);
      if (!applied) {
        insertTx(peer, items);
        return;
      }
      const txId = db.queryOne<{ id: number }>(
        "SELECT id FROM transactions WHERE uuid = ?",
        [peer.uuid],
      )!.id;
      db.run("DELETE FROM line_items WHERE transaction_id = ?", [txId]);
      for (const item of items) {
        db.run(
          `INSERT INTO line_items(transaction_id, name, qty, price, uuid, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)`,
          [txId, item.name, item.qty, item.price, item.uuid, item.updated_at],
        );
      }
    };
    const softDeleteTx = (uuid: string, deletedAt: string) => {
      db.run(
        "UPDATE transactions SET deleted_at = ?, updated_at = ?, last_synced_at = ? WHERE uuid = ? AND deleted_at = ''",
        [deletedAt, deletedAt, deletedAt, uuid],
      );
      upsertTombstone(uuid, "transaction", deletedAt);
    };
    const touchLastSynced = (table: string, uuid: string, value: string) => {
      db.run(`UPDATE ${table} SET last_synced_at = ? WHERE uuid = ?`, [value, uuid]);
    };
    const upsertTombstone = (uuid: string, kind: string, deletedAt: string) => {
      db.run(
        `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
         VALUES (?, ?, ?, ?, 0)
         ON CONFLICT(uuid) DO UPDATE SET deleted_at = excluded.deleted_at, synced = 0
         WHERE excluded.deleted_at > sync_tombstones.deleted_at`,
        [uuid, kind, deletedAt, repo.localDeviceId()],
      );
    };

    // 交易新增/修改/删除
    for (const peer of diff.addTx) {
      if (decisions.dupes?.[peer.uuid] === "drop") continue;
      insertTx(peer, pkg.lineItems.get(peer.uuid) || []);
      result.add_transactions += 1;
      affectedDates.add(peer.date);
    }
    for (const peer of diff.modTx) {
      updateTx(peer, pkg.lineItems.get(peer.uuid) || []);
      result.modify_transactions += 1;
      affectedDates.add(peer.date);
    }
    for (const deletion of diff.delTx) {
      softDeleteTx(deletion.uuid, deletion.deleted_at);
      result.delete_transactions += 1;
    }

    // 按天冲突
    for (const [day, items] of diff.dayConflicts) {
      const dayDecision = decisions.days?.[day];
      const mode = dayDecision?.mode || "keep_local";
      for (const item of items) {
        const choice =
          mode === "keep_local"
            ? "local"
            : mode === "keep_peer"
              ? "peer"
              : dayDecision?.items?.[item.uuid] || "local";
        const peer = item.peer;
        const itemsList = peer ? pkg.lineItems.get(item.uuid) || [] : [];
        if (item.kind === "both_changed") {
          if (choice === "peer" && peer) {
            updateTx(peer, itemsList);
            result.modify_transactions += 1;
            affectedDates.add(peer.date);
          } else if (choice === "drop") {
            softDeleteTx(item.uuid, now);
            result.delete_transactions += 1;
          } else {
            touchLastSynced("transactions", item.uuid, peer!.updated_at);
          }
        } else if (item.kind === "delete_vs_modify") {
          if (peer) {
            if (choice === "peer") {
              if (item.local === null) {
                reviveTx(peer, itemsList);
                result.add_transactions += 1;
              } else {
                updateTx(peer, itemsList);
                result.modify_transactions += 1;
              }
              affectedDates.add(peer.date);
            } else if (choice === "drop") {
              softDeleteTx(item.uuid, now);
              result.delete_transactions += 1;
            } else {
              touchLastSynced("transactions", item.uuid, peer.updated_at);
            }
          } else {
            if (choice === "peer" || choice === "drop") {
              softDeleteTx(item.uuid, now);
              result.delete_transactions += 1;
            } else {
              touchLastSynced("transactions", item.uuid, item.deleted_at || now);
            }
          }
        } else {
          // resurrect
          if (choice === "peer" && peer) {
            db.run("DELETE FROM sync_tombstones WHERE uuid = ?", [item.uuid]);
            insertTx(peer, itemsList);
            result.add_transactions += 1;
            affectedDates.add(peer.date);
          } else {
            db.run("UPDATE sync_tombstones SET deleted_at = ? WHERE uuid = ?", [
              peer!.updated_at,
              item.uuid,
            ]);
          }
        }
      }
    }

    // 目标
    const insertGoal = (peer: GoalRow) => {
      db.run(
        `INSERT INTO goals(name, price, saved, priority, status, cooldown_until,
          expected_date, note, created_at, achieved_at, uuid, device_id, updated_at,
          deleted_at, last_synced_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)`,
        [
          peer.name, peer.price, peer.saved, peer.priority, peer.status,
          peer.cooldown_until, peer.expected_date, peer.note, peer.created_at,
          peer.achieved_at, peer.uuid, peer.device_id, peer.updated_at, peer.updated_at,
        ],
      );
    };
    const updateGoal = (peer: GoalRow) => {
      db.run(
        `UPDATE goals SET name = ?, price = ?, saved = ?, priority = ?, status = ?,
          cooldown_until = ?, expected_date = ?, note = ?, achieved_at = ?,
          updated_at = ?, last_synced_at = ? WHERE uuid = ?`,
        [
          peer.name, peer.price, peer.saved, peer.priority, peer.status,
          peer.cooldown_until, peer.expected_date, peer.note, peer.achieved_at,
          peer.updated_at, peer.updated_at, peer.uuid,
        ],
      );
    };
    const reviveGoal = (peer: GoalRow) => {
      db.run(
        `UPDATE goals SET name = ?, price = ?, saved = ?, priority = ?, status = ?,
          cooldown_until = ?, expected_date = ?, note = ?, created_at = ?, achieved_at = ?,
          device_id = ?, updated_at = ?, deleted_at = '', last_synced_at = ?
         WHERE uuid = ? AND deleted_at <> ''`,
        [
          peer.name, peer.price, peer.saved, peer.priority, peer.status,
          peer.cooldown_until, peer.expected_date, peer.note, peer.created_at,
          peer.achieved_at, peer.device_id, peer.updated_at, peer.updated_at, peer.uuid,
        ],
      );
      const applied = (db.query<{ n: number }>("SELECT changes() AS n")[0]?.n || 0) > 0;
      db.run("DELETE FROM sync_tombstones WHERE uuid = ?", [peer.uuid]);
      if (!applied) insertGoal(peer);
    };
    const softDeleteGoal = (uuid: string, deletedAt: string) => {
      db.run(
        "UPDATE goals SET deleted_at = ?, updated_at = ?, last_synced_at = ? WHERE uuid = ? AND deleted_at = ''",
        [deletedAt, deletedAt, deletedAt, uuid],
      );
      upsertTombstone(uuid, "goal", deletedAt);
    };

    for (const peer of diff.addGoals) {
      insertGoal(peer);
      result.add_goals += 1;
    }
    for (const peer of diff.modGoals) {
      updateGoal(peer);
      result.modify_goals += 1;
    }
    for (const deletion of diff.delGoals) {
      softDeleteGoal(deletion.uuid, deletion.deleted_at);
      result.delete_goals += 1;
    }
    for (const item of diff.goalConflicts) {
      let choice = decisions.goals?.[item.uuid];
      if (item.kind === "both_changed") {
        if (!choice) choice = item.suggested;
        if (choice === "peer" && item.peer) {
          updateGoal(item.peer);
          result.modify_goals += 1;
        } else if (choice === "drop") {
          softDeleteGoal(item.uuid, now);
          result.delete_goals += 1;
        } else {
          touchLastSynced("goals", item.uuid, item.peer!.updated_at);
        }
      } else if (item.kind === "delete_vs_modify") {
        if (item.peer) {
          if (choice === "peer") {
            if (item.local === null) {
              reviveGoal(item.peer);
              result.add_goals += 1;
            } else {
              updateGoal(item.peer);
              result.modify_goals += 1;
            }
          } else if (choice === "drop") {
            softDeleteGoal(item.uuid, now);
            result.delete_goals += 1;
          } else {
            touchLastSynced("goals", item.uuid, item.peer.updated_at);
          }
        } else {
          if (choice === "peer" || choice === "drop") {
            softDeleteGoal(item.uuid, now);
            result.delete_goals += 1;
          } else {
            touchLastSynced("goals", item.uuid, item.deleted_at || now);
          }
        }
      } else if (choice === "peer" && item.peer) {
        db.run("DELETE FROM sync_tombstones WHERE uuid = ?", [item.uuid]);
        insertGoal(item.peer);
        result.add_goals += 1;
      } else {
        db.run("UPDATE sync_tombstones SET deleted_at = ? WHERE uuid = ?", [
          item.peer!.updated_at,
          item.uuid,
        ]);
      }
    }

    // 省下的钱（自动）
    for (const peer of diff.addWins) {
      db.run(
        `INSERT INTO savings_wins(goal_name, amount, date, created_at, uuid, device_id,
          updated_at, deleted_at, last_synced_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)`,
        [
          peer.goal_name, peer.amount, peer.date, peer.created_at, peer.uuid,
          peer.device_id, peer.updated_at, peer.updated_at,
        ],
      );
      result.add_savings_wins += 1;
    }
    for (const peer of diff.modWins) {
      db.run(
        `UPDATE savings_wins SET goal_name = ?, amount = ?, date = ?, updated_at = ?,
          last_synced_at = ? WHERE uuid = ?`,
        [peer.goal_name, peer.amount, peer.date, peer.updated_at, peer.updated_at, peer.uuid],
      );
      result.modify_savings_wins += 1;
    }
    for (const deletion of diff.delWins) {
      db.run(
        `UPDATE savings_wins SET deleted_at = ?, updated_at = ?, last_synced_at = ?
         WHERE uuid = ? AND deleted_at = ''`,
        [deletion.deleted_at, deletion.deleted_at, deletion.deleted_at, deletion.uuid],
      );
      upsertTombstone(deletion.uuid, "savings_win", deletion.deleted_at);
      result.delete_savings_wins += 1;
    }

    // 新 tombstone
    for (const tomb of diff.newTombstones) {
      db.run(
        `INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced)
         VALUES (?, ?, ?, ?, 0)
         ON CONFLICT(uuid) DO UPDATE SET deleted_at = excluded.deleted_at
         WHERE excluded.deleted_at > sync_tombstones.deleted_at`,
        [tomb.uuid, tomb.kind, tomb.deleted_at, tomb.device_id],
      );
    }

    // 物理清理已同步的软删除行
    const synced = db
      .query<{ uuid: string }>("SELECT uuid FROM sync_tombstones WHERE synced = 1")
      .map((r) => r.uuid);
    if (synced.length) {
      const deletedTx = db.query<{ id: number; uuid: string }>(
        "SELECT id, uuid FROM transactions WHERE deleted_at <> ''",
      );
      for (const row of deletedTx) {
        if (synced.includes(row.uuid)) {
          db.run("DELETE FROM line_items WHERE transaction_id = ?", [row.id]);
          db.run("DELETE FROM transactions WHERE id = ?", [row.id]);
        }
      }
      const placeholders = synced.map(() => "?").join(",");
      db.run(`DELETE FROM goals WHERE deleted_at <> '' AND uuid IN (${placeholders})`, synced);
      db.run(`DELETE FROM savings_wins WHERE deleted_at <> '' AND uuid IN (${placeholders})`, synced);
    }

    // 受影响日期的总结过期
    for (const day of affectedDates) {
      if (DATE_RE.test(day)) {
        db.run(
          "UPDATE summaries SET expired = 1 WHERE period_start <= ? AND period_end >= ?",
          [day, day],
        );
      }
    }

    // 同步事件与设备登记
    const conflictDates = [...diff.dayConflicts.keys()].sort();
    const summaryParts = [
      `新增交易 ${result.add_transactions}`,
      `修改交易 ${result.modify_transactions}`,
      `删除交易 ${result.delete_transactions}`,
      `目标变化 ${result.add_goals + result.modify_goals + result.delete_goals}`,
    ];
    if (conflictDates.length) summaryParts.push(`冲突日期 ${conflictDates.length}`);
    db.run(
      `INSERT INTO sync_events(package_id, source_device, exported_at, imported_at,
        direction, result, conflict_dates, decisions)
       VALUES (?, ?, ?, ?, 'import', ?, ?, ?)`,
      [
        pkg.manifest.device_id,
        pkg.manifest.device_id,
        pkg.manifest.created_at || "",
        now,
        summaryParts.join("，"),
        conflictDates.join(","),
        JSON.stringify(decisions),
      ],
    );
    db.run(
      `INSERT INTO sync_devices(device_id, name, platform, created_at, last_export_at, last_import_at)
       VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(device_id) DO UPDATE SET name = excluded.name, platform = excluded.platform`,
      [
        pkg.manifest.device_id,
        pkg.manifest.device_name || "另一台设备",
        pkg.manifest.platform || "desktop",
        now,
        pkg.manifest.created_at || "",
        now,
      ],
    );
    db.run("UPDATE sync_devices SET last_import_at = ? WHERE device_id = ?", [
      now,
      repo.localDeviceId(),
    ]);
  });

  await repo.save();
  return result;
}

export { uuidHex };
