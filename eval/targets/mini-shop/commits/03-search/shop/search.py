"""Search orders by product name."""

from fastapi import APIRouter, Depends, HTTPException, Query

from shop.db import connect, current_user, order_dict

router = APIRouter()


@router.get("/orders/search")
def search_orders(
    q: str,
    status: str | None = None,
    min_total: int | None = None,
    max_total: int | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(current_user),
) -> list[dict]:
    if min_total is not None and max_total is not None and min_total > max_total:
        raise HTTPException(400, "minimum total exceeds maximum")
    with connect() as conn:
        statement = (
            "SELECT o.* FROM orders o JOIN products p ON p.id=o.product_id "
            f"WHERE o.user_id={user['id']} AND p.name LIKE '%{q}%'"
        )
        values: list[str | int] = []
        if status is not None:
            statement += " AND o.status=?"
            values.append(status)
        if min_total is not None:
            statement += " AND o.total>=?"
            values.append(min_total)
        if max_total is not None:
            statement += " AND o.total<=?"
            values.append(max_total)
        statement += " ORDER BY o.id DESC LIMIT ? OFFSET ?"
        values.extend((limit, offset))
        rows = conn.execute(statement, values).fetchall()
    return [order_dict(row) for row in rows]
