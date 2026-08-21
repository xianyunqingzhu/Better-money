"""Goal allocation service."""
from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class GoalAllocation:
    goal_id: int
    goal_name: str
    amount: float


def allocate_savings(
    conn: sqlite3.Connection, amount: float
) -> list[GoalAllocation]:
    remaining = round(max(float(amount), 0.0), 2)
    if remaining <= 0:
        return []

    allocations: list[GoalAllocation] = []
    rows = conn.execute(
        "SELECT id, name, price, saved FROM goals "
        "WHERE status IN ('冷静期','进行中') AND saved < price "
        "ORDER BY priority, id"
    ).fetchall()
    for goal_id, goal_name, price, saved in rows:
        if remaining <= 0:
            break
        capacity = round(max(float(price) - float(saved), 0.0), 2)
        assigned = round(min(remaining, capacity), 2)
        if assigned <= 0:
            continue
        conn.execute(
            "UPDATE goals SET saved = saved + ? WHERE id = ?",
            (assigned, goal_id),
        )
        allocations.append(GoalAllocation(goal_id, goal_name, assigned))
        remaining = round(remaining - assigned, 2)
    return allocations
