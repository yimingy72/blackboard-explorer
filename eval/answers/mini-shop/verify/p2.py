from concurrent.futures import ThreadPoolExecutor

from common import balance, post, shop


def verify():
    success = 0
    with shop() as (client, users):
        for _ in range(20):
            order = post(client, users, "alice", "/orders", {"product_id": 1, "quantity": 1}).json()
            before = balance(client, users, "alice")
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: post(client, users, "alice", f"/orders/{order['id']}/refund", {}), range(2)
                ))
            after = balance(client, users, "alice")
            success += all(result.status_code == 200 for result in results) and after - before == 20_000
        assert success >= 18, f"double credits: {success}/20"
        return f"double credits: {success}/20"


if __name__ == "__main__":
    print(verify())
