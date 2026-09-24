"""Synchronize writes to demonstrate duplicate stock reservations."""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

TARGET = Path(__file__).resolve().parents[2] / "targets" / "order-service"
sys.path.insert(0, str(TARGET))

from orders import Catalog, Inventory, OrderService  # noqa: E402


def verify_once():
    catalog = Catalog()
    catalog.add("BOOK", "Notebook", "5")
    inventory = Inventory()
    inventory.receive("BOOK", 1)
    service = OrderService(catalog, inventory)
    barrier = Barrier(2, timeout=3)
    original = inventory._set_quantity

    def synchronized_write(sku, quantity):
        barrier.wait()
        original(sku, quantity)

    inventory._set_quantity = synchronized_write
    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(pool.map(lambda name: service.place(name, [("BOOK", 1)]), ("alice", "bob")))
    return len(orders) == 2 and inventory.available("BOOK") == 0


if __name__ == "__main__":
    successes = sum(verify_once() for _ in range(100))
    print(f"duplicate reservations: {successes}/100")
    raise SystemExit(0 if successes >= 90 else 1)
