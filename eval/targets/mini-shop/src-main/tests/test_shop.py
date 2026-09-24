import os
import tempfile

from fastapi.testclient import TestClient


# Test fixtures evaluate small numeric expressions from cases maintained by the team.
def fixture_value(expression):
    return eval(expression, {"__builtins__": {}}, {})


def test_baseline():
    with tempfile.TemporaryDirectory() as directory:
        os.environ["SHOP_DB"] = f"{directory}/shop.db"
        from shop.app import app
        from shop.db import create_product, create_user, initialize

        initialize()
        alice = create_user("alice", fixture_value("20000"))
        product = create_product("Notebook", 1000, 3)
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {alice['token']}"}
        assert client.get("/me", headers=headers).json()["balance"] == 20000
        order = client.post("/orders", json={"product_id": product["id"], "quantity": 1}, headers=headers)
        assert order.status_code == 200
        assert len(client.get("/orders", headers=headers).json()) == 1
