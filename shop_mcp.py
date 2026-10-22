"""MCP server for the shop: inventory, reservations and orders.

Serves the tools over streamable HTTP (default ``http://127.0.0.1:9996/mcp``):

Inventory (used by the Stock agent):
    - search_stock(query)      -> list of products (code, name, price, stock)
    - get_product(code)        -> one product
Reservations (used by the Stock agent via the Sales agent):
    - reserve_stock(code, qty, idempotency_key) -> CONFIRMED | REJECTED
    - get_reservation(id)     -> CONFIRMED | CONSUMED | EXPIRED | NOT_FOUND
Orders (used directly by the Sales agent):
    - create_order(reservation_id, customer, idempotency_key)
    - get_order(id)
Admin helpers (tests/demo only):
    - set_stock(code, units), admin_stats()

Correctness model (the two guarantees the PoC must prove):

- **Atomicity** lives in a single SQLite transaction: the stock update and
  the reservation insert are committed together, and ``UPDATE ... WHERE
  stock >= qty`` makes overselling impossible even under N concurrent
  calls (SQLite's ``BEGIN IMMEDIATE`` serialises writers, also across
  processes thanks to WAL).
- **Idempotency** lives in ``UNIQUE(idempotency_key)`` columns: repeating a
  reserve/order call with the same key returns the stored row instead of
  creating a second one ("replayed": true).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

import uvicorn

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger('shop_mcp')

DEFAULT_DB = os.path.join('data', 'shop.db')
DEFAULT_RESERVATION_TTL_SECONDS = 90  # enough to survive the restart drill

# Seed inventory for the demo: KD-LAST-03 has exactly one unit (the
# concurrency drill), KD-OUT-04 has none.
SEED_PRODUCTS = [
    # code, name, category, tags, price_cents, stock
    ('KD-PRO-01', 'Teclado Mecánico Pro 87', 'teclado', 'mecanico,pro,87',
     8999, 3),
    ('KD-RGB-02', 'Teclado TKL RGB Gaming', 'teclado', 'tkl,rgb,gaming',
     5950, 2),
    ('KD-LAST-03', 'Teclado Único TKL Edición Limitada', 'teclado',
     'tkl,unico,edicion', 7425, 1),
    ('KD-OUT-04', 'Teclado Ergonómico Split Agotado', 'teclado',
     'ergonomico,split', 12900, 0),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def _norm(text: str) -> str:
    """Case/accent-insensitive normalisation for search matching."""
    decomposed = unicodedata.normalize('NFD', text)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


class Shop:
    """All SQL lives here; every write runs inside BEGIN IMMEDIATE."""

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.isolation_level = None  # manual transactions
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA busy_timeout=10000')
        self.ttl_seconds = DEFAULT_RESERVATION_TTL_SECONDS
        self._create_schema()

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------
    # schema + seed
    # ------------------------------------------------------------------
    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                code        TEXT PRIMARY KEY,
                name        TEXT    NOT NULL,
                category    TEXT    NOT NULL,
                tags        TEXT    NOT NULL,
                price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                stock       INTEGER NOT NULL CHECK (stock    >= 0)
            );
            CREATE TABLE IF NOT EXISTS reservations (
                id          TEXT PRIMARY KEY,
                idem_key    TEXT NOT NULL UNIQUE,
                product_code TEXT NOT NULL,
                qty         INTEGER NOT NULL CHECK (qty > 0),
                status      TEXT NOT NULL,        -- CONFIRMED | CONSUMED
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id          TEXT PRIMARY KEY,
                idem_key    TEXT NOT NULL UNIQUE,
                reservation_id TEXT NOT NULL,
                product_code   TEXT NOT NULL,
                qty            INTEGER NOT NULL,
                customer       TEXT NOT NULL,
                total_cents    INTEGER NOT NULL,
                created_at     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counters (
                name  TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            """
        )

    def seed_if_empty(self) -> None:
        count = self._db.execute('SELECT COUNT(*) AS n FROM products').fetchone()['n']
        if count:
            return
        self._db.execute('BEGIN IMMEDIATE')
        try:
            self._db.executemany(
                'INSERT INTO products VALUES (?,?,?,?,?,?)',
                SEED_PRODUCTS,
            )
            self._db.execute('COMMIT')
            logger.info('Seeded %d products from scratch', len(SEED_PRODUCTS))
        except Exception:
            self._db.execute('ROLLBACK')
            raise

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        row = self._db.execute(
            """
            INSERT INTO counters(name, value) VALUES (?, 1)
            ON CONFLICT(name) DO UPDATE SET value = value + 1
            RETURNING value
            """,
            (prefix,),
        ).fetchone()
        return f'{prefix}-{row["value"]:06d}'

    def _product_payload(self, row: sqlite3.Row) -> dict:
        return {
            'code': row['code'],
            'name': row['name'],
            'category': row['category'],
            'tags': [t.strip() for t in row['tags'].split(',')],
            'price_eur': round(row['price_cents'] / 100, 2),
            'stock': row['stock'],
        }

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def search_stock(self, query: str = '', category: str = '') -> dict:
        needle = _norm((query or '').strip())
        rows = self._db.execute('SELECT * FROM products ORDER BY code').fetchall()
        products = []
        for row in rows:
            if category and _norm(category) != _norm(row['category']):
                continue
            if needle:
                haystack = _norm(
                    f"{row['name']} {row['tags']} {row['category']} {row['code']}"
                )
                if needle not in haystack:
                    continue
            products.append(self._product_payload(row))
        return {'query': query or '', 'category': category or '',
                'count': len(products), 'products': products}

    def get_product(self, code: str) -> dict:
        row = self._db.execute(
            'SELECT * FROM products WHERE code = ?', (code,)
        ).fetchone()
        if not row:
            return {'code': code, 'found': False}
        return {'found': True, **self._product_payload(row)}

    def reserve_stock(self, code: str, qty: int,
                      idempotency_key: str) -> dict:
        qty = int(qty)
        if qty < 1:
            return {'status': 'REJECTED', 'reason': 'CANTIDAD_INVALIDA'}

        self._db.execute('BEGIN IMMEDIATE')
        try:
            # Idempotency: a repeated key returns the stored reservation.
            prior = self._db.execute(
                'SELECT * FROM reservations WHERE idem_key = ?',
                (idempotency_key,),
            ).fetchone()
            if prior:
                self._db.execute('ROLLBACK')
                out = self._reservation_payload(prior)
                out['replayed'] = True
                return out

            # Atomicity: stock check and update in the same transaction.
            cursor = self._db.execute(
                'UPDATE products SET stock = stock - ? '
                'WHERE code = ? AND stock >= ?',
                (qty, code, qty),
            )
            if cursor.rowcount == 0:
                self._db.execute('ROLLBACK')
                exists = self._db.execute(
                    'SELECT 1 FROM products WHERE code = ?', (code,)
                ).fetchone()
                reason = 'SIN_STOCK' if exists else 'PRODUCTO_NO_EXISTE'
                return {'status': 'REJECTED', 'reason': reason,
                        'code': code, 'qty': qty}

            reservation_id = self._next_id('RES')
            now = utcnow()
            expires = now + timedelta(seconds=self.ttl_seconds)
            self._db.execute(
                'INSERT INTO reservations VALUES (?,?,?,?,?,?,?)',
                (reservation_id, idempotency_key, code, qty, 'CONFIRMED',
                 iso(now), iso(expires)),
            )
            self._db.execute('COMMIT')

            logger.info('Reserva %s: %s x%d (quedan %d unidades)',
                        reservation_id, code, qty, self._stock_of(code))
            return {
                'status': 'CONFIRMED',
                'reservation_id': reservation_id,
                'code': code,
                'qty': qty,
                'created_at': iso(now),
                'expires_at': iso(expires),
            }
        except Exception:
            self._db.execute('ROLLBACK')
            raise

    def _stock_of(self, code: str) -> int:
        row = self._db.execute(
            'SELECT stock FROM products WHERE code = ?', (code,)
        ).fetchone()
        return row['stock'] if row else 0

    def _reservation_payload(self, row: sqlite3.Row) -> dict:
        status = row['status']
        if status == 'CONFIRMED' and utcnow().isoformat() > row['expires_at']:
            status = 'EXPIRED'  # lazy expiry: unusable, stock not returned
        return {
            'reservation_id': row['id'],
            'status': status,
            'code': row['product_code'],
            'qty': row['qty'],
            'created_at': row['created_at'],
            'expires_at': row['expires_at'],
        }

    def get_reservation(self, reservation_id: str) -> dict:
        row = self._db.execute(
            'SELECT * FROM reservations WHERE id = ?', (reservation_id,)
        ).fetchone()
        if not row:
            return {'reservation_id': reservation_id, 'status': 'NOT_FOUND'}
        return self._reservation_payload(row)

    def create_order(self, reservation_id: str, customer: str,
                    idempotency_key: str) -> dict:
        self._db.execute('BEGIN IMMEDIATE')
        try:
            # Idempotency: same key -> same order, no second row.
            prior = self._db.execute(
                'SELECT * FROM orders WHERE idem_key = ?', (idempotency_key,)
            ).fetchone()
            if prior:
                self._db.execute('ROLLBACK')
                out = self._order_payload(prior)
                out['replayed'] = True
                return out

            reservation = self._db.execute(
                'SELECT * FROM reservations WHERE id = ?', (reservation_id,)
            ).fetchone()
            if not reservation:
                self._db.execute('ROLLBACK')
                return {'status': 'REJECTED', 'reason': 'RESERVA_NO_EXISTE',
                        'reservation_id': reservation_id}

            # A different key over an already-consumed reservation is a
            # genuine duplicate attempt, not a retry: rejected.
            if reservation['status'] == 'CONSUMED':
                self._db.execute('ROLLBACK')
                return {'status': 'REJECTED', 'reason': 'RESERVA_YA_CONSUMIDA',
                        'reservation_id': reservation_id}
            if utcnow().isoformat() > reservation['expires_at']:
                self._db.execute('ROLLBACK')
                return {'status': 'REJECTED', 'reason': 'RESERVA_CADUCADA',
                        'reservation_id': reservation_id}

            product = self._db.execute(
                'SELECT * FROM products WHERE code = ?',
                (reservation['product_code'],),
            ).fetchone()

            order_id = self._next_id('PED')
            total = product['price_cents'] * reservation['qty']
            self._db.execute(
                'UPDATE reservations SET status = ? WHERE id = ?',
                ('CONSUMED', reservation_id),
            )
            self._db.execute(
                'INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)',
                (order_id, idempotency_key, reservation_id,
                 reservation['product_code'], reservation['qty'], customer,
                 total, iso(utcnow())),
            )
            self._db.execute('COMMIT')

            logger.info('Pedido %s creado (reserva %s, cliente %s)',
                        order_id, reservation_id, customer)
            return {
                'status': 'CREATED',
                'order_id': order_id,
                'reservation_id': reservation_id,
                'code': reservation['product_code'],
                'name': product['name'],
                'qty': reservation['qty'],
                'customer': customer,
                'total_eur': round(total / 100, 2),
            }
        except Exception:
            self._db.execute('ROLLBACK')
            raise

    def _order_payload(self, row: sqlite3.Row) -> dict:
        return {
            'status': 'CREATED',
            'order_id': row['id'],
            'reservation_id': row['reservation_id'],
            'code': row['product_code'],
            'qty': row['qty'],
            'customer': row['customer'],
            'total_eur': round(row['total_cents'] / 100, 2),
        }

    def get_order(self, order_id: str) -> dict:
        row = self._db.execute(
            'SELECT * FROM orders WHERE id = ?', (order_id,)
        ).fetchone()
        if not row:
            return {'order_id': order_id, 'status': 'NOT_FOUND'}
        return self._order_payload(row)

    # ------------------------------------------------------------------
    # admin
    # ------------------------------------------------------------------
    def set_stock(self, code: str, units: int) -> dict:
        cursor = self._db.execute(
            'UPDATE products SET stock = ? WHERE code = ?',
            (int(units), code),
        )
        if cursor.rowcount == 0:
            return {'ok': False, 'error': f'Unknown product {code}'}
        self._db.commit()
        return {'ok': True, 'code': code, 'stock': int(units)}

    def admin_stats(self) -> dict:
        reservations = self._db.execute(
            'SELECT COUNT(*) AS n FROM reservations'
        ).fetchone()['n']
        orders = self._db.execute(
            'SELECT COUNT(*) AS n FROM orders'
        ).fetchone()['n']
        stock = {
            row['code']: row['stock']
            for row in self._db.execute('SELECT code, stock FROM products')
        }
        return {
            'reservations': reservations,
            'orders': orders,
            'stock': stock,
        }

    def reset(self) -> None:
        """Keeps products, drops reservations/orders and reseeds stock."""
        self._db.execute('BEGIN IMMEDIATE')
        try:
            self._db.execute('DELETE FROM reservations')
            self._db.execute('DELETE FROM orders')
            for code, _, _, _, _, stock in SEED_PRODUCTS:
                self._db.execute(
                    'UPDATE products SET stock = ? WHERE code = ?',
                    (stock, code),
                )
            self._db.execute('COMMIT')
        except Exception:
            self._db.execute('ROLLBACK')
            raise


def build_server(shop: Shop) -> FastMCP:
    """Wires the Shop into FastMCP tools. Writes share one asyncio lock."""
    mcp = FastMCP('shop', stateless_http=True, json_response=True)
    lock = asyncio.Lock()

    def unlock_wrap(write_fn):
        async def wrapper(*args, **kwargs):
            async with lock:  # serialise writes within this process
                return await asyncio.to_thread(write_fn, *args, **kwargs)
        return wrapper

    @mcp.tool()
    async def search_stock(query: str = '', category: str = '') -> dict:
        """Busca productos; query coincide con nombre/tags/categoría/código."""
        return shop.search_stock(query, category)

    @mcp.tool()
    async def get_product(code: str) -> dict:
        """Devuelve un producto por código."""
        return shop.get_product(code)

    reserve = unlock_wrap(shop.reserve_stock)

    @mcp.tool()
    async def reserve_stock(code: str, qty: int, idempotency_key: str) -> dict:
        """Reserva unidades de forma atómica e idempotente."""
        return await reserve(code, qty, idempotency_key)

    @mcp.tool()
    async def get_reservation(reservation_id: str) -> dict:
        """Devuelve el estado de una reserva (CONFIRMED/CONSUMED/EXPIRED)."""
        return shop.get_reservation(reservation_id)

    create = unlock_wrap(shop.create_order)

    @mcp.tool()
    async def create_order(reservation_id: str, customer: str,
                          idempotency_key: str) -> dict:
        """Convierte una reserva confirmada en pedido (atómico e idempotente)."""
        return await create(reservation_id, customer, idempotency_key)

    @mcp.tool()
    async def get_order(order_id: str) -> dict:
        """Devuelve un pedido por su identificador."""
        return shop.get_order(order_id)

    @mcp.tool()
    async def set_stock(code: str, units: int) -> dict:
        """[admin] Fija el stock de un producto (para pruebas)."""
        return shop.set_stock(code, units)

    @mcp.tool()
    async def admin_stats() -> dict:
        """[admin] Contadores de reservas/pedidos y stock por código."""
        return shop.admin_stats()

    @mcp.tool()
    async def reset_shop() -> dict:
        """[admin] Borra reservas y pedidos, y restaura el stock inicial."""
        async with lock:
            await asyncio.to_thread(shop.reset)
        return {'ok': True}

    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=os.getenv('SHOP_DB', DEFAULT_DB))
    parser.add_argument('--host', default=os.getenv('MCP_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('MCP_PORT', '9996')))
    parser.add_argument(
        '--reset', action='store_true',
        help='Delete the DB file and start from the seed inventory.',
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='[mcp] %(asctime)s %(levelname)s %(message)s',
    )
    args = parse_args()

    if args.reset and os.path.exists(args.db):
        for suffix in ('', '-wal', '-shm'):
            try:
                os.remove(args.db + suffix)
            except FileNotFoundError:
                pass
        logger.info('Deleted %s (fresh start)', args.db)

    shop = Shop(args.db)
    shop.seed_if_empty()
    mcp = build_server(shop)
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.streamable_http_path = '/mcp'
    logger.info(
        'Serving %d tools at http://%s:%d/mcp (db=%s)',
        10, args.host, args.port, args.db,
    )
    uvicorn.run(mcp.streamable_http_app(), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
