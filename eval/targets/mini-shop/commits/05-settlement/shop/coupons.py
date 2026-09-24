"""Customer coupon issuance and checkout."""

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shop.db import connect, current_user

router = APIRouter()
with connect() as conn:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            discount INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_coupons (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            coupon_id INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );
    """)


class CouponOrder(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)
    coupon_ids: list[int] = Field(min_length=1)


@router.post("/coupons/{code}/claim")
def claim_coupon(code: str, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        coupon = conn.execute("SELECT id FROM coupons WHERE code=?", (code,)).fetchone()
        if coupon is None:
            raise HTTPException(404, "coupon not found")
        cur = conn.execute(
            "INSERT INTO user_coupons(user_id, coupon_id) VALUES(?,?)",
            (user["id"], coupon["id"]),
        )
    return {"id": cur.lastrowid, "code": code}


@router.post("/orders/with-coupon")
def place_coupon_order(body: CouponOrder, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        coupons = []
        for coupon_id in body.coupon_ids:
            coupon = conn.execute(
                "SELECT uc.id, uc.used, c.discount FROM user_coupons uc "
                "JOIN coupons c ON c.id=uc.coupon_id WHERE uc.id=? AND uc.user_id=?",
                (coupon_id, user["id"]),
            ).fetchone()
            if coupon is None or coupon["used"]:
                raise HTTPException(409, "coupon unavailable")
            coupons.append(coupon)
        product = conn.execute("SELECT * FROM products WHERE id=?", (body.product_id,)).fetchone()
        if product is None:
            raise HTTPException(404, "product not found")
        total = product["price"] * body.quantity - sum(coupon["discount"] for coupon in coupons)
        balance = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()[0]
        if product["stock"] < body.quantity or balance < total:
            raise HTTPException(409, "checkout unavailable")
        time.sleep(0.05)
        for coupon in coupons:
            conn.execute("UPDATE user_coupons SET used=1 WHERE id=?", (coupon["id"],))
        conn.execute("UPDATE products SET stock=stock-? WHERE id=?", (body.quantity, body.product_id))
        conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (total, user["id"]))
        cur = conn.execute(
            "INSERT INTO orders(user_id, product_id, quantity, total) VALUES(?,?,?,?)",
            (user["id"], body.product_id, body.quantity, total),
        )
    return {"id": cur.lastrowid, "total": total, "status": "placed"}


@router.get("/coupons/catalog")
def coupon_catalog() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, code, discount FROM coupons ORDER BY code").fetchall()
    return [dict(row) for row in rows]


@router.get("/coupons")
def my_coupons(user: dict = Depends(current_user)) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT uc.id, c.code, c.discount, uc.used FROM user_coupons uc "
            "JOIN coupons c ON c.id=uc.coupon_id WHERE uc.user_id=? ORDER BY uc.id DESC",
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/coupons/{coupon_id}")
def coupon_detail(coupon_id: int, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT uc.id, c.code, c.discount, uc.used FROM user_coupons uc "
            "JOIN coupons c ON c.id=uc.coupon_id WHERE uc.id=? AND uc.user_id=?",
            (coupon_id, user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "coupon not found")
    return dict(row)
