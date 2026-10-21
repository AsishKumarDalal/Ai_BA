"""Shared test fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def sales_db(tmp_path: Path) -> Path:
    """A small but realistic sales database."""
    path = tmp_path / "sales.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE customers (
            id           INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            region       TEXT NOT NULL,
            email        TEXT,
            country      TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE products (
            id        INTEGER PRIMARY KEY,
            name      TEXT NOT NULL,
            category  TEXT NOT NULL,
            price     REAL NOT NULL
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

        CREATE TABLE order_items (
            id         INTEGER PRIMARY KEY,
            order_id   INTEGER NOT NULL REFERENCES orders(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity   INTEGER NOT NULL
        );

        CREATE VIEW v_revenue_by_region AS
            SELECT c.region, SUM(o.amount) AS revenue, COUNT(*) AS orders
              FROM orders o JOIN customers c ON c.id = o.customer_id
             WHERE o.status = 'paid'
             GROUP BY c.region;

        INSERT INTO customers (id, name, region, email, country, created_at) VALUES
            (1, 'Acme Corp',   'East', 'acme@example.com',   'US', '2024-01-05'),
            (2, 'Globex',      'West', 'contact@globex.com',  'US', '2024-02-11'),
            (3, 'Initech',     'East', 'sales@initech.io',    'UK', '2024-03-02'),
            (4, 'Umbrella',    'North','hello@umbrella.org',  'CA', '2024-05-19'),
            (5, 'Hooli',       'South','ap@hooli.co',         'US', '2024-06-30'),
            (6, 'Stark Ind',   'West', 'irene@stark.com',      'US', '2024-07-14');

        INSERT INTO products (id, name, category, price) VALUES
            (1, 'Widget',    'hardware', 25.0),
            (2, 'Gadget',    'hardware', 40.0),
            (3, 'Doohickey', 'software', 12.5),
            (4, 'Thingamajig','services', 99.0);

        INSERT INTO orders (id, customer_id, product_id, amount, quantity, status, ordered_at) VALUES
            (1, 1, 1,  250.0, 10, 'paid',    '2024-07-01'),
            (2, 1, 3,  125.0, 10, 'paid',    '2024-07-04'),
            (3, 2, 2,  400.0, 10, 'paid',    '2024-07-08'),
            (4, 2, 4,  990.0, 10, 'paid',    '2024-07-12'),
            (5, 3, 1,   75.0,  3, 'paid',    '2024-08-02'),
            (6, 3, 3,   50.0,  4, 'pending', '2024-08-06'),
            (7, 4, 4,  198.0,  2, 'paid',    '2024-08-21'),
            (8, 5, 2,  120.0,  3, 'refunded','2024-09-02'),
            (9, 6, 1,  175.0,  7, 'paid',    '2024-09-14'),
            (10,6, 4,  297.0,  3, 'paid',    '2024-09-27');

        INSERT INTO order_items (id, order_id, product_id, quantity) VALUES
            (1, 1, 1, 10), (2, 2, 3, 10), (3, 3, 2, 10), (4, 4, 4, 10),
            (5, 5, 1, 3), (6, 7, 4, 2), (7, 9, 1, 7), (8, 10, 4, 3);
        """
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    return path
