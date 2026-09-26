import aiosqlite
from typing import Optional, List, Dict, Any


class Database:
    def __init__(self, db_path: str = "autobuy.db"):
        self.db_path = db_path

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS watched_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    site_key TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    product_sku TEXT,
                    target_price REAL,
                    buy_qty INTEGER NOT NULL DEFAULT 1,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                CREATE TABLE IF NOT EXISTS products_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_key TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    product_sku TEXT,
                    price REAL,
                    in_stock BOOLEAN DEFAULT 0,
                    available_count INTEGER DEFAULT 0,
                    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(site_key, product_sku)
                );
                CREATE TABLE IF NOT EXISTS purchase_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    site_key TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    price REAL,
                    status TEXT,
                    order_id TEXT,
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            try:
                await conn.execute(
                    "ALTER TABLE watched_products ADD COLUMN buy_qty INTEGER NOT NULL DEFAULT 1"
                )
            except Exception:
                pass
            await conn.commit()

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
            await conn.commit()

    async def add_user(self, user_id: int, username: str):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            await conn.commit()

    async def add_watched_product(
        self,
        user_id: int,
        site_key: str,
        product_name: str,
        product_sku: Optional[str] = None,
        target_price: Optional[float] = None,
        buy_qty: int = 1
    ) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "INSERT INTO watched_products "
                "(user_id, site_key, product_name, product_sku, target_price, buy_qty) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, site_key, product_name, product_sku, target_price, buy_qty)
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_watched_products(
        self,
        user_id: int,
        status: str = "active"
    ) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM watched_products WHERE user_id = ? AND status = ?",
                (user_id, status)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def update_product_status(self, product_id: int, status: str):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE watched_products SET status = ? WHERE id = ?",
                (status, product_id)
            )
            await conn.commit()

    async def update_product_cache(
        self,
        site_key: str,
        product_name: str,
        product_sku: str,
        price: float,
        in_stock: bool,
        available_count: int = 0
    ):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                INSERT INTO products_cache
                    (site_key, product_name, product_sku, price, in_stock, available_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_key, product_sku)
                DO UPDATE SET
                    price=excluded.price,
                    in_stock=excluded.in_stock,
                    available_count=excluded.available_count,
                    last_checked=CURRENT_TIMESTAMP
            """, (site_key, product_name, product_sku, price, in_stock, available_count))
            await conn.commit()

    async def get_cached_products(self, site_key: str) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM products_cache WHERE site_key = ?", (site_key,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def add_purchase(
        self,
        user_id: int,
        site_key: str,
        product_name: str,
        price: float,
        status: str,
        order_id: Optional[str] = None
    ):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO purchase_history "
                "(user_id, site_key, product_name, price, status, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, site_key, product_name, price, status, order_id)
            )
            await conn.commit()

    async def remove_watched_product(self, product_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE watched_products SET status = 'inactive' WHERE id = ?",
                (product_id,)
            )
            await conn.commit()

    async def clear_purchase_history(self, user_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "DELETE FROM purchase_history WHERE user_id = ?",
                (user_id,)
            )
            await conn.commit()

    async def get_purchase_history(
        self,
        user_id: int,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM purchase_history "
                "WHERE user_id = ? ORDER BY purchased_at DESC LIMIT ?",
                (user_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]