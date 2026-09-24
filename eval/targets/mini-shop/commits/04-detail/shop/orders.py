"""Order routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shop.db import connect, current_user, order_dict

router = APIRouter()


class OrderRequest(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


@router.post("/orders")
def place_order(body: OrderRequest, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        product = conn.execute("SELECT * FROM products WHERE id=?", (body.product_id,)).fetchone()
        if product is None:
            raise HTTPException(404, "product not found")
        total = product["price"] * body.quantity
        if product["stock"] < body.quantity:
            raise HTTPException(409, "insufficient stock")
        balance = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()[0]
        if balance < total:
            raise HTTPException(409, "insufficient balance")
        conn.execute("UPDATE products SET stock=stock-? WHERE id=?", (body.quantity, body.product_id))
        conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (total, user["id"]))
        cur = conn.execute(
            "INSERT INTO orders(user_id, product_id, quantity, total) VALUES(?,?,?,?)",
            (user["id"], body.product_id, body.quantity, total),
        )
        conn.commit()
        return {"id": cur.lastrowid, "total": total, "status": "placed"}


@router.get("/orders")
def list_orders(sort: str = "newest", user: dict = Depends(current_user)) -> list[dict]:
    columns = {"newest": "created_at DESC, id DESC", "oldest": "created_at ASC, id ASC", "total": "total DESC"}
    if sort not in columns:
        raise HTTPException(400, "unknown sort order")
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM orders WHERE user_id=? ORDER BY {columns[sort]}", (user["id"],)
        ).fetchall()
    return [order_dict(row) for row in rows]
