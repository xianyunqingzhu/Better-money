"""共享同步包：导出、校验、预览、按天冲突合并、目标冲突与删除传播。

验收口径与《手机端设计与实施计划》§10「共享」一节对应。
注意：账本连接互斥（同一时刻只能有一个普通连接），测试里在调用
export/import/status 等自开连接的函数前必须先关闭手里的连接。
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app import db, sync
from app.config import load_config, save_config
from app.paths import get_paths, reset_paths_cache


# ---------- 双设备模拟 ----------

def switch_home(path: Path) -> None:
    os.environ["BETTER_MONEY_HOME"] = str(path)
    reset_paths_cache()
    sync.reset_identity_cache()


@pytest.fixture
def device_a(tmp_path):
    home = tmp_path / "a"
    home.mkdir()
    switch_home(home)
    db.init_db()
    conn = db.get_conn()
    cfg = load_config()
    sync.ensure_device_identity(conn, cfg)
    conn.commit()
    conn.close()
    return home


@pytest.fixture
def device_b(tmp_path):
    home = tmp_path / "b"
    home.mkdir()
    switch_home(home)
    db.init_db()
    conn = db.get_conn()
    cfg = load_config()
    sync.ensure_device_identity(conn, cfg)
    conn.commit()
    conn.close()
    return home


def home_conn() -> sqlite3.Connection:
    return db.get_conn()


def insert_tx(
    conn,
    date="2026-08-20",
    amount=10.0,
    type="支出",
    category="餐饮",
    merchant="",
    note="",
    source="手动",
    estimated=0,
    uuid=None,
    updated_at=None,
):
    now = db.now_str()
    cur = conn.execute(
        "INSERT INTO transactions(date, amount, type, category, merchant, "
        "note, source, estimated, created_at, updated_at, uuid, device_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, amount, type, category, merchant, note, source, estimated,
         now, updated_at or now, uuid or sync.new_uuid(),
         sync.local_device_id(conn)),
    )
    return cur.lastrowid


def insert_goal(
    conn, name="新目标", price=100.0, saved=0.0, status="冷静期",
    uuid=None, updated_at=None, priority=0,
):
    now = db.now_str()
    cur = conn.execute(
        "INSERT INTO goals(name, price, saved, priority, status, "
        "cooldown_until, expected_date, note, created_at, achieved_at, "
        "uuid, device_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '', '', '', ?, '', ?, ?, ?)",
        (name, price, saved, priority, status, now, uuid or sync.new_uuid(),
         sync.local_device_id(conn), updated_at or now),
    )
    return cur.lastrowid


def tx_by_uuid(conn, uuid):
    return conn.execute(
        "SELECT * FROM transactions WHERE uuid = ?", (uuid,)).fetchone()


def goal_by_uuid(conn, uuid):
    return conn.execute("SELECT * FROM goals WHERE uuid = ?", (uuid,)).fetchone()


def touch(conn, table, uuid, updated_at):
    conn.execute(f"UPDATE {table} SET updated_at = ? WHERE uuid = ?",
                 (updated_at, uuid))


def export_package():
    return sync.export_share_package()


def import_package(path_or_bytes, decisions=None):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) \
        else Path(path_or_bytes).read_bytes()
    token = sync.stage_import_package(data)
    preview = sync.preview_import(token)
    result = sync.apply_import(token, decisions or {})
    sync.discard_import(token)
    return preview, result


# ---------- 导出结构 ----------

def test_export_package_structure_and_no_secrets(device_a):
    conn = home_conn()
    insert_tx(conn, date="2026-08-20", amount=29.9, merchant="KFC")
    insert_goal(conn, name="耳机", price=1299)
    cfg = load_config()
    cfg["api_key"] = "sk-top-secret"
    save_config(cfg)
    conn.commit()
    conn.close()

    info = export_package()
    archive = Path(info["path"])
    assert archive.exists()

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert names == {
            "manifest.json", "transactions.json", "line_items.json",
            "goals.json", "savings_wins.json", "settings_public.json",
            "tombstones.json",
        }
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format"] == "better-money-share"
        assert manifest["counts"]["transactions"] == 1
        txs = json.loads(zf.read("transactions.json"))
        assert txs[0]["amount"] == "29.90"
        assert "id" not in txs[0]
        goals = json.loads(zf.read("goals.json"))
        assert goals[0]["price"] == "1299.00"
        all_bytes = b"".join(zf.read(n) for n in names)
        assert b"sk-top-secret" not in all_bytes

    # 导出后：本机不再有「待同步变化」，且记录了导出事件
    conn = home_conn()
    status = sync.sync_status(conn)
    assert status["pending_changes"] == 0
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM sync_events ORDER BY id DESC LIMIT 1")]
    assert events and events[0]["direction"] == "export"
    conn.close()


def test_import_into_empty_device(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    insert_tx(conn, date="2026-08-19", amount=12.5, merchant="食堂")
    insert_tx(conn, date="2026-08-20", amount=50, type="收入", category="兼职")
    insert_goal(conn, name="相机", price=500, saved=120)
    conn.commit()
    conn.close()
    info = export_package()

    switch_home(device_b)
    preview, result = import_package(info["path"])
    assert preview["summary"]["add_transactions"] == 2
    assert preview["summary"]["add_goals"] == 1
    assert preview["summary"]["conflict_days"] == []
    assert result["add_transactions"] == 2 and result["add_goals"] == 1

    conn = home_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM goals WHERE deleted_at = ''"
    ).fetchone()[0] == 1
    conn.close()


def test_reimport_is_idempotent(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    insert_tx(conn, date="2026-08-19", amount=30)
    conn.commit()
    conn.close()
    info = export_package()

    switch_home(device_b)
    import_package(info["path"])
    preview2, result2 = import_package(info["path"])
    assert preview2["summary"]["add_transactions"] == 0
    assert preview2["summary"]["modify_transactions"] == 0
    assert result2["add_transactions"] == 0

    conn = home_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''"
    ).fetchone()[0] == 1
    conn.close()


def test_single_side_edit_auto_adopts(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    tx_id = insert_tx(conn, date="2026-08-19", amount=30, merchant="奶茶店")
    uuid = conn.execute(
        "SELECT uuid FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    info1 = export_package()

    switch_home(device_b)
    import_package(info1["path"])

    # 仅 A 修改
    switch_home(device_a)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 10:00:00")
    conn.execute("UPDATE transactions SET amount = 31 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    info2 = export_package()

    switch_home(device_b)
    preview, _ = import_package(info2["path"])
    assert preview["summary"]["modify_transactions"] == 1
    assert preview["summary"]["conflict_days"] == []
    conn = home_conn()
    assert tx_by_uuid(conn, uuid)["amount"] == 31
    conn.close()


def test_both_edited_same_day_conflicts_and_modes(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    tx_id = insert_tx(conn, date="2026-08-19", amount=30, merchant="奶茶店")
    uuid = conn.execute(
        "SELECT uuid FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    info1 = export_package()

    switch_home(device_b)
    import_package(info1["path"])

    # 两侧都改（同日同 uuid）
    switch_home(device_a)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 09:00:00")
    conn.execute("UPDATE transactions SET amount = 31 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    info2 = export_package()

    switch_home(device_b)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 09:30:00")
    conn.execute("UPDATE transactions SET amount = 32 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()

    preview, _ = import_package(info2["path"], decisions={
        "days": {"2026-08-19": {"mode": "keep_local"}}})
    assert [d["date"] for d in preview["summary"]["conflict_days"]] == ["2026-08-19"]
    assert preview["summary"]["conflict_days"][0]["items"][0]["kind"] == "both_changed"
    conn = home_conn()
    assert tx_by_uuid(conn, uuid)["amount"] == 32  # 保留本机
    conn.close()

    # keep_peer：需要新的双侧修改才会再次冲突
    switch_home(device_a)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 10:00:00")
    conn.execute("UPDATE transactions SET amount = 33 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    info3 = export_package()

    switch_home(device_b)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 10:30:00")
    conn.execute("UPDATE transactions SET amount = 34 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    import_package(info3["path"], decisions={
        "days": {"2026-08-19": {"mode": "keep_peer"}}})
    conn = home_conn()
    assert tx_by_uuid(conn, uuid)["amount"] == 33  # 采用导入包
    conn.close()

    # merge + drop：再次双侧修改
    switch_home(device_a)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 11:00:00")
    conn.execute("UPDATE transactions SET amount = 35 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    info4 = export_package()

    switch_home(device_b)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 11:30:00")
    conn.commit()
    conn.close()
    import_package(info4["path"], decisions={
        "days": {"2026-08-19": {
            "mode": "merge", "items": {uuid: "drop"}}}})
    conn = home_conn()
    row = tx_by_uuid(conn, uuid)
    assert row["deleted_at"]
    conn.close()


def test_delete_propagates_via_tombstone(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    tx_id = insert_tx(conn, date="2026-08-19", amount=30)
    uuid = conn.execute(
        "SELECT uuid FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    info1 = export_package()

    switch_home(device_b)
    import_package(info1["path"])

    switch_home(device_a)
    conn = home_conn()
    conn.execute(
        "UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ?",
        ("2026-08-22 11:00:00", "2026-08-22 11:00:00", tx_id))
    conn.execute(
        "INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced) "
        "VALUES (?, 'transaction', ?, ?, 0)",
        (uuid, "2026-08-22 11:00:00", sync.local_device_id(conn)))
    conn.commit()
    conn.close()
    info2 = export_package()

    switch_home(device_b)
    preview, _ = import_package(info2["path"])
    assert preview["summary"]["delete_transactions"] == 1
    conn = home_conn()
    assert tx_by_uuid(conn, uuid)["deleted_at"]
    conn.close()

    # B 侧 tombstone 同步后：再导入触发物理清理，tombstone 永久保留
    conn = home_conn()
    conn.execute("UPDATE sync_tombstones SET synced = 1")
    conn.commit()
    conn.close()
    import_package(info2["path"])
    conn = home_conn()
    assert tx_by_uuid(conn, uuid) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM sync_tombstones WHERE uuid = ?", (uuid,)
    ).fetchone()[0] == 1
    conn.close()


def test_delete_vs_modify_conflict(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    tx_id = insert_tx(conn, date="2026-08-19", amount=30)
    uuid = conn.execute(
        "SELECT uuid FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    info1 = export_package()

    switch_home(device_b)
    import_package(info1["path"])

    # A 删除；B 修改
    switch_home(device_a)
    conn = home_conn()
    conn.execute(
        "UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ?",
        ("2026-08-22 11:00:00", "2026-08-22 11:00:00", tx_id))
    conn.execute(
        "INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced) "
        "VALUES (?, 'transaction', ?, ?, 0)",
        (uuid, "2026-08-22 11:00:00", sync.local_device_id(conn)))
    conn.commit()
    conn.close()
    info2 = export_package()

    switch_home(device_b)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 11:30:00")
    conn.execute("UPDATE transactions SET amount = 35 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()

    preview, _ = import_package(info2["path"], decisions={
        "days": {"2026-08-19": {"mode": "keep_local"}}})
    item = preview["summary"]["conflict_days"][0]["items"][0]
    assert item["kind"] == "delete_vs_modify"
    conn = home_conn()
    assert tx_by_uuid(conn, uuid)["amount"] == 35  # 保留本机修改
    conn.close()

    # 采用包内删除
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 12:00:00")
    conn.commit()
    conn.close()
    import_package(info2["path"], decisions={
        "days": {"2026-08-19": {"mode": "keep_peer"}}})
    conn = home_conn()
    assert tx_by_uuid(conn, uuid)["deleted_at"]
    conn.close()


def test_resurrect_conflict_after_our_deletion(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    tx_id = insert_tx(conn, date="2026-08-19", amount=30)
    uuid = conn.execute(
        "SELECT uuid FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]
    conn.commit()
    # A 本地删除（从未同步给 B）
    conn.execute(
        "UPDATE transactions SET deleted_at = '2026-08-22 08:00:00', "
        "updated_at = '2026-08-22 08:00:00' WHERE id = ?", (tx_id,))
    conn.execute(
        "INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced) "
        "VALUES (?, 'transaction', '2026-08-22 08:00:00', ?, 0)",
        (uuid, sync.local_device_id(conn)))
    conn.commit()
    conn.close()

    # B 从未收到删除，B 自己修改了该笔并导出（用相同 uuid 模拟）
    switch_home(device_b)
    conn = home_conn()
    insert_tx(conn, date="2026-08-19", amount=33, uuid=uuid,
              updated_at="2026-08-22 09:00:00")
    conn.commit()
    conn.close()
    info = export_package()

    switch_home(device_a)
    preview, _ = import_package(info["path"], decisions={
        "days": {"2026-08-19": {"mode": "keep_local"}}})
    item = preview["summary"]["conflict_days"][0]["items"][0]
    assert item["kind"] == "delete_vs_modify"  # 本机行仍软删除存在
    conn = home_conn()
    assert tx_by_uuid(conn, uuid)["deleted_at"]  # 保持删除
    conn.close()

    # 物理清理后，对端再修改 → resurrect 冲突
    switch_home(device_a)
    conn = home_conn()
    conn.execute("UPDATE sync_tombstones SET synced = 1 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    import_package(info["path"])  # 触发清理（peer 未再变化，无冲突）
    conn = home_conn()
    assert tx_by_uuid(conn, uuid) is None
    conn.close()

    switch_home(device_b)
    conn = home_conn()
    touch(conn, "transactions", uuid, "2026-08-22 10:00:00")
    conn.execute("UPDATE transactions SET amount = 40 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    info2 = export_package()

    switch_home(device_a)
    preview2, _ = import_package(info2["path"], decisions={
        "days": {"2026-08-19": {"mode": "keep_local"}}})
    item2 = preview2["summary"]["conflict_days"][0]["items"][0]
    assert item2["kind"] == "resurrect"
    conn = home_conn()
    assert tx_by_uuid(conn, uuid) is None  # 依然保持删除
    conn.close()


def test_suspected_duplicate_default_keeps_both(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    insert_tx(conn, date="2026-08-19", amount=30, merchant="奶茶店")
    conn.commit()
    conn.close()

    switch_home(device_b)
    conn = home_conn()
    insert_tx(conn, date="2026-08-19", amount=30, merchant="奶茶店")
    conn.commit()
    conn.close()
    info = export_package()

    switch_home(device_a)
    preview, result = import_package(info["path"])
    assert preview["summary"]["dupes"]
    assert result["add_transactions"] == 1  # 默认保留两笔
    conn = home_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''"
    ).fetchone()[0] == 2
    conn.close()

    # 重复导入幂等，不再新增
    import_package(info["path"])
    conn = home_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''"
    ).fetchone()[0] == 2
    conn.close()


def test_goal_conflicts_and_required_decisions(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    goal_id = insert_goal(conn, name="耳机", price=1299, saved=0)
    uuid = conn.execute(
        "SELECT uuid FROM goals WHERE id = ?", (goal_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    info1 = export_package()

    switch_home(device_b)
    import_package(info1["path"])

    switch_home(device_a)
    conn = home_conn()
    touch(conn, "goals", uuid, "2026-08-22 09:00:00")
    conn.execute("UPDATE goals SET saved = 50 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()
    info2 = export_package()

    switch_home(device_b)
    conn = home_conn()
    touch(conn, "goals", uuid, "2026-08-22 09:30:00")
    conn.execute("UPDATE goals SET saved = 60 WHERE uuid = ?", (uuid,))
    conn.commit()
    conn.close()

    preview, _ = import_package(info2["path"])
    conflict = preview["summary"]["goal_conflicts"][0]
    assert conflict["kind"] == "both_changed"
    assert conflict["suggested"] == "local"  # B 的 updated_at 更新
    conn = home_conn()
    assert goal_by_uuid(conn, uuid)["saved"] == 60
    conn.close()

    # 删除 vs 修改：缺决策必须拒绝且回滚
    switch_home(device_a)
    conn = home_conn()
    conn.execute(
        "UPDATE goals SET deleted_at = '2026-08-22 10:00:00', "
        "updated_at = '2026-08-22 10:00:00' WHERE uuid = ?", (uuid,))
    conn.execute(
        "INSERT INTO sync_tombstones(uuid, kind, deleted_at, device_id, synced) "
        "VALUES (?, 'goal', '2026-08-22 10:00:00', ?, 0)",
        (uuid, sync.local_device_id(conn)))
    conn.commit()
    conn.close()
    info3 = export_package()

    switch_home(device_b)
    conn = home_conn()
    touch(conn, "goals", uuid, "2026-08-22 10:30:00")
    conn.commit()
    before = goal_by_uuid(conn, uuid)["saved"]
    conn.close()
    with pytest.raises(sync.ShareError):
        import_package(info3["path"], decisions={})
    conn = home_conn()
    assert goal_by_uuid(conn, uuid)["saved"] == before  # 回滚，未变
    conn.close()

    import_package(info3["path"], decisions={
        "goals": {uuid: "local"}})
    conn = home_conn()
    assert not goal_by_uuid(conn, uuid)["deleted_at"]  # 保留本机修改
    conn.close()


def test_settings_preview_and_apply(device_a, device_b):
    switch_home(device_a)
    conn = home_conn()
    cfg = load_config()
    cfg.update({"monthly_budget": 2000.0, "cooldown_days": 10})
    save_config(cfg)
    conn.commit()
    conn.close()
    info = export_package()

    switch_home(device_b)
    preview, _ = import_package(info["path"], decisions={
        "settings": "apply_package"})
    assert preview["settings"]["conflict"]
    assert load_config()["monthly_budget"] == 2000.0
    assert load_config()["cooldown_days"] == 10

    # 默认保留本机：改回去后再导一次
    cfg = load_config()
    cfg["monthly_budget"] = 3000.0
    save_config(cfg)
    import_package(info["path"])
    assert load_config()["monthly_budget"] == 3000.0


def test_bad_packages_rejected(device_a):
    conn = home_conn()
    insert_tx(conn, date="2026-08-19", amount=30)
    conn.commit()
    conn.close()
    info = export_package()
    good = Path(info["path"]).read_bytes()

    def rebuild(mutate=None):
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(out, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if mutate is not None:
                    data = mutate(item.filename, data)
                dst.writestr(item, data)
        return out.getvalue()

    def stage(data, expect_error):
        with pytest.raises(sync.ShareError) as exc:
            sync.stage_import_package(data)
        assert expect_error in str(exc.value)

    # 整库备份冒充共享包
    stage(rebuild(lambda name, data:
                  data.replace(b'"better-money-share"', b'"better-money-backup"')
                  if name == "manifest.json" else data),
          "不是 Better-money 共享包")
    # 未知成员
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("api_key.json", "{}")
    stage(out.getvalue(), "未知文件")
    # 符号链接
    out2 = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(out2, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        link = zipfile.ZipInfo("manifest.json")
        link.external_attr = 0o120000 << 16
        dst.writestr(link, "{}")
    stage(out2.getvalue(), "符号链接")
    # 缺少成员
    out3 = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(out3, "w") as dst:
        for item in src.infolist():
            if item.filename != "goals.json":
                dst.writestr(item, src.read(item.filename))
    stage(out3.getvalue(), "缺少文件")
    # uuid 重复
    out4 = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(out4, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "transactions.json":
                txs = json.loads(data)
                txs.append(dict(txs[0]))
                data = json.dumps(txs)
            dst.writestr(item, data)
    stage(out4.getvalue(), "重复 uuid")


def test_pending_changes_status(device_a):
    conn = home_conn()
    assert sync.sync_status(conn)["pending_changes"] == 0
    insert_tx(conn, date="2026-08-19", amount=30)
    conn.commit()
    assert sync.sync_status(conn)["pending_changes"] == 1
    conn.close()

    export_package()

    conn = home_conn()
    assert sync.sync_status(conn)["pending_changes"] == 0
    conn.execute(
        "UPDATE transactions SET updated_at = '2026-08-22 12:00:00' "
        "WHERE amount = 30")
    conn.commit()
    assert sync.sync_status(conn)["pending_changes"] == 1
    conn.close()


def test_share_api_endpoints(app_home):
    """API 层：导出下载、上传预览、应用、状态与记录。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        # 造一笔数据
        response = client.post("/api/transactions", json={
            "date": "2026-08-19", "amount": 66.6, "type": "支出",
            "category": "餐饮", "merchant": "API店", "note": "", "source": "手动"})
        assert response.status_code == 200

        status = client.get("/api/share/status").json()
        assert status["pending_changes"] >= 1

        # 导出
        exported = client.get("/api/share/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"
        package = exported.content

        status = client.get("/api/share/status").json()
        assert status["pending_changes"] == 0

        # 上传预览 → 应用（导入本机自己的包：无冲突、幂等）
        preview = client.post(
            "/api/share/preview",
            files={"file": ("share.zip", package, "application/zip")})
        assert preview.status_code == 200
        body = preview.json()
        assert body["summary"]["add_transactions"] == 0  # 同包重复导入幂等

        applied = client.post("/api/share/apply", json={
            "token": body["token"], "decisions": {}})
        assert applied.status_code == 200
        assert applied.json()["ok"] is True

        events = client.get("/api/share/events").json()
        assert events and events[0]["direction"] == "import"

        # 坏包 → 400 且不写库
        bad = client.post(
            "/api/share/preview",
            files={"file": ("bad.zip", b"not a zip", "application/zip")})
        assert bad.status_code == 400


def test_migration_v2_to_v3_backfills_uuids(tmp_path):
    """构造 v2 旧库（无 uuid/同步列）→ init_db 后应完整回填。"""
    home = tmp_path / "old"
    home.mkdir()
    switch_home(home)
    get_paths().data_dir.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(get_paths().db_path)
    legacy.executescript("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, amount REAL NOT NULL, type TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '其他', merchant TEXT DEFAULT '',
            note TEXT DEFAULT '', source TEXT DEFAULT '手动',
            estimated INTEGER DEFAULT 0, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL REFERENCES transactions(id),
            name TEXT NOT NULL, qty REAL DEFAULT 1, price REAL DEFAULT 0
        );
        CREATE TABLE goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, price REAL NOT NULL, saved REAL DEFAULT 0,
            priority INTEGER DEFAULT 100, status TEXT DEFAULT '冷静期',
            cooldown_until TEXT DEFAULT '', expected_date TEXT DEFAULT '',
            note TEXT DEFAULT '', created_at TEXT NOT NULL,
            achieved_at TEXT DEFAULT ''
        );
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_type TEXT NOT NULL, period_start TEXT NOT NULL,
            period_end TEXT NOT NULL, content TEXT DEFAULT '',
            image_path TEXT DEFAULT '', expired INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, diff REAL NOT NULL, note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE savings_wins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_name TEXT NOT NULL, amount REAL NOT NULL, date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE pending_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_text TEXT DEFAULT '', image_path TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
    """)
    legacy.execute(
        "INSERT INTO transactions(date, amount, type, category, created_at, updated_at) "
        "VALUES ('2026-08-01', 20, '支出', '餐饮', '2026-08-01 10:00:00', '2026-08-01 10:00:00')")
    legacy.execute("PRAGMA user_version = 2")
    legacy.commit()
    legacy.close()

    db.init_db()
    conn = home_conn()
    cfg = load_config()
    sync.ensure_device_identity(conn, cfg)
    conn.commit()
    rows = conn.execute("SELECT * FROM transactions").fetchall()
    assert len(rows) == 1
    assert sync.UUID_RE.match(rows[0]["uuid"])
    assert rows[0]["device_id"] == cfg["device_id"]
    assert rows[0]["deleted_at"] == "" and rows[0]["last_synced_at"] == ""
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    conn.close()
