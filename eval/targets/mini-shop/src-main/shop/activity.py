"""Customer purchase history and spending summaries."""

from contextlib import closing
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from shop.db import connect, current_user

router = APIRouter(prefix="/account")


def date_range(start: date | None, end: date | None) -> tuple[str, list[str]]:
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "start must not be after end")
    clauses = []
    values = []
    if start is not None:
        clauses.append("date(o.created_at)>=?")
        values.append(start.isoformat())
    if end is not None:
        clauses.append("date(o.created_at)<=?")
        values.append(end.isoformat())
    return (" AND " + " AND ".join(clauses) if clauses else "", values)


@router.get("/orders/history")
def purchase_history(
    start: date | None = None,
    end: date | None = None,
    product_id: int | None = Query(default=None, gt=0),
    status: Literal["placed", "cancelled", "shipped", "delivered"] | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(current_user),
) -> dict:
    clause, dates = date_range(start, end)
    values = [user["id"], *dates]
    if product_id is not None:
        clause += " AND o.product_id=?"
        values.append(product_id)
    if status is not None:
        clause += " AND o.status=?"
        values.append(status)
    with closing(connect()) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM orders o WHERE o.user_id=?" + clause, values
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT o.id, o.product_id, p.name AS product_name, o.quantity, "
            "o.total, o.status, o.created_at FROM orders o "
            "JOIN products p ON p.id=o.product_id WHERE o.user_id=?"
            + clause
            + " ORDER BY o.created_at DESC, o.id DESC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": count,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < count,
    }


@router.get("/orders/summary")
def purchase_summary(
    start: date | None = None,
    end: date | None = None,
    user: dict = Depends(current_user),
) -> dict:
    clause, dates = date_range(start, end)
    values = [user["id"], *dates]
    with closing(connect()) as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS order_count, COALESCE(SUM(o.quantity),0) AS units, "
            "COALESCE(SUM(o.total),0) AS ordered_amount, "
            "COALESCE(MAX(o.total),0) AS largest_order "
            "FROM orders o WHERE o.user_id=?" + clause,
            values,
        ).fetchone()
        statuses = conn.execute(
            "SELECT o.status, COUNT(*) AS order_count, SUM(o.total) AS ordered_amount "
            "FROM orders o WHERE o.user_id=?" + clause + " GROUP BY o.status ORDER BY o.status",
            values,
        ).fetchall()
    return {
        **dict(totals),
        "by_status": [dict(row) for row in statuses],
        "start": start,
        "end": end,
    }


@router.get("/orders/monthly")
def monthly_purchases(
    year: int = Query(ge=1970, le=9998),
    user: dict = Depends(current_user),
) -> dict:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT strftime('%m',created_at) AS month, COUNT(*) AS order_count, "
            "SUM(total) AS ordered_amount, SUM(quantity) AS units "
            "FROM orders WHERE user_id=? AND created_at>=? AND created_at<? "
            "GROUP BY month ORDER BY month",
            (user["id"], f"{year:04d}-01-01", f"{year + 1:04d}-01-01"),
        ).fetchall()
    by_month = {int(row["month"]): dict(row) for row in rows}
    items = []
    for month in range(1, 13):
        row = by_month.get(month, {"order_count": 0, "ordered_amount": 0, "units": 0})
        items.append({**row, "month": month})
    return {"year": year, "months": items}


@router.get("/products/purchased")
def purchased_products(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(current_user),
) -> dict:
    with closing(connect()) as conn:
        total = conn.execute(
            "SELECT COUNT(DISTINCT product_id) FROM orders WHERE user_id=?", (user["id"],)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, COUNT(o.id) AS order_count, "
            "SUM(o.quantity) AS ordered_units, MAX(o.created_at) AS last_ordered_at "
            "FROM orders o JOIN products p ON p.id=o.product_id WHERE o.user_id=? "
            "GROUP BY p.id ORDER BY last_ordered_at DESC, p.id DESC LIMIT ? OFFSET ?",
            (user["id"], page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < total,
    }
