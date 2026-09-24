"""Create local users and products for a fresh database."""

from shop.db import connect, create_product, create_user, initialize

initialize()
with connect() as conn:
    if conn.execute("SELECT count(*) FROM users").fetchone()[0]:
        raise SystemExit("database already contains users")
for name in ("alice", "bob"):
    print(create_user(name, 20_000, f"{name}-local-password"))
print(create_product("Notebook", 10_000, 100))
print(create_product("Pencil", 500, 100))
