"""Category browsing and inventory queries over the ordinary product table."""

from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shop.catalog import (
    assign_product_category,
    create_category,
    initialize_catalog,
    router,
    set_category_active,
)
from shop.db import connect, create_product, initialize


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_DB", str(tmp_path / "shop.db"))
    initialize()
    initialize_catalog()
    initialize_catalog()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_local_category_management_is_idempotent_and_checks_links(client):
    stationery = create_category("Stationery", "stationery", sort_order=2)
    pencils = create_category("Pencils", "pencils", parent_id=stationery["id"])
    notebook = create_product("Notebook", 1000, 4)
    assign_product_category(notebook["id"], stationery["id"])
    assign_product_category(notebook["id"], stationery["id"])
    with closing(connect()) as conn:
        count = conn.execute("SELECT COUNT(*) FROM product_categories").fetchone()[0]
    assert count == 1
    assert pencils["parent_id"] == stationery["id"]
    with pytest.raises(ValueError, match="slug"):
        create_category("Duplicate", "stationery")
    with pytest.raises(ValueError, match="parent"):
        create_category("Orphan", "orphan", parent_id=999)
    with pytest.raises(ValueError, match="product"):
        assign_product_category(999, stationery["id"])
    with pytest.raises(ValueError, match="category"):
        assign_product_category(notebook["id"], 999)
    with pytest.raises(ValueError, match="category"):
        create_category("Bad", "not/a/slug")


def test_category_pagination_hierarchy_and_visibility(client):
    tools = create_category("Tools", "tools", sort_order=2)
    paper = create_category("Paper", "paper", sort_order=1)
    child = create_category("Cutters", "cutters", parent_id=tools["id"])
    product = create_product("Scissors", 2500, 3)
    assign_product_category(product["id"], child["id"])
    first = client.get("/catalog/categories", params={"page": 1, "page_size": 1}).json()
    assert first["total"] == 2 and first["pages"] == 2
    assert first["page_size"] == 1 and first["has_next"] is True
    assert [item["slug"] for item in first["items"]] == ["paper"]
    second = client.get("/catalog/categories", params={"page": 2, "page_size": 1}).json()
    assert [item["slug"] for item in second["items"]] == ["tools"]
    assert second["has_next"] is False
    children = client.get("/catalog/categories", params={"parent_id": tools["id"]}).json()
    assert [item["slug"] for item in children["items"]] == ["cutters"]
    assert client.get(f"/catalog/categories/{child['id']}").json()["product_count"] == 1
    assert client.get(f"/catalog/categories/{paper['id']}").json()["product_count"] == 0
    set_category_active(child["id"], False)
    assert client.get(f"/catalog/categories/{child['id']}").status_code == 404
    assert client.get("/catalog/categories", params={"parent_id": tools["id"]}).json()["total"] == 0
    assert client.get("/catalog/categories", params={"parent_id": 999}).status_code == 404


def test_product_filters_sorting_and_category_browse(client):
    office = create_category("Office", "office")
    art = create_category("Art", "art")
    notebook = create_product("Notebook", 1000, 4)
    pencil = create_product("Pencil", 200, 0)
    paper = create_product("Paper", 500, 8)
    percent = create_product("100% Cotton", 900, 1)
    for product in (notebook, pencil, paper):
        assign_product_category(product["id"], office["id"])
    assign_product_category(paper["id"], art["id"])

    page = client.get(
        "/catalog/products",
        params={"category_id": office["id"], "in_stock": True, "sort": "price", "descending": True},
    ).json()
    assert [item["name"] for item in page["items"]] == ["Notebook", "Paper"]
    assert page["total"] == 2
    assert page["items"][1]["categories"] == [
        {"id": office["id"], "name": "Office", "slug": "office"},
        {"id": art["id"], "name": "Art", "slug": "art"},
    ]
    assert (
        client.get("/catalog/products", params={"in_stock": False}).json()["items"][0]["id"]
        == pencil["id"]
    )
    assert (
        client.get("/catalog/products", params={"min_price": 700, "max_price": 1000}).json()[
            "total"
        ]
        == 2
    )
    assert (
        client.get("/catalog/products", params={"q": "%"}).json()["items"][0]["id"] == percent["id"]
    )
    assert client.get(f"/catalog/products/{pencil['id']}").json()["available"] is False
    category_items = client.get(
        f"/catalog/categories/{office['id']}/products", params={"sort": "name", "page_size": 2}
    ).json()
    assert [item["name"] for item in category_items["items"]] == ["Notebook", "Paper"]
    assert category_items["pages"] == 2


def test_inventory_summary_tracks_stock_and_rejects_invalid_queries(client):
    category = create_category("Food", "food")
    apple = create_product("Apple", 300, 5)
    bread = create_product("Bread", 700, 0)
    create_product("Other", 100, 2)
    assign_product_category(apple["id"], category["id"])
    assign_product_category(bread["id"], category["id"])
    summary = client.get("/catalog/inventory/summary").json()
    assert summary == {
        "category_id": None,
        "total_products": 3,
        "available_products": 2,
        "out_of_stock_products": 1,
        "available_units": 7,
        "inventory_value": 1700,
    }
    subset = client.get("/catalog/inventory/summary", params={"category_id": category["id"]}).json()
    assert subset["available_units"] == 5 and subset["inventory_value"] == 1500
    with closing(connect()) as conn:
        conn.execute("UPDATE products SET stock=stock-2 WHERE id=?", (apple["id"],))
    assert client.get("/catalog/inventory/summary").json()["available_units"] == 5
    assert client.get("/catalog/products", params={"page": 0}).status_code == 422
    assert (
        client.get("/catalog/products", params={"sort": "price;DROP TABLE products"}).status_code
        == 422
    )
    assert (
        client.get("/catalog/products", params={"min_price": 1000, "max_price": 10}).status_code
        == 422
    )
    assert client.get("/catalog/products", params={"category_id": 999}).status_code == 404
    assert client.get("/catalog/products/999").status_code == 404
