"""Read-only account history for refunds and issued coupons."""

from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from shop.db import connect, current_user, require_order

router = APIRouter(prefix="/account")


@router.get("/refunds")
def refund_history(
    user: Annotated[dict, Depends(current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    order_id: Annotated[int | None, Query(gt=0)] = None,
) -> dict:
    with closing(connect()) as conn:
        if order_id is not None:
            require_order(conn, order_id, user["id"])
        where = "o.user_id=?"
        values: list[int] = [user["id"]]
        if order_id is not None:
            where += " AND o.id=?"
            values.append(order_id)
        source = " FROM refunds r JOIN orders o ON o.id=r.order_id WHERE " + where
        total = conn.execute("SELECT COUNT(*)" + source, values).fetchone()[0]
        rows = conn.execute(
            "SELECT r.id, r.order_id, r.amount, r.created_at"
            + source
            + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < total,
    }


@router.get("/coupons")
def coupon_history(
    user: Annotated[dict, Depends(current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    used: bool | None = None,
) -> dict:
    where = "uc.user_id=?"
    values: list[int] = [user["id"]]
    if used is not None:
        where += " AND uc.used=?"
        values.append(int(used))
    source = " FROM user_coupons uc JOIN coupons c ON c.id=uc.coupon_id WHERE " + where
    with closing(connect()) as conn:
        total = conn.execute("SELECT COUNT(*)" + source, values).fetchone()[0]
        rows = conn.execute(
            "SELECT uc.id, c.code, c.discount, uc.used"
            + source
            + " ORDER BY uc.id DESC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < total,
    }
