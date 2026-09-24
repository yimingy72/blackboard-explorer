"""Purchase history, reporting periods and customer boundaries."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shop.activity import router
from shop.db import connect, create_product, create_user, initialize


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_DB", str(tmp_path / "activity.db"))
    initialize()
    alice = create_user("alice")
    bob = create_user("bob")
    book = create_product("Notebook", 1000, 10)
    pen = create_product("Pen", 250, 20)
    with connect() as conn:
        for owner, product, quantity, amount, created in [
            (alice, book, 2, 2000, "2026-01-31 23:59:00"),
            (alice, pen, 4, 1000, "2026-02-01 00:00:00"),
            (alice, book, 1, 1000, "2026-02-01 00:00:00"),
            (bob, pen, 1, 250, "2026-02-01 00:00:00"),
        ]:
            conn.execute(
                "INSERT INTO orders(user_id,product_id,quantity,total,created_at) "
                "VALUES(?,?,?,?,?)",
                (owner["id"], product["id"], quantity, amount, created),
            )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, {"Authorization": f"Bearer {alice['token']}"}, book, pen


def test_history_pagination_and_dates(history):
    client, headers, book, _ = history
    page = client.get("/account/orders/history?page_size=1", headers=headers).json()
    assert page["total"] == 3
    assert page["has_next"] is True
    assert page["items"][0]["id"] == 3
    last = client.get("/account/orders/history?page=3&page_size=1", headers=headers).json()
    assert last["items"][0]["id"] == 1
    assert last["has_next"] is False
    january = client.get(
        "/account/orders/history?start=2026-01-01&end=2026-01-31", headers=headers
    ).json()
    assert [item["id"] for item in january["items"]] == [1]
    same_day = client.get(
        "/account/orders/history?start=2026-02-01&end=2026-02-01", headers=headers
    ).json()
    assert same_day["total"] == 2
    filtered = client.get(
        f"/account/orders/history?product_id={book['id']}&status=placed", headers=headers
    ).json()
    assert filtered["total"] == 2


def test_summaries_do_not_include_other_customers(history):
    client, headers, _, _ = history
    summary = client.get("/account/orders/summary", headers=headers).json()
    assert summary["order_count"] == 3
    assert summary["units"] == 7
    assert summary["ordered_amount"] == 4000
    assert summary["largest_order"] == 2000
    assert summary["by_status"] == [{"status": "placed", "order_count": 3, "ordered_amount": 4000}]
    monthly = client.get("/account/orders/monthly?year=2026", headers=headers).json()["months"]
    assert len(monthly) == 12
    assert monthly[0]["ordered_amount"] == 2000
    assert monthly[1]["order_count"] == 2
    assert monthly[2] == {"month": 3, "order_count": 0, "ordered_amount": 0, "units": 0}
    empty = client.get("/account/orders/summary?start=2027-01-01", headers=headers).json()
    assert empty["ordered_amount"] == 0
    assert empty["by_status"] == []


def test_purchased_products_include_current_catalog_values(history):
    client, headers, book, _ = history
    with connect() as conn:
        conn.execute("UPDATE products SET stock=0, price=1500 WHERE id=?", (book["id"],))
    listing = client.get("/account/products/purchased", headers=headers).json()
    assert listing["total"] == 2
    row = next(item for item in listing["items"] if item["id"] == book["id"])
    assert (row["stock"], row["price"], row["ordered_units"]) == (0, 1500, 3)


@pytest.mark.parametrize(
    "path",
    [
        "/account/orders/history?page=0",
        "/account/orders/history?page_size=101",
        "/account/orders/history?product_id=-1",
        "/account/orders/history?status=unknown",
        "/account/orders/summary?start=2026-03-01&end=2026-01-01",
        "/account/orders/summary?start=not-a-date",
        "/account/orders/monthly?year=9999",
        "/account/products/purchased?page_size=0",
    ],
)
def test_query_validation(history, path):
    client, headers, _, _ = history
    assert client.get(path, headers=headers).status_code == 422


def test_history_requires_login(history):
    client, _, _, _ = history
    assert client.get("/account/orders/history").status_code == 401
