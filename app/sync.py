"""共享同步包：全量快照导出、导入预览与按天冲突合并。

共享包不是整库备份，而是「交易/目标/省下的钱 + 删除标记 + 公开设置」的
可重复导入快照。合并规则：

- 记录级冲突判定基于 last_synced_at：某条记录在两侧相对上次同步都有变化
  才进入冲突；只有一侧变化自动采用变化侧；重复导入同一包不产生变化。
- 交易冲突按日期分组，目标冲突按 UUID 逐条处理。
- 删除通过软删除 + tombstone 传播，物理清理只在 tombstone 已同步后发生。
- 包内不含 API Key、总结正文、图片原件、日志与运行状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid as _uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.ai import VALID_TYPES
from app.config import load_config, save_config
from app.db import get_conn, now_str
from app.paths import get_paths
from app.version import APP_VERSION

SHARE_FORMAT = "better-money-share"
SHARE_FORMAT_VERSION = 1
MIN_SHARE_SCHEMA_VERSION = 3
MAX_PACKAGE_UNCOMPRESSED = 50 * 1024 * 1024
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
STAGING_TTL_SECONDS = 24 * 3600

MEMBERS = (
    "manifest.json",
    "transactions.json",
    "line_items.json",
    "goals.json",
    "savings_wins.json",
    "settings_public.json",
    "tombstones.json",
)

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

PUBLIC_SETTING_KEYS = (
    "initial_balance", "initial_balance_date", "monthly_budget",
    "auto_save_ratio", "cooldown_days",
)

TX_FIELDS = (
    "date", "amount", "type", "category", "merchant", "note", "source",
    "estimated",
)
GOAL_FIELDS = (
    "name", "price", "saved", "priority", "status", "cooldown_until",
    "expected_date", "note", "achieved_at",
)


class ShareError(Exception):
    """共享包校验或处理失败；message 直接面向用户。"""


_LOCAL_DEVICE_ID: str | None = None


def reset_identity_cache() -> None:
    global _LOCAL_DEVICE_ID
    _LOCAL_DEVICE_ID = None


def ensure_device_identity(
    conn: sqlite3.Connection, cfg: dict, save=save_config
) -> dict:
    """保证本机有 device_id、登记到 sync_devices、回填旧行的 device_id。"""
    global _LOCAL_DEVICE_ID
    device_id = str(cfg.get("device_id") or "").strip()
    changed = False
    if not device_id:
        device_id = _uuid.uuid4().hex
        cfg["device_id"] = device_id
        changed = True
    name = str(cfg.get("device_name") or "").strip() or "电脑"
    if cfg.get("device_name") != name:
        cfg["device_name"] = name
        changed = True
    if changed:
        save(cfg)
    now = now_str()
    conn.execute(
        "INSERT INTO sync_devices(device_id, name, platform, created_at) "
        "VALUES (?, ?, 'desktop', ?) "
        "ON CONFLICT(device_id) DO UPDATE SET name = excluded.name",
        (device_id, name, now),
    )
    for table in ("transactions", "goals", "savings_wins"):
        conn.execute(
            f"UPDATE {table} SET device_id = ? WHERE device_id = ''",
            (device_id,),
        )
    _LOCAL_DEVICE_ID = device_id
    return {"device_id": device_id, "name": name, "platform": "desktop"}


def local_device_id(conn: sqlite3.Connection) -> str:
    if _LOCAL_DEVICE_ID:
        return _LOCAL_DEVICE_ID
    cfg = load_config()
    return str(ensure_device_identity(conn, cfg)["device_id"])


def new_uuid() -> str:
    return _uuid.uuid4().hex


# ---------- 数值与校验辅助 ----------

def _money_str(value: Any) -> str:
    return f"{float(value or 0):.2f}"


def _money_num(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ShareError(f"{field_name} 金额无效: {value}")
    try:
        decimal = Decimal(str(value).strip())
    except InvalidOperation:
        raise ShareError(f"{field_name} 金额无效: {value}") from None
    rounded = decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def _require(value: Any, condition: bool, message: str) -> None:
    if not condition:
        raise ShareError(message)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_public_settings(cfg: dict) -> dict:
    """与包内设置同型（float 两位小数 / int），便于逐项比较。"""
    return {
        "initial_balance": _money_num(cfg.get("initial_balance") or 0, "初始余额"),
        "initial_balance_date": str(cfg.get("initial_balance_date") or ""),
        "monthly_budget": _money_num(cfg.get("monthly_budget") or 0, "月预算"),
        "auto_save_ratio": _money_num(cfg.get("auto_save_ratio") or 0, "自动存比例"),
        "cooldown_days": int(cfg.get("cooldown_days") or 7),
    }


# ---------- 导出 ----------

def export_share_package() -> dict:
    """一致性快照 → 校验 → 原子落盘，并登记 last_synced_at 与 sync_events。"""
    paths = get_paths()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cfg = load_config()
        device = ensure_device_identity(conn, cfg)
        live_tx = [
            dict(r) for r in conn.execute(
                "SELECT * FROM transactions WHERE deleted_at = '' "
                "ORDER BY date, id")
        ]
        tx_uuids = [r["uuid"] for r in live_tx]
        line_items = _query_many(
            conn, "SELECT * FROM line_items", ())
        line_items = [dict(r) for r in line_items
                      if r["transaction_id"] in {t["id"] for t in live_tx}]
        goals = [dict(r) for r in conn.execute(
            "SELECT * FROM goals WHERE deleted_at = '' ORDER BY priority, id")]
        wins = [dict(r) for r in conn.execute(
            "SELECT * FROM savings_wins WHERE deleted_at = '' "
            "ORDER BY date, id")]
        tombstones = [dict(r) for r in conn.execute(
            "SELECT * FROM sync_tombstones ORDER BY deleted_at, id")]

        tx_dates = sorted({r["date"] for r in live_tx if DATE_RE.match(r["date"] or "")})
        manifest = {
            "format": SHARE_FORMAT,
            "format_version": SHARE_FORMAT_VERSION,
            "app_version": APP_VERSION,
            "schema_version": 3,
            "device_id": device["device_id"],
            "device_name": device["name"],
            "platform": "desktop",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "date_min": tx_dates[0] if tx_dates else "",
            "date_max": tx_dates[-1] if tx_dates else "",
            "counts": {
                "transactions": len(live_tx),
                "goals": len(goals),
                "savings_wins": len(wins),
                "line_items": len(line_items),
                "tombstones": len(tombstones),
            },
            "includes": {
                "transactions": True,
                "goals": True,
                "savings_wins": True,
                "images": False,
                "summaries": False,
                "api_key": False,
            },
        }
        payload = {
            "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2),
            "transactions.json": json.dumps(
                [_tx_to_share(r) for r in live_tx],
                ensure_ascii=False, indent=2),
            "line_items.json": json.dumps(
                [_li_to_share(r, {t["id"]: t["uuid"] for t in live_tx})
                 for r in line_items],
                ensure_ascii=False, indent=2),
            "goals.json": json.dumps(
                [_goal_to_share(r) for r in goals],
                ensure_ascii=False, indent=2),
            "savings_wins.json": json.dumps(
                [_win_to_share(r) for r in wins],
                ensure_ascii=False, indent=2),
            "settings_public.json": json.dumps(
                _local_public_settings(cfg), ensure_ascii=False, indent=2),
            "tombstones.json": json.dumps(
                [_tombstone_to_share(r) for r in tombstones],
                ensure_ascii=False, indent=2),
        }

        paths.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"better-money-share-{device['device_id'][:8]}-{stamp}.zip"
        tmp_path = paths.backups_dir / (filename + ".tmp")
        final_path = paths.backups_dir / filename
        with zipfile.ZipFile(
            tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for member, content in payload.items():
                archive.writestr(member, content)

        package_id = _sha256(tmp_path.read_bytes())
        parsed = _read_package(tmp_path)  # 自校验：结构/字段/数量
        counts = parsed["manifest"]["counts"]
        if counts["transactions"] != len(live_tx) or counts["goals"] != len(goals):
            raise ShareError("导出自校验失败：清单数量与实际数据不一致")
        os.replace(tmp_path, final_path)

        now = now_str()
        for table in ("transactions", "goals", "savings_wins"):
            conn.execute(
                f"UPDATE {table} SET last_synced_at = updated_at "
                f"WHERE deleted_at = ''")
            conn.execute(
                f"UPDATE {table} SET last_synced_at = deleted_at "
                f"WHERE deleted_at <> '' AND last_synced_at < deleted_at")
        conn.execute("UPDATE sync_tombstones SET synced = 1")
        conn.execute(
            "UPDATE sync_devices SET last_export_at = ? WHERE device_id = ?",
            (now, device["device_id"]))
        conn.execute(
            "INSERT INTO sync_events(package_id, source_device, exported_at, "
            "imported_at, direction, result, conflict_dates, decisions) "
            "VALUES (?, ?, ?, '', 'export', ?, '', '')",
            (package_id, device["device_id"], now,
             f"导出 {len(live_tx)} 笔交易、{len(goals)} 个目标"))
        conn.commit()
        return {
            "ok": True,
            "filename": filename,
            "path": str(final_path),
            "package_id": package_id,
            "counts": counts,
        }
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _tx_to_share(row: dict) -> dict:
    return {
        "uuid": row["uuid"],
        "device_id": row["device_id"],
        "date": row["date"],
        "amount": _money_str(row["amount"]),
        "type": row["type"],
        "category": row["category"],
        "merchant": row["merchant"],
        "note": row["note"],
        "source": row["source"],
        "estimated": int(row["estimated"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _li_to_share(row: dict, tx_uuid_by_id: dict) -> dict:
    return {
        "uuid": row["uuid"],
        "transaction_uuid": tx_uuid_by_id.get(row["transaction_id"], ""),
        "name": row["name"],
        "qty": _money_str(row["qty"]),
        "price": _money_str(row["price"]),
        "updated_at": row["updated_at"],
    }


def _goal_to_share(row: dict) -> dict:
    return {
        "uuid": row["uuid"],
        "device_id": row["device_id"],
        "name": row["name"],
        "price": _money_str(row["price"]),
        "saved": _money_str(row["saved"]),
        "priority": int(row["priority"]),
        "status": row["status"],
        "cooldown_until": row["cooldown_until"],
        "expected_date": row["expected_date"],
        "note": row["note"],
        "created_at": row["created_at"],
        "achieved_at": row["achieved_at"],
        "updated_at": row["updated_at"],
    }


def _win_to_share(row: dict) -> dict:
    return {
        "uuid": row["uuid"],
        "device_id": row["device_id"],
        "goal_name": row["goal_name"],
        "amount": _money_str(row["amount"]),
        "date": row["date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _tombstone_to_share(row: dict) -> dict:
    return {
        "uuid": row["uuid"],
        "kind": row["kind"],
        "deleted_at": row["deleted_at"],
        "device_id": row["device_id"],
    }


# ---------- 包读取与校验 ----------

def _read_package(path: Path) -> dict:
    """安全读取并校验共享包 → 规范化结构（金额转 float，时间保持原文）。"""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ShareError("共享包无法解析：不是有效的 ZIP 文件") from None
    with archive:
        infos = archive.infolist()
        found: set[str] = set()
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            if _is_symlink(info):
                raise ShareError("共享包内包含符号链接，已拒绝导入")
            name = info.filename.replace("\\", "/")
            if name not in MEMBERS:
                raise ShareError(f"共享包内包含未知文件：{name}")
            if name in found:
                raise ShareError(f"共享包内文件重复：{name}")
            found.add(name)
            total += info.file_size
        if total > MAX_PACKAGE_UNCOMPRESSED:
            raise ShareError("共享包解压后体积超过上限")
        missing = set(MEMBERS) - found
        if missing:
            raise ShareError(f"共享包缺少文件：{'、'.join(sorted(missing))}")
        raw = {name: archive.read(name) for name in MEMBERS}

    sections: dict[str, Any] = {}
    for name in MEMBERS:
        try:
            sections[name] = json.loads(raw[name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ShareError(f"共享包文件 {name} 不是合法 JSON") from None

    manifest = sections["manifest.json"]
    _require(manifest, isinstance(manifest, dict), "manifest 必须是 JSON 对象")
    _require(
        manifest, manifest.get("format") == SHARE_FORMAT,
        "这不是 Better-money 共享包（format 不符）；完整备份请用「完整备份」功能")
    _require(
        manifest, manifest.get("format_version") == SHARE_FORMAT_VERSION,
        f"不支持的共享包格式版本：{manifest.get('format_version')}")
    schema = manifest.get("schema_version")
    _require(
        manifest,
        isinstance(schema, int)
        and MIN_SHARE_SCHEMA_VERSION <= schema <= 3,
        f"不支持的共享包数据版本：{schema}")
    _require(
        manifest,
        UUID_RE.match(str(manifest.get("device_id") or "")),
        "共享包缺少有效的来源设备标识")

    tx_list = _parse_list(sections, "transactions.json", "uuid")
    li_list = _parse_list(sections, "line_items.json", "uuid")
    goal_list = _parse_list(sections, "goals.json", "uuid")
    win_list = _parse_list(sections, "savings_wins.json", "uuid")
    tomb_list = _parse_list(sections, "tombstones.json", "uuid")
    settings = sections["settings_public.json"]
    _require(settings, isinstance(settings, dict), "settings_public 必须是对象")

    tx_uuids = set()
    for item in tx_list:
        _check_timestamp(item, "created_at")
        _check_timestamp(item, "updated_at")
        _require(item, DATE_RE.match(str(item.get("date") or "")),
                  f"交易日期无效：{item.get('date')}")
        _require(item, item.get("type") in VALID_TYPES,
                  f"交易类型无效：{item.get('type')}")
        item["amount"] = _money_num(item.get("amount"), "交易金额")
        item["estimated"] = int(item.get("estimated") or 0)
        tx_uuids.add(item["uuid"])

    li_map: dict[str, list[dict]] = {}
    for item in li_list:
        _check_timestamp(item, "updated_at")
        _require(item, item.get("transaction_uuid") in tx_uuids,
                  f"单品明细引用了不存在的交易：{item.get('transaction_uuid')}")
        item["qty"] = _money_num(item.get("qty"), "单品数量")
        item["price"] = _money_num(item.get("price"), "单品单价")
        li_map.setdefault(item["transaction_uuid"], []).append(item)

    for item in goal_list:
        _check_timestamp(item, "created_at")
        _check_timestamp(item, "updated_at")
        for key in ("price", "saved"):
            item[key] = _money_num(item.get(key), f"目标{key}")
        item["priority"] = int(item.get("priority") or 100)

    for item in win_list:
        _check_timestamp(item, "created_at")
        _check_timestamp(item, "updated_at")
        _require(item, DATE_RE.match(str(item.get("date") or "")),
                  f"省下的钱日期无效：{item.get('date')}")
        item["amount"] = _money_num(item.get("amount"), "省下的钱金额")

    live_uuids = tx_uuids | {g["uuid"] for g in goal_list} | {
        w["uuid"] for w in win_list}
    for item in tomb_list:
        _check_timestamp(item, "deleted_at")
        _require(item, item.get("kind") in ("transaction", "goal", "savings_win"),
                  f"tombstone 类型无效：{item.get('kind')}")
        _require(item, item["uuid"] not in live_uuids,
                  f"tombstone 与数据文件冲突：{item['uuid']}")

    for key in PUBLIC_SETTING_KEYS:
        _require(settings, key in settings, f"共享设置缺少字段：{key}")
    settings_norm = {
        "initial_balance": _money_num(settings.get("initial_balance"), "初始余额"),
        "initial_balance_date": str(settings.get("initial_balance_date") or ""),
        "monthly_budget": _money_num(settings.get("monthly_budget"), "月预算"),
        "auto_save_ratio": _money_num(settings.get("auto_save_ratio"), "自动存比例"),
        "cooldown_days": int(settings.get("cooldown_days") or 7),
    }
    if settings_norm["initial_balance_date"] and not DATE_RE.match(
        settings_norm["initial_balance_date"]
    ):
        raise ShareError("共享设置里初始余额日期无效")

    return {
        "manifest": manifest,
        "transactions": tx_list,
        "line_items": li_map,
        "goals": goal_list,
        "savings_wins": win_list,
        "tombstones": tomb_list,
        "settings": settings_norm,
    }


def _parse_list(sections: dict, name: str, key_field: str) -> list[dict]:
    value = sections[name]
    _require(value, isinstance(value, list), f"{name} 必须是数组")
    seen: set[str] = set()
    result = []
    for item in value:
        _require(item, isinstance(item, dict), f"{name} 含非对象元素")
        uuid = str(item.get("uuid") or "")
        _require(item, UUID_RE.match(uuid), f"{name} 含无效 uuid：{uuid}")
        _require(item, uuid not in seen, f"{name} 含重复 uuid：{uuid}")
        seen.add(uuid)
        result.append(item)
    return result


def _check_timestamp(item: dict, key: str) -> None:
    value = str(item.get(key) or "")
    _require(item, TS_RE.match(value), f"时间字段 {key} 无效：{value}")


# ---------- 导入暂存 ----------

def _staging_dir() -> Path:
    paths = get_paths()
    directory = paths.runtime_dir / "share-staging"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def stage_import_package(data: bytes) -> str:
    if not data:
        raise ShareError("上传内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ShareError("共享包文件过大")
    directory = _staging_dir()
    cutoff = time.time() - STAGING_TTL_SECONDS
    for old in directory.glob("*.zip"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    token = secrets.token_hex(16)
    (directory / f"{token}.zip").write_bytes(data)
    try:
        _read_package(directory / f"{token}.zip")  # 提前校验，坏包立即报错
    except BaseException:
        (directory / f"{token}.zip").unlink(missing_ok=True)
        raise
    return token


def _staging_path(token: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ShareError("导入会话无效")
    path = _staging_dir() / f"{token}.zip"
    if not path.exists():
        raise ShareError("导入会话已过期，请重新选择共享包")
    return path


def discard_import(token: str) -> None:
    try:
        _staging_path(token).unlink(missing_ok=True)
    except ShareError:
        pass


# ---------- 差异计算 ----------

def _local_maps(conn: sqlite3.Connection) -> dict:
    return {
        "transactions": {r["uuid"]: dict(r) for r in conn.execute(
            "SELECT * FROM transactions")},
        "goals": {r["uuid"]: dict(r) for r in conn.execute(
            "SELECT * FROM goals")},
        "savings_wins": {r["uuid"]: dict(r) for r in conn.execute(
            "SELECT * FROM savings_wins")},
        "tombstones": {r["uuid"]: dict(r) for r in conn.execute(
            "SELECT * FROM sync_tombstones")},
    }


@dataclass
class _Diff:
    add_tx: list[dict] = field(default_factory=list)
    mod_tx: list[dict] = field(default_factory=list)
    del_tx: list[dict] = field(default_factory=list)   # {uuid, deleted_at}
    keep_local_tx: int = 0
    day_conflicts: dict = field(default_factory=dict)  # date → [entries]
    add_goals: list[dict] = field(default_factory=list)
    mod_goals: list[dict] = field(default_factory=list)
    del_goals: list[dict] = field(default_factory=list)
    goal_conflicts: list[dict] = field(default_factory=list)
    add_wins: list[dict] = field(default_factory=list)
    mod_wins: list[dict] = field(default_factory=list)
    del_wins: list[dict] = field(default_factory=list)
    new_tombstones: list[dict] = field(default_factory=list)
    dupes: list[dict] = field(default_factory=list)


def _changed(row: dict) -> bool:
    return (row.get("updated_at") or "") != (row.get("last_synced_at") or "")


def _diff_against_local(conn: sqlite3.Connection, pkg: dict) -> _Diff:
    local = _local_maps(conn)
    diff = _Diff()

    local_tx = local["transactions"]
    peer_tombs = {t["uuid"]: t for t in pkg["tombstones"]
                  if t["kind"] == "transaction"}

    for peer in pkg["transactions"]:
        uuid = peer["uuid"]
        row = local_tx.get(uuid)
        tomb = local["tombstones"].get(uuid)
        if row is None or row["deleted_at"]:
            if row is not None and str(row["last_synced_at"] or "") >= str(
                peer["updated_at"]
            ):
                continue  # 已就「保持删除」处理过这个对端版本
            if tomb and str(tomb["deleted_at"] or "") >= str(peer["updated_at"]):
                continue  # 本机删除更新，保持删除
            if row is not None:
                _add_day_conflict(diff, peer["date"], {
                    "uuid": uuid, "kind": "delete_vs_modify",
                    "local": None, "peer": peer})
            elif tomb:
                _add_day_conflict(diff, peer["date"], {
                    "uuid": uuid, "kind": "resurrect",
                    "local": None, "peer": peer})
            else:
                diff.add_tx.append(peer)
        elif _changed(row) and peer["updated_at"] != row["last_synced_at"]:
            _add_day_conflict(diff, peer["date"], {
                "uuid": uuid, "kind": "both_changed",
                "local": row, "peer": peer})
        elif peer["updated_at"] != row["last_synced_at"]:
            diff.mod_tx.append(peer)
        elif _changed(row):
            diff.keep_local_tx += 1

    for tomb in pkg["tombstones"]:
        if tomb["kind"] != "transaction":
            continue
        uuid = tomb["uuid"]
        row = local_tx.get(uuid)
        ltomb = local["tombstones"].get(uuid)
        if row is not None and not row["deleted_at"]:
            if _changed(row) and str(row["updated_at"]) > str(tomb["deleted_at"]):
                _add_day_conflict(diff, row["date"], {
                    "uuid": uuid, "kind": "delete_vs_modify",
                    "local": row, "peer": None,
                    "deleted_at": tomb["deleted_at"]})
            else:
                diff.del_tx.append({"uuid": uuid, "deleted_at": tomb["deleted_at"]})
        elif ltomb is None:
            diff.new_tombstones.append(tomb)
        elif str(tomb["deleted_at"]) > str(ltomb["deleted_at"]):
            diff.new_tombstones.append(tomb)

    # 疑似重复：仅针对「包内新增」的交易
    live_local = [r for r in local_tx.values() if not r["deleted_at"]]
    for peer in diff.add_tx:
        for row in live_local:
            if (row["date"] == peer["date"]
                    and float(row["amount"] or 0) == peer["amount"]
                    and row["merchant"] == peer["merchant"]
                    and row["uuid"] != peer["uuid"]):
                diff.dupes.append({
                    "uuid": peer["uuid"], "date": peer["date"],
                    "amount": peer["amount"], "merchant": peer["merchant"],
                    "local_uuid": row["uuid"],
                })
                break

    # 目标：按 UUID 逐条
    local_goals = local["goals"]
    for peer in pkg["goals"]:
        uuid = peer["uuid"]
        row = local_goals.get(uuid)
        tomb = local["tombstones"].get(uuid)
        if row is None or row["deleted_at"]:
            if tomb and str(tomb["deleted_at"] or "") >= str(peer["updated_at"]):
                continue
            if row is not None:
                diff.goal_conflicts.append({
                    "uuid": uuid, "kind": "delete_vs_modify",
                    "suggested": "peer", "local": None, "peer": peer})
            elif tomb:
                diff.goal_conflicts.append({
                    "uuid": uuid, "kind": "resurrect",
                    "suggested": "local", "local": None, "peer": peer})
            else:
                diff.add_goals.append(peer)
        elif _changed(row) and peer["updated_at"] != row["last_synced_at"]:
            suggested = "peer" if peer["updated_at"] >= row["updated_at"] else "local"
            diff.goal_conflicts.append({
                "uuid": uuid, "kind": "both_changed", "suggested": suggested,
                "local": row, "peer": peer})
        elif peer["updated_at"] != row["last_synced_at"]:
            diff.mod_goals.append(peer)
        elif _changed(row):
            diff.keep_local_tx += 1

    goal_tombs = {t["uuid"]: t for t in pkg["tombstones"]
                  if t["kind"] == "goal"}
    for tomb in goal_tombs.values():
        uuid = tomb["uuid"]
        row = local_goals.get(uuid)
        ltomb = local["tombstones"].get(uuid)
        if row is not None and not row["deleted_at"]:
            if _changed(row) and str(row["updated_at"]) > str(tomb["deleted_at"]):
                diff.goal_conflicts.append({
                    "uuid": uuid, "kind": "delete_vs_modify",
                    "suggested": "local", "local": row, "peer": None,
                    "deleted_at": tomb["deleted_at"]})
            else:
                diff.del_goals.append({"uuid": uuid, "deleted_at": tomb["deleted_at"]})
        elif ltomb is None:
            diff.new_tombstones.append(tomb)
        elif str(tomb["deleted_at"]) > str(ltomb["deleted_at"]):
            diff.new_tombstones.append(tomb)

    # 省下的钱：自动按时间戳（不产生冲突面板）
    local_wins = local["savings_wins"]
    for peer in pkg["savings_wins"]:
        uuid = peer["uuid"]
        row = local_wins.get(uuid)
        tomb = local["tombstones"].get(uuid)
        if row is None or row["deleted_at"]:
            if tomb and str(tomb["deleted_at"] or "") >= str(peer["updated_at"]):
                continue
            if row is None and tomb is None:
                diff.add_wins.append(peer)
            elif row is not None and str(row["updated_at"]) < str(peer["updated_at"]):
                diff.mod_wins.append(peer)
        elif peer["updated_at"] > row["updated_at"]:
            diff.mod_wins.append(peer)

    for tomb in pkg["tombstones"]:
        if tomb["kind"] != "savings_win":
            continue
        uuid = tomb["uuid"]
        row = local_wins.get(uuid)
        ltomb = local["tombstones"].get(uuid)
        if row is not None and not row["deleted_at"]:
            if str(row["updated_at"]) > str(tomb["deleted_at"]):
                continue  # 本机修改更新，保留
            diff.del_wins.append({"uuid": uuid, "deleted_at": tomb["deleted_at"]})
        elif ltomb is None:
            diff.new_tombstones.append(tomb)
        elif str(tomb["deleted_at"]) > str(ltomb["deleted_at"]):
            diff.new_tombstones.append(tomb)

    return diff


def _add_day_conflict(diff: _Diff, day: str, entry: dict) -> None:
    key = day if DATE_RE.match(str(day or "")) else "未知日期"
    diff.day_conflicts.setdefault(key, []).append(entry)


def _public_diff(entry: dict) -> dict:
    return {
        "uuid": entry["uuid"],
        "date": entry.get("date", ""),
        "amount": _money_str(entry.get("amount")),
        "type": entry.get("type", ""),
        "category": entry.get("category", ""),
        "merchant": entry.get("merchant", ""),
        "note": entry.get("note", ""),
        "source": entry.get("source", ""),
        "estimated": int(entry.get("estimated") or 0),
        "updated_at": entry.get("updated_at", ""),
    }


# ---------- 预览 ----------

def preview_import(token: str) -> dict:
    path = _staging_path(token)
    pkg = _read_package(path)
    conn = get_conn()
    try:
        cfg = load_config()
        diff = _diff_against_local(conn, pkg)
        package_settings = pkg["settings"]
        local_settings = _local_public_settings(cfg)
        settings_conflict = any(
            local_settings.get(key) != package_settings.get(key)
            for key in PUBLIC_SETTING_KEYS
        )
        tx_dates = sorted({t["date"] for t in pkg["transactions"]})
        return {
            "token": token,
            "package": {
                "device_id": pkg["manifest"]["device_id"],
                "device_name": pkg["manifest"].get("device_name") or "另一台设备",
                "platform": pkg["manifest"].get("platform") or "",
                "created_at": pkg["manifest"].get("created_at") or "",
                "counts": pkg["manifest"]["counts"],
                "date_min": tx_dates[0] if tx_dates else "",
                "date_max": tx_dates[-1] if tx_dates else "",
            },
            "settings": {
                "local": local_settings,
                "package": package_settings,
                "conflict": settings_conflict,
            },
            "summary": {
                "add_transactions": len(diff.add_tx),
                "modify_transactions": len(diff.mod_tx),
                "delete_transactions": len(diff.del_tx),
                "keep_local_transactions": diff.keep_local_tx,
                "add_goals": len(diff.add_goals),
                "modify_goals": len(diff.mod_goals),
                "delete_goals": len(diff.del_goals),
                "add_savings_wins": len(diff.add_wins),
                "modify_savings_wins": len(diff.mod_wins),
                "delete_savings_wins": len(diff.del_wins),
                "new_tombstones": len(diff.new_tombstones),
                "conflict_days": [
                    {
                        "date": day,
                        "items": [
                            {
                                "uuid": item["uuid"],
                                "kind": item["kind"],
                                "local": (
                                    _public_diff(item["local"])
                                    if item["local"] else None),
                                "peer": (
                                    _public_diff(item["peer"])
                                    if item["peer"] else None),
                            }
                            for item in items
                        ],
                    }
                    for day, items in sorted(diff.day_conflicts.items())
                ],
                "goal_conflicts": [
                    {
                        "uuid": item["uuid"],
                        "kind": item["kind"],
                        "suggested": item["suggested"],
                        "local": (
                            _goal_public(item["local"]) if item["local"] else None),
                        "peer": (
                            _goal_public(item["peer"]) if item["peer"] else None),
                    }
                    for item in diff.goal_conflicts
                ],
                "dupes": [
                    {
                        "uuid": item["uuid"], "date": item["date"],
                        "amount": _money_str(item["amount"]),
                        "merchant": item["merchant"],
                        "local_uuid": item["local_uuid"],
                    }
                    for item in diff.dupes
                ],
            },
        }
    finally:
        conn.close()


def _goal_public(entry: dict) -> dict:
    return {
        "uuid": entry.get("uuid", ""),
        "name": entry.get("name", ""),
        "price": _money_str(entry.get("price")),
        "saved": _money_str(entry.get("saved")),
        "status": entry.get("status", ""),
        "updated_at": entry.get("updated_at", ""),
    }


# ---------- 应用合并 ----------

def apply_import(token: str, decisions: dict) -> dict:
    path = _staging_path(token)
    pkg = _read_package(path)
    cfg = load_config()

    if decisions.get("settings") == "apply_package":
        for key in PUBLIC_SETTING_KEYS:
            cfg[key] = pkg["settings"][key]
        save_config(cfg)

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_device_identity(conn, cfg)
        diff = _diff_against_local(conn, pkg)
        _validate_decisions(diff, decisions)

        affected_dates: set[str] = set()
        result = _apply_transactions(conn, pkg, diff, decisions, affected_dates)
        _apply_goals(conn, pkg, diff, decisions, result)
        _apply_wins(conn, pkg, diff, result)
        _apply_tombstones(conn, diff)
        _apply_dupes(diff, decisions)

        _purge_synced_soft_deleted(conn)
        for day in affected_dates:
            if DATE_RE.match(day or ""):
                conn.execute(
                    "UPDATE summaries SET expired = 1 "
                    "WHERE period_start <= ? AND period_end >= ?", (day, day))

        now = now_str()
        conflict_dates = sorted(diff.day_conflicts.keys())
        conn.execute(
            "INSERT INTO sync_events(package_id, source_device, exported_at, "
            "imported_at, direction, result, conflict_dates, decisions) "
            "VALUES (?, ?, ?, ?, 'import', ?, ?, ?)",
            (pkg["manifest"]["device_id"], pkg["manifest"]["device_id"],
             pkg["manifest"].get("created_at") or "", now,
             _result_summary(result, diff), ",".join(conflict_dates),
             json.dumps(decisions, ensure_ascii=False)))
        conn.execute(
            "INSERT INTO sync_devices(device_id, name, platform, created_at, "
            "last_export_at, last_import_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "name = excluded.name, platform = excluded.platform",
            (pkg["manifest"]["device_id"],
             pkg["manifest"].get("device_name") or "另一台设备",
             pkg["manifest"].get("platform") or "mobile",
             now, pkg["manifest"].get("created_at") or "", now))
        device = ensure_device_identity(conn, cfg)
        conn.execute(
            "UPDATE sync_devices SET last_import_at = ? WHERE device_id = ?",
            (now, device["device_id"]))
        conn.commit()
        result["conflict_days_resolved"] = len(conflict_dates)
        return {"ok": True, **result}
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _result_summary(result: dict, diff: _Diff) -> str:
    parts = [
        f"新增交易 {result['add_transactions']}",
        f"修改交易 {result['modify_transactions']}",
        f"删除交易 {result['delete_transactions']}",
        f"目标变化 {result['add_goals'] + result['modify_goals'] + result['delete_goals']}",
    ]
    if diff.day_conflicts:
        parts.append(f"冲突日期 {len(diff.day_conflicts)}")
    return "，".join(parts)


def _validate_decisions(diff: _Diff, decisions: dict) -> None:
    if not isinstance(decisions, dict):
        raise ShareError("决策数据无效")
    day_decisions = decisions.get("days") or {}
    for day, items in diff.day_conflicts.items():
        mode = day_decisions.get(day, {}).get("mode")
        if mode not in ("keep_local", "keep_peer", "merge"):
            raise ShareError(f"日期 {day} 的冲突还没有处理")
        if mode == "merge":
            item_decisions = day_decisions.get(day, {}).get("items") or {}
            for item in items:
                if item_decisions.get(item["uuid"]) not in ("local", "peer", "drop"):
                    raise ShareError(
                        f"日期 {day} 的合并模式缺少逐条处理结果")
    goal_decisions = decisions.get("goals") or {}
    for item in diff.goal_conflicts:
        if item["kind"] == "delete_vs_modify":
            if goal_decisions.get(item["uuid"]) not in ("local", "peer", "drop"):
                raise ShareError("目标「删除与修改」冲突还没有处理")
        elif item["kind"] == "resurrect":
            if goal_decisions.get(item["uuid"]) not in ("local", "peer"):
                raise ShareError("目标「复活与删除」冲突还没有处理")
    if decisions.get("settings") not in ("keep_local", "apply_package", None, ""):
        raise ShareError("设置冲突的决策无效")


def _resolve_day_items(diff: _Diff, day: str, decisions: dict) -> dict:
    """返回 {uuid: 'local'|'peer'|'drop'}。"""
    day_decisions = (decisions.get("days") or {}).get(day, {})
    mode = day_decisions.get("mode", "keep_local")
    items = diff.day_conflicts.get(day, [])
    if mode == "keep_local":
        return {item["uuid"]: "local" for item in items}
    if mode == "keep_peer":
        return {item["uuid"]: "peer" for item in items}
    return dict(day_decisions.get("items") or {})


def _apply_transactions(
    conn: sqlite3.Connection, pkg: dict, diff: _Diff,
    decisions: dict, affected_dates: set[str],
) -> dict:
    counts = {"add_transactions": 0, "modify_transactions": 0,
              "delete_transactions": 0}
    li_map = pkg["line_items"]

    day_resolutions: dict[str, dict] = {}
    for day in diff.day_conflicts:
        day_resolutions[day] = _resolve_day_items(diff, day, decisions)

    for peer in diff.add_tx:
        if decisions.get("dupes") or {}:
            if (decisions["dupes"].get(peer["uuid"]) == "drop"):
                continue
        _insert_tx(conn, peer, li_map.get(peer["uuid"], []))
        counts["add_transactions"] += 1
        affected_dates.add(peer["date"])

    for peer in diff.mod_tx:
        _update_tx(conn, peer, li_map.get(peer["uuid"], []))
        counts["modify_transactions"] += 1
        affected_dates.add(peer["date"])

    for deletion in diff.del_tx:
        _soft_delete_tx(conn, deletion["uuid"], deletion["deleted_at"])
        counts["delete_transactions"] += 1

    for day, items in diff.day_conflicts.items():
        resolutions = day_resolutions[day]
        for item in items:
            uuid = item["uuid"]
            choice = resolutions.get(uuid, "local")
            peer = item["peer"]
            local = item["local"]
            if item["kind"] == "both_changed":
                if choice == "peer" and peer is not None:
                    _update_tx(conn, peer, li_map.get(uuid, []))
                    counts["modify_transactions"] += 1
                    affected_dates.add(peer["date"])
                elif choice == "drop":
                    _soft_delete_tx(conn, uuid, now_str())
                    counts["delete_transactions"] += 1
                elif choice == "local":
                    _touch_last_synced(conn, "transactions", uuid,
                                       peer["updated_at"])
            elif item["kind"] == "delete_vs_modify":
                if peer is not None:
                    # 包内是活记录（更新），本机是删除
                    if choice == "peer":
                        if local is None:
                            _revive_tx(conn, peer, li_map.get(uuid, []))
                            counts["add_transactions"] += 1
                        else:
                            _update_tx(conn, peer, li_map.get(uuid, []))
                            counts["modify_transactions"] += 1
                        affected_dates.add(peer["date"])
                    elif choice == "drop":
                        _soft_delete_tx(conn, uuid, now_str())
                        counts["delete_transactions"] += 1
                    else:
                        _touch_last_synced(conn, "transactions", uuid,
                                           peer["updated_at"])
                else:
                    # 包内是删除，本机有更新
                    if choice in ("peer", "drop"):
                        _soft_delete_tx(conn, uuid, now_str())
                        counts["delete_transactions"] += 1
                    else:
                        _touch_last_synced(
                            conn, "transactions", uuid,
                            item.get("deleted_at") or now_str())
            elif item["kind"] == "resurrect":
                if choice == "peer" and peer is not None:
                    conn.execute(
                        "DELETE FROM sync_tombstones WHERE uuid = ?", (uuid,))
                    _insert_tx(conn, peer, li_map.get(uuid, []))
                    counts["add_transactions"] += 1
                    affected_dates.add(peer["date"])
                else:
                    conn.execute(
                        "UPDATE sync_tombstones SET deleted_at = ? "
                        "WHERE uuid = ?", (peer["updated_at"], uuid))

    return counts


def _insert_tx(conn: sqlite3.Connection, peer: dict, items: list) -> None:
    cur = conn.execute(
        "INSERT INTO transactions(date, amount, type, category, merchant, "
        "note, source, estimated, created_at, updated_at, uuid, device_id, "
        "deleted_at, last_synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
        (peer["date"], peer["amount"], peer["type"], peer["category"],
         peer["merchant"], peer["note"], peer["source"], peer["estimated"],
         peer["created_at"], peer["updated_at"], peer["uuid"],
         peer["device_id"], peer["updated_at"]))
    tx_id = cur.lastrowid
    for item in items:
        conn.execute(
            "INSERT INTO line_items(transaction_id, name, qty, price, uuid, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tx_id, item["name"], item["qty"], item["price"],
             item["uuid"], item["updated_at"]))


def _update_tx(conn: sqlite3.Connection, peer: dict, items: list) -> None:
    conn.execute(
        "UPDATE transactions SET date = ?, amount = ?, type = ?, "
        "category = ?, merchant = ?, note = ?, source = ?, estimated = ?, "
        "updated_at = ?, last_synced_at = ? WHERE uuid = ?",
        (peer["date"], peer["amount"], peer["type"], peer["category"],
         peer["merchant"], peer["note"], peer["source"], peer["estimated"],
         peer["updated_at"], peer["updated_at"], peer["uuid"]))
    tx_id = conn.execute(
        "SELECT id FROM transactions WHERE uuid = ?", (peer["uuid"],)
    ).fetchone()[0]
    conn.execute(
        "DELETE FROM line_items WHERE transaction_id = ?", (tx_id,))
    for item in items:
        conn.execute(
            "INSERT INTO line_items(transaction_id, name, qty, price, uuid, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tx_id, item["name"], item["qty"], item["price"],
             item["uuid"], item["updated_at"]))


def _revive_tx(conn: sqlite3.Connection, peer: dict, items: list) -> None:
    """恢复本机已软删除的交易：采用包内版本并清除删除标记与 tombstone。"""
    updated = conn.execute(
        "UPDATE transactions SET date = ?, amount = ?, type = ?, "
        "category = ?, merchant = ?, note = ?, source = ?, estimated = ?, "
        "created_at = ?, updated_at = ?, device_id = ?, deleted_at = '', "
        "last_synced_at = ? WHERE uuid = ? AND deleted_at <> ''",
        (peer["date"], peer["amount"], peer["type"], peer["category"],
         peer["merchant"], peer["note"], peer["source"], peer["estimated"],
         peer["created_at"], peer["updated_at"], peer["device_id"],
         peer["updated_at"], peer["uuid"])).rowcount
    conn.execute("DELETE FROM sync_tombstones WHERE uuid = ?", (peer["uuid"],))
    if not updated:
        _insert_tx(conn, peer, items)
        return
    tx_id = conn.execute(
        "SELECT id FROM transactions WHERE uuid = ?", (peer["uuid"],)
    ).fetchone()[0]
    conn.execute("DELETE FROM line_items WHERE transaction_id = ?", (tx_id,))
    for item in items:
        conn.execute(
            "INSERT INTO line_items(transaction_id, name, qty, price, uuid, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tx_id, item["name"], item["qty"], item["price"],
             item["uuid"], item["updated_at"]))


def _soft_delete_tx(conn: sqlite3.Connection, uuid: str, deleted_at: str) -> None:
    conn.execute(
        "UPDATE transactions SET deleted_at = ?, updated_at = ?, "
        "last_synced_at = ? WHERE uuid = ? AND deleted_at = ''",
        (deleted_at, deleted_at, deleted_at, uuid))
    _upsert_tombstone(conn, uuid, "transaction", deleted_at)


def _touch_last_synced(
    conn: sqlite3.Connection, table: str, uuid: str, value: str
) -> None:
    conn.execute(
        f"UPDATE {table} SET last_synced_at = ? WHERE uuid = ?", (value, uuid))


def _apply_goals(
    conn: sqlite3.Connection, pkg: dict, diff: _Diff,
    decisions: dict, result: dict,
) -> None:
    counts = {"add_goals": 0, "modify_goals": 0, "delete_goals": 0}
    goal_decisions = decisions.get("goals") or {}

    for peer in diff.add_goals:
        _insert_goal(conn, peer)
        counts["add_goals"] += 1
    for peer in diff.mod_goals:
        _update_goal(conn, peer)
        counts["modify_goals"] += 1
    for deletion in diff.del_goals:
        _soft_delete_goal(conn, deletion["uuid"], deletion["deleted_at"])
        counts["delete_goals"] += 1

    for item in diff.goal_conflicts:
        uuid = item["uuid"]
        choice = goal_decisions.get(uuid)
        if item["kind"] == "both_changed":
            if choice in (None, ""):
                choice = item["suggested"]
            if choice == "peer" and item["peer"] is not None:
                _update_goal(conn, item["peer"])
                counts["modify_goals"] += 1
            elif choice == "drop":
                _soft_delete_goal(conn, uuid, now_str())
                counts["delete_goals"] += 1
            else:
                _touch_last_synced(
                    conn, "goals", uuid, item["peer"]["updated_at"])
        elif item["kind"] == "delete_vs_modify":
            if item["peer"] is not None:
                # 包内是活记录（更新），本机是删除
                if choice == "peer":
                    if item["local"] is None:
                        _revive_goal(conn, item["peer"])
                        counts["add_goals"] += 1
                    else:
                        _update_goal(conn, item["peer"])
                        counts["modify_goals"] += 1
                elif choice == "drop":
                    _soft_delete_goal(conn, uuid, now_str())
                    counts["delete_goals"] += 1
                else:
                    _touch_last_synced(conn, "goals", uuid,
                                       item["peer"]["updated_at"])
            else:
                # 包内是删除，本机有更新
                if choice in ("peer", "drop"):
                    _soft_delete_goal(conn, uuid, now_str())
                    counts["delete_goals"] += 1
                else:
                    _touch_last_synced(
                        conn, "goals", uuid,
                        item.get("deleted_at") or now_str())
        elif item["kind"] == "resurrect":
            if choice == "peer":
                conn.execute(
                    "DELETE FROM sync_tombstones WHERE uuid = ?", (uuid,))
                _insert_goal(conn, item["peer"])
                counts["add_goals"] += 1
            else:
                conn.execute(
                    "UPDATE sync_tombstones SET deleted_at = ? WHERE uuid = ?",
                    (item["peer"]["updated_at"], uuid))

    result.update(counts)


def _insert_goal(conn: sqlite3.Connection, peer: dict) -> None:
    conn.execute(
        "INSERT INTO goals(name, price, saved, priority, status, "
        "cooldown_until, expected_date, note, created_at, achieved_at, "
        "uuid, device_id, updated_at, deleted_at, last_synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
        (peer["name"], peer["price"], peer["saved"], peer["priority"],
         peer["status"], peer["cooldown_until"], peer["expected_date"],
         peer["note"], peer["created_at"], peer["achieved_at"],
         peer["uuid"], peer["device_id"], peer["updated_at"],
         peer["updated_at"]))


def _update_goal(conn: sqlite3.Connection, peer: dict) -> None:
    conn.execute(
        "UPDATE goals SET name = ?, price = ?, saved = ?, priority = ?, "
        "status = ?, cooldown_until = ?, expected_date = ?, note = ?, "
        "achieved_at = ?, updated_at = ?, last_synced_at = ? WHERE uuid = ?",
        (peer["name"], peer["price"], peer["saved"], peer["priority"],
         peer["status"], peer["cooldown_until"], peer["expected_date"],
         peer["note"], peer["achieved_at"], peer["updated_at"],
         peer["updated_at"], peer["uuid"]))


def _revive_goal(conn: sqlite3.Connection, peer: dict) -> None:
    """恢复本机已软删除的目标：采用包内版本并清除删除标记与 tombstone。"""
    updated = conn.execute(
        "UPDATE goals SET name = ?, price = ?, saved = ?, priority = ?, "
        "status = ?, cooldown_until = ?, expected_date = ?, note = ?, "
        "created_at = ?, achieved_at = ?, device_id = ?, updated_at = ?, "
        "deleted_at = '', last_synced_at = ? WHERE uuid = ? AND deleted_at <> ''",
        (peer["name"], peer["price"], peer["saved"], peer["priority"],
         peer["status"], peer["cooldown_until"], peer["expected_date"],
         peer["note"], peer["created_at"], peer["achieved_at"],
         peer["device_id"], peer["updated_at"], peer["updated_at"],
         peer["uuid"])).rowcount
    conn.execute("DELETE FROM sync_tombstones WHERE uuid = ?", (peer["uuid"],))
    if not updated:
        _insert_goal(conn, peer)


def _soft_delete_goal(
    conn: sqlite3.Connection, uuid: str, deleted_at: str
) -> None:
    conn.execute(
        "UPDATE goals SET deleted_at = ?, updated_at = ?, last_synced_at = ? "
        "WHERE uuid = ? AND deleted_at = ''",
        (deleted_at, deleted_at, deleted_at, uuid))
    _upsert_tombstone(conn, uuid, "goal", deleted_at)


def _apply_wins(
    conn: sqlite3.Connection, pkg: dict, diff: _Diff, result: dict
) -> None:
    counts = {"add_savings_wins": 0, "modify_savings_wins": 0,
              "delete_savings_wins": 0}
    for peer in diff.add_wins:
        conn.execute(
            "INSERT INTO savings_wins(goal_name, amount, date, created_at, "
            "uuid, device_id, updated_at, deleted_at, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)",
            (peer["goal_name"], peer["amount"], peer["date"],
             peer["created_at"], peer["uuid"], peer["device_id"],
             peer["updated_at"], peer["updated_at"]))
        counts["add_savings_wins"] += 1
    for peer in diff.mod_wins:
        conn.execute(
            "UPDATE savings_wins SET goal_name = ?, amount = ?, date = ?, "
            "updated_at = ?, last_synced_at = ? WHERE uuid = ?",
            (peer["goal_name"], peer["amount"], peer["date"],
             peer["updated_at"], peer["updated_at"], peer["uuid"]))
        counts["modify_savings_wins"] += 1
    for deletion in diff.del_wins:
        conn.execute(
            "UPDATE savings_wins SET deleted_at = ?, updated_at = ?, "
            "last_synced_at = ? WHERE uuid = ? AND deleted_at = ''",
            (deletion["deleted_at"], deletion["deleted_at"],
             deletion["deleted_at"], deletion["uuid"]))
        _upsert_tombstone(conn, deletion["uuid"], "savings_win",
                          deletion["deleted_at"])
        counts["delete_savings_wins"] += 1
    result.update(counts)


def _apply_tombstones(conn: sqlite3.Connection, diff: _Diff) -> None:
    for tomb in diff.new_tombstones:
        conn.execute(
            "INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, "
            "synced) VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(uuid) DO UPDATE SET deleted_at = excluded.deleted_at "
            "WHERE excluded.deleted_at > sync_tombstones.deleted_at",
            (tomb["uuid"], tomb["kind"], tomb["deleted_at"],
             tomb["device_id"]))


def _apply_dupes(diff: _Diff, decisions: dict) -> None:
    pass  # dupes 决策在 add 阶段已跳过；保留扩展点


def _upsert_tombstone(
    conn: sqlite3.Connection, uuid: str, kind: str, deleted_at: str
) -> None:
    conn.execute(
        "INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced) "
        "VALUES (?, ?, ?, ?, 0) "
        "ON CONFLICT(uuid) DO UPDATE SET deleted_at = excluded.deleted_at, "
        "synced = 0 WHERE excluded.deleted_at > sync_tombstones.deleted_at",
        (uuid, kind, deleted_at, local_device_id(conn)))


def _purge_synced_soft_deleted(conn: sqlite3.Connection) -> None:
    """物理清理已同步的软删除行；tombstone 永久保留防止复活。"""
    synced = {
        row["uuid"]
        for row in conn.execute(
            "SELECT uuid FROM sync_tombstones WHERE synced = 1")
    }
    if not synced:
        return
    rows = conn.execute(
        "SELECT id, uuid FROM transactions WHERE deleted_at <> ''"
    ).fetchall()
    for row in rows:
        if row["uuid"] in synced:
            conn.execute(
                "DELETE FROM line_items WHERE transaction_id = ?", (row["id"],))
            conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
    conn.execute(
        "DELETE FROM goals WHERE deleted_at <> '' AND uuid IN "
        f"({','.join('?' for _ in synced)})", tuple(synced))
    conn.execute(
        "DELETE FROM savings_wins WHERE deleted_at <> '' AND uuid IN "
        f"({','.join('?' for _ in synced)})", tuple(synced))


# ---------- 状态与记录 ----------

def sync_status(conn: sqlite3.Connection = None) -> dict:
    owns = conn is None
    if owns:
        conn = get_conn()
    try:
        def pending(table: str) -> int:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE "
                f"(deleted_at = '' AND updated_at != last_synced_at) "
                f"OR (deleted_at <> '' AND deleted_at > last_synced_at)"
            ).fetchone()[0]

        device = load_config().get("device_id") or ""
        row = conn.execute(
            "SELECT name, last_export_at, last_import_at FROM sync_devices "
            "WHERE device_id = ?", (device,)).fetchone() if device else None
        peers = conn.execute(
            "SELECT device_id, name, platform, last_export_at, last_import_at "
            "FROM sync_devices WHERE device_id != ? ORDER BY id",
            (device,)).fetchall() if device else []
        return {
            "pending_changes": sum(
                pending(t) for t in ("transactions", "goals", "savings_wins")),
            "device_name": row["name"] if row else "电脑",
            "last_export_at": row["last_export_at"] if row else "",
            "last_import_at": row["last_import_at"] if row else "",
            "peers": [dict(p) for p in peers],
        }
    finally:
        if owns:
            conn.close()


def list_sync_events(limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM sync_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _query_many(conn: sqlite3.Connection, sql: str, args) -> list:
    return conn.execute(sql, args).fetchall()
