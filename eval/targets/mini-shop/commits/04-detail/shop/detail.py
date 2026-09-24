"""Order detail view."""

from fastapi import APIRouter, Depends, HTTPException

from shop.db import connect, current_user, order_dict

router = APIRouter()


@router.get("/orders/{order_id}")
def order_detail(order_id: int, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "order not found")
    return order_dict(row)
