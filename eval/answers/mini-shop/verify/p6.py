from common import balance, post, shop


def verify():
    with shop() as (client, users):
        coupons = [post(client, users, "alice", f"/coupons/{code}/claim", {}).json()["id"]
                   for code in ("SAVE60A", "SAVE60B")]
        before = balance(client, users, "alice")
        response = post(client, users, "alice", "/orders/with-coupon", {
            "product_id": 1, "quantity": 1, "coupon_ids": coupons,
        })
        after = balance(client, users, "alice")
        assert response.status_code == 200 and response.json()["total"] == -2000 and after == before + 2000
        return f"stacked checkout: total={response.json()['total']}, balance {before}->{after}"


if __name__ == "__main__":
    print(verify())
