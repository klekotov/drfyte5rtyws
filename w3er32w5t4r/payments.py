# --- payments.py ---
import asyncio
import logging
from typing import Dict, Optional, Any, List
from datetime import datetime
from curl_cffi.requests import AsyncSession
from config import DEFAULT_HEADERS, TIMEOUT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALLOWED_PRODUCTS = {
    "MM2 SQUAD",
    "Blade Ball Squad",
    "JailBreak Squad",
    "Adopt Me Squad",
}


class InfernoCookiesPayment:
    BASE_URL          = "https://inferno-cookies.com"
    LOGIN_ENDPOINT    = "/api/v1/auth/login"
    ORDERS_ENDPOINT   = "/api/v1/orders"
    PRODUCTS_ENDPOINT = "/api/v1/products"

    def __init__(self):
        self.session: Optional[AsyncSession] = None
        self.token:   Optional[str]          = None

    async def init(self):
        self.session = AsyncSession(impersonate="chrome120")

    async def close(self):
        if self.session:
            await self.session.close()

    async def authenticate(self, email: str, password: str) -> bool:
        try:
            resp = await self.session.post(
                f"{self.BASE_URL}{self.LOGIN_ENDPOINT}",
                json={"email": email, "password": password},
                headers={
                    "Content-Type": "application/json",
                    "Origin":       self.BASE_URL,
                    "Referer":      f"{self.BASE_URL}/",
                    "User-Agent":   DEFAULT_HEADERS["User-Agent"],
                },
                timeout=TIMEOUT
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                self.token = (
                    data.get('access_token') or
                    data.get('token')        or
                    data.get('jwt')
                )
                if self.token:
                    logger.info("✓ Authenticated")
                    return True
                logger.error(f"Token not found: {data}")
                return False
            logger.error(f"Auth failed: {resp.status_code} - {resp.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False

    async def get_catalog(self, email: str, password: str) -> Dict[str, Any]:
        """Public entry — used by /buy to show UI buttons. Filters to ALLOWED_PRODUCTS."""
        await self.init()
        try:
            auth_ok = await self.authenticate(email, password)
            if not auth_ok:
                return {'success': False, 'error': 'Authentication failed', 'items': {}}

            resp = await self.session.get(
                f"{self.BASE_URL}{self.PRODUCTS_ENDPOINT}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent":    DEFAULT_HEADERS["User-Agent"],
                },
                timeout=TIMEOUT
            )

            if resp.status_code == 200:
                data  = resp.json()
                items = await self._parse_catalog_response(data)
                # UI-only filter — buttons show only the four known products
                items = {k: v for k, v in items.items() if k in ALLOWED_PRODUCTS}
                return {'success': True, 'items': items}

            return {'success': False, 'error': f"Status {resp.status_code}", 'items': {}}

        except Exception as e:
            logger.error(f"Catalog error: {e}")
            return {'success': False, 'error': str(e), 'items': {}}
        finally:
            await self.close()

    async def _parse_catalog_response(self, data) -> Dict[str, str]:
        items: Dict[str, str] = {}
        if isinstance(data, list):
            for p in data:
                name = p.get('name') or p.get('title') or str(p.get('id'))
                pid  = p.get('id')   or p.get('product_id')
                if name and pid:
                    items[name] = str(pid)
        elif isinstance(data, dict):
            for p in (data.get('products') or data.get('data') or data.get('items') or []):
                name = p.get('name') or p.get('title') or str(p.get('id'))
                pid  = p.get('id')   or p.get('product_id')
                if name and pid:
                    items[name] = str(pid)
        return items

    async def get_catalog_in_session(self) -> Dict[str, str]:
        """
        Uses already-open authenticated session.
        Returns FULL catalog — no filter. Custom names need to resolve against everything.
        """
        if not self.token:
            return {}
        try:
            resp = await self.session.get(
                f"{self.BASE_URL}{self.PRODUCTS_ENDPOINT}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent":    DEFAULT_HEADERS["User-Agent"],
                },
                timeout=TIMEOUT
            )
            if resp.status_code != 200:
                logger.error(f"Catalog in-session failed: {resp.status_code}")
                return {}
            return await self._parse_catalog_response(resp.json())
        except Exception as e:
            logger.error(f"In-session catalog error: {e}")
            return {}

    async def place_order(self, product_ids: List[str]) -> Dict[str, Any]:
        if not self.token:
            return {'success': False, 'error': 'Not authenticated'}
        try:
            resp = await self.session.post(
                f"{self.BASE_URL}{self.ORDERS_ENDPOINT}",
                json={"product_ids": product_ids},
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {self.token}",
                    "Origin":        self.BASE_URL,
                    "Referer":       f"{self.BASE_URL}/",
                    "User-Agent":    DEFAULT_HEADERS["User-Agent"],
                },
                timeout=TIMEOUT
            )
            if resp.status_code in (200, 201):
                result   = resp.json()
                order_id = result.get('order_id')
                logger.info(f"✓ ORDER: {order_id} | {len(product_ids)} items")
                return {
                    'success':                  True,
                    'order_id':                 order_id,
                    'bundle_download_url':      result.get('bundle_download_url'),
                    'bundle_download_filename': result.get('bundle_download_filename'),
                    'purchased_qty':            len(product_ids),
                    'timestamp':                datetime.now().isoformat()
                }
            elif resp.status_code == 401:
                self.token = None
                return {'success': False, 'error': 'Token expired'}
            logger.error(f"Order failed: {resp.status_code} - {resp.text[:300]}")
            return {
                'success': False,
                'error':   f"Status {resp.status_code}",
                'detail':  resp.text[:200],
                'insufficient_funds': resp.status_code in (402, 400) and (
                    'balance' in resp.text.lower() or
                    'insufficient' in resp.text.lower() or
                    'funds' in resp.text.lower()
                )
            }
        except asyncio.TimeoutError:
            return {'success': False, 'error': 'Timeout'}
        except Exception as e:
            logger.error(f"Order error: {e}")
            return {'success': False, 'error': str(e)}

    async def execute_purchase_with_shrink(
        self,
        product_ids: List[str],
        max_retries: int = 5,
        shrink_step: int = 1
    ) -> Dict[str, Any]:
        ids      = list(product_ids)
        attempts = 0

        while ids and attempts < max_retries:
            result = await self.place_order(ids)
            if result['success']:
                result['final_qty'] = len(ids)
                return result

            if result.get('insufficient_funds'):
                attempts += 1
                drop = max(shrink_step, len(ids) // 10)
                ids  = ids[:-drop]
                logger.warning(
                    f"Insufficient funds — shrinking to {len(ids)} items (attempt {attempts}/{max_retries})"
                )
                continue

            result['final_qty'] = 0
            return result

        if not ids:
            return {'success': False, 'error': 'Shrunk to zero — balance too low for even 1 item', 'final_qty': 0}

        return {'success': False, 'error': f'Max retries ({max_retries}) hit', 'final_qty': 0}

    async def execute_purchase(self, product_ids: List[str], email: str, password: str) -> Dict[str, Any]:
        await self.init()
        try:
            auth_ok = await self.authenticate(email, password)
            if not auth_ok:
                return {'success': False, 'error': 'Authentication failed'}
            return await self.execute_purchase_with_shrink(product_ids)
        finally:
            await self.close()

    async def resolve_and_purchase(
        self,
        product_names_qty: Dict[str, int],
        email: str,
        password: str
    ) -> Dict[str, Any]:
        """
        1. Auth
        2. GET /api/v1/products — full catalog, no filter
        3. Match names → UUIDs (exact then partial)
        4. Build product_ids = [uuid] * qty
        5. POST /api/v1/orders with shrink on insufficient funds
        """
        await self.init()
        try:
            auth_ok = await self.authenticate(email, password)
            if not auth_ok:
                return {
                    'success': False, 'error': 'Authentication failed',
                    'resolved': {}, 'unresolved': list(product_names_qty.keys())
                }

            catalog = await self.get_catalog_in_session()
            if not catalog:
                return {
                    'success': False, 'error': 'Catalog empty or unreachable',
                    'resolved': {}, 'unresolved': list(product_names_qty.keys())
                }

            catalog_lower = {k.lower(): (k, v) for k, v in catalog.items()}
            resolved:   Dict[str, str] = {}
            unresolved: List[str]      = []

            for name in product_names_qty:
                nl = name.lower()
                if nl in catalog_lower:
                    resolved[name] = catalog_lower[nl][1]
                else:
                    match = next(
                        (v for ck, (_, v) in catalog_lower.items() if nl in ck),
                        None
                    )
                    if match:
                        resolved[name] = match
                    else:
                        unresolved.append(name)

            if not resolved:
                return {
                    'success':    False,
                    'error':      f"UUID not found for: {', '.join(unresolved)}",
                    'resolved':   {},
                    'unresolved': unresolved
                }

            product_ids: List[str] = []
            for name, uuid in resolved.items():
                product_ids.extend([uuid] * product_names_qty[name])

            result = await self.execute_purchase_with_shrink(product_ids)
            result['resolved']   = resolved
            result['unresolved'] = unresolved
            return result

        finally:
            await self.close()