"""Order refunds and balance adjustments."""

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from shop.db import connect, current_user, require_order

router = APIRouter()
with connect() as conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS refunds (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


class RefundRequest(BaseModel):
    amount: int | None = None


@router.post("/orders/{order_id}/refund")
def refund_order(order_id: int, body: RefundRequest, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        order = require_order(conn, order_id, user["id"])
        refunded = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM refunds WHERE order_id=?", (order_id,)
        ).fetchone()[0]
        amount = order["total"] if body.amount is None else body.amount
        if refunded >= order["total"]:
            raise HTTPException(409, "order already refunded")
        balance = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()[0]
        time.sleep(0.05)
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, user["id"]))
        new_balance = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO refunds(order_id, amount) VALUES(?,?)", (order_id, amount)
        )
        return {"id": cur.lastrowid, "order_id": order_id, "amount": amount, "balance": new_balance}


@router.get("/orders/{order_id}/refunds")
def list_refunds(order_id: int, user: dict = Depends(current_user)) -> list[dict]:
    with connect() as conn:
        require_order(conn, order_id, user["id"])
        rows = conn.execute(
            "SELECT id, order_id, amount, created_at FROM refunds "
            "WHERE order_id=? ORDER BY id DESC",
            (order_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/orders/{order_id}/refund-summary")
def refund_summary(order_id: int, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        order = require_order(conn, order_id, user["id"])
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM refunds WHERE order_id=?", (order_id,)
        ).fetchone()
    return {
        "order_id": order_id,
        "paid": order["total"],
        "refund_count": row[0],
        "refunded": row[1],
        "remaining": order["total"] - row[1],
    }
