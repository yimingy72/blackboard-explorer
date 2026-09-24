"""Public catalog browsing and local category administration."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from shop.db import connect

router = APIRouter(prefix="/catalog", tags=["catalog"])
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SORT_COLUMNS = {
    "id": "p.id",
    "name": "p.name COLLATE NOCASE",
    "price": "p.price",
    "stock": "p.stock",
}


def initialize_catalog() -> None:
    """Create the category tables alongside the existing product table."""
    with closing(connect()) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                parent_id INTEGER REFERENCES categories(id),
                sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS idx_categories_parent
                ON categories(parent_id, active, sort_order, id);
            CREATE TABLE IF NOT EXISTS product_categories (
                product_id INTEGER NOT NULL REFERENCES products(id),
                category_id INTEGER NOT NULL REFERENCES categories(id),
                PRIMARY KEY (product_id, category_id)
            );
            CREATE INDEX IF NOT EXISTS idx_product_categories_category
                ON product_categories(category_id, product_id);
        """)


def create_category(
    name: str,
    slug: str,
    description: str = "",
    parent_id: int | None = None,
    sort_order: int = 0,
) -> dict:
    """Local-only administration; no unauthenticated write route is exposed."""
    name, slug = name.strip(), slug.strip().lower()
    if not name or not _SLUG.fullmatch(slug) or sort_order < 0:
        raise ValueError("invalid category")
    with closing(connect()) as conn:
        if (
            parent_id is not None
            and conn.execute("SELECT 1 FROM categories WHERE id=?", (parent_id,)).fetchone() is None
        ):
            raise ValueError("parent category not found")
        try:
            cursor = conn.execute(
                "INSERT INTO categories(name, slug, description, parent_id, sort_order) "
                "VALUES(?,?,?,?,?)",
                (name, slug, description.strip(), parent_id, sort_order),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("category slug already exists") from exc
        row = conn.execute("SELECT * FROM categories WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def assign_product_category(product_id: int, category_id: int) -> None:
    """Link an existing product to an existing category, idempotently."""
    with closing(connect()) as conn:
        if conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone() is None:
            raise ValueError("product not found")
        if conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone() is None:
            raise ValueError("category not found")
        conn.execute(
            "INSERT OR IGNORE INTO product_categories(product_id, category_id) VALUES(?,?)",
            (product_id, category_id),
        )


def set_category_active(category_id: int, active: bool) -> None:
    with closing(connect()) as conn:
        cursor = conn.execute(
            "UPDATE categories SET active=? WHERE id=?", (int(active), category_id)
        )
        if cursor.rowcount == 0:
            raise ValueError("category not found")


def _category(conn: sqlite3.Connection, category_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM categories WHERE id=? AND active=1", (category_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "category not found")
    return dict(row)


def _page(total: int, page: int, page_size: int, items: list[dict]) -> dict:
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "has_next": page * page_size < total,
    }


def _category_labels(conn: sqlite3.Connection, product_ids: list[int]) -> dict[int, list[dict]]:
    if not product_ids:
        return {}
    marks = ",".join("?" for _ in product_ids)
    rows = conn.execute(
        "SELECT pc.product_id, c.id, c.name, c.slug FROM product_categories pc "
        "JOIN categories c ON c.id=pc.category_id "
        f"WHERE c.active=1 AND pc.product_id IN ({marks}) ORDER BY c.sort_order, c.id",
        product_ids,
    ).fetchall()
    result: dict[int, list[dict]] = {product_id: [] for product_id in product_ids}
    for row in rows:
        result[row["product_id"]].append(
            {"id": row["id"], "name": row["name"], "slug": row["slug"]}
        )
    return result


def _products(
    conn: sqlite3.Connection,
    *,
    page: int,
    page_size: int,
    q: str | None,
    category_id: int | None,
    min_price: int | None,
    max_price: int | None,
    in_stock: bool | None,
    sort: str,
    descending: bool,
) -> dict:
    conditions: list[str] = []
    values: list[object] = []
    if q:
        literal = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if literal:
            conditions.append("p.name LIKE ? ESCAPE '\\'")
            values.append(f"%{literal}%")
    if category_id is not None:
        _category(conn, category_id)
        conditions.append(
            "EXISTS (SELECT 1 FROM product_categories pc "
            "WHERE pc.product_id=p.id AND pc.category_id=?)"
        )
        values.append(category_id)
    if min_price is not None:
        conditions.append("p.price>=?")
        values.append(min_price)
    if max_price is not None:
        conditions.append("p.price<=?")
        values.append(max_price)
    if in_stock is not None:
        conditions.append("p.stock>0" if in_stock else "p.stock=0")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = conn.execute("SELECT COUNT(*) FROM products p" + where, values).fetchone()[0]
    direction = "DESC" if descending else "ASC"
    rows = conn.execute(
        "SELECT p.id, p.name, p.price, p.stock FROM products p"
        + where
        + f" ORDER BY {_SORT_COLUMNS[sort]} {direction}, p.id ASC LIMIT ? OFFSET ?",
        [*values, page_size, (page - 1) * page_size],
    ).fetchall()
    labels = _category_labels(conn, [row["id"] for row in rows])
    items = [
        {**dict(row), "available": row["stock"] > 0, "categories": labels[row["id"]]}
        for row in rows
    ]
    return _page(total, page, page_size, items)


@router.get("/categories")
def categories(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    parent_id: int | None = Query(default=None, ge=1),
) -> dict:
    with closing(connect()) as conn:
        if parent_id is not None:
            _category(conn, parent_id)
        where = "parent_id IS NULL" if parent_id is None else "parent_id=?"
        values: tuple[int, ...] = () if parent_id is None else (parent_id,)
        total = conn.execute(
            f"SELECT COUNT(*) FROM categories WHERE active=1 AND {where}", values
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, name, slug, description, parent_id, sort_order "
            f"FROM categories WHERE active=1 AND {where} "
            "ORDER BY sort_order, id LIMIT ? OFFSET ?",
            (*values, page_size, (page - 1) * page_size),
        ).fetchall()
    return _page(total, page, page_size, [dict(row) for row in rows])


@router.get("/categories/{category_id}")
def category_detail(category_id: int) -> dict:
    with closing(connect()) as conn:
        item = _category(conn, category_id)
        item["product_count"] = conn.execute(
            "SELECT COUNT(*) FROM product_categories WHERE category_id=?", (category_id,)
        ).fetchone()[0]
    return item


@router.get("/categories/{category_id}/products")
def category_products(
    category_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    min_price: int | None = Query(default=None, ge=0),
    max_price: int | None = Query(default=None, ge=0),
    in_stock: bool | None = None,
    sort: Literal["id", "name", "price", "stock"] = "id",
    descending: bool = False,
) -> dict:
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(422, "min_price exceeds max_price")
    with closing(connect()) as conn:
        return _products(
            conn,
            page=page,
            page_size=page_size,
            q=None,
            category_id=category_id,
            min_price=min_price,
            max_price=max_price,
            in_stock=in_stock,
            sort=sort,
            descending=descending,
        )


@router.get("/products")
def products(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = None,
    category_id: int | None = Query(default=None, ge=1),
    min_price: int | None = Query(default=None, ge=0),
    max_price: int | None = Query(default=None, ge=0),
    in_stock: bool | None = None,
    sort: Literal["id", "name", "price", "stock"] = "id",
    descending: bool = False,
) -> dict:
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(422, "min_price exceeds max_price")
    with closing(connect()) as conn:
        return _products(
            conn,
            page=page,
            page_size=page_size,
            q=q,
            category_id=category_id,
            min_price=min_price,
            max_price=max_price,
            in_stock=in_stock,
            sort=sort,
            descending=descending,
        )


@router.get("/products/{product_id}")
def product_detail(product_id: int) -> dict:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT id, name, price, stock FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "product not found")
        return {
            **dict(row),
            "available": row["stock"] > 0,
            "categories": _category_labels(conn, [product_id])[product_id],
        }


@router.get("/inventory/summary")
def inventory_summary(category_id: int | None = Query(default=None, ge=1)) -> dict:
    with closing(connect()) as conn:
        values: tuple[int, ...] = ()
        where = ""
        if category_id is not None:
            _category(conn, category_id)
            where = (
                " WHERE EXISTS (SELECT 1 FROM product_categories pc "
                "WHERE pc.product_id=p.id AND pc.category_id=?)"
            )
            values = (category_id,)
        row = conn.execute(
            "SELECT COUNT(*) AS total_products, "
            "COALESCE(SUM(CASE WHEN p.stock>0 THEN 1 ELSE 0 END),0) AS available_products, "
            "COALESCE(SUM(CASE WHEN p.stock=0 THEN 1 ELSE 0 END),0) AS out_of_stock_products, "
            "COALESCE(SUM(p.stock),0) AS available_units, "
            "COALESCE(SUM(p.stock*p.price),0) AS inventory_value "
            "FROM products p" + where,
            values,
        ).fetchone()
    return {"category_id": category_id, **dict(row)}
