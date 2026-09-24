"""SQLite storage for the shop."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import Header, HTTPException


def database_path() -> Path:
    return Path(os.environ.get("SHOP_DB", "shop.sqlite3"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path(), timeout=5, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def initialize() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price >= 0),
                stock INTEGER NOT NULL CHECK (stock >= 0)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL,
                total INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'placed',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)


def password_digest(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()


def create_user(name: str, balance: int = 0, password: str = "local-password") -> dict:
    token = secrets.token_hex(16)
    salt = secrets.token_hex(16)
    digest = password_digest(password, salt)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users(name, token, password_salt, password_hash, balance) VALUES(?,?,?,?,?)",
            (name, token, salt, digest, balance),
        )
        return {"id": cur.lastrowid, "name": name, "token": token, "balance": balance}


def login(name: str, password: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if row is None or not hmac.compare_digest(password_digest(password, row["password_salt"]), row["password_hash"]):
        return None
    return {"id": row["id"], "name": row["name"], "token": row["token"]}


def create_product(name: str, price: int, stock: int) -> dict:
    with connect() as conn:
        cur = conn.execute("INSERT INTO products(name, price, stock) VALUES(?,?,?)", (name, price, stock))
        return {"id": cur.lastrowid, "name": name, "price": price, "stock": stock}


def current_user(authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ")
    with connect() as conn:
        row = conn.execute("SELECT id, name, balance FROM users WHERE token=?", (token,)).fetchone()
    if row is None:
        raise HTTPException(401, "invalid token")
    return dict(row)


def require_order(conn: sqlite3.Connection, order_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "order not found")
    return row


def order_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}
