# --- parser.py ---
import asyncio
import json
import random
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Any
from curl_cffi.requests import AsyncSession
from config import TIMEOUT, PROXY_LIST

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IMPERSONATE_POOL = [
    "chrome120",
    "chrome119",
    "chrome116",
    "safari17_0",
    "edge101",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.9,ru;q=0.8",
    "en-CA,en;q=0.9",
]

REFERERS = [
    "https://inferno-cookies.com/",
    "https://inferno-cookies.com/products",
    "https://inferno-cookies.com/shop",
]


def _build_headers() -> Dict[str, str]:
    return {
        "Accept":             "application/json, text/html,*/*;q=0.8",
        "Accept-Language":    random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding":    "gzip, deflate, br",
        "Cache-Control":      "no-cache",
        "Pragma":             "no-cache",
        "Referer":            random.choice(REFERERS),
        "Sec-Ch-Ua":          '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile":   "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest":     "empty",
        "Sec-Fetch-Mode":     "cors",
        "Sec-Fetch-Site":     "same-origin",
        "Connection":         "keep-alive",
        "DNT":                "1",
    }


class SiteParser:
    def __init__(self, site_config: Dict[str, Any]):
        self.config         = site_config
        self.session: Optional[AsyncSession] = None
        self._request_count = 0
        self._rotate_after  = random.randint(10, 18)
        self._last_etag:    Optional[str] = None
        self._last_content: Optional[str] = None

    async def _new_session(self):
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass
        fingerprint  = random.choice(IMPERSONATE_POOL)
        self.session = AsyncSession(impersonate=fingerprint)
        self._request_count = 0
        self._rotate_after  = random.randint(10, 18)
        logger.debug(f"Session → {fingerprint} | direct")

    async def init(self):
        await self._new_session()
        await self._warm()

    async def _warm(self):
        try:
            base_url = self.config.get("base_url", "")
            await self.session.get(base_url, headers=_build_headers(), timeout=15)
            logger.debug(f"Warmed: {base_url}")
        except Exception as e:
            logger.debug(f"Warm failed (non-critical): {e}")

    async def close(self):
        if self.session:
            await self.session.close()

    async def _fetch(
        self,
        url: str,
        method: str = "GET",
        retries: int = 3,
        **kwargs
    ) -> Optional[str]:
        self._request_count += 1
        if self._request_count >= self._rotate_after:
            await self._new_session()
            await self._warm()

        headers = _build_headers()
        if self._last_etag:
            headers["If-None-Match"] = self._last_etag

        for attempt in range(retries):
            try:
                resp = await self.session.request(
                    method, url,
                    timeout=TIMEOUT,
                    headers=headers,
                    **kwargs
                )

                if resp.status_code == 304 and self._last_content:
                    return self._last_content

                if resp.status_code == 200:
                    etag = resp.headers.get("ETag") or resp.headers.get("etag")
                    if etag:
                        self._last_etag    = etag
                        self._last_content = resp.text
                    return resp.text

                if resp.status_code in (429, 503):
                    wait = 2.0 * (attempt + 1)
                    logger.warning(f"Rate limited ({resp.status_code}) — wait {wait}s")
                    await asyncio.sleep(wait)
                    await self._new_session()
                    continue

                if resp.status_code in (403, 407):
                    logger.warning(f"Blocked ({resp.status_code}) — new session")
                    await self._new_session()
                    continue

                logger.warning(f"Status {resp.status_code}: {url}")
                return None

            except asyncio.TimeoutError:
                logger.error(f"Timeout attempt {attempt + 1}/{retries}")
                await self._new_session()

            except Exception as e:
                logger.error(f"Fetch error: {e}")
                await self._new_session()

        logger.error(f"All {retries} attempts failed: {url}")
        return None

    async def get_all_products(self) -> List[Dict[str, Any]]:
        url     = f"{self.config['base_url']}{self.config['product_endpoint']}"
        content = await self._fetch(url)
        if not content:
            return []
        return (
            self._parse_json(content)
            if self.config["parser_type"] == "json"
            else self._parse_html(content)
        )

    def _parse_json(self, content: str) -> List[Dict[str, Any]]:
        try:
            data  = json.loads(content)
            items = (
                data if isinstance(data, list)
                else (
                    data.get("products") or data.get("data") or data.get("items") or []
                ) if isinstance(data, dict)
                else []
            )
            products = []
            for item in items:
                in_stock = item.get("in_stock", item.get("available_count", 0) > 0)
                product  = {
                    "name":            item.get("name", item.get("title", "Unknown")),
                    "id":              item.get("id", item.get("sku", "")),
                    "price":           float(item.get("price", 0)),
                    "in_stock":        in_stock,
                    "available_count": int(item.get("available_count", 0)),
                    "url":             item.get("url", ""),
                    "image":           item.get("image", item.get("image_url", "")),
                    "raw":             item,
                }
                if product["name"]:
                    products.append(product)
            return products
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return []

    def _parse_html(self, content: str) -> List[Dict[str, Any]]:
        try:
            soup     = BeautifulSoup(content, "html.parser")
            products = []
            for elem in soup.select("[data-product], .product-item, .product-card"):
                try:
                    name_elem  = elem.select_one("h2, .product-name, [data-name]")
                    price_elem = elem.select_one(".price, [data-price], .product-price")
                    sku_elem   = elem.select_one("[data-sku], .sku")
                    stock_elem = elem.select_one("[data-stock], .in-stock, .stock-status")
                    product = {
                        "name":            name_elem.get_text(strip=True) if name_elem else "Unknown",
                        "id":              sku_elem.get_text(strip=True) if sku_elem else "",
                        "price":           self._extract_price(price_elem.get_text(strip=True)) if price_elem else 0,
                        "in_stock":        stock_elem is not None,
                        "available_count": 1 if stock_elem else 0,
                        "url":             elem.get("href", ""),
                        "image":           "",
                    }
                    if product["name"] and product["name"] != "Unknown":
                        products.append(product)
                except Exception as e:
                    logger.debug(f"Element parse error: {e}")
            return products
        except Exception as e:
            logger.error(f"HTML parse error: {e}")
            return []

    @staticmethod
    def _extract_price(price_str: str) -> float:
        import re
        match = re.search(r"[\d.,]+", price_str.replace(",", "."))
        if match:
            try:
                return float(match.group())
            except Exception:
                return 0.0
        return 0.0

    async def search_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        all_products = await self.get_all_products()
        search_lower = product_name.lower()
        for p in all_products:
            if p["name"].lower() == search_lower:
                return p
        for p in all_products:
            if search_lower in p["name"].lower():
                return p
        return None

    async def check_stock(self, product_name: str) -> Dict[str, Any]:
        product = await self.search_product(product_name)
        if product:
            return {
                "found":           True,
                "in_stock":        product["in_stock"],
                "available_count": product.get("available_count", 0),
                "product":         product,
            }
        return {"found": False, "in_stock": False, "available_count": 0}