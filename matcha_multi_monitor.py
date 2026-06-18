import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from plyer import notification
except Exception:
    notification = None

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    def load_dotenv_fallback() -> None:
        env_paths = [
            os.path.join(os.getcwd(), ".env"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ]
        loaded_paths = set()

        for env_path in env_paths:
            if env_path in loaded_paths or not os.path.exists(env_path):
                continue

            loaded_paths.add(env_path)

            with open(env_path, "r", encoding="utf-8-sig") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export ") :].lstrip()
                    if "=" not in line:
                        continue

                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if not key:
                        continue
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ["'", '"']:
                        value = value[1:-1]

                    os.environ.setdefault(key, value)

    load_dotenv_fallback()

try:
    import msvcrt
except Exception:
    msvcrt = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRUE_VALUES = {"true", "1", "yes", "y", "on"}
FALSE_VALUES = {"false", "0", "no", "n", "off"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,zh-TW;q=0.7",
    "Connection": "keep-alive",
}

SITE_DEFAULTS = {
    "horii": {
        "site_name": "Horii Shichimeien",
        "product_url": "https://horiishichimeien.com/collections/all?selected=%E6%8A%B9%E8%8C%B6",
        "login_url": "https://horiishichimeien.com/account/login",
        "shopify_products_json_url": "https://horiishichimeien.com/collections/%E6%8A%B9%E8%8C%B6/products.json?limit=250",
        "use_shopify_products_json": "true",
        "target_product_names": "",
        "target_product_urls": "",
        "product_link_href_parts": "/products/",
        "shipping_check_urls": (
            "https://horiishichimeien.com/cart,"
            "https://horiishichimeien.com/pages/international-orders,"
            "https://horiishichimeien.com/pages/shipping"
        ),
    },
    "koyamaen": {
        "site_name": "Marukyu Koyamaen",
        "product_url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha/principal",
        "login_url": "https://www.marukyu-koyamaen.co.jp/english/shop/account",
        "shopify_products_json_url": "",
        "use_shopify_products_json": "false",
        "target_product_names": "",
        "target_product_urls": "",
        "product_link_href_parts": "/english/shop/products/",
        "shipping_check_urls": (
            "https://www.marukyu-koyamaen.co.jp/english/shop/cart,"
            "https://www.marukyu-koyamaen.co.jp/english/shop/account/account-addresses"
        ),
    },
    "ippodo": {
        "site_name": "Ippodo Tea Global",
        "product_url": "https://global.ippodo-tea.co.jp/collections/matcha",
        "login_url": "https://global.ippodo-tea.co.jp/account/login",
        "shopify_products_json_url": "",
        "use_shopify_products_json": "false",
        "target_product_names": "",
        "target_product_urls": (
            "https://global.ippodo-tea.co.jp/collections/matcha/products/matcha5010331,"
            "https://global.ippodo-tea.co.jp/collections/matcha/products/matcha5010431,"
            "https://global.ippodo-tea.co.jp/collections/matcha/products/matcha6105023,"
            "https://global.ippodo-tea.co.jp/collections/matcha/products/matcha5010931,"
            "https://global.ippodo-tea.co.jp/collections/matcha/products/matcha5010831,"
            "https://global.ippodo-tea.co.jp/collections/matcha/products/matcha5018231"
        ),
        "product_link_href_parts": "/products/",
        "shipping_check_urls": (
            "https://global.ippodo-tea.co.jp/cart,"
            "https://global.ippodo-tea.co.jp/account/addresses"
        ),
    },
}


def normalize_product_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def normalize_url_key(value: str) -> str:
    return (value or "").strip().rstrip("/")


def is_koyamaen_url(value: str) -> bool:
    return "marukyu-koyamaen.co.jp" in (value or "").casefold()


def safe_debug_filename_part(product_name: str, product_url: str) -> str:
    normalized_name = re.sub(r"[^a-z0-9]+", "_", (product_name or "").casefold()).strip("_")
    if normalized_name:
        return normalized_name[:80]

    path_bits = [bit for bit in urlparse(product_url or "").path.split("/") if bit]
    slug = path_bits[-1] if path_bits else "product"
    normalized_slug = re.sub(r"[^a-z0-9]+", "_", slug.casefold()).strip("_")
    return (normalized_slug or "product")[:80]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def env_bool(name: str, default: str | bool = False) -> bool:
    raw_default = "true" if default is True else "false" if default is False else str(default)
    value = os.getenv(name, raw_default).strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return raw_default.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        return float(value)
    except ValueError:
        return default


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def env_list(name: str, default: str = "") -> list[str]:
    return split_csv(os.getenv(name, default))


def prefixed_env(profile: str, key: str) -> str:
    return f"{profile.upper()}_{key}"


def profile_value(profile: str, key: str, default: str = "") -> str:
    env_name = prefixed_env(profile, key.upper())
    return os.getenv(env_name, SITE_DEFAULTS.get(profile, {}).get(key, default))


@dataclass
class SiteConfig:
    profile: str
    enabled: bool
    site_name: str
    product_url: str
    login_url: str
    shopify_products_json_url: str
    use_shopify_products_json: bool
    target_product_names: list[str]
    target_product_urls: list[str]
    product_link_href_parts: list[str]
    shipping_check_urls: list[str]
    login_email: str
    login_password: str
    enable_login: bool

    @property
    def target_product_name_keys(self) -> set[str]:
        return {key for key in (normalize_product_name(name) for name in self.target_product_names) if key}

    @property
    def target_product_url_keys(self) -> set[str]:
        return {key for key in (normalize_url_key(url) for url in self.target_product_urls) if key}


@dataclass(frozen=True)
class AppConfig:
    """All environment-driven settings, read once at startup via from_env()."""

    check_interval_seconds: int
    run_once: bool
    single_instance: bool
    auto_add_to_cart: bool
    alert_on_unknown_stock: bool
    enable_login: bool
    telegram_verify_ssl: bool
    enable_local_notification: bool
    test_telegram: bool
    simulate_in_stock: bool
    debug: bool
    summary_alert_every_run: bool
    alert_when_any_in_stock: bool
    local_notification_title_max_chars: int
    local_notification_message_max_chars: int
    telegram_message_max_chars: int
    target_country_name: str
    target_country_code: str
    verify_ssl: bool
    http_max_retries: int
    request_delay_seconds: float
    loop_jitter_seconds: int
    max_site_workers: int
    log_dir: str
    log_file: str
    lock_file: str
    alert_state_file: str
    log_max_bytes: int
    log_backup_count: int

    @classmethod
    def from_env(cls) -> "AppConfig":
        log_dir = os.getenv("LOG_DIR", os.path.join(BASE_DIR, "logs"))
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(BASE_DIR, log_dir)
        log_file = os.getenv("LOG_FILE", "matcha_multi_monitor.log")
        lock_file = os.getenv("LOCK_FILE", "matcha_multi_monitor.lock")
        alert_state_file = os.getenv("ALERT_STATE_FILE", os.path.join(log_dir, "matcha_alert_state.json"))
        if not os.path.isabs(log_file):
            log_file = os.path.join(BASE_DIR, log_file)
        if not os.path.isabs(lock_file):
            lock_file = os.path.join(BASE_DIR, lock_file)
        if not os.path.isabs(alert_state_file):
            alert_state_file = os.path.join(BASE_DIR, alert_state_file)
        return cls(
            check_interval_seconds=env_int("CHECK_INTERVAL_SECONDS", 600),
            run_once=env_bool("RUN_ONCE", "false"),
            single_instance=env_bool("SINGLE_INSTANCE", "true"),
            auto_add_to_cart=env_bool("AUTO_ADD_TO_CART", "true"),
            alert_on_unknown_stock=env_bool("ALERT_ON_UNKNOWN_STOCK", "false"),
            enable_login=env_bool("ENABLE_LOGIN", "false"),
            telegram_verify_ssl=env_bool("TELEGRAM_VERIFY_SSL", "false"),
            enable_local_notification=env_bool("ENABLE_LOCAL_NOTIFICATION", "true"),
            test_telegram=env_bool("TEST_TELEGRAM", "false"),
            simulate_in_stock=env_bool("SIMULATE_IN_STOCK", "false"),
            debug=env_bool("DEBUG", "FALSE"),
            summary_alert_every_run=env_bool("SUMMARY_ALERT_EVERY_RUN", "false"),
            alert_when_any_in_stock=env_bool("ALERT_WHEN_ANY_IN_STOCK", "true"),
            local_notification_title_max_chars=env_int("LOCAL_NOTIFICATION_TITLE_MAX_CHARS", 63),
            local_notification_message_max_chars=env_int("LOCAL_NOTIFICATION_MESSAGE_MAX_CHARS", 240),
            telegram_message_max_chars=env_int("TELEGRAM_MESSAGE_MAX_CHARS", 3900),
            target_country_name=os.getenv("TARGET_COUNTRY_NAME", "Taiwan").strip() or "Taiwan",
            target_country_code=os.getenv("TARGET_COUNTRY_CODE", "TW").strip() or "TW",
            verify_ssl=env_bool("VERIFY_SSL", "true"),
            http_max_retries=env_int("HTTP_MAX_RETRIES", 3),
            request_delay_seconds=env_float("REQUEST_DELAY_SECONDS", 0.5),
            loop_jitter_seconds=env_int("LOOP_JITTER_SECONDS", 30),
            max_site_workers=env_int("MAX_SITE_WORKERS", 4),
            log_dir=log_dir,
            log_file=log_file,
            lock_file=lock_file,
            alert_state_file=alert_state_file,
            log_max_bytes=env_int("LOG_MAX_BYTES", 5 * 1024 * 1024),
            log_backup_count=env_int("LOG_BACKUP_COUNT", 3),
        )


APP = AppConfig.from_env()
os.makedirs(APP.log_dir, exist_ok=True)

LOCK_HANDLE = None


def configured_profiles() -> list[str]:
    raw_profiles = os.getenv("SITE_PROFILES", "").strip()
    if raw_profiles:
        profiles = [profile.strip().lower() for profile in raw_profiles.split(",") if profile.strip()]
    else:
        legacy_profile = os.getenv("SITE_PROFILE", "").strip().lower()
        profiles = [legacy_profile] if legacy_profile else ["horii", "koyamaen", "ippodo"]

    known = []
    for profile in profiles:
        if profile in SITE_DEFAULTS and profile not in known:
            known.append(profile)
        elif profile:
            write_global_log(f"Unknown SITE profile skipped: {profile}")
    return known


def build_site_config(profile: str) -> SiteConfig:
    defaults = SITE_DEFAULTS[profile]
    login_email = (
        os.getenv(prefixed_env(profile, "LOGIN_EMAIL"), "").strip()
        or os.getenv("LOGIN_EMAIL", "").strip()
        or os.getenv("MATCHA_LOGIN_EMAIL", "").strip()
    )
    login_password = (
        os.getenv(prefixed_env(profile, "LOGIN_PASSWORD"), "")
        or os.getenv("LOGIN_PASSWORD", "")
        or os.getenv("MATCHA_LOGIN_PASSWORD", "")
    )
    use_json_default = profile_value(profile, "use_shopify_products_json", defaults.get("use_shopify_products_json", "false"))

    return SiteConfig(
        profile=profile,
        enabled=env_bool(prefixed_env(profile, "ENABLED"), "true"),
        site_name=profile_value(profile, "site_name", defaults["site_name"]),
        product_url=profile_value(profile, "product_url", defaults["product_url"]),
        login_url=profile_value(profile, "login_url", defaults["login_url"]),
        shopify_products_json_url=profile_value(profile, "shopify_products_json_url", defaults.get("shopify_products_json_url", "")).strip(),
        use_shopify_products_json=env_bool(prefixed_env(profile, "USE_SHOPIFY_PRODUCTS_JSON"), use_json_default),
        target_product_names=env_list(prefixed_env(profile, "TARGET_PRODUCT_NAMES"), defaults.get("target_product_names", "")),
        target_product_urls=env_list(prefixed_env(profile, "TARGET_PRODUCT_URLS"), defaults.get("target_product_urls", "")),
        product_link_href_parts=env_list(prefixed_env(profile, "PRODUCT_LINK_HREF_PARTS"), defaults.get("product_link_href_parts", "")),
        shipping_check_urls=env_list(prefixed_env(profile, "SHIPPING_CHECK_URLS"), defaults.get("shipping_check_urls", "")),
        login_email=login_email,
        login_password=login_password,
        # Per-site override; defaults to the global ENABLE_LOGIN so the old behavior is preserved.
        enable_login=env_bool(prefixed_env(profile, "ENABLE_LOGIN"), APP.enable_login),
    )


def trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _build_log_formatter() -> logging.Formatter:
    return logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def _make_rotating_handler(path: str) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=APP.log_max_bytes, backupCount=APP.log_backup_count, encoding="utf-8"
    )
    handler.setFormatter(_build_log_formatter())
    return handler


class _SafeStreamHandler(logging.StreamHandler):
    """Console handler that degrades gracefully on consoles without UTF-8.

    We do the write ourselves instead of delegating to ``StreamHandler.emit``,
    because that base method swallows ``UnicodeEncodeError`` and routes it to
    ``handleError`` before our handler could retry with a safe encoding.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self.stream
            line = self.format(record) + self.terminator
            try:
                stream.write(line)
            except UnicodeEncodeError:
                encoding = getattr(stream, "encoding", None) or "utf-8"
                stream.write(line.encode(encoding, "replace").decode(encoding, "replace"))
            self.flush()
        except Exception:
            self.handleError(record)


def _prefer_utf8_console() -> None:
    # Modern Windows terminals handle UTF-8; legacy cp950/Big5 consoles cannot encode
    # Japanese-only kanji. Switch to UTF-8 with replacement so output never crashes.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _build_global_logger() -> logging.Logger:
    _prefer_utf8_console()
    logger = logging.getLogger("matcha")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        console = _SafeStreamHandler(sys.stdout)
        console.setFormatter(_build_log_formatter())
        logger.addHandler(console)
        logger.addHandler(_make_rotating_handler(APP.log_file))
    return logger


GLOBAL_LOGGER = _build_global_logger()
_SITE_LOGGERS: dict[str, logging.Logger] = {}


def _site_logger(profile: str) -> logging.Logger:
    logger = _SITE_LOGGERS.get(profile)
    if logger is None:
        logger = logging.getLogger(f"matcha.site.{profile}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(_make_rotating_handler(os.path.join(APP.log_dir, f"{profile}.log")))
        _SITE_LOGGERS[profile] = logger
    return logger


def write_global_log(message: str) -> None:
    GLOBAL_LOGGER.info(message)


def write_site_log(cfg: SiteConfig, message: str) -> None:
    line = f"[{cfg.profile}] {message}"
    GLOBAL_LOGGER.info(line)
    _site_logger(cfg.profile).info(line)


def acquire_single_instance_lock() -> bool:
    global LOCK_HANDLE
    if not APP.single_instance:
        return True

    if msvcrt is None:
        # POSIX environments are usually fine when scheduled by one process. This mirrors the original script behavior.
        write_global_log("Single-instance lock skipped: msvcrt is not available")
        return True

    LOCK_HANDLE = open(APP.lock_file, "a+", encoding="utf-8")
    LOCK_HANDLE.seek(0)
    try:
        msvcrt.locking(LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        write_global_log("Another Matcha Monitor instance is already running. Stop the existing process before starting a new one.")
        LOCK_HANDLE.close()
        LOCK_HANDLE = None
        return False

    LOCK_HANDLE.seek(0)
    LOCK_HANDLE.truncate()
    LOCK_HANDLE.write(f"pid={os.getpid()} started={now_text()}\n")
    LOCK_HANDLE.flush()
    return True


def send_local_notification(title: str, message: str) -> bool:
    if not APP.enable_local_notification:
        write_global_log("Local notification skipped: ENABLE_LOCAL_NOTIFICATION is false")
        return False

    if notification is None:
        write_global_log("Local notification skipped: plyer is not installed")
        return False

    safe_title = trim_text(title, APP.local_notification_title_max_chars)
    safe_message = trim_text(message.replace("\n", " "), APP.local_notification_message_max_chars)
    notification.notify(title=safe_title, message=safe_message, app_name="Matcha Monitor", timeout=15)
    write_global_log(f"Local notification sent: {safe_title} - {safe_message}")
    return True


def telegram_error_hint(status_code: int, description: str) -> str:
    """Human-friendly hint for common Telegram sendMessage failures (channel setup, etc.)."""
    desc = (description or "").lower()
    if status_code == 401:
        return " — TELEGRAM_BOT_TOKEN looks invalid or revoked."
    if status_code == 404:
        return " — bot token path not found; check TELEGRAM_BOT_TOKEN."
    if "chat not found" in desc:
        return " — TELEGRAM_CHAT_ID is wrong, or the bot was never added to the channel."
    if status_code == 403 or "not a member" in desc or "kicked" in desc or "bot was blocked" in desc:
        return " — bot is not a member/admin of the channel (or was blocked). Add it as admin with post rights."
    if "not enough rights" in desc or "need administrator" in desc or "have no rights" in desc:
        return " — bot needs admin / post-messages permission in the channel."
    if "chat_id is empty" in desc or "chat_id" in desc:
        return " — TELEGRAM_CHAT_ID is empty or malformed. Use a numeric id, @public_channel, or -100... id."
    return ""


def send_telegram_alert(title: str, message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        write_global_log("Telegram alert skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = trim_text(f"{title}\n\n{message}", APP.telegram_message_max_chars)
    try:
        response = requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
            verify=APP.telegram_verify_ssl,
        )
    except requests.RequestException as e:
        # NOTE: never log the exception message/URL — it can contain the bot token.
        write_global_log(f"Telegram send failed: request error ({type(e).__name__}).")
        raise RuntimeError(f"Telegram request error: {type(e).__name__}") from None

    if not response.ok:
        description = ""
        try:
            description = (response.json() or {}).get("description", "")
        except ValueError:
            description = trim_text(response.text, 200)
        hint = telegram_error_hint(response.status_code, description)
        # Telegram's JSON description does not contain the token; safe to log.
        write_global_log(f"Telegram send failed: HTTP {response.status_code} {description}{hint}")
        raise RuntimeError(f"Telegram HTTP {response.status_code}: {description}") from None

    write_global_log("Telegram alert sent")
    return True


def send_alert(title: str, message: str) -> bool:
    write_global_log("=" * 80)
    write_global_log(title)
    write_global_log(message)
    sent = False
    try:
        sent = send_local_notification(title, message) or sent
    except Exception as e:
        write_global_log(f"Local notification failed: {e}")
    try:
        sent = send_telegram_alert(title, message) or sent
    except Exception as e:
        write_global_log(f"Telegram alert failed: {e}")
    return sent


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=APP.http_max_retries,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_page(session: requests.Session, cfg: SiteConfig, url: str) -> str:
    write_site_log(cfg, f"Fetching: {url}")
    response = session.get(url, headers=HEADERS, timeout=30, verify=APP.verify_ssl)
    response.raise_for_status()
    write_site_log(cfg, f"Fetched HTML length: {len(response.text)}")
    return response.text


def fetch_json(session: requests.Session, cfg: SiteConfig, url: str) -> dict:
    write_site_log(cfg, f"Fetching JSON: {url}")
    headers = {**HEADERS, "Accept": "application/json,text/javascript,*/*;q=0.8"}
    response = session.get(url, headers=headers, timeout=30, verify=APP.verify_ssl)
    response.raise_for_status()
    data = response.json()
    keys = ", ".join(data.keys()) if isinstance(data, dict) else type(data).__name__
    write_site_log(cfg, f"Fetched JSON keys: {keys}")
    return data


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True)


def save_koyamaen_unknown_html(cfg: SiteConfig, product_url: str, html: str, result: dict) -> None:
    if cfg.profile != "koyamaen" or not APP.debug:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"koyamaen_unknown_{timestamp}.html"
    path = os.path.join(APP.log_dir, filename)
    sample = trim_text(result.get("page_text_sample") or page_text(html), 500)
    try:
        os.makedirs(APP.log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html or "")
        write_site_log(cfg, f"Koyamaen UNKNOWN HTML saved: {path}")
    except Exception as e:
        write_site_log(cfg, f"Koyamaen UNKNOWN HTML save failed: {e}")
    write_site_log(cfg, f"Koyamaen UNKNOWN page_text_sample: {sample}")


def save_koyamaen_debug_detail_html(product_url: str, product_name: str, html: str) -> None:
    if not APP.debug or not is_koyamaen_url(product_url):
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename_part = safe_debug_filename_part(product_name, product_url)
    filename = f"koyamaen_debug_{filename_part}_{timestamp}.html"
    path = os.path.join(APP.log_dir, filename)
    try:
        os.makedirs(APP.log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html or "")
        write_global_log(f"[koyamaen] DEBUG detail HTML saved: {path}")
    except Exception as e:
        write_global_log(f"[koyamaen] DEBUG detail HTML save failed for {product_url}: {e}")


# ============================================================
# Member login
# ============================================================


def find_login_form(soup: BeautifulSoup):
    candidates = []
    for form in soup.select("form"):
        has_password = form.select_one(
            "input[type='password'], input[name='password'], input[name='pwd'], input[name='customer[password]']"
        )
        has_login_id = form.select_one(
            "input[name='username'], input[name='email'], input[name='customer[email]'], "
            "input[name='user_login'], input[type='email'], input[type='text']"
        )
        if not has_password or not has_login_id:
            continue

        form_bits = " ".join(
            value or ""
            for value in [form.get("id"), form.get("name"), " ".join(form.get("class", [])), form.get("action"), form.get_text(" ", strip=True)]
        ).lower()
        score = 1
        if "login" in form_bits or "log in" in form_bits:
            score += 2
        candidates.append((score, form))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def set_form_field(data: dict, form, selectors: str, value: str, fallback_name: str) -> None:
    field = form.select_one(selectors)
    field_name = field.get("name") if field else fallback_name
    if field_name:
        data[field_name] = value


def collect_input_fields(form, *, skip_noninput_types: bool = False, quantity_default: bool = False) -> dict:
    data = {}
    for input_tag in form.select("input[name]"):
        input_type = (input_tag.get("type") or "text").lower()
        name = input_tag.get("name")
        value = input_tag.get("value") or ""
        if skip_noninput_types and input_type in ["button", "file", "image", "reset"]:
            continue
        if input_type in ["checkbox", "radio"] and not input_tag.has_attr("checked"):
            continue
        if quantity_default and name == "quantity" and not value:
            value = "1"
        data[name] = value
    return data


def collect_select_fields(form) -> dict:
    data = {}
    for select in form.select("select[name]"):
        selected = select.select_one("option[selected]:not([disabled])")
        fallback = select.select_one("option[value]:not([disabled])")
        option = selected or fallback
        if option:
            data[select.get("name")] = option.get("value") or option.get_text(strip=True)
    return data


def build_login_form_data(form, cfg: SiteConfig) -> dict:
    data = collect_input_fields(form, skip_noninput_types=True)
    data.update(collect_select_fields(form))

    for textarea in form.select("textarea[name]"):
        data[textarea.get("name")] = textarea.get_text()

    submit_button = form.select_one("button[name], input[type='submit'][name]")
    if submit_button:
        submit_name = submit_button.get("name")
        if submit_name and submit_name not in data:
            data[submit_name] = submit_button.get("value") or submit_button.get_text(strip=True)

    set_form_field(
        data,
        form,
        "input[name='username'], input[name='email'], input[name='customer[email]'], "
        "input[name='user_login'], input[type='email'], input[type='text']",
        cfg.login_email,
        "username",
    )
    set_form_field(
        data,
        form,
        "input[name='password'], input[name='pwd'], input[name='customer[password]'], input[type='password']",
        cfg.login_password,
        "password",
    )

    remember_field = form.select_one("input[name='rememberme']")
    if remember_field and "rememberme" not in data:
        data["rememberme"] = remember_field.get("value") or "forever"
    return data


def is_logged_in_account_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text_lower = soup.get_text(" ", strip=True).lower()
    if soup.select('a[href*="customer-logout"], a[href*="logout"]'):
        return True
    if find_login_form(soup):
        return False
    logged_in_keywords = [
        "log out",
        "logout",
        "account details",
        "edit account",
        "dashboard",
        "orders",
        "addresses",
        "ログアウト",
        "注文履歴",
        "アカウント詳細",
    ]
    return any(keyword in text_lower for keyword in logged_in_keywords)


def login_failure_reason(html: str) -> str:
    text_lower = page_text(html).lower()
    if "missing captcha token" in text_lower:
        return "Shopify requires a CAPTCHA token for automated login"
    if "captcha" in text_lower or "recaptcha" in text_lower:
        return "captcha challenge may be required"
    failure_keywords = [
        "incorrect password",
        "invalid username",
        "unknown email",
        "not registered",
        "login failed",
        "invalid login credentials",
        "error",
        "正しくありません",
        "一致しません",
        "エラー",
    ]
    if any(keyword in text_lower for keyword in failure_keywords):
        return "site returned a login error"
    if find_login_form(BeautifulSoup(html, "html.parser")):
        return "still on login form after submit"
    return "login success could not be confirmed"


def safe_login_form_fields(data: dict) -> dict:
    sensitive_names = ("password", "username", "email", "login", "user")
    return {key: ("***" if any(name in key.lower() for name in sensitive_names) else value) for key, value in data.items()}


def login_to_member_account(session: requests.Session, cfg: SiteConfig) -> dict:
    if not cfg.enable_login:
        return {"status": "DISABLED", "message": "Member login is disabled."}
    if not cfg.login_email or not cfg.login_password:
        return {"status": "SKIPPED", "message": f"Member login skipped for {cfg.site_name}: email or password is missing."}

    try:
        login_html = fetch_page(session, cfg, cfg.login_url)
    except Exception as e:
        return {"status": "FAILED", "message": f"Could not open login page: {e}"}

    if is_logged_in_account_page(login_html):
        return {"status": "LOGGED_IN", "message": "Member login was already active."}

    soup = BeautifulSoup(login_html, "html.parser")
    form = find_login_form(soup)
    if not form:
        return {"status": "FAILED", "message": "Login form was not found on the account page."}

    data = build_login_form_data(form, cfg)
    action_url = urljoin(cfg.login_url, form.get("action") or cfg.login_url)
    method = (form.get("method") or "post").lower()
    parsed = urlparse(cfg.login_url)
    submit_headers = {**HEADERS, "Referer": cfg.login_url, "Origin": f"{parsed.scheme}://{parsed.netloc}"}
    safe_fields = safe_login_form_fields(data)
    write_site_log(cfg, f"Login form fields: {safe_fields}")

    try:
        write_site_log(cfg, f"Submitting member login form: {action_url}")
        if method == "get":
            response = session.get(action_url, params=data, headers=submit_headers, timeout=30, verify=APP.verify_ssl)
        else:
            response = session.post(action_url, data=data, headers=submit_headers, timeout=30, verify=APP.verify_ssl)
        response.raise_for_status()

        if is_logged_in_account_page(response.text):
            return {"status": "LOGGED_IN", "message": "Member login succeeded."}

        account_html = fetch_page(session, cfg, cfg.login_url)
        if is_logged_in_account_page(account_html):
            return {"status": "LOGGED_IN", "message": "Member login succeeded."}
        return {"status": "FAILED", "message": f"Member login failed: {login_failure_reason(account_html)}."}
    except requests.HTTPError as e:
        response = e.response
        if response is not None and response.text:
            return {"status": "FAILED", "message": f"Member login failed: {login_failure_reason(response.text)} ({response.status_code})."}
        return {"status": "FAILED", "message": f"Member login request failed: {e}"}
    except Exception as e:
        return {"status": "FAILED", "message": f"Member login request failed: {e}"}


# ============================================================
# Stock detection
# ============================================================


def parse_product_links(html: str, base_url: str, cfg: SiteConfig) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for link in soup.select("a[href]"):
        href = link.get("href")
        if not href:
            continue
        if cfg.product_link_href_parts and not any(part in href for part in cfg.product_link_href_parts):
            continue

        full_url = urljoin(base_url, href)
        path = urlparse(full_url).path
        if "/catalog/" in full_url:
            continue
        if full_url.endswith(".js") or full_url.endswith(".json"):
            continue
        if cfg.product_link_href_parts and not any(part in path or part in full_url for part in cfg.product_link_href_parts):
            continue
        if full_url not in links:
            links.append(full_url)
    return links


def remove_sitewide_stock_notice(text: str) -> str:
    notice_patterns = [
        r"About limited availability of Matcha products.*?restock those sold out products as soon as possible\.?",
        r"Dear customers, We have been receiving an unexpected high volume of orders.*?restock those sold out products as soon as possible\.?",
    ]
    cleaned = text
    for pattern in notice_patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def parse_product_name(soup: BeautifulSoup) -> str:
    generic_names = {"product detail", "products", "matcha", "just a moment...", "attention required!"}
    heading = soup.select_one("h1")
    if heading:
        name = heading.get_text(" ", strip=True)
        if name and name.lower() not in generic_names:
            return name
    title = soup.select_one("title")
    if title:
        name = title.get_text(" ", strip=True).split("|", 1)[0].strip()
        if name and name.lower() not in generic_names:
            return name
    return ""


SOLD_OUT_KEYWORDS = (
    "this product is currently out of stock and unavailable",
    "this product is currently out of stock",
    "currently out of stock and unavailable",
    "currently out of stock",
    "out of stock and unavailable",
    "out of stock",
    "sold out",
    "売り切れ",
    "在庫切れ",
    "お取り扱いできません",
    "憯脯???",
    "?典澈??",
    "???????",
)

IN_STOCK_KEYWORDS = (
    "カートに入れる",
    "add to cart",
    "add-to-cart",
)


def has_sold_out_keyword(text: str) -> bool:
    text_lower = (text or "").casefold()
    return any(keyword.casefold() in text_lower for keyword in SOLD_OUT_KEYWORDS)


def tag_bits(tag) -> str:
    values = [
        tag.get("id"),
        tag.get("name"),
        tag.get("value"),
        tag.get("aria-label"),
        tag.get("href"),
        tag.get("action"),
        " ".join(tag.get("class", [])),
        tag.get_text(" ", strip=True),
    ]
    return " ".join(value or "" for value in values).casefold()


def is_disabled_control(tag) -> bool:
    return tag.has_attr("disabled") or (tag.get("aria-disabled") or "").lower() == "true"


def has_sold_out_marker(bits: str) -> bool:
    return "sold-out" in bits or "variant-sold-out" in bits or has_sold_out_keyword(bits)


def is_shipping_calculator_form(tag) -> bool:
    bits = tag_bits(tag)
    return (
        "woocommerce-shipping-calculator" in bits
        or "calculate shipping" in bits
        or "shipping & handling fee" in bits
    )


def write_koyamaen_add_to_cart_debug(product_url: str, hit_type: str, tag) -> None:
    if not is_koyamaen_url(product_url):
        return

    bits = tag_bits(tag)
    write_global_log(
        "[koyamaen] DEBUG add_to_cart_hit "
        f"product_url={product_url} "
        f"hit_type={hit_type} "
        f"disabled={is_disabled_control(tag)} "
        f"contains_sold_out_keyword={has_sold_out_marker(bits)} "
        f"tag_bits={trim_text(bits, 300)}"
    )


def has_cart_form_or_add_to_cart_signal(soup: BeautifulSoup) -> bool:
    if soup.select_one('a[href*="add-to-cart="], a[href*="add-to-cart"], [class*="add-to-cart"], [id*="add-to-cart"]'):
        return True
    for form in soup.select("form"):
        bits = tag_bits(form)
        if "cart" in bits or "カート" in bits:
            return True
    for control in soup.select("button, input[type='submit'], input[type='button']"):
        bits = tag_bits(control)
        if any(keyword.casefold() in bits for keyword in IN_STOCK_KEYWORDS) or ("add" in bits and "cart" in bits):
            return True
    return False


def has_enabled_add_to_cart_control(soup: BeautifulSoup, product_url: str = "") -> bool:
    for link in soup.select('a[href*="add-to-cart="], a[href*="add-to-cart"]'):
        if not is_disabled_control(link):
            write_koyamaen_add_to_cart_debug(product_url, "link", link)
            return True

    for control in soup.select("button, input[type='submit'], input[type='button']"):
        if is_disabled_control(control):
            continue
        control_bits = tag_bits(control)
        if has_sold_out_keyword(control_bits):
            continue
        if any(keyword.casefold() in control_bits for keyword in IN_STOCK_KEYWORDS) or ("add" in control_bits and "cart" in control_bits):
            write_koyamaen_add_to_cart_debug(product_url, "global_control", control)
            return True

    for cart_form in soup.select("form.cart, form[action*='/cart/add'], form[action*='cart/add'], form[action*='cart'], form.product-form"):
        if is_shipping_calculator_form(cart_form):
            continue
        form_bits = tag_bits(cart_form)
        if "sold-out" in form_bits or "variant-sold-out" in form_bits or has_sold_out_keyword(form_bits):
            continue
        if "cart" in form_bits:
            write_koyamaen_add_to_cart_debug(product_url, "form", cart_form)
            return True
        for control in cart_form.select("button, input[type='submit'], input[type='button']"):
            if not is_disabled_control(control) and not has_sold_out_keyword(tag_bits(control)):
                write_koyamaen_add_to_cart_debug(product_url, "form", control)
                return True

    fallback_link = soup.select_one('a[href*="add-to-cart="]')
    if fallback_link:
        write_koyamaen_add_to_cart_debug(product_url, "link", fallback_link)
        return True

    cart_forms = soup.select("form.cart, form[action*='/cart/add'], form[action*='cart/add'], form.product-form")
    if not cart_forms:
        return False

    for cart_form in cart_forms:
        if is_shipping_calculator_form(cart_form):
            continue
        form_bits = " ".join(
            value or ""
            for value in [cart_form.get("id"), cart_form.get("name"), " ".join(cart_form.get("class", [])), cart_form.get_text(" ", strip=True)]
        ).casefold()
        if "sold-out" in form_bits or "variant-sold-out" in form_bits:
            continue

        for control in cart_form.select("button, input[type='submit']"):
            if control.has_attr("disabled"):
                continue
            if (control.get("aria-disabled") or "").lower() == "true":
                continue

            control_bits = " ".join(
                value or ""
                for value in [
                    control.get("name"),
                    control.get("value"),
                    control.get("aria-label"),
                    " ".join(control.get("class", [])),
                    control.get_text(" ", strip=True),
                ]
            ).casefold()
            if any(keyword in control_bits for keyword in ["売り切れ", "sold out", "out of stock"]):
                continue
            if ("add" in control_bits and "cart" in control_bits) or "カートに追加" in control_bits:
                write_koyamaen_add_to_cart_debug(product_url, "fallback_form", control)
                return True
    return False


def parse_script_json(script_tag):
    raw_json = script_tag.string or script_tag.get_text()
    raw_json = (raw_json or "").strip()
    if not raw_json:
        return None
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        return None


def shopify_product_result_from_data(product: dict, product_url: str, page_sample: str = ""):
    variants = product.get("variants") or []
    available_variants = [variant for variant in variants if variant.get("available") is True]
    product_name = product.get("title") or ""

    if product.get("available") is True or available_variants:
        variant_labels = [variant.get("public_title") or variant.get("title") or str(variant.get("id")) for variant in available_variants]
        variant_text = f" Available variants: {', '.join(label for label in variant_labels if label)}." if variant_labels else ""
        return {
            "status": "IN_STOCK",
            "message": product_name and f"{product_name} appears to be available.{variant_text}" or f"Product appears to be available.{variant_text}",
            "url": product_url,
            "product_name": product_name,
            "page_text_sample": page_sample,
        }

    if product.get("available") is False or variants:
        return {
            "status": "SOLD_OUT",
            "message": product_name and f"{product_name} appears to be sold out." or "Product appears to be sold out.",
            "url": product_url,
            "product_name": product_name,
            "page_text_sample": page_sample,
        }
    return None


def shopify_product_result_from_ld_json(data, product_url: str, page_sample: str = ""):
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        graph_items = item.get("@graph")
        if isinstance(graph_items, list):
            nested_result = shopify_product_result_from_ld_json(graph_items, product_url, page_sample)
            if nested_result:
                return nested_result

        type_value = item.get("@type")
        type_values = type_value if isinstance(type_value, list) else [type_value]
        if "Product" not in type_values:
            continue

        product_name = item.get("name") or ""
        offers = item.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        availability_values = [str(offer.get("availability", "")).casefold() for offer in offers if isinstance(offer, dict)]

        if any("instock" in value or "in_stock" in value for value in availability_values):
            return {
                "status": "IN_STOCK",
                "message": product_name and f"{product_name} appears to be available." or "Product appears to be available.",
                "url": product_url,
                "product_name": product_name,
                "page_text_sample": page_sample,
            }
        if availability_values and all("outofstock" in value for value in availability_values):
            return {
                "status": "SOLD_OUT",
                "message": product_name and f"{product_name} appears to be sold out." or "Product appears to be sold out.",
                "url": product_url,
                "product_name": product_name,
                "page_text_sample": page_sample,
            }
    return None


def parse_embedded_shopify_stock(soup: BeautifulSoup, product_url: str, page_sample: str = ""):
    for script in soup.select('script[type="application/json"][id^="ProductJson"]'):
        data = parse_script_json(script)
        if isinstance(data, dict):
            result = shopify_product_result_from_data(data, product_url, page_sample)
            if result:
                return result

    for script in soup.select('script[type="application/ld+json"]'):
        data = parse_script_json(script)
        if data is not None:
            result = shopify_product_result_from_ld_json(data, product_url, page_sample)
            if result:
                return result
    return None


def shopify_product_json_url(product_url: str) -> str:
    parsed = urlparse(product_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    match = re.search(r"/products/([^/?#]+)", parsed.path)
    if not match:
        return ""
    handle = match.group(1)
    return f"{parsed.scheme}://{parsed.netloc}/products/{handle}.js"


def shopify_cart_add_url(product_url: str) -> str:
    parsed = urlparse(product_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/cart/add.js"


def fetch_shopify_product_data(session: requests.Session, cfg: SiteConfig, product_url: str):
    json_url = shopify_product_json_url(product_url)
    if not json_url:
        return None

    headers = {**HEADERS, "Accept": "application/json,text/javascript,*/*;q=0.8"}
    try:
        write_site_log(cfg, f"Fetching Shopify product JSON: {json_url}")
        response = session.get(json_url, headers=headers, timeout=30, verify=APP.verify_ssl)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        product_data = response.json()
    except (requests.RequestException, ValueError, TypeError) as e:
        write_site_log(cfg, f"Shopify product JSON check skipped: {product_url} - {e}")
        return None

    if not isinstance(product_data, dict) or "variants" not in product_data:
        return None
    write_site_log(cfg, f"Fetched Shopify product JSON: {product_data.get('handle') or product_data.get('title') or product_url}")
    return product_data


def shopify_variants(product_data: dict) -> list[dict]:
    variants = product_data.get("variants") or []
    return variants if isinstance(variants, list) else []


def first_available_shopify_variant(product_data: dict):
    for variant in shopify_variants(product_data):
        if variant.get("available"):
            return variant
    return None


def shopify_variant_label(variant) -> str:
    if not variant:
        return ""
    title = variant.get("public_title") or variant.get("title") or ""
    sku = variant.get("sku") or ""
    variant_id = variant.get("id") or ""
    parts = []
    if title and title.lower() != "default title":
        parts.append(str(title))
    if sku:
        parts.append(f"SKU {sku}")
    if variant_id:
        parts.append(f"variant {variant_id}")
    return ", ".join(parts)


def parse_shopify_product_stock(product_data: dict, product_url: str) -> dict:
    product_name = product_data.get("title") or ""
    variant = first_available_shopify_variant(product_data)
    sample = json.dumps(product_data, ensure_ascii=False)[:800]
    if variant:
        variant_label = shopify_variant_label(variant)
        message = product_name or "Product"
        message = f"{message} is available on Shopify ({variant_label})." if variant_label else f"{message} is available on Shopify."
        return {
            "status": "IN_STOCK",
            "message": message,
            "url": product_url,
            "product_name": product_name,
            "variant_id": variant.get("id"),
            "page_text_sample": sample,
        }
    if shopify_variants(product_data):
        return {
            "status": "SOLD_OUT",
            "message": product_name and f"{product_name} is sold out according to Shopify product JSON." or "Product is sold out according to Shopify product JSON.",
            "url": product_url,
            "product_name": product_name,
            "page_text_sample": sample,
        }
    return {
        "status": "UNKNOWN",
        "message": product_name and f"Could not find Shopify variants for {product_name}." or "Could not find Shopify variants for product.",
        "url": product_url,
        "product_name": product_name,
        "page_text_sample": sample,
    }


def page_has_product_data(text: str, html: str = "") -> bool:
    has_sku = bool(re.search(r"\bSKU\b", text, flags=re.IGNORECASE))
    return has_sku or 'id="ProductJson' in html or 'action="/cart/add"' in html or "action='/cart/add'" in html or "cart/add" in html


# Challenge detection is intentionally strict and runs only after product parsing.
# Koyamaen product pages can include Cloudflare JavaScript while still being real
# product HTML, so single markers like cf-chl are never enough on their own.
CHALLENGE_PAGE_MARKERS = (
    "cf-chl",
    "challenge-platform",
    "/cdn-cgi/challenge-platform",
    "just a moment...",
    "checking your browser before accessing",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
)


def looks_like_blocked_or_challenge_page(
    html: str,
    soup: BeautifulSoup | None = None,
    stock_text: str = "",
    has_cart_control: bool = False,
) -> bool:
    """True only for a clear anti-bot page with no product/stock signals."""
    if not html:
        return False
    lowered = html.lower()
    soup = soup or BeautifulSoup(html, "html.parser")
    title = soup.select_one("title")
    title_text = title.get_text(" ", strip=True).casefold() if title else ""
    has_challenge_title = title_text in {"just a moment...", "attention required! | cloudflare"}
    has_challenge_platform = "/cdn-cgi/challenge-platform" in lowered
    has_product_heading = bool(soup.select_one("h1") and parse_product_name(soup))
    has_product_stock_text = has_sold_out_keyword(stock_text)
    has_cart_signal = has_cart_control or has_cart_form_or_add_to_cart_signal(soup)
    return (
        has_challenge_title
        and has_challenge_platform
        and not has_product_heading
        and not has_cart_signal
        and not has_product_stock_text
    )


def parse_single_product_stock(html: str, product_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = page_text(html)
    stock_text = remove_sitewide_stock_notice(text)
    text_lower = stock_text.casefold()
    product_name = parse_product_name(soup)
    save_koyamaen_debug_detail_html(product_url, product_name, html)

    shopify_result = parse_embedded_shopify_stock(soup, product_url, text[:800])
    if shopify_result:
        if not shopify_result.get("product_name"):
            shopify_result["product_name"] = product_name
        return shopify_result

    sold_out_keywords = [
        "this product is currently out of stock and unavailable",
        "this product is currently out of stock",
        "currently out of stock and unavailable",
        "currently out of stock",
        "out of stock and unavailable",
        "out of stock",
        "sold out",
        "売り切れ",
        "在庫切れ",
        "お取り扱いできません",
    ]
    is_sold_out = has_sold_out_keyword(stock_text)
    has_cart_control = has_enabled_add_to_cart_control(soup, product_url)
    login_required = "you must register and login to shop" in text_lower
    if is_koyamaen_url(product_url):
        write_global_log(
            "[koyamaen] DEBUG parse_single_product_stock "
            f"product_url={product_url} "
            f"product_name={product_name or ''} "
            f"is_sold_out={is_sold_out} "
            f"has_cart_control={has_cart_control} "
            f"login_required={login_required} "
            f"stock_text={trim_text(stock_text, 500)}"
        )

    if has_cart_control:
        return {
            "status": "IN_STOCK",
            "message": product_name and f"{product_name} appears to be available." or "Product appears to be available.",
            "url": product_url,
            "product_name": product_name,
            "page_text_sample": text[:800],
        }
    if is_sold_out:
        return {
            "status": "SOLD_OUT",
            "message": product_name and f"{product_name} appears to be sold out." or "Product appears to be sold out.",
            "url": product_url,
            "product_name": product_name,
            "page_text_sample": text[:800],
        }
    if looks_like_blocked_or_challenge_page(html, soup, stock_text, has_cart_control):
        write_global_log("Cloudflare / anti-bot challenge page detected after product parsing; stock status set to UNKNOWN.")
        if APP.debug:
            write_global_log(f"Challenge page for {product_url}; sample: {trim_text(text, 500)}")
        return {
            "status": "UNKNOWN",
            "message": "Cloudflare / anti-bot challenge page; stock status could not be confirmed.",
            "url": product_url,
            "product_name": "",
            "page_text_sample": text[:800],
        }
    if login_required:
        # A login wall on its own is NOT proof of stock. Without an enabled
        # add-to-cart control or explicit availability data we cannot confirm
        # the product is buyable, so stay conservative and report UNKNOWN
        # rather than risk a false "in stock" alert.
        if APP.debug:
            write_global_log(f"Login required, stock unconfirmed for {product_url}; sample: {trim_text(stock_text, 300)}")
        return {
            "status": "UNKNOWN",
            "message": product_name and f"Login required and stock could not be confirmed for {product_name}." or "Login required and stock could not be confirmed.",
            "url": product_url,
            "product_name": product_name,
            "page_text_sample": text[:800],
        }
    return {
        "status": "UNKNOWN",
        "message": product_name and f"Could not determine {product_name} stock status." or "Could not determine product stock status.",
        "url": product_url,
        "product_name": product_name,
        "page_text_sample": text[:800],
    }


def format_target_product_names(cfg: SiteConfig) -> str:
    return ", ".join(cfg.target_product_names) if cfg.target_product_names else "all products"


def format_target_product_urls(cfg: SiteConfig) -> str:
    return ", ".join(cfg.target_product_urls) if cfg.target_product_urls else "none"


def format_target_product_filters(cfg: SiteConfig) -> str:
    parts = []
    if cfg.target_product_names:
        parts.append(f"names: {format_target_product_names(cfg)}")
    if cfg.target_product_urls:
        parts.append(f"urls: {format_target_product_urls(cfg)}")
    return "; ".join(parts) if parts else "all products"


def is_target_product(cfg: SiteConfig, product_name: str, product_url: str = "") -> bool:
    name_keys = cfg.target_product_name_keys
    url_keys = cfg.target_product_url_keys
    if not name_keys and not url_keys:
        return True

    product_url_key = normalize_url_key(product_url)
    if product_url_key and product_url_key in url_keys:
        return True

    normalized_name = normalize_product_name(product_name)
    if not normalized_name:
        return False

    padded_name = f" {normalized_name} "
    return any(normalized_name == target_key or f" {target_key} " in padded_name for target_key in name_keys)


def product_result_label(result: dict) -> str:
    return result.get("product_name") or result.get("url") or "Unknown product"


def unique_urls(urls: list[str]) -> list[str]:
    unique = []
    seen = set()
    for url in urls:
        url_key = normalize_url_key(url)
        if not url or not url_key or url_key in seen:
            continue
        seen.add(url_key)
        unique.append(url)
    return unique


def summarize_stock_results(cfg: SiteConfig, results: list[dict], source_url: str, total_products=None) -> dict:
    checked_products = len(results)
    total_products = total_products if total_products is not None else checked_products
    target_results = []
    target_in_stock_results = []
    non_target_in_stock_results = []

    for result in results:
        target_product = is_target_product(cfg, result.get("product_name", ""), result.get("url", ""))
        if not target_product:
            if result["status"] == "IN_STOCK":
                non_target_in_stock_results.append(result)
            continue

        target_results.append(result)
        if result["status"] == "IN_STOCK":
            target_in_stock_results.append(result)

    if target_in_stock_results:
        first_result = target_in_stock_results[0]
        product_names = ", ".join(product_result_label(result) for result in target_in_stock_results)
        return {
            **first_result,
            "message": f"{len(target_in_stock_results)} target product(s) in stock: {product_names}.",
            "checked_products": checked_products,
            "total_products": total_products,
            "target_products_checked": len(target_results),
            "in_stock_products": target_in_stock_results,
        }

    if not target_results:
        sample = results[0]["page_text_sample"] if results else ""
        return {
            "status": "UNKNOWN",
            "message": f"No products matched the target product filters: {format_target_product_filters(cfg)}.",
            "url": source_url,
            "page_text_sample": sample,
            "checked_products": checked_products,
            "total_products": total_products,
            "target_products_checked": 0,
        }

    if all(result["status"] == "SOLD_OUT" for result in target_results):
        message = f"All target products appear to be sold out: {format_target_product_filters(cfg)}."
        if non_target_in_stock_results:
            ignored_names = ", ".join(product_result_label(result) for result in non_target_in_stock_results)
            message = f"{message} Ignored non-target in-stock products: {ignored_names}."
        return {
            "status": "SOLD_OUT",
            "message": message,
            "url": source_url,
            "page_text_sample": target_results[0]["page_text_sample"],
            "checked_products": checked_products,
            "total_products": total_products,
            "target_products_checked": len(target_results),
        }

    unknown_target_results = [result for result in target_results if result["status"] == "UNKNOWN"]
    unknown_names = ", ".join(product_result_label(result) for result in unknown_target_results)
    unknown_suffix = f" for: {unknown_names}" if unknown_names else ""
    sample = unknown_target_results[0]["page_text_sample"] if unknown_target_results else target_results[0]["page_text_sample"]
    return {
        "status": "UNKNOWN",
        "message": f"Could not determine target product stock status{unknown_suffix}.",
        "url": source_url,
        "page_text_sample": sample,
        "checked_products": checked_products,
        "total_products": total_products,
        "target_products_checked": len(target_results),
    }


def should_retry_koyamaen_unknown_after_login(cfg: SiteConfig, result: dict, product_url: str) -> bool:
    if cfg.profile != "koyamaen" or result.get("status") != "UNKNOWN":
        return False
    if not cfg.target_product_name_keys and not cfg.target_product_url_keys:
        return True
    if normalize_url_key(product_url) in cfg.target_product_url_keys:
        return True
    product_name = result.get("product_name") or ""
    return bool(product_name and is_target_product(cfg, product_name, product_url))


def parse_collection_products_json_stock(session: requests.Session, cfg: SiteConfig):
    if not cfg.use_shopify_products_json or not cfg.shopify_products_json_url:
        return None

    try:
        data = fetch_json(session, cfg, cfg.shopify_products_json_url)
    except Exception as e:
        write_site_log(cfg, f"Shopify collection products JSON check failed: {e}")
        return None

    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list) or not products:
        write_site_log(cfg, "Shopify collection products JSON did not include products")
        return None

    write_site_log(cfg, f"Checking {len(products)} Shopify products from collection JSON")
    results = []
    for product in products:
        handle = product.get("handle") or ""
        parsed = urlparse(cfg.product_url)
        product_url = f"{parsed.scheme}://{parsed.netloc}/products/{handle}" if handle and parsed.scheme and parsed.netloc else cfg.product_url
        result = shopify_product_result_from_data(product, product_url, (product.get("body_html") or "")[:800])
        if not result:
            result = {
                "status": "UNKNOWN",
                "message": "Could not determine product stock status from Shopify collection JSON.",
                "url": product_url,
                "product_name": product.get("title") or "",
                "page_text_sample": (product.get("body_html") or "")[:800],
            }
        results.append(result)
        target_label = "target" if is_target_product(cfg, result.get("product_name", ""), result.get("url", "")) else "not target"
        write_site_log(cfg, f"Product status: {result['status']} - {product_result_label(result)} - {result.get('url', '')} ({target_label})")

    return summarize_stock_results(cfg, results, cfg.shopify_products_json_url, len(products))


def build_product_urls(html: str, cfg: SiteConfig) -> list[str]:
    product_links = parse_product_links(html, cfg.product_url, cfg)
    if product_links:
        if cfg.profile == "koyamaen":
            write_site_log(
                cfg,
                f"Koyamaen catalog listed {len(product_links)} product links; validating stock on each detail page.",
            )
        return unique_urls(cfg.target_product_urls + product_links)
    if shopify_product_json_url(cfg.product_url):
        return unique_urls(cfg.target_product_urls + [cfg.product_url])
    if page_has_product_data(page_text(html), html):
        return unique_urls(cfg.target_product_urls + [cfg.product_url])
    return unique_urls(cfg.target_product_urls)


def parse_product_detail_stock(session: requests.Session, cfg: SiteConfig, product_url: str) -> dict:
    shopify_product_data = fetch_shopify_product_data(session, cfg, product_url)
    if shopify_product_data:
        return parse_shopify_product_stock(shopify_product_data, product_url)

    product_html = fetch_page(session, cfg, product_url)
    result = parse_single_product_stock(product_html, product_url)
    if result.get("status") != "UNKNOWN" or cfg.profile != "koyamaen":
        return result

    if not should_retry_koyamaen_unknown_after_login(cfg, result, product_url):
        write_site_log(cfg, f"Koyamaen stock UNKNOWN for non-target product; login retry skipped: {product_url}")
        save_koyamaen_unknown_html(cfg, product_url, product_html, result)
        return result

    if cfg.enable_login:
        login_attempted = getattr(session, "_koyamaen_stock_login_attempted", False)
        if not login_attempted:
            setattr(session, "_koyamaen_stock_login_attempted", True)
            write_site_log(cfg, f"Koyamaen stock UNKNOWN before login; retrying after member login: {product_url}")
            login_result = login_to_member_account(session, cfg)
            write_site_log(cfg, f"Koyamaen stock retry login status: {login_result['status']}")
            write_site_log(cfg, login_result["message"])

        retry_html = fetch_page(session, cfg, product_url)
        retry_result = parse_single_product_stock(retry_html, product_url)
        if retry_result.get("status") != "UNKNOWN":
            write_site_log(cfg, f"Koyamaen stock retry resolved: {retry_result['status']} - {product_url}")
            return retry_result
        save_koyamaen_unknown_html(cfg, product_url, retry_html, retry_result)
        return retry_result

    save_koyamaen_unknown_html(cfg, product_url, product_html, result)
    return result


def parse_stock_status(session: requests.Session, cfg: SiteConfig, html: str) -> dict:
    collection_json_result = parse_collection_products_json_stock(session, cfg)
    if collection_json_result:
        return collection_json_result

    product_urls = build_product_urls(html, cfg)
    if not product_urls:
        result = parse_single_product_stock(html, cfg.product_url)
        if result.get("status") == "UNKNOWN":
            save_koyamaen_unknown_html(cfg, cfg.product_url, html, result)
        if is_target_product(cfg, result.get("product_name", ""), result.get("url", cfg.product_url)):
            return result
        return {
            "status": "SOLD_OUT",
            "message": f"{product_result_label(result)} is not in the target product list ({format_target_product_filters(cfg)}); skipping stock alert.",
            "url": result.get("url", cfg.product_url),
            "page_text_sample": result.get("page_text_sample", page_text(html)[:800]),
            "checked_products": 1,
            "total_products": 1,
            "target_products_checked": 0,
        }

    write_site_log(cfg, f"Checking {len(product_urls)} product detail URLs")
    results = []
    for index, product_url in enumerate(product_urls):
        if index and APP.request_delay_seconds > 0:
            time.sleep(APP.request_delay_seconds)
        try:
            result = parse_product_detail_stock(session, cfg, product_url)
            results.append(result)
            product_name = result.get("product_name")
            product_label = f"{product_name} - " if product_name else ""
            target_label = "target" if is_target_product(cfg, product_name or "", product_url) else "not target"
            write_site_log(cfg, f"Product status: {result['status']} - {product_label}{product_url} ({target_label})")
        except Exception as e:
            write_site_log(cfg, f"Product detail check failed: {product_url} - {e}")

    return summarize_stock_results(cfg, results, cfg.product_url, len(product_urls))


# ============================================================
# Shipping-to-target detection
# ============================================================


def post_shopify_cart_add(session: requests.Session, cfg: SiteConfig, product_url: str, variant_id) -> bool:
    action_url = shopify_cart_add_url(product_url)
    if not action_url:
        write_site_log(cfg, "Auto add-to-cart skipped: could not build Shopify cart URL")
        return False

    headers = {**HEADERS, "Accept": "application/json,text/javascript,*/*;q=0.8", "X-Requested-With": "XMLHttpRequest"}
    data = {"id": str(variant_id), "quantity": "1"}
    try:
        response = session.post(action_url, data=data, headers=headers, timeout=30, verify=APP.verify_ssl)
        if response.status_code == 422:
            write_site_log(cfg, f"Auto add-to-cart rejected by Shopify: {trim_text(response.text, 300)}")
            return False
        response.raise_for_status()
    except requests.RequestException as e:
        write_site_log(cfg, f"Auto add-to-cart failed on Shopify endpoint: {e}")
        return False

    write_site_log(cfg, f"Auto add-to-cart submitted Shopify variant: {variant_id} ({product_url})")
    return True


def add_shopify_product_to_cart(session: requests.Session, cfg: SiteConfig, product_url: str, variant_id=None):
    # When the stock check already resolved a variant id, reuse it and skip the extra product JSON fetch.
    if variant_id:
        return post_shopify_cart_add(session, cfg, product_url, variant_id)

    product_data = fetch_shopify_product_data(session, cfg, product_url)
    if not product_data:
        return None

    variant = first_available_shopify_variant(product_data)
    if not variant or not variant.get("id"):
        write_site_log(cfg, "Auto add-to-cart skipped: no available Shopify variant")
        return False

    return post_shopify_cart_add(session, cfg, product_url, variant.get("id"))


def add_product_to_cart(session: requests.Session, cfg: SiteConfig, product_url: str, variant_id=None) -> bool:
    if not APP.auto_add_to_cart:
        write_site_log(cfg, "Auto add-to-cart skipped: AUTO_ADD_TO_CART is disabled")
        return False

    shopify_result = add_shopify_product_to_cart(session, cfg, product_url, variant_id)
    if shopify_result is not None:
        return shopify_result

    try:
        html = fetch_page(session, cfg, product_url)
    except Exception as e:
        write_site_log(cfg, f"Auto add-to-cart failed while fetching product: {e}")
        return False

    soup = BeautifulSoup(html, "html.parser")
    if not has_enabled_add_to_cart_control(soup, product_url):
        write_site_log(cfg, "Auto add-to-cart skipped: no enabled add-to-cart control found")
        return False

    for link in soup.select('a[href*="add-to-cart="]'):
        href = link.get("href")
        if not href:
            continue
        add_url = urljoin(product_url, href)
        response = session.get(add_url, headers=HEADERS, timeout=30, verify=APP.verify_ssl)
        response.raise_for_status()
        write_site_log(cfg, f"Auto add-to-cart used link: {add_url}")
        return True

    form = soup.select_one("form.cart, form[action*='/cart/add'], form[action*='cart/add'], form.product-form")
    if not form:
        write_site_log(cfg, "Auto add-to-cart skipped: no cart form found on product page")
        return False

    data = collect_input_fields(form, quantity_default=True)
    data.update(collect_select_fields(form))

    button = form.select_one("button:not([disabled])[name], input[type='submit']:not([disabled])[name]")
    if button and button.get("name"):
        data[button.get("name")] = button.get("value") or button.get_text(strip=True)
    if "quantity" not in data:
        data["quantity"] = "1"

    method = (form.get("method") or "post").lower()
    action_url = urljoin(product_url, form.get("action") or product_url)
    if method == "get":
        response = session.get(action_url, params=data, headers=HEADERS, timeout=30, verify=APP.verify_ssl)
    else:
        response = session.post(action_url, data=data, headers=HEADERS, timeout=30, verify=APP.verify_ssl)
    response.raise_for_status()
    write_site_log(cfg, f"Auto add-to-cart submitted form: {action_url}")
    return True


def option_matches_target_country(option) -> bool:
    value = (option.get("value") or "").strip().lower()
    label = option.get_text(" ", strip=True).lower()
    target_name = APP.target_country_name.lower()
    target_code = APP.target_country_code.lower()
    return value == target_code or target_name in label


def parse_shipping_status(html: str, source_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    text_lower = text.lower()
    country_options = soup.select(
        "select#billing_country option,"
        "select#shipping_country option,"
        "select[name='billing_country'] option,"
        "select[name='shipping_country'] option,"
        "select[name='calc_shipping_country'] option,"
        "select.country_to_state option"
    )

    if not country_options:
        for select in soup.select("select"):
            name_bits = " ".join(
                value or ""
                for value in [select.get("id"), select.get("name"), " ".join(select.get("class", [])), select.get("aria-label")]
            ).lower()
            option_text = select.get_text(" ", strip=True).lower()
            looks_like_country_select = (
                "country" in name_bits
                or "destination" in name_bits
                or ("japan" in option_text and ("united states" in option_text or "taiwan" in option_text))
            )
            if looks_like_country_select:
                country_options.extend(select.select("option"))

    if country_options:
        if any(option_matches_target_country(option) for option in country_options):
            return {
                "status": "AVAILABLE",
                "message": f"Shipping country option includes {APP.target_country_name}.",
                "url": source_url,
                "page_text_sample": text[:800],
            }
        return {
            "status": "UNAVAILABLE",
            "message": f"Shipping country options do not include {APP.target_country_name}.",
            "url": source_url,
            "page_text_sample": text[:800],
        }

    target_near_unavailable = APP.target_country_name.lower() in text_lower and any(
        keyword in text_lower for keyword in ["not available", "not ship", "cannot ship", "no shipping", "unavailable"]
    )
    if target_near_unavailable:
        return {
            "status": "UNAVAILABLE",
            "message": f"Page text suggests {APP.target_country_name} shipping is unavailable.",
            "url": source_url,
            "page_text_sample": text[:800],
        }

    return {
        "status": "UNKNOWN",
        "message": "Could not find shipping country options. The site may require a cart item or login before showing checkout countries.",
        "url": source_url,
        "page_text_sample": text[:800],
    }


def check_shipping_to_target(session: requests.Session, cfg: SiteConfig) -> dict:
    results = []
    for url in cfg.shipping_check_urls:
        try:
            html = fetch_page(session, cfg, url)
            result = parse_shipping_status(html, url)
            results.append(result)
            write_site_log(cfg, f"Shipping status from {url}: {result['status']}")
            if result["status"] in ["AVAILABLE", "UNAVAILABLE"]:
                return result
        except Exception as e:
            write_site_log(cfg, f"Shipping check failed: {url} - {e}")

    if results:
        return results[0]
    return {
        "status": "UNKNOWN",
        "message": "All shipping check URLs failed.",
        "url": ", ".join(cfg.shipping_check_urls),
        "page_text_sample": "",
    }


# ============================================================
# Main check
# ============================================================


def result_summary_line(site_result: dict) -> str:
    cfg = site_result["config"]
    stock = site_result.get("stock_result", {})
    shipping = site_result.get("shipping_result", {})
    product_label = stock.get("product_name") or stock.get("url") or cfg.product_url
    return (
        f"[{cfg.site_name}] Stock: {stock.get('status', 'ERROR')} | "
        f"Shipping to {APP.target_country_name}: {shipping.get('status', 'UNKNOWN')} | "
        f"{product_label}"
    )


def in_stock_results(results: list[dict]) -> list[dict]:
    alert_results = []
    for result in results:
        stock_result = result.get("stock_result", {})
        if stock_result.get("status") != "IN_STOCK":
            continue

        products = stock_result.get("in_stock_products") or [stock_result]
        for product in products:
            alert_results.append({**result, "stock_result": product})
    return alert_results


def in_stock_alert_key(site_result: dict) -> str:
    cfg = site_result["config"]
    stock = site_result.get("stock_result", {})
    product_url = normalize_url_key(stock.get("url") or cfg.product_url)
    product_name = normalize_product_name(stock.get("product_name") or "")
    return f"{cfg.profile}|{product_url}|{product_name}"


def load_alerted_in_stock_keys() -> set[str]:
    try:
        with open(APP.alert_state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return set()
    except Exception as e:
        write_global_log(f"Alert state read failed: {e}")
        return set()

    keys = data.get("in_stock_keys", [])
    if not isinstance(keys, list):
        return set()
    return {str(key) for key in keys if key}


def save_alerted_in_stock_keys(keys: set[str]) -> None:
    data = {
        "updated_at": now_text(),
        "in_stock_keys": sorted(keys),
    }
    try:
        os.makedirs(os.path.dirname(APP.alert_state_file), exist_ok=True)
        with open(APP.alert_state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_global_log(f"Alert state write failed: {e}")


def format_in_stock_result_for_alert(site_result: dict) -> str:
    cfg = site_result["config"]
    stock = site_result.get("stock_result", {})
    shipping = site_result.get("shipping_result", {})
    product_name = stock.get("product_name") or "Unknown product"
    product_url = stock.get("url") or cfg.product_url

    lines = [
        f"[{cfg.site_name}]",
        f"商品: {product_name}",
        f"連結: {product_url}",
    ]
    if "added_to_cart" in stock:
        if not APP.auto_add_to_cart:
            add_text = "未啟用"
        else:
            add_text = "成功" if stock.get("added_to_cart") else "失敗"
        lines.append(f"自動加入購物車: {add_text}")
    lines.append(f"寄送 {APP.target_country_name}: {shipping.get('status', 'UNKNOWN')}")
    return "\n".join(lines)


def format_site_result_for_alert(site_result: dict) -> str:
    cfg = site_result["config"]
    login_result = site_result.get("login_result", {})
    stock_result = site_result.get("stock_result", {})
    shipping_result = site_result.get("shipping_result", {})
    error = site_result.get("error")

    if error:
        return f"[{cfg.site_name}]\nERROR: {error}\n"

    stock_text = (
        f"Stock: {stock_result.get('status')}\n"
        f"{stock_result.get('message')}\n"
        f"Product page: {stock_result.get('url', cfg.product_url)}"
    )
    if "added_to_cart" in stock_result:
        stock_text += f"\nAuto add-to-cart: {stock_result.get('added_to_cart')}"

    login_text = f"Member login: {login_result.get('status')}\n{login_result.get('message')}"
    shipping_text = (
        f"Shipping to {APP.target_country_name}: {shipping_result.get('status')}\n"
        f"{shipping_result.get('message')}\n"
        f"Shipping check page: {shipping_result.get('url', '')}"
    )
    return f"[{cfg.site_name}]\n{login_text}\n\n{stock_text}\n\n{shipping_text}\n"


def site_has_unalerted_in_stock(cfg: SiteConfig, stock_result: dict, alerted_keys: set) -> bool:
    """True if this site is IN_STOCK with at least one product we have NOT alerted yet.

    Drives the "log in only when needed" rule: we refresh Koyamaen's ship-to-Taiwan
    status the moment a NEW product appears, never on empty stock and never again while
    the same product stays in stock. That keeps logins rare (so the 15-minute run does
    not look bot-like / risk the Koyamaen account) while still answering "can it ship to
    Taiwan?" exactly when it matters."""
    if stock_result.get("status") != "IN_STOCK":
        return False
    products = stock_result.get("in_stock_products") or [stock_result]
    for product in products:
        key = in_stock_alert_key({"config": cfg, "stock_result": product})
        if key not in alerted_keys:
            return True
    return False


def check_site_once(cfg: SiteConfig, alerted_keys=None) -> dict:
    # alerted_keys lets us tell a brand-new in-stock product (→ log in + refresh shipping)
    # from one we already alerted (→ skip the login). Loaded once per run by the caller;
    # default-load here so direct/test calls still work.
    if alerted_keys is None:
        alerted_keys = load_alerted_in_stock_keys()

    site_result = {"config": cfg}
    write_site_log(cfg, "=" * 80)
    write_site_log(cfg, f"Starting {cfg.site_name} stock and {APP.target_country_name} shipping check")
    write_site_log(cfg, f"Product URL: {cfg.product_url}")
    write_site_log(cfg, f"Shopify collection products JSON URL: {cfg.shopify_products_json_url or 'disabled'}")
    write_site_log(cfg, f"Target product names: {format_target_product_names(cfg)}")
    write_site_log(cfg, f"Target product URLs: {format_target_product_urls(cfg)}")

    session = build_session()
    try:
        # Stock is detectable WITHOUT logging in, so check it first. Only when a new
        # in-stock product shows up (and login is enabled for this site) do we log in,
        # add to cart, and re-check ship-to-Taiwan — all in this same run.
        html = fetch_page(session, cfg, cfg.product_url)
        stock_result = parse_stock_status(session, cfg, html)
        site_result["stock_result"] = stock_result

        follow_up = (
            stock_result["status"] == "IN_STOCK"
            and cfg.enable_login
            and site_has_unalerted_in_stock(cfg, stock_result, alerted_keys)
        )

        if follow_up:
            login_result = login_to_member_account(session, cfg)
            site_result["login_result"] = login_result
            write_site_log(cfg, f"Member login status: {login_result['status']}")
            write_site_log(cfg, login_result["message"])

            products = stock_result.get("in_stock_products") or [stock_result]
            for product in products:
                added_to_cart = add_product_to_cart(
                    session, cfg, product.get("url", cfg.product_url), product.get("variant_id")
                )
                product["added_to_cart"] = added_to_cart
                write_site_log(cfg, f"Auto add-to-cart result for {product_result_label(product)}: {added_to_cart}")
            stock_result["added_to_cart"] = products[0].get("added_to_cart") if products else None

            shipping_result = check_shipping_to_target(session, cfg)
        else:
            # Out of stock, login disabled, or already alerted (we logged in on an earlier
            # run). Skip the login/cart/shipping round-trip to keep logins minimal.
            shipping_result = {
                "status": "UNKNOWN",
                "message": "Shipping check skipped: only runs when a new in-stock product appears with login enabled.",
                "url": ", ".join(cfg.shipping_check_urls),
            }

        site_result["shipping_result"] = shipping_result

        write_site_log(cfg, f"Stock status: {stock_result['status']}")
        write_site_log(cfg, stock_result["message"])
        write_site_log(cfg, f"Shipping-to-{APP.target_country_name} status: {shipping_result['status']}")
        write_site_log(cfg, shipping_result["message"])

        if stock_result["status"] == "UNKNOWN" and APP.alert_on_unknown_stock:
            site_result["needs_review"] = True
        return site_result
    except Exception as e:
        site_result["error"] = str(e)
        write_site_log(cfg, f"Check failed: {e}")
        return site_result


def check_all_sites_once(configs: list[SiteConfig]) -> list[dict]:
    for cfg in configs:
        if not cfg.enabled:
            write_site_log(cfg, "Skipped: site is disabled")

    enabled = [cfg for cfg in configs if cfg.enabled]
    if not enabled:
        return []

    # Load the de-dup state once so every site sees the same "already alerted" snapshot
    # when deciding whether this is a new in-stock product worth logging in for.
    alerted_keys = load_alerted_in_stock_keys()

    workers = max(1, min(APP.max_site_workers, len(enabled)))
    if workers == 1:
        return [check_site_once(cfg, alerted_keys) for cfg in enabled]

    # Sites are independent (own session per call); run them concurrently. map() preserves order.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(lambda cfg: check_site_once(cfg, alerted_keys), enabled))


def send_summary_if_needed(results: list[dict]) -> None:
    summary_lines = [result_summary_line(result) for result in results]
    write_global_log("Run summary:")
    for line in summary_lines:
        write_global_log(line)

    current_in_stock_results = in_stock_results(results)
    current_keys = {in_stock_alert_key(result) for result in current_in_stock_results}
    previous_keys = load_alerted_in_stock_keys()
    new_in_stock_results = [result for result in current_in_stock_results if in_stock_alert_key(result) not in previous_keys]

    if not APP.alert_when_any_in_stock:
        write_global_log("No alert: ALERT_WHEN_ANY_IN_STOCK is false.")
        return

    if new_in_stock_results:
        title = f"Matcha in-stock alert ({len(new_in_stock_results)})"
        detail = "\n\n".join(format_in_stock_result_for_alert(result) for result in new_in_stock_results)
        if send_alert(title, detail):
            save_alerted_in_stock_keys(current_keys)
        else:
            write_global_log("Alert was not marked as sent because no notification channel succeeded.")
        return

    if current_in_stock_results:
        write_global_log("No alert: in-stock products were already alerted.")
        save_alerted_in_stock_keys(current_keys)
        return

    if not any(result.get("error") for result in results):
        save_alerted_in_stock_keys(set())

    if APP.summary_alert_every_run or APP.alert_on_unknown_stock:
        write_global_log("No alert: Telegram is configured to notify only when a new target product is in stock.")
    else:
        write_global_log("No alert: no target product appears in stock.")


def simulate_alert(configs: list[SiteConfig]) -> None:
    names = ", ".join(cfg.site_name for cfg in configs if cfg.enabled)
    send_alert(
        "[SIMULATION] Matcha monitor summary",
        f"Profiles: {names}\nStock: IN_STOCK simulation\nShipping to {APP.target_country_name}: UNKNOWN simulation",
    )


def main() -> None:
    if not acquire_single_instance_lock():
        return

    configs = [build_site_config(profile) for profile in configured_profiles()]
    enabled_configs = [cfg for cfg in configs if cfg.enabled]

    write_global_log("Matcha Multi Monitor started")
    write_global_log(f"SITE_PROFILES: {', '.join(cfg.profile for cfg in configs) or 'none'}")
    write_global_log(f"Enabled profiles: {', '.join(cfg.profile for cfg in enabled_configs) or 'none'}")
    write_global_log(f"RUN_ONCE: {APP.run_once}")
    write_global_log(f"CHECK_INTERVAL_SECONDS: {APP.check_interval_seconds}")
    write_global_log(f"LOG_FILE: {APP.log_file}")
    write_global_log(f"LOG_DIR: {APP.log_dir}")
    write_global_log(f"SINGLE_INSTANCE: {APP.single_instance}")
    write_global_log(f"LOCK_FILE: {APP.lock_file}")
    write_global_log(f"ALERT_STATE_FILE: {APP.alert_state_file}")
    write_global_log(f"AUTO_ADD_TO_CART: {APP.auto_add_to_cart}")
    write_global_log(f"ALERT_ON_UNKNOWN_STOCK: {APP.alert_on_unknown_stock}")
    write_global_log(f"ENABLE_LOGIN: {APP.enable_login}")
    write_global_log(f"TELEGRAM_VERIFY_SSL: {APP.telegram_verify_ssl}")
    write_global_log(f"ENABLE_LOCAL_NOTIFICATION: {APP.enable_local_notification}")
    write_global_log(f"TEST_TELEGRAM: {APP.test_telegram}")
    write_global_log(f"SIMULATE_IN_STOCK: {APP.simulate_in_stock}")
    write_global_log(f"DEBUG: {APP.debug}")
    write_global_log(f"SUMMARY_ALERT_EVERY_RUN: {APP.summary_alert_every_run}")
    write_global_log(f"TARGET_COUNTRY_NAME: {APP.target_country_name}")
    write_global_log(f"TARGET_COUNTRY_CODE: {APP.target_country_code}")
    write_global_log(f"VERIFY_SSL: {APP.verify_ssl}")
    write_global_log(f"HTTP_MAX_RETRIES: {APP.http_max_retries}")
    write_global_log(f"REQUEST_DELAY_SECONDS: {APP.request_delay_seconds}")
    write_global_log(f"LOOP_JITTER_SECONDS: {APP.loop_jitter_seconds}")
    write_global_log(f"MAX_SITE_WORKERS: {APP.max_site_workers}")

    if not enabled_configs:
        write_global_log("No enabled site profiles. Check SITE_PROFILES or *_ENABLED settings.")
        return

    if APP.simulate_in_stock:
        simulate_alert(enabled_configs)
        return

    if APP.test_telegram:
        if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
            write_global_log("Telegram test skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
            return
        try:
            send_telegram_alert("Matcha Multi Monitor Telegram test", f"Notification test at {now_text()}")
            write_global_log("Telegram test sent")
        except Exception as e:
            write_global_log(f"Telegram test failed: {e}")
        return

    if APP.run_once:
        results = check_all_sites_once(enabled_configs)
        send_summary_if_needed(results)
        return

    while True:
        try:
            results = check_all_sites_once(enabled_configs)
            send_summary_if_needed(results)
        except Exception as e:
            write_global_log(f"Run failed, will retry next cycle: {type(e).__name__}: {e}")
        wait_seconds = APP.check_interval_seconds + random.uniform(0, max(0, APP.loop_jitter_seconds))
        write_global_log(f"Waiting {wait_seconds:.0f} seconds before next check")
        time.sleep(wait_seconds)


if __name__ == "__main__":
    main()
