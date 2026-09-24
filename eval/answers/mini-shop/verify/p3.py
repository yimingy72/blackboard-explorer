from concurrent.futures import ThreadPoolExecutor

from common import post, shop


def verify():
    success = 0
    with shop() as (client, users):
        post(client, users, "alice", "/balance/charge", {"amount": 1_000_000})
        for _ in range(20):
            coupon = post(client, users, "alice", "/coupons/SAVE60A/claim", {}).json()
            body = {"product_id": 1, "quantity": 1, "coupon_ids": [coupon["id"]]}
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: post(client, users, "alice", "/orders/with-coupon", body), range(2)
                ))
            success += all(result.status_code == 200 for result in results)
        assert success >= 18, f"duplicate coupon use: {success}/20"
        return f"duplicate coupon use: {success}/20"


if __name__ == "__main__":
    print(verify())
