#!/usr/bin/env python3
"""
Daily Othoba.com price scrape, built for unattended scheduled runs.

Differences from the Colab notebook:
  * no Colab / Drive / tqdm-widget dependencies
  * configuration comes from environment variables
  * writes data/othoba_YYYY-MM-DD.csv
  * exits non-zero when the harvest looks wrong, so the scheduler alerts you

That last point matters more than it sounds. A silent failure does not produce
an error, it produces a *hole in the panel* — and a missing day cannot be
recovered later, because the site only ever shows today's prices. Better to be
emailed about a broken run than to discover the gap months afterwards.

Environment variables (all optional):
    FOOD_ONLY=1            restrict to Grocery + Daily Bazar branches
    SCOPE=leaf             leaf | top | all
    WORKERS=3              parallel headless Chrome instances
    PAGE_SIZE=80           items per page (40 / 60 / 80)
    MAX_PAGES=0            0 = no cap
    MIN_ROWS=300           fail the run below this many rows
    OUT_DIR=data
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait

# --------------------------------------------------------------------------- config
BASE = "https://othoba.com"
FOOD_ONLY = os.environ.get("FOOD_ONLY", "1") == "1"
SCOPE = os.environ.get("SCOPE", "leaf")
WORKERS = int(os.environ.get("WORKERS", "3"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "80"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "0")) or None
MIN_ROWS = int(os.environ.get("MIN_ROWS", "300"))
OUT_DIR = os.environ.get("OUT_DIR", "data")
PAGE_WAIT = int(os.environ.get("PAGE_WAIT", "25"))
RECYCLE_EVERY = 20
REQUEST_TIMEOUT = 30
MAX_PAGES_HARD_CAP = 400

# Dhaka is UTC+6; stamp rows with local date so the panel lines up with BBS
DHAKA = timezone(timedelta(hours=6))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
    "Connection": "keep-alive",
}

OK, EMPTY, GONE, BLOCKED, SERVER_ERR, UNKNOWN = (
    "ok", "empty", "gone", "blocked", "server", "unknown")


def log(msg):
    ts = datetime.now(DHAKA).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- fetch
def classify(status, html):
    if status in (403, 429):
        return BLOCKED
    if status == 404:
        return GONE
    if status >= 500:
        return SERVER_ERR
    if not html:
        return UNKNOWN
    low = html[:6000].lower()
    if "just a moment" in low or "cf-browser-verification" in low or "challenge-platform" in low:
        return BLOCKED
    if "product-details" in html:
        return OK
    if "no products were found" in html.lower():
        return EMPTY
    return UNKNOWN


_session = requests.Session()
_session.headers.update(HEADERS)


def fetch_static(url, max_attempts=3):
    """The menu and the product counter are server-rendered, so these are cheap."""
    for attempt in range(1, max_attempts + 1):
        try:
            r = _session.get(url, timeout=REQUEST_TIMEOUT)
            if r.history and urlparse(r.url).path in ("/", ""):
                return r.status_code, r.text, GONE
            v = classify(r.status_code, r.text)
            if v in (OK, EMPTY, GONE):
                return r.status_code, r.text, v
        except requests.RequestException:
            pass
        time.sleep(1.5 * attempt)
    return 0, "", UNKNOWN


PRICE_READY_JS = """
var e = document.querySelectorAll('ins[id^="price_"]');
if (e.length === 0) return false;
for (var i = 0; i < e.length; i++) {
    if (e[i].textContent.trim().length > 0) return true;
}
return false;
"""

NO_PRODUCTS_JS = """
return document.querySelectorAll('div.product-details').length === 0;
"""


def make_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    opts.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2})
    opts.page_load_strategy = "eager"
    # Selenium 4.6+ resolves chromedriver itself, so no webdriver_manager needed
    return webdriver.Chrome(options=opts)


class Browser:
    """One headless Chrome, with session recovery and periodic recycling."""

    def __init__(self):
        self.driver = make_driver()
        self.pages = 0

    def _restart(self):
        try:
            self.driver.quit()
        except Exception:
            pass
        self.driver = make_driver()
        self.pages = 0

    def get(self, url):
        for attempt in range(3):
            try:
                if self.pages >= RECYCLE_EVERY:
                    self._restart()
                self.driver.get(url)
                self.pages += 1
                try:
                    WebDriverWait(self.driver, PAGE_WAIT).until(
                        lambda d: d.execute_script(PRICE_READY_JS)
                        or d.execute_script(NO_PRODUCTS_JS))
                except Exception:
                    pass
                html = self.driver.page_source
                return 200, html, classify(200, html)
            except Exception:
                self._restart()
                time.sleep(2 * (attempt + 1))
        return 0, "", UNKNOWN

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------- parse
MONEY_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
SOLD_RE = re.compile(r"([\d,]+)\s*Sold", re.I)
SHOWING_RE = re.compile(r"Showing\s*([\d,]+)\s*-\s*([\d,]+)\s*of\s*([\d,]+)", re.I)
COUNT_RE = re.compile(r"of\s*([\d,]+)\s*Products", re.I)
SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(ml|l|ltr|litre|liter|mg|g|gm|gms|gram|grams|kg|kgs|pcs|pc|piece|pieces|"
    r"pack|packs|set|sets|tablet|tablets|capsule|capsules)\b", re.I)

UNIT_CANON = {
    "l": "l", "ltr": "l", "litre": "l", "liter": "l", "ml": "ml", "mg": "mg",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kgs": "kg", "pc": "pcs", "pcs": "pcs", "piece": "pcs",
    "pieces": "pcs", "pack": "pack", "packs": "pack", "set": "set", "sets": "set",
    "tablet": "tablets", "tablets": "tablets",
    "capsule": "capsules", "capsules": "capsules",
}


def to_number(text):
    if not text:
        return None
    m = MONEY_RE.search(text.replace("\u09f3", "").replace("৳", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def extract_size(name):
    if not name:
        return None, None, None
    matches = SIZE_RE.findall(name)
    if not matches:
        return None, None, None
    value, raw_unit = matches[-1]
    unit = UNIT_CANON.get(raw_unit.lower(), raw_unit.lower())
    try:
        value = float(value)
    except ValueError:
        return None, None, None
    return f"{value:g} {unit}", value, unit


def _text(node):
    return node.get_text(strip=True) if node else None


def parse_products(html, category_slug=None):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for card in soup.select("div.product-details"):
        pid_input = card.select_one("input.dl-product-id")
        pid = pid_input["value"].strip() if pid_input and pid_input.has_attr("value") else None

        # Anchor on the id: each card also holds an empty
        # ins#product_totalsolditemmobile_<pid> with the same class.
        price_node = card.select_one(f"ins#price_{pid}") if pid else None
        if price_node is None:
            for cand in card.select("ins.new-price"):
                if to_number(cand.get_text()):
                    price_node = cand
                    break

        old_node = card.select_one(f"del#oldPrice_{pid}") if pid else card.select_one("del.old-price")
        name_node = card.select_one("h4.product-name a")
        name = _text(name_node)
        href = name_node.get("href") if name_node else None

        sku_input = card.select_one("input.dl-product-sku")
        vendor_input = card.select_one("input.dl-vendor-name")
        cat_input = card.select_one("input.dl-category-name")
        sold_node = card.select_one(f"#product_totalsolditem_{pid}") if pid else None
        sold_m = SOLD_RE.search(_text(sold_node) or "")
        rating_node = card.select_one(f"#ratingStar_{pid}") if pid else None

        price = to_number(_text(price_node))
        old_price = to_number(_text(old_node))
        if not name or price is None:
            continue

        amount_txt, size_val, size_unit = extract_size(name)
        rows.append({
            "Product ID": pid,
            "Name": name,
            "Amount": amount_txt,
            "Size Value": size_val,
            "Size Unit": size_unit,
            "Price (BDT)": price,
            "Old Price (BDT)": old_price,
            "Discount (%)": (round((old_price - price) / old_price * 100, 2)
                             if old_price and old_price > 0 and old_price >= price else None),
            "SKU": sku_input["value"].strip() if sku_input and sku_input.has_attr("value") else None,
            "Vendor": vendor_input["value"].strip() if vendor_input and vendor_input.has_attr("value") else None,
            "Category": cat_input["value"].strip() if cat_input and cat_input.has_attr("value") else category_slug,
            "Scraped Category": category_slug,
            "Units Sold": int(sold_m.group(1).replace(",", "")) if sold_m else None,
            "Rating": to_number(_text(rating_node)),
            "URL": urljoin(BASE, href) if href else None,
        })
    return rows


def parse_total(html):
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    m = SHOWING_RE.search(text)
    if m:
        return tuple(int(g.replace(",", "")) for g in m.groups())
    m = COUNT_RE.search(text)
    if m:
        return (None, None, int(m.group(1).replace(",", "")))
    return (None, None, None)


# --------------------------------------------------------------------------- discovery
SKIP_PATHS = {
    "cart", "wishlist", "compareproducts", "register", "login", "customer",
    "order", "passwordrecovery", "about-us", "contactus", "career",
    "privacy-policy", "terms-and-conditions", "seller-info",
    "how-to-shop-on-othoba", "cancellation-and-returns", "same-day-delivery",
    "certified-products", "featured-recommendation", "search", "newproducts",
    "Common", "sitemap",
}

FOOD_SLUGS = {
    "food-grocery", "daily-bazar", "quick-commerce",
    "food-item", "milk", "liquid-milk", "powder-milk", "frozen-food-snacks",
    "oil", "fresh-vegetables", "rice", "flour", "spice", "spices", "ready-mix",
    "groceries", "eggs", "meat-fish", "fruits-vegetable", "lentil", "salt-sugar",
    "food-additives", "vinegars", "noodles-item", "noodles", "macaroni",
    "snacks", "pasta-macaroni", "pasta-macarony", "biscuites-toast", "cereal",
    "sauces", "ice-cream", "soup-2", "local-snacks", "chips",
    "dairy", "ghee-butter", "cheese", "yogurt", "popular-item",
    "bread-bakery", "bread", "biscuit-cookies", "dessert", "spreads",
    "bakery-snacks", "sweetmeat", "confectionery", "vermicellies",
    "breakfast-products", "chocolate-candy", "rice-biryani",
    "grocery-staples", "bakery-breakfast", "daily-cooking", "spice-herb",
    "baking-2", "dairy-chilled-eggs", "milk-3", "eggs-2", "cheese-3",
    "yogurt-3", "butter-3", "laban-borhani", "ghee-3",
}


def discover_categories(seed=f"{BASE}/food-grocery"):
    status, html, verdict = fetch_static(seed)
    if verdict != OK:
        raise RuntimeError(f"Could not load seed page ({verdict}, HTTP {status})")

    soup = BeautifulSoup(html, "lxml")
    found = {}
    for nav in soup.select("ul"):
        for a in nav.select("a[href]"):
            href = a["href"]
            if href.startswith(("#", "tel:", "javascript")):
                continue
            full = urljoin(BASE, href)
            if urlparse(full).netloc not in ("othoba.com", "www.othoba.com"):
                continue
            path = urlparse(full).path.strip("/")
            if not path or "/" in path or path in SKIP_PATHS:
                continue
            if re.search(r"-\d{5,}$", path):
                continue

            depth = max(len(a.find_parents("ul")) - 1, 0)
            name = a.get_text(" ", strip=True) or path
            li = a.find_parent("li")
            has_children = bool(li and li.find("ul") and li.find("ul").find("a", href=True))

            prev = found.get(path)
            if prev is None or depth < prev["depth"]:
                found[path] = {"slug": path, "name": name,
                               "depth": depth, "has_children": has_children}
            elif has_children:
                found[path]["has_children"] = True

    return sorted(found.values(), key=lambda c: (c["depth"], c["slug"]))


# --------------------------------------------------------------------------- scrape
def scrape_category(browser, cat, page_size, max_pages=None):
    slug = cat["slug"]
    rows, seen = [], set()
    page, expected_pages = 1, None

    first_url = f"{BASE}/{slug}?pagenumber=1" + (f"&pagesize={page_size}" if page_size else "")
    _s, _h, _v = fetch_static(first_url)
    if _v == GONE:
        return [], GONE, 0
    _f, _l, total = parse_total(_h)
    if total is not None and page_size:
        expected_pages = max(1, -(-total // page_size))
    if total == 0:
        return [], EMPTY, 0

    while True:
        if max_pages and page > max_pages:
            break
        if page > MAX_PAGES_HARD_CAP:
            break

        url = f"{BASE}/{slug}?pagenumber={page}" + (f"&pagesize={page_size}" if page_size else "")
        status, html, verdict = browser.get(url)

        if verdict == GONE:
            return rows, (GONE if page == 1 else OK), page - 1
        if verdict not in (OK, EMPTY):
            return rows, (verdict if page == 1 else OK), page - 1

        page_rows = parse_products(html, slug)
        new = [r for r in page_rows if r["Product ID"] not in seen]
        seen.update(r["Product ID"] for r in new)
        rows.extend(new)

        if not page_rows or not new:
            break
        if expected_pages and page >= expected_pages:
            break

        page += 1
        time.sleep(random.uniform(0.2, 0.6))

    return rows, (OK if rows else EMPTY), page


# --------------------------------------------------------------------------- main
lock = threading.Lock()


def main():
    started = time.time()
    run_date = datetime.now(DHAKA).strftime("%Y-%m-%d")
    log(f"Run for {run_date} (Asia/Dhaka)")

    cats = discover_categories()
    log(f"menu: {len(cats)} categories")

    if SCOPE == "top":
        cats = [c for c in cats if c["depth"] == 0]
    elif SCOPE == "leaf":
        cats = [c for c in cats if not c["has_children"]] or cats
    if FOOD_ONLY:
        food = [c for c in cats if c["slug"] in FOOD_SLUGS]
        if food:
            cats = food
    log(f"scope={SCOPE} food_only={FOOD_ONLY} -> {len(cats)} categories")

    # Size the run so the biggest categories start first
    for c in cats:
        u = f"{BASE}/{c['slug']}?pagenumber=1&pagesize={PAGE_SIZE}"
        _s, _h, _v = fetch_static(u)
        _f, _l, t = parse_total(_h) if _v == OK else (None, None, None)
        c["products"] = t or 0
    cats.sort(key=lambda c: -c["products"])
    log(f"expecting ~{sum(c['products'] for c in cats)} products")

    all_rows, stats = [], {"ok": 0, "empty": 0, "gone": 0, "error": 0}

    def worker(chunk, wid):
        browser = Browser()
        local = []
        try:
            for cat in chunk:
                try:
                    rows, verdict, pages = scrape_category(
                        browser, cat, PAGE_SIZE, MAX_PAGES)
                except Exception as e:
                    log(f"[W{wid}] ERR  {cat['slug']}: {type(e).__name__}: {e}")
                    with lock:
                        stats["error"] += 1
                    continue
                local += rows
                with lock:
                    stats[verdict if verdict in stats else "error"] = \
                        stats.get(verdict if verdict in stats else "error", 0) + 1
                log(f"[W{wid}] {verdict.upper():6s} {cat['slug']:<26s} "
                    f"{len(rows):5d} items {pages:3d}p")
        finally:
            browser.close()
        with lock:
            all_rows.extend(local)

    chunks = [cats[i::WORKERS] for i in range(WORKERS)]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(worker, ch, i) for i, ch in enumerate(chunks) if ch]
        for f in as_completed(futs):
            f.result()

    if not all_rows:
        log("FATAL: no rows collected")
        return 1

    df = pd.DataFrame(all_rows)
    # A product can appear under several categories. Keep one row per product,
    # but pick the surviving category deterministically: without the sort the
    # winner depends on which worker finished first, so a product's category
    # label could flip between days and destabilise category-level series.
    df = (df.sort_values(["Product ID", "Scraped Category"])
            .drop_duplicates(subset=["Product ID"], keep="first")
            .reset_index(drop=True))
    df.insert(0, "Date", run_date)

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"othoba_{run_date}.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    mins = (time.time() - started) / 60
    log("-" * 56)
    log(f"products      : {len(df)}")
    log(f"categories    : ok={stats['ok']} empty={stats['empty']} "
        f"gone={stats['gone']} error={stats['error']}")
    log(f"median price  : {df['Price (BDT)'].median():.0f} BDT")
    log(f"size parsed   : {df['Amount'].notna().mean():.0%}")
    log(f"elapsed       : {mins:.1f} min")
    log(f"written       : {out}")
    log("-" * 56)

    # Guard rails. A silent partial harvest is worse than a loud failure,
    # because a missing day can never be back-filled.
    if len(df) < MIN_ROWS:
        log(f"FAIL: only {len(df)} rows, expected at least {MIN_ROWS}")
        return 1
    if stats["error"] > len(cats) / 3:
        log(f"FAIL: {stats['error']} categories errored out of {len(cats)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
