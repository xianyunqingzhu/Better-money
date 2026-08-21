import pytest

from app import db
from app.config import load_config, save_config
from app.goals import allocate_savings


def insert_goal(
    conn,
    name,
    *,
    price,
    saved,
    priority,
    status,
):
    cursor = conn.execute(
        "INSERT INTO goals(name, price, saved, priority, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'now')",
        (name, price, saved, priority, status),
    )
    conn.commit()
    return cursor.lastrowid


def goal_saved(conn, goal_id):
    return conn.execute(
        "SELECT saved FROM goals WHERE id = ?", (goal_id,)
    ).fetchone()[0]


def test_allocate_savings_fills_eligible_goals_in_priority_order(conn):
    first = insert_goal(
        conn, "相机", price=100, saved=90, priority=0, status="进行中"
    )
    second = insert_goal(
        conn, "旅行", price=200, saved=20, priority=1, status="冷静期"
    )
    paused = insert_goal(
        conn, "电脑", price=500, saved=10, priority=2, status="已暂停"
    )
    completed = insert_goal(
        conn, "耳机", price=300, saved=10, priority=3, status="已达成"
    )

    allocations = allocate_savings(conn, 50)

    assert [(item.goal_id, item.goal_name, item.amount) for item in allocations] == [
        (first, "相机", 10.0),
        (second, "旅行", 40.0),
    ]
    assert goal_saved(conn, first) == 100
    assert goal_saved(conn, second) == 60
    assert goal_saved(conn, paused) == 10
    assert goal_saved(conn, completed) == 10


def test_allocate_savings_uses_id_as_tiebreaker_and_keeps_cents(conn):
    first = insert_goal(
        conn, "第一分", price=0.01, saved=0, priority=4, status="冷静期"
    )
    second = insert_goal(
        conn, "第二分", price=1, saved=0, priority=4, status="进行中"
    )

    allocations = allocate_savings(conn, 0.03)

    assert [(item.goal_id, item.amount) for item in allocations] == [
        (first, 0.01),
        (second, 0.02),
    ]
    assert goal_saved(conn, first) == pytest.approx(0.01)
    assert goal_saved(conn, second) == pytest.approx(0.02)


def test_allocate_savings_ignores_full_and_overfilled_goals(conn):
    full = insert_goal(
        conn, "刚好存满", price=100, saved=100, priority=0, status="进行中"
    )
    overfilled = insert_goal(
        conn, "已经超额", price=100, saved=120, priority=1, status="冷静期"
    )

    assert allocate_savings(conn, 50) == []
    assert goal_saved(conn, full) == 100
    assert goal_saved(conn, overfilled) == 120


def test_allocate_savings_with_no_goals_is_empty(conn):
    assert allocate_savings(conn, 50) == []


@pytest.mark.parametrize("amount", [0, -0.01, -100])
def test_allocate_savings_with_nonpositive_amount_is_a_noop(conn, amount):
    goal_id = insert_goal(
        conn, "书", price=50, saved=5, priority=0, status="进行中"
    )

    assert allocate_savings(conn, amount) == []
    assert goal_saved(conn, goal_id) == 5


def test_allocate_savings_does_not_commit_the_callers_transaction(conn):
    goal_id = insert_goal(
        conn, "键盘", price=100, saved=25, priority=0, status="进行中"
    )

    allocations = allocate_savings(conn, 10)
    assert [(item.goal_id, item.amount) for item in allocations] == [(goal_id, 10.0)]
    assert goal_saved(conn, goal_id) == 35

    conn.rollback()

    assert goal_saved(conn, goal_id) == 25


def test_create_income_returns_allocations_without_second_ledger_deduction(client):
    cfg = load_config()
    cfg.update({"initial_balance": 100.0, "auto_save_ratio": 0.333})
    save_config(cfg)

    conn = db.get_conn()
    first = insert_goal(
        conn, "短目标", price=1, saved=0, priority=0, status="进行中"
    )
    second = insert_goal(
        conn, "长目标", price=10, saved=0, priority=1, status="冷静期"
    )
    conn.close()

    response = client.post(
        "/api/transactions",
        json={
            "date": "2026-08-21",
            "amount": 10,
            "type": "收入",
            "category": "兼职",
            "merchant": "",
            "note": "",
            "source": "手动",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"ok", "id", "savings_allocations"}
    assert payload["ok"] is True
    assert payload["savings_allocations"] == [
        {"goal_id": first, "goal_name": "短目标", "amount": 1.0},
        {"goal_id": second, "goal_name": "长目标", "amount": 2.33},
    ]
    assert client.get("/api/summary").json()["balance"] == 110.0

    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert goal_saved(conn, first) == 1
    assert goal_saved(conn, second) == pytest.approx(2.33)
    conn.close()


def test_create_expense_keeps_existing_fields_and_returns_empty_allocations(client):
    response = client.post(
        "/api/transactions",
        json={
            "date": "2026-08-21",
            "amount": 12.5,
            "type": "支出",
            "category": "餐饮",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert isinstance(payload["id"], int)
    assert payload["savings_allocations"] == []


def test_delete_goal_returns_deleted_values_and_preserves_transactions(client):
    conn = db.get_conn()
    goal_id = insert_goal(
        conn, "相机", price=100, saved=42.5, priority=0, status="进行中"
    )
    cursor = conn.execute(
        "INSERT INTO transactions"
        "(date, amount, type, category, merchant, note, source, created_at, updated_at) "
        "VALUES ('2026-08-20', 88, '支出', '购物', '书店', '', '手动', 'now', 'now')"
    )
    transaction_id = cursor.lastrowid
    conn.commit()
    conn.close()

    response = client.delete(f"/api/goals/{goal_id}")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "name": "相机",
        "saved": 42.5,
    }
    conn = db.get_conn()
    assert conn.execute(
        "SELECT id FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()[0] == transaction_id
    assert conn.execute(
        "SELECT id FROM goals WHERE id = ?", (goal_id,)
    ).fetchone() is None
    conn.close()


def test_delete_missing_goal_returns_exact_not_found_contract(client):
    response = client.delete("/api/goals/999999")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "message": "目标不存在"}
