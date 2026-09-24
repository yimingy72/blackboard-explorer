"""Profile and delivery address routes."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shop.db import connect, current_user

router = APIRouter()
CurrentUser = Annotated[dict, Depends(current_user)]


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = Field(default=None, min_length=6, max_length=24, pattern=r"^[+0-9 ()-]+$")


class AddressInput(BaseModel):
    recipient: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=6, max_length=24, pattern=r"^[+0-9 ()-]+$")
    region: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=80)
    street: str = Field(min_length=1, max_length=240)
    postal_code: str = Field(default="", max_length=20)
    label: str = Field(default="", max_length=40)


def initialize_accounts() -> None:
    """Create account tables alongside the shop's existing tables."""
    with closing(connect()) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                display_name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS addresses (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                recipient TEXT NOT NULL,
                phone TEXT NOT NULL,
                region TEXT NOT NULL,
                city TEXT NOT NULL,
                street TEXT NOT NULL,
                postal_code TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS addresses_owner_idx ON addresses(user_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS addresses_one_default_idx
                ON addresses(user_id) WHERE is_default = 1;
        """)


def _profile(conn: sqlite3.Connection, user: dict) -> dict:
    row = conn.execute(
        "SELECT display_name, email, phone, updated_at FROM profiles WHERE user_id=?",
        (user["id"],),
    ).fetchone()
    return {
        "user_id": user["id"],
        "username": user["name"],
        "display_name": row["display_name"] if row else user["name"],
        "email": row["email"] if row else None,
        "phone": row["phone"] if row else None,
        "updated_at": row["updated_at"] if row else None,
    }


def _address(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "recipient": row["recipient"],
        "phone": row["phone"],
        "region": row["region"],
        "city": row["city"],
        "street": row["street"],
        "postal_code": row["postal_code"],
        "label": row["label"],
        "is_default": bool(row["is_default"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _owned_address(conn: sqlite3.Connection, address_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM addresses WHERE id=? AND user_id=?", (address_id, user_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "address not found")
    return row


@router.get("/profile")
def get_profile(user: CurrentUser) -> dict:
    with closing(connect()) as conn:
        return _profile(conn, user)


@router.patch("/profile")
def update_profile(body: ProfileUpdate, user: CurrentUser) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "at least one profile field is required")
    if "display_name" in changes and changes["display_name"] is None:
        raise HTTPException(422, "display name cannot be empty")
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = _profile(conn, user)
        display_name = changes.get("display_name", previous["display_name"])
        email = changes.get("email", previous["email"])
        phone = changes.get("phone", previous["phone"])
        conn.execute(
            """INSERT INTO profiles(user_id, display_name, email, phone)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               display_name=excluded.display_name, email=excluded.email,
               phone=excluded.phone, updated_at=CURRENT_TIMESTAMP""",
            (user["id"], display_name, email, phone),
        )
        return _profile(conn, user)


@router.get("/addresses")
def list_addresses(user: CurrentUser) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM addresses WHERE user_id=? ORDER BY is_default DESC, id ASC",
            (user["id"],),
        ).fetchall()
        return [_address(row) for row in rows]


@router.post("/addresses", status_code=201)
def create_address(body: AddressInput, user: CurrentUser) -> dict:
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM addresses WHERE user_id=? LIMIT 1", (user["id"],)
        ).fetchone()
        values = body.model_dump()
        cursor = conn.execute(
            """INSERT INTO addresses(
                user_id, recipient, phone, region, city, street, postal_code, label, is_default
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user["id"],
                values["recipient"],
                values["phone"],
                values["region"],
                values["city"],
                values["street"],
                values["postal_code"],
                values["label"],
                int(exists is None),
            ),
        )
        address_id = cursor.lastrowid
        assert address_id is not None
        return _address(_owned_address(conn, address_id, user["id"]))


@router.get("/addresses/{address_id}")
def get_address(address_id: int, user: CurrentUser) -> dict:
    with closing(connect()) as conn:
        return _address(_owned_address(conn, address_id, user["id"]))


@router.put("/addresses/{address_id}")
def update_address(address_id: int, body: AddressInput, user: CurrentUser) -> dict:
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _owned_address(conn, address_id, user["id"])
        values = body.model_dump()
        conn.execute(
            """UPDATE addresses SET recipient=?, phone=?, region=?, city=?, street=?,
               postal_code=?, label=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (
                values["recipient"],
                values["phone"],
                values["region"],
                values["city"],
                values["street"],
                values["postal_code"],
                values["label"],
                address_id,
                user["id"],
            ),
        )
        return _address(_owned_address(conn, address_id, user["id"]))


@router.delete("/addresses/{address_id}", status_code=204)
def delete_address(address_id: int, user: CurrentUser) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        old = _owned_address(conn, address_id, user["id"])
        conn.execute("DELETE FROM addresses WHERE id=? AND user_id=?", (address_id, user["id"]))
        if old["is_default"]:
            successor = conn.execute(
                "SELECT id FROM addresses WHERE user_id=? ORDER BY id LIMIT 1", (user["id"],)
            ).fetchone()
            if successor:
                conn.execute(
                    "UPDATE addresses SET is_default=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (successor["id"],),
                )


@router.post("/addresses/{address_id}/default")
def set_default_address(address_id: int, user: CurrentUser) -> dict:
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _owned_address(conn, address_id, user["id"])
        conn.execute("UPDATE addresses SET is_default=0 WHERE user_id=?", (user["id"],))
        conn.execute(
            "UPDATE addresses SET is_default=1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND user_id=?",
            (address_id, user["id"]),
        )
        return _address(_owned_address(conn, address_id, user["id"]))
