#!/usr/bin/env python3
"""Re-verify price and stock for every recorded retailer product, and optionally
discover new candidate products, from a normal internet connection.

  python3 scripts/refresh_listings.py              # re-check known products
  python3 scripts/refresh_listings.py --discover   # also re-run searches -> candidates file
  python3 scripts/build.py                         # regrade from the newest evidence

Re-checks APPEND dated rows to raw/listings_raw.tsv (the log is append-only;
build.py grades on the newest row per retailer+product, so history is kept).
Discovery never auto-classifies: new products land in raw/candidates_<date>.tsv
for a human to mark exact / variant / none and paste into the log.

Endpoints used (public storefront APIs the sites themselves use):
  Shopify      {base}/products/{handle}.js           price in cents, 'available'
               {base}/search/suggest.json?q=...       predictive search
  WooCommerce  {base}/wp-json/wc/store/v1/products?slug=...|search=...
               prices.price in minor units, 'is_in_stock'
FairPrice is not refreshed: its robots.txt disallows automated access.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw" / "listings_raw.tsv"
UA = "CulinaCompass-research/0.1 (student project; polite refresh, 1 req/s)"

PLATFORM = {
    "waangoo": ("shopify", "https://www.waangoo.com"),
    "selvistores": ("shopify", "https://selvistores.sg"),
    "sgwetmarket": ("shopify", "https://sgwetmarket.com.sg"),
    "kiasumart": ("woocommerce", "https://kiasumart.com"),
    "farms2home": ("woocommerce", "https://farms2home.sg"),
}
FIELDS = ["ingredient_id", "retailer_id", "query", "product_title", "price_sgd",
          "in_stock", "product_path", "match", "verified_on"]


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ---- pure parsers (unit-tested with fixtures) ------------------------------
def parse_shopify_product_js(obj: dict) -> dict:
    return {"title": obj["title"], "price_sgd": f"{obj['price'] / 100:.2f}",
            "in_stock": "true" if obj.get("available") else "false"}


def parse_shopify_suggest(obj: dict) -> list[dict]:
    prods = obj.get("resources", {}).get("results", {}).get("products", [])
    return [{"title": p["title"], "price_sgd": f"{float(p['price']):.2f}",
             "in_stock": "true" if p.get("available") else "false",
             "path": "/products/" + p["handle"]} for p in prods]


def parse_woo_products(arr: list) -> list[dict]:
    out = []
    for p in arr:
        minor = int(p["prices"].get("currency_minor_unit", 2))
        out.append({"title": p["name"],
                    "price_sgd": f"{int(p['prices']['price']) / 10 ** minor:.2f}",
                    "in_stock": "true" if p.get("is_in_stock") else "false",
                    "path": p["permalink"]})
    return out


def handle_from_path(path: str) -> str:
    return path.rstrip("/").split("/")[-1].split("?")[0]


# ---- refresh -------------------------------------------------------------
def recheck(row: dict) -> dict | None:
    platform, base = PLATFORM[row["retailer_id"]]
    handle = handle_from_path(row["product_path"])
    if platform == "shopify":
        p = parse_shopify_product_js(get_json(f"{base}/products/{handle}.js"))
        path = f"/products/{handle}"
    else:
        found = parse_woo_products(get_json(
            f"{base}/wp-json/wc/store/v1/products?slug={urllib.parse.quote(handle)}"))
        if not found:
            return None
        p, path = found[0], found[0]["path"]
    return {**row, "product_title": p["title"], "price_sgd": p["price_sgd"],
            "in_stock": p["in_stock"], "product_path": path,
            "verified_on": date.today().isoformat()}


def discover(item: str, retailer: str, query: str) -> list[dict]:
    platform, base = PLATFORM[retailer]
    q = urllib.parse.quote(query)
    if platform == "shopify":
        found = parse_shopify_suggest(get_json(
            f"{base}/search/suggest.json?q={q}&resources[type]=product&resources[limit]=10"))
    else:
        found = parse_woo_products(get_json(
            f"{base}/wp-json/wc/store/v1/products?search={q}&per_page=10"))
    return [{"ingredient_id": item, "retailer_id": retailer, "query": query,
             "product_title": f["title"], "price_sgd": f["price_sgd"], "in_stock": f["in_stock"],
             "product_path": f["path"], "match": "?", "verified_on": date.today().isoformat()}
            for f in found]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(RAW, newline="", encoding="utf-8"), delimiter="\t"))
    known = {}
    for r in rows:
        if r["retailer_id"] in PLATFORM and r["match"] in ("exact", "variant") \
                and r["product_path"] not in ("", "-"):
            known[(r["ingredient_id"], r["retailer_id"], handle_from_path(r["product_path"]))] = r
    fresh, failed = [], []
    for key, r in known.items():
        try:
            new = recheck(r)
            if new is None:
                new = {**r, "in_stock": "false", "verified_on": date.today().isoformat(),
                       "product_title": r["product_title"] + " [delisted]"}
            fresh.append(new)
        except Exception as e:  # network errors are reported, never silently graded
            failed.append((key, repr(e)))
        time.sleep(a.delay)
    with open(RAW, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t")
        for r in fresh:
            w.writerow({k: r[k] for k in FIELDS})
    print(f"re-checked {len(fresh)} products; {len(failed)} failed")
    for k, e in failed[:20]:
        print("  FAIL", k, e)

    if a.discover:
        seen_titles = {(r["retailer_id"], r["product_title"]) for r in rows}
        queries = {(r["ingredient_id"], r["retailer_id"], r["query"]) for r in rows
                   if r["retailer_id"] in PLATFORM and not r["query"].startswith(("collection:", "category:"))}
        cands = []
        for item, retailer, query in sorted(queries):
            try:
                cands += [c for c in discover(item, retailer, query)
                          if (retailer, c["product_title"]) not in seen_titles]
            except Exception as e:
                print("  FAIL discover", item, retailer, query, repr(e))
            time.sleep(a.delay)
        out = ROOT / "raw" / f"candidates_{date.today().isoformat()}.tsv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t")
            w.writeheader()
            w.writerows(cands)
        print(f"{len(cands)} new candidate products -> {out.name} (classify 'match' before merging)")


if __name__ == "__main__":
    main()
