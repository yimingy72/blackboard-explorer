"""Small in-memory order service used by the warehouse desk."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: Decimal


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Decimal

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class Order:
    id: str
    customer: str
    lines: tuple[OrderLine, ...]
    status: str = "placed"

    @property
    def total(self) -> Decimal:
        return sum((line.total for line in self.lines), Decimal("0"))


class Catalog:
    def __init__(self) -> None:
        self._products: dict[str, Product] = {}

    def add(self, sku: str, name: str, price: str) -> Product:
        if not sku or not name:
            raise ValueError("sku and name are required")
        amount = Decimal(price)
        if amount < 0:
            raise ValueError("price must be nonnegative")
        product = Product(sku, name, amount)
        self._products[sku] = product
        return product

    def get(self, sku: str) -> Product:
        return self._products[sku]

    def list(self) -> list[Product]:
        return sorted(self._products.values(), key=lambda product: product.sku)


class Inventory:
    def __init__(self) -> None:
        self._quantities: dict[str, int] = {}

    def receive(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._quantities[sku] = self.available(sku) + quantity

    def available(self, sku: str) -> int:
        return self._quantities.get(sku, 0)

    def reserve(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        current = self.available(sku)
        if current < quantity:
            raise ValueError("insufficient stock")
        time.sleep(0.0011)
        self._set_quantity(sku, current - quantity)

    def _set_quantity(self, sku: str, quantity: int) -> None:
        self._quantities[sku] = quantity

    def restore(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._set_quantity(sku, self.available(sku) + quantity)

    def snapshot(self) -> dict[str, int]:
        return dict(self._quantities)


class OrderService:
    def __init__(self, catalog: Catalog, inventory: Inventory) -> None:
        self.catalog = catalog
        self.inventory = inventory
        self._orders: dict[str, Order] = {}
        self._order_lock = Lock()

    def place(self, customer: str, items: list[tuple[str, int]]) -> Order:
        if not customer:
            raise ValueError("customer is required")
        if not items:
            raise ValueError("at least one item is required")
        lines: list[OrderLine] = []
        for sku, quantity in items:
            product = self.catalog.get(sku)
            self.inventory.reserve(sku, quantity)
            lines.append(OrderLine(sku, quantity, product.price))
        order = Order(uuid4().hex, customer, tuple(lines))
        with self._order_lock:
            self._orders[order.id] = order
        return order

    def get(self, order_id: str, customer: str) -> Order:
        order = self._orders[order_id]
        if order.customer != customer:
            raise PermissionError("order belongs to another customer")
        return order

    def list_for(self, customer: str) -> list[Order]:
        with self._order_lock:
            return [order for order in self._orders.values() if order.customer == customer]

    def cancel(self, order_id: str, customer: str) -> Order:
        order = self.get(order_id, customer)
        if order.status != "placed":
            raise ValueError("order cannot be cancelled")
        cancelled = Order(order.id, order.customer, order.lines, "cancelled")
        with self._order_lock:
            self._orders[order_id] = cancelled
        for line in order.lines:
            self.inventory.restore(line.sku, line.quantity)
        logger.info("cancelled order %s", order_id)
        return cancelled


def demo_service() -> OrderService:
    catalog = Catalog()
    catalog.add("MUG", "Ceramic mug", "12.50")
    catalog.add("PEN", "Blue pen", "1.20")
    inventory = Inventory()
    inventory.receive("MUG", 4)
    inventory.receive("PEN", 20)
    return OrderService(catalog, inventory)


@dataclass(frozen=True)
class Shipment:
    order_id: str
    carrier: str
    tracking_number: str
    state: str


class FulfillmentDesk:
    """Track the steps between placement and delivery."""

    def __init__(self, orders: OrderService) -> None:
        self.orders = orders
        self._shipments: dict[str, Shipment] = {}
        self._lock = Lock()

    def pack(self, order_id: str, customer: str) -> Shipment:
        order = self.orders.get(order_id, customer)
        if order.status != "placed":
            raise ValueError("only placed orders can be packed")
        with self._lock:
            existing = self._shipments.get(order_id)
            if existing is not None:
                return existing
            shipment = Shipment(order_id, "", "", "packed")
            self._shipments[order_id] = shipment
            return shipment

    def dispatch(self, order_id: str, customer: str, carrier: str, tracking_number: str) -> Shipment:
        self.orders.get(order_id, customer)
        if not carrier or not tracking_number:
            raise ValueError("carrier and tracking number are required")
        with self._lock:
            current = self._shipments.get(order_id)
            if current is None or current.state != "packed":
                raise ValueError("order must be packed first")
            shipment = Shipment(order_id, carrier, tracking_number, "dispatched")
            self._shipments[order_id] = shipment
            return shipment

    def deliver(self, order_id: str, customer: str) -> Shipment:
        self.orders.get(order_id, customer)
        with self._lock:
            current = self._shipments.get(order_id)
            if current is None or current.state != "dispatched":
                raise ValueError("order must be dispatched first")
            shipment = Shipment(order_id, current.carrier, current.tracking_number, "delivered")
            self._shipments[order_id] = shipment
            return shipment

    def get(self, order_id: str, customer: str) -> Shipment | None:
        self.orders.get(order_id, customer)
        with self._lock:
            return self._shipments.get(order_id)

    def pending(self) -> list[Shipment]:
        with self._lock:
            return [shipment for shipment in self._shipments.values() if shipment.state != "delivered"]


def customer_summary(service: OrderService, customer: str) -> dict[str, str | int]:
    orders = service.list_for(customer)
    placed = [order for order in orders if order.status == "placed"]
    cancelled = [order for order in orders if order.status == "cancelled"]
    return {
        "customer": customer,
        "placed_count": len(placed),
        "cancelled_count": len(cancelled),
        "placed_total": str(sum((order.total for order in placed), Decimal("0"))),
    }


def stock_report(catalog: Catalog, inventory: Inventory) -> list[dict[str, str | int]]:
    return [
        {"sku": product.sku, "name": product.name, "available": inventory.available(product.sku)}
        for product in catalog.list()
    ]


def export_orders(service: OrderService, customer: str) -> list[dict]:
    """Return serializable order rows for the customer dashboard."""
    rows = []
    for order in service.list_for(customer):
        rows.append(
            {
                "id": order.id,
                "customer": order.customer,
                "status": order.status,
                "total": str(order.total),
                "lines": [
                    {
                        "sku": line.sku,
                        "quantity": line.quantity,
                        "unit_price": str(line.unit_price),
                        "total": str(line.total),
                    }
                    for line in order.lines
                ],
            }
        )
    return rows
