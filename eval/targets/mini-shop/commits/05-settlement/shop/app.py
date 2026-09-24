"""HTTP application for the shop."""

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from shop.db import connect, create_user, current_user, initialize, login
from shop.orders import router as orders_router

initialize()
app = FastAPI(title="Mini Shop")
app.include_router(orders_router)


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1)
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    name: str
    password: str


class ChargeRequest(BaseModel):
    amount: int = Field(gt=0)


@app.post("/users")
def register(body: RegisterRequest) -> dict:
    try:
        return create_user(body.name, password=body.password)
    except Exception as exc:
        raise HTTPException(409, "name already exists") from exc


@app.post("/login")
def login_user(body: LoginRequest) -> dict:
    user = login(body.name, body.password)
    if user is None:
        raise HTTPException(401, "invalid credentials")
    return user


@app.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return user


@app.post("/balance/charge")
def charge(body: ChargeRequest, user: dict = Depends(current_user)) -> dict:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        balance = conn.execute(
            "UPDATE users SET balance=balance+? WHERE id=? RETURNING balance",
            (body.amount, user["id"]),
        ).fetchone()[0]
        conn.commit()
    return {"balance": balance}


@app.get("/products")
def products() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return [dict(row) for row in rows]

from shop.coupons import router as coupons_router
app.include_router(coupons_router)
from shop.refunds import router as refunds_router
app.include_router(refunds_router)
from shop.search import router as search_router
app.include_router(search_router)
from shop.detail import router as detail_router
app.include_router(detail_router)

from shop.accounts import initialize_accounts, router as accounts_router
from shop.activity import router as activity_router
from shop.catalog import initialize_catalog, router as catalog_router

initialize_accounts()
initialize_catalog()
app.include_router(accounts_router)
app.include_router(activity_router)
app.include_router(catalog_router)
