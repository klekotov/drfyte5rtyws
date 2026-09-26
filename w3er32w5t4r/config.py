# --- config.py ---
import os

# Telegram
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не установлен!")

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///autobuy.db")

INFERNO_EMAIL    = os.getenv("INFERNO_EMAIL", "")
INFERNO_PASSWORD = os.getenv("INFERNO_PASSWORD", "")

# Sites
SITES_CONFIG = {
    "inferno_cookies": {
        "name":                "Inferno Cookies",
        "base_url":            "https://inferno-cookies.com",
        "product_endpoint":    "/api/v1/products",
        "categories_endpoint": "/api/v1/categories",
        "orders_endpoint":     "/api/v1/orders",
        "requires_auth":       False,
        "parser_type":         "json"
    }
}

CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "0.01"))
TIMEOUT        = float(os.getenv("TIMEOUT", "10.0"))

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

PROXY_LIST = []