from common import balance, post, shop


def verify():
    with shop() as (client, users):
        order = post(client, users, "alice", "/orders", {"product_id": 1, "quantity": 1}).json()
        before = balance(client, users, "alice")
        response = post(client, users, "alice", f"/orders/{order['id']}/refund", {"amount": -100})
        after = balance(client, users, "alice")
        assert response.status_code == 200 and after == before - 100
        return f"refund -100: {response.status_code}, balance {before}->{after}"


if __name__ == "__main__":
    print(verify())
