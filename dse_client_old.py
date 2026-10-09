"""
dse_client.py
Fetches live share prices from the Dhaka Stock Exchange (DSE).

DSE has no official public API, so this scrapes the public live-price
table published at dsebd.org. This is the same approach used by every
community DSE tool (dse-scraper, DSE_COMPANY_SCRAPER_Python, etc).

If dsebd.org changes its page layout, only this file needs updating —
everything else (portfolio, bot, alerts) talks to the clean dict this
module returns.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup

DSE_LIVE_URL = "https://www.dsebd.org/latest_share_price_all.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Simple in-memory cache so hammering /portfolio or /price commands
# doesn't re-scrape the whole market every time within a short window.
_CACHE_TTL_SECONDS = 60
_cache: Dict[str, "SharePrice"] = {}
_cache_ts: float = 0.0


@dataclass
class SharePrice:
    trading_code: str
    ltp: float          # last traded price
    high: Optional[float]
    low: Optional[float]
    close_price: Optional[float]
    ycp: Optional[float]  # yesterday's close price
    change: Optional[float]
    change_pct: Optional[float]
    volume: Optional[int]


def _to_float(text: str) -> Optional[float]:
    text = text.strip().replace(",", "")
    if text in ("", "--", "-", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(text: str) -> Optional[int]:
    val = _to_float(text)
    return int(val) if val is not None else None


def fetch_all_prices(force_refresh: bool = False) -> Dict[str, SharePrice]:
    """
    Returns a dict of trading_code -> SharePrice for every listed
    instrument currently on the DSE live price page. Cached for
    _CACHE_TTL_SECONDS to keep this polite to dsebd.org.
    """
    global _cache, _cache_ts

    if not force_refresh and _cache and (time.time() - _cache_ts) < _CACHE_TTL_SECONDS:
        return _cache

    resp = requests.get(DSE_LIVE_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", {"id": "smSelect"}) or soup.find(
        "table", string=re.compile("TRADING CODE", re.I)
    )

    if table is None:
        # Fall back: dsebd.org wraps the price table in nested tables;
        # find the table whose header row contains "TRADING CODE".
        for t in soup.find_all("table"):
            header_text = t.find("tr")
            if header_text and "TRADING CODE" in header_text.get_text().upper():
                table = t
                break

    if table is None:
        raise RuntimeError(
            "Could not locate the price table on dsebd.org — the page "
            "layout may have changed. Inspect the HTML and update "
            "dse_client.py's table selector."
        )

    rows = table.find_all("tr")[1:]  # skip header
    results: Dict[str, SharePrice] = {}

    for row in rows:
        cols = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cols) < 9:
            continue

        # Column order on dsebd.org's all-shares table:
        # # | TRADING CODE | LTP | HIGH | LOW | CLOSEP | YCP | CHANGE | %CHANGE | TRADE | VALUE(mn) | VOLUME
        try:
            trading_code = cols[1].strip()
            if not trading_code:
                continue
            ltp = _to_float(cols[2])
            if ltp is None:
                continue
            price = SharePrice(
                trading_code=trading_code,
                ltp=ltp,
                high=_to_float(cols[3]),
                low=_to_float(cols[4]),
                close_price=_to_float(cols[5]),
                ycp=_to_float(cols[6]),
                change=_to_float(cols[7]),
                change_pct=_to_float(cols[8].replace("%", "")) if len(cols) > 8 else None,
                volume=_to_int(cols[-1]),
            )
            results[trading_code.upper()] = price
        except (IndexError, ValueError):
            continue

    if not results:
        raise RuntimeError(
            "Parsed 0 rows from dsebd.org — layout likely changed, "
            "update the column mapping in dse_client.py."
        )

    _cache = results
    _cache_ts = time.time()
    return results


def get_price(trading_code: str) -> Optional[SharePrice]:
    """Look up a single trading code, e.g. get_price('BRACBANK')."""
    prices = fetch_all_prices()
    return prices.get(trading_code.upper())


def search_codes(substring: str, limit: int = 15) -> list[str]:
    """Find trading codes containing `substring` (case-insensitive)."""
    prices = fetch_all_prices()
    sub = substring.upper()
    return [code for code in prices if sub in code][:limit]


if __name__ == "__main__":
    # Quick manual test: python dse_client.py BRACBANK
    import sys

    prices = fetch_all_prices()
    print(f"Fetched {len(prices)} instruments from DSE.")
    code = sys.argv[1].upper() if len(sys.argv) > 1 else "BRACBANK"
    p = prices.get(code)
    print(p if p else f"{code} not found")
