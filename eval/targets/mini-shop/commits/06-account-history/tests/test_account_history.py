"""Account history endpoints with a standalone application."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shop.account_history import router
from shop.db import connect, create_product, create_user, initialize


@pytest.fixture
def shop_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_DB", str(tmp_path / "history.sqlite3"))
    initialize()
    alice = create_user("alice")
    bob = create_user("bob")
    product = create_product("Notebook", 1000, 10)
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE refunds (
                id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
                amount INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT UNIQUE, discount INTEGER);
            CREATE TABLE user_coupons (
                id INTEGER PRIMARY KEY, user_id INTEGER, coupon_id INTEGER,
                used INTEGER DEFAULT 0
            );
        """)
        orders = []
        for owner in (alice, alice, bob):
            orders.append(
                conn.execute(
                    "INSERT INTO orders(user_id, product_id, quantity, total) VALUES(?,?,?,?)",
                    (owner["id"], product["id"], 1, 1000),
                ).lastrowid
            )
        conn.executemany(
            "INSERT INTO refunds(order_id, amount) VALUES(?,?)",
            [(orders[0], 100), (orders[1], 200), (orders[0], 300), (orders[2], 400)],
        )
        conn.executemany(
            "INSERT INTO coupons(code, discount) VALUES(?,?)",
            [("FIRST", 100), ("SECOND", 200), ("THIRD", 300), ("BOB", 400)],
        )
        conn.executemany(
            "INSERT INTO user_coupons(user_id, coupon_id, used) VALUES(?,?,?)",
            [(alice["id"], 1, 1), (alice["id"], 2, 0), (alice["id"], 3, 1), (bob["id"], 4, 0)],
        )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), {"Authorization": f"Bearer {alice['token']}"}, orders


def test_refund_history_pages_and_order_filter(shop_client):
    client, headers, orders = shop_client
    first = client.get("/account/refunds", params={"page_size": 2}, headers=headers)
    assert first.status_code == 200
    body = first.json()
    assert [item["id"] for item in body["items"]] == [3, 2]
    assert [item["order_id"] for item in body["items"]] == [orders[0], orders[1]]
    assert [item["amount"] for item in body["items"]] == [300, 200]
    assert all(item["created_at"] for item in body["items"])
    assert (body["total"], body["page"], body["page_size"], body["has_next"]) == (3, 1, 2, True)
    last = client.get(
        "/account/refunds", params={"page": 2, "page_size": 2}, headers=headers
    ).json()
    assert [item["amount"] for item in last["items"]] == [100]
    assert (last["total"], last["page"], last["has_next"]) == (3, 2, False)
    by_order = client.get(
        "/account/refunds", params={"order_id": orders[0]}, headers=headers
    ).json()
    assert [item["amount"] for item in by_order["items"]] == [300, 100]
    assert by_order["total"] == 2


def test_coupon_history_pages_and_status(shop_client):
    client, headers, _ = shop_client
    first = client.get("/account/coupons", params={"page_size": 2}, headers=headers).json()
    assert [item["code"] for item in first["items"]] == ["THIRD", "SECOND"]
    assert (first["total"], first["page"], first["page_size"], first["has_next"]) == (3, 1, 2, True)
    last = client.get(
        "/account/coupons", params={"page": 2, "page_size": 2}, headers=headers
    ).json()
    assert [item["code"] for item in last["items"]] == ["FIRST"]
    assert last["has_next"] is False
    used = client.get("/account/coupons", params={"used": "true"}, headers=headers).json()
    assert [item["code"] for item in used["items"]] == ["THIRD", "FIRST"]
    assert used["total"] == 2
    unused = client.get("/account/coupons", params={"used": "false"}, headers=headers).json()
    assert [item["code"] for item in unused["items"]] == ["SECOND"]
    assert unused["total"] == 1


def test_history_requires_login_and_owned_order(shop_client):
    client, headers, orders = shop_client
    assert client.get("/account/refunds").status_code == 401
    assert client.get("/account/coupons").status_code == 401
    other_order = client.get("/account/refunds", params={"order_id": orders[2]}, headers=headers)
    assert other_order.status_code == 404


@pytest.mark.parametrize(
    "path,params",
    [
        ("/account/refunds", {"page_size": 0}),
        ("/account/refunds", {"page": 0}),
        ("/account/refunds", {"order_id": 0}),
        ("/account/coupons", {"page_size": 101}),
        ("/account/coupons", {"page": -1}),
        ("/account/coupons", {"used": "maybe"}),
    ],
)
def test_invalid_filters_are_rejected(shop_client, path, params):
    client, headers, _ = shop_client
    assert client.get(path, params=params, headers=headers).status_code == 422
