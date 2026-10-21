#!/usr/bin/env python3
"""Create a small demo sales database for trying the harness out.

    python harness/examples/make_demo_db.py /tmp/demo.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE customers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    region     TEXT NOT NULL,
    country    TEXT NOT NULL,
    email      TEXT,
    segment    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE products (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    category TEXT NOT NULL,
    price    REAL NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    amount      REAL NOT NULL,
    quantity    INTEGER NOT NULL,
    status      TEXT NOT NULL,
    ordered_at  TEXT NOT NULL
);

CREATE VIEW v_revenue_by_region AS
    SELECT c.region, SUM(o.amount) AS revenue, COUNT(*) AS orders
      FROM orders o JOIN customers c ON c.id = o.customer_id
     WHERE o.status = 'paid'
     GROUP BY c.region;
"""

CUSTOMERS = [
    (1, "Acme Corp", "East", "US", "acme@example.com", "enterprise", "2024-01-05"),
    (2, "Globex", "West", "US", "contact@globex.com", "mid-market", "2024-02-11"),
    (3, "Initech", "East", "UK", "sales@initech.io", "enterprise", "2024-03-02"),
    (4, "Umbrella Ltd", "North", "CA", "hello@umbrella.org", "smb", "2024-05-19"),
    (5, "Hooli", "South", "US", "ap@hooli.co", "mid-market", "2024-06-30"),
    (6, "Stark Industries", "West", "US", "irene@stark.com", "enterprise", "2024-07-14"),
    (7, "Wonka Ltd", "North", "GB", "orders@wonka.co", "smb", "2024-08-02"),
    (8, "Cyberdyne", "West", "US", "sales@cyberdyne.io", "mid-market", "2024-09-09"),
    (9, "Tyrell Corp", "South", "US", "billy@tyrell.com", "enterprise", "2024-10-21"),
    (10, "Soylent", "East", "US", "hello@soylent.co", "smb", "2024-11-30"),
]

PRODUCTS = [
    (1, "Widget", "hardware", 25.0),
    (2, "Gadget", "hardware", 40.0),
    (3, "Doohickey", "software", 12.5),
    (4, "Thingamajig", "services", 99.0),
    (5, "Sprocket", "hardware", 18.0),
]

# (id, customer, product, amount, quantity, status, date)
ORDERS = [
    (1, 1, 1, 250.0, 10, "paid", "2024-07-01"),
    (2, 1, 3, 125.0, 10, "paid", "2024-07-04"),
    (3, 1, 4, 990.0, 10, "paid", "2024-07-09"),
    (4, 2, 2, 400.0, 10, "paid", "2024-07-08"),
    (5, 2, 4, 495.0, 5, "paid", "2024-07-12"),
    (6, 3, 1, 75.0, 3, "paid", "2024-08-02"),
    (7, 3, 3, 50.0, 4, "pending", "2024-08-06"),
    (8, 4, 4, 198.0, 2, "paid", "2024-08-21"),
    (9, 4, 5, 180.0, 10, "paid", "2024-08-25"),
    (10, 5, 2, 120.0, 3, "refunded", "2024-09-02"),
    (11, 5, 1, 225.0, 9, "paid", "2024-09-05"),
    (12, 6, 4, 297.0, 3, "paid", "2024-09-14"),
    (13, 6, 2, 200.0, 5, "paid", "2024-09-18"),
    (14, 7, 1, 150.0, 6, "paid", "2024-10-03"),
    (15, 7, 3, 100.0, 8, "paid", "2024-10-11"),
    (16, 8, 5, 360.0, 20, "paid", "2024-10-22"),
    (17, 8, 4, 594.0, 6, "paid", "2024-11-01"),
    (18, 9, 2, 160.0, 4, "paid", "2024-11-15"),
    (19, 9, 4, 891.0, 9, "paid", "2024-11-28"),
    (20, 10, 1, 125.0, 5, "paid", "2024-12-04"),
    (21, 10, 3, 87.5, 7, "paid", "2024-12-19"),
    (22, 2, 3, 187.5, 15, "paid", "2024-12-27"),
    (23, 6, 1, 200.0, 8, "pending", "2025-01-08"),
    (24, 4, 2, 240.0, 6, "paid", "2025-01-15"),
]


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "demo.db").expanduser()
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?)", CUSTOMERS)
    connection.executemany("INSERT INTO products VALUES (?,?,?,?)", PRODUCTS)
    connection.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", ORDERS)
    connection.commit()
    connection.close()
    print(f"created {path} with 4 tables, 1 view, {len(ORDERS)} orders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
