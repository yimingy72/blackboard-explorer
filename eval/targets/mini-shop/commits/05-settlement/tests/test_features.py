"""Ordinary checkout, search, and account flows."""

import os
import tempfile

from fastapi.testclient import TestClient


def test_coupon_checkout_and_refund():
    with tempfile.TemporaryDirectory() as directory:
        os.environ["SHOP_DB"] = f"{directory}/shop.db"
        from shop.app import app
        from shop.db import connect, create_product, create_user, initialize

        initialize()
        with connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS coupons (id INTEGER PRIMARY KEY, code TEXT UNIQUE, discount INTEGER);
                CREATE TABLE IF NOT EXISTS user_coupons (id INTEGER PRIMARY KEY, user_id INTEGER, coupon_id INTEGER, used INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS refunds (id INTEGER PRIMARY KEY, order_id INTEGER, amount INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            """)
            conn.execute("INSERT INTO coupons(code, discount) VALUES('WELCOME', 1000)")
        user = create_user("alice", 20_000, "alice-password")
        product = create_product("Notebook", 10_000, 10)
        headers = {"Authorization": f"Bearer {user['token']}"}
        client = TestClient(app)
        login = client.post("/login", json={"name": "alice", "password": "alice-password"})
        assert login.status_code == 200
        assert login.json()["token"] == user["token"]
        coupon = client.post("/coupons/WELCOME/claim", headers=headers).json()
        response = client.post(
            "/orders/with-coupon",
            headers=headers,
            json={"product_id": product["id"], "quantity": 1, "coupon_ids": [coupon["id"]]},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 9000
        order_id = response.json()["id"]
        assert client.get(f"/orders/{order_id}", headers=headers).status_code == 200
        assert len(client.get("/orders/search", params={"q": "Note"}, headers=headers).json()) == 1
        assert len(client.get("/orders", params={"sort": "total"}, headers=headers).json()) == 1
        refunded = client.post(f"/orders/{order_id}/refund", headers=headers, json={"amount": 9000})
        assert refunded.status_code == 200
        assert client.get("/me", headers=headers).json()["balance"] == 20_000


def test_sort_rejects_unknown_value():
    with tempfile.TemporaryDirectory() as directory:
        os.environ["SHOP_DB"] = f"{directory}/shop.db"
        from shop.app import app
        from shop.db import create_user, initialize

        initialize()
        user = create_user("alice", 1000, "alice-password")
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {user['token']}"}
        assert client.get("/orders", params={"sort": "unsupported"}, headers=headers).status_code == 400
