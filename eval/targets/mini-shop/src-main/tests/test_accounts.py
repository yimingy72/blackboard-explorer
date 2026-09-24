"""Profile and delivery address behavior through the shop application."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from shop.accounts import initialize_accounts
from shop.db import connect, create_user, initialize


@pytest.fixture
def shop(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_DB", str(tmp_path / "shop.sqlite3"))
    from shop.app import app

    initialize()
    initialize_accounts()
    alice = create_user("alice", balance=3000)
    bob = create_user("bob", balance=7000)
    with TestClient(app) as client:
        yield (
            client,
            {"Authorization": f"Bearer {alice['token']}"},
            {"Authorization": f"Bearer {bob['token']}"},
        )


def address(recipient="Ada", street="1 Main Street", **other):
    return {
        "recipient": recipient,
        "phone": "+1 202 555 0188",
        "region": "North",
        "city": "River City",
        "street": street,
        "postal_code": "10001",
        "label": "Home",
        **other,
    }


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/profile", None),
        ("PATCH", "/profile", {"display_name": "Someone"}),
        ("GET", "/addresses", None),
        ("POST", "/addresses", address()),
        ("GET", "/addresses/1", None),
        ("PUT", "/addresses/1", address()),
        ("DELETE", "/addresses/1", None),
        ("POST", "/addresses/1/default", None),
    ],
)
def test_account_routes_require_login(shop, method, path, body):
    client, _, _ = shop
    response = client.request(method, path, json=body)
    assert response.status_code == 401


def test_profile_defaults_partial_updates_and_clear(shop):
    client, alice, _ = shop
    original = client.get("/profile", headers=alice)
    assert original.status_code == 200
    assert original.json() == {
        "user_id": 1,
        "username": "alice",
        "display_name": "alice",
        "email": None,
        "phone": None,
        "updated_at": None,
    }
    updated = client.patch(
        "/profile",
        json={"display_name": "Ada Lovelace", "email": "ada@example.test"},
        headers=alice,
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Ada Lovelace"
    assert updated.json()["email"] == "ada@example.test"
    assert updated.json()["updated_at"]
    phone = client.patch("/profile", json={"phone": "+1 202 555 0101"}, headers=alice)
    assert phone.status_code == 200
    assert phone.json()["display_name"] == "Ada Lovelace"
    assert phone.json()["email"] == "ada@example.test"
    cleared = client.patch("/profile", json={"email": None}, headers=alice)
    assert cleared.json()["email"] is None
    assert cleared.json()["phone"] == "+1 202 555 0101"
    assert client.get("/me", headers=alice).json() == {"id": 1, "name": "alice", "balance": 3000}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"display_name": None},
        {"display_name": ""},
        {"email": "not-an-email"},
        {"phone": "letters-only"},
        {"phone": "1"},
    ],
)
def test_profile_rejects_invalid_fields_without_changing_saved_data(shop, body):
    client, alice, _ = shop
    assert (
        client.patch("/profile", json={"display_name": "Valid"}, headers=alice).status_code == 200
    )
    assert client.patch("/profile", json=body, headers=alice).status_code == 422
    assert client.get("/profile", headers=alice).json()["display_name"] == "Valid"


def test_profile_is_private_to_its_owner(shop):
    client, alice, bob = shop
    assert (
        client.patch(
            "/profile", json={"display_name": "Alice", "email": "alice@example.test"}, headers=alice
        ).status_code
        == 200
    )
    assert client.get("/profile", headers=bob).json()["display_name"] == "bob"
    assert client.patch("/profile", json={"display_name": "Bob"}, headers=bob).status_code == 200
    assert client.get("/profile", headers=alice).json()["email"] == "alice@example.test"


def test_first_address_is_default_and_list_is_ordered(shop):
    client, alice, _ = shop
    first = client.post("/addresses", json=address(), headers=alice)
    assert first.status_code == 201
    assert first.json()["is_default"] is True
    second = client.post("/addresses", json=address("Nora", "2 Oak Road"), headers=alice)
    assert second.status_code == 201
    assert second.json()["is_default"] is False
    listed = client.get("/addresses", headers=alice)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [first.json()["id"], second.json()["id"]]
    assert (
        client.get(f"/addresses/{second.json()['id']}", headers=alice).json()["street"]
        == "2 Oak Road"
    )


def test_update_preserves_default_and_rejects_invalid_address(shop):
    client, alice, _ = shop
    created = client.post("/addresses", json=address(), headers=alice).json()
    changed = client.put(
        f"/addresses/{created['id']}",
        json=address("Nora", "8 Pine Street", label="Office"),
        headers=alice,
    )
    assert changed.status_code == 200
    assert changed.json()["recipient"] == "Nora"
    assert changed.json()["street"] == "8 Pine Street"
    assert changed.json()["label"] == "Office"
    assert changed.json()["is_default"] is True
    invalid = client.put(f"/addresses/{created['id']}", json=address(phone="abc"), headers=alice)
    assert invalid.status_code == 422
    assert (
        client.get(f"/addresses/{created['id']}", headers=alice).json()["street"] == "8 Pine Street"
    )


def test_switch_default_and_promote_successor_on_delete(shop):
    client, alice, _ = shop
    first = client.post("/addresses", json=address("One"), headers=alice).json()["id"]
    second = client.post("/addresses", json=address("Two"), headers=alice).json()["id"]
    third = client.post("/addresses", json=address("Three"), headers=alice).json()["id"]
    chosen = client.post(f"/addresses/{third}/default", headers=alice)
    assert chosen.status_code == 200
    assert chosen.json()["is_default"] is True
    listed = client.get("/addresses", headers=alice).json()
    assert [item["id"] for item in listed] == [third, first, second]
    assert sum(item["is_default"] for item in listed) == 1
    assert client.delete(f"/addresses/{third}", headers=alice).status_code == 204
    listed = client.get("/addresses", headers=alice).json()
    assert [item["id"] for item in listed] == [first, second]
    assert listed[0]["is_default"] is True
    assert client.delete(f"/addresses/{first}", headers=alice).status_code == 204
    assert client.get("/addresses", headers=alice).json()[0]["id"] == second
    assert client.delete(f"/addresses/{second}", headers=alice).status_code == 204
    assert client.get("/addresses", headers=alice).json() == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "POST"])
def test_other_users_address_cannot_be_accessed_or_changed(shop, method):
    client, alice, bob = shop
    own = client.post("/addresses", json=address("Alice"), headers=alice).json()["id"]
    path = f"/addresses/{own}/default" if method == "POST" else f"/addresses/{own}"
    response = client.request(
        method, path, json=address("Bob") if method == "PUT" else None, headers=bob
    )
    assert response.status_code == 404
    assert client.get("/addresses", headers=bob).json() == []
    assert client.get(f"/addresses/{own}", headers=alice).json()["recipient"] == "Alice"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM addresses WHERE user_id=1").fetchone()[0] == 1


def test_missing_address_operations_return_not_found(shop):
    client, alice, _ = shop
    assert client.get("/addresses/999", headers=alice).status_code == 404
    assert client.put("/addresses/999", json=address(), headers=alice).status_code == 404
    assert client.delete("/addresses/999", headers=alice).status_code == 404
    assert client.post("/addresses/999/default", headers=alice).status_code == 404


def test_parallel_creates_and_default_changes_keep_one_default(shop):
    client, alice, _ = shop

    def create(index):
        return client.post(
            "/addresses",
            json=address(f"Recipient {index}", f"{index} Main Street"),
            headers=alice,
        )

    with ThreadPoolExecutor(max_workers=6) as workers:
        created = list(workers.map(create, range(12)))
    assert all(response.status_code == 201 for response in created)
    ids = [response.json()["id"] for response in created]
    assert len(set(ids)) == 12
    assert sum(item["is_default"] for item in client.get("/addresses", headers=alice).json()) == 1

    def choose(address_id):
        return client.post(f"/addresses/{address_id}/default", headers=alice)

    with ThreadPoolExecutor(max_workers=6) as workers:
        changed = list(workers.map(choose, ids))
    assert all(response.status_code == 200 for response in changed)
    listed = client.get("/addresses", headers=alice).json()
    assert len(listed) == 12
    assert sum(item["is_default"] for item in listed) == 1
