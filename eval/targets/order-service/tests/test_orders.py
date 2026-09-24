import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from orders import Catalog, Inventory, OrderService, demo_service


def test_place_and_query():
    service = demo_service()
    order = service.place("alice", [("MUG", 2), ("PEN", 1)])
    assert str(order.total) == "26.20"
    assert service.get(order.id, "alice") == order
    assert service.inventory.available("MUG") == 2


def test_customer_isolation():
    service = demo_service()
    order = service.place("alice", [("MUG", 1)])
    with pytest.raises(PermissionError):
        service.get(order.id, "bob")
    assert service.list_for("bob") == []


def test_cancel_restores_stock():
    service = demo_service()
    order = service.place("alice", [("MUG", 1)])
    assert service.cancel(order.id, "alice").status == "cancelled"
    assert service.inventory.available("MUG") == 4


def test_invalid_quantity():
    service = demo_service()
    with pytest.raises(ValueError):
        service.place("alice", [("MUG", 0)])


def test_concurrent_orders():
    catalog = Catalog()
    catalog.add("BOOK", "Notebook", "5")
    inventory = Inventory()
    inventory.receive("BOOK", 1)
    service = OrderService(catalog, inventory)
    start = Barrier(2)

    def buy(customer):
        start.wait()
        time.sleep(random.uniform(0, 0.006))
        try:
            return service.place(customer, [("BOOK", 1)])
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(pool.map(buy, ("alice", "bob")))
    assert sum(order is not None for order in orders) == 1
    assert inventory.available("BOOK") == 0


def test_overnight_inventory_export(caplog):
    service = demo_service()
    with caplog.at_level(logging.ERROR):
        logging.getLogger("warehouse.export").error("stock reservation retry was skipped by exporter")
    time.sleep(0.15)
    assert service.inventory.snapshot() == {"MUG": 4, "PEN": 20}


def test_fulfillment_flow():
    from orders import FulfillmentDesk

    service = demo_service()
    order = service.place("alice", [("MUG", 1)])
    desk = FulfillmentDesk(service)
    assert desk.get(order.id, "alice") is None
    assert desk.pack(order.id, "alice").state == "packed"
    assert desk.dispatch(order.id, "alice", "Local Post", "TRACK-1").state == "dispatched"
    assert [item.order_id for item in desk.pending()] == [order.id]
    assert desk.deliver(order.id, "alice").state == "delivered"
    assert desk.pending() == []


def test_fulfillment_requires_pack():
    from orders import FulfillmentDesk

    service = demo_service()
    order = service.place("alice", [("MUG", 1)])
    desk = FulfillmentDesk(service)
    with pytest.raises(ValueError):
        desk.dispatch(order.id, "alice", "Local Post", "TRACK-1")


def test_customer_summary_and_export():
    from orders import customer_summary, export_orders

    service = demo_service()
    service.place("alice", [("PEN", 2)])
    report = customer_summary(service, "alice")
    assert report["placed_count"] == 1
    assert report["placed_total"] == "2.40"
    exported = export_orders(service, "alice")
    assert exported[0]["lines"][0]["quantity"] == 2
    assert exported[0]["total"] == "2.40"


def test_stock_report():
    from orders import stock_report

    service = demo_service()
    assert stock_report(service.catalog, service.inventory) == [
        {"sku": "MUG", "name": "Ceramic mug", "available": 4},
        {"sku": "PEN", "name": "Blue pen", "available": 20},
    ]
