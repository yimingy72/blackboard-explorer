from common import auth, post, shop


def verify():
    with shop() as (client, users):
        order = post(client, users, "alice", "/orders", {"product_id": 1, "quantity": 1}).json()
        response = client.get(f"/orders/{order['id']}", headers=auth(users, "bob"))
        assert response.status_code == 200
        assert response.json()["user_id"] != client.get("/me", headers=auth(users, "bob")).json()["id"]
        return f"bob GET alice order: {response.status_code}, order_id={order['id']}"


if __name__ == "__main__":
    print(verify())
