"""
dse_client.py
Fetches live share prices from the Dhaka Stock Exchange (DSE).

DSE has no official public API, so this scrapes the public live-price
table published at dsebd.org.

If dsebd.org changes its page layout, only this file needs updating —
everything else (portfolio, bot, alerts) talks to the clean dict this
module returns.

Notes on two failure modes we've hit:

1. Endpoint churn. `latest_share_price_all.php` was removed (404 as of
   Sep 2026). DSE_LIVE_URLS is a candidate list tried in order; add or
   reorder entries there, no other code needs to change.

2. TLS. A CERTIFICATE_VERIFY_FAILED / "unable to get local issuer
   certificate" error means either your local trust store is stale
   (`pip install -U certifi`) or dsebd.org is serving an incomplete
   chain. For the latter, build a merged bundle and point DSE_CA_BUNDLE
   at it:

       openssl s_client -connect www.dsebd.org:443 \
           -servername www.dsebd.org </dev/null 2>/dev/null \
           | openssl x509 -out leaf.pem
       openssl x509 -in leaf.pem -noout -text | grep -A1 "CA Issuers"
       curl -sO <that URL>
       openssl x509 -inform DER -in <file>.crt -out intermediate.pem
       cat "$(python -c 'import certifi;print(certifi.where())')" \
           intermediate.pem > dse-bundle.pem
       export DSE_CA_BUNDLE=$PWD/dse-bundle.pem

   DSE_INSECURE=1 disables verification as a last-resort, temporary
   unblock — it logs a warning every time it's used. Don't wire it in
   permanently.

Columns are resolved from the header row by name rather than fixed
position, so DSE inserting/reordering a column no longer silently
corrupts every row.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import certifi
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Tried in order; first one that returns a parseable price table wins.
DSE_LIVE_URLS = [
    "https://www.dsebd.org/latest_share_price_scroll_l.php",
    "https://www.dsebd.org/latest_share_price_scroll_by_ltp.php",
    "https://www.dsebd.org/latest_share_price_scroll_by_change.php",
    "https://www.dsebd.org/latest_share_price_all.php",  # legacy, 404 since ~2026
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Override if dsebd.org serves an incomplete chain, or you're behind a
# TLS-intercepting proxy. See module docstring.
CA_BUNDLE = os.environ.get("DSE_CA_BUNDLE", certifi.where())

# Temporary escape hatch only. Logs a warning whenever it's used.
INSECURE = os.environ.get("DSE_INSECURE") == "1"

# Simple in-memory cache so hammering /portfolio or /price commands
# doesn't re-scrape the whole market every time within a short window.
_CACHE_TTL_SECONDS = 60
_cache: Dict[str, "SharePrice"] = {}
_cache_ts: float = 0.0
_last_good_url: Optional[str] = None

_session = requests.Session()
_session.headers.update(HEADERS)
if INSECURE:
    logger.warning(
        "DSE_INSECURE=1 — TLS verification is DISABLED. Prices are "
        "unauthenticated and may be tampered with in transit."
    )
    _session.verify = False
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
else:
    _session.verify = CA_BUNDLE


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


# Maps our field name -> the header labels dsebd.org has used for it.
# Matched longest-alias-first so "% CHANGE" isn't swallowed by "CHANGE".
_FIELD_ALIASES: Dict[str, List[str]] = {
    "trading_code": ["TRADING CODE", "TRADINGCODE", "CODE"],
    "ltp": ["LTP", "LAST TRADE PRICE", "LTP*"],
    "high": ["HIGH"],
    "low": ["LOW"],
    "close_price": ["CLOSEP", "CLOSEP*", "CLOSE PRICE", "CLOSE"],
    "ycp": ["YCP", "YCP*", "YESTERDAY CLOSE"],
    "change_pct": ["% CHANGE", "CHANGE %", "PCHANGE", "%CHANGE"],
    "change": ["CHANGE"],
    "volume": ["VOLUME", "VOL", "TOTAL VOLUME"],
}
_REQUIRED_FIELDS = ("trading_code", "ltp")


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split()).strip()


def _normalise_header(text: str) -> str:
    out = _clean(text).upper()
    for ch in "()*.":
        out = out.replace(ch, "")
    return " ".join(out.split())


def _to_float(text: str) -> Optional[float]:
    text = _clean(text).replace(",", "").replace("%", "")
    if text in ("", "--", "-", "N/A", "NA"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(text: str) -> Optional[int]:
    val = _to_float(text)
    return int(val) if val is not None else None


def _find_price_table(soup: BeautifulSoup):
    """The price table is the one whose header row mentions TRADING CODE."""
    for table in soup.find_all("table"):
        head = table.find("tr")
        if head and "TRADING CODE" in _normalise_header(head.get_text()):
            return table
    return None


def _map_columns(header_row) -> Dict[str, int]:
    """Resolve field name -> column index from the header row."""
    cells = [_normalise_header(c.get_text()) for c in header_row.find_all(["th", "td"])]
    mapping: Dict[str, int] = {}
    taken: set = set()

    candidates = sorted(
        ((alias, field) for field, aliases in _FIELD_ALIASES.items() for alias in aliases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for alias, field in candidates:
        if field in mapping:
            continue
        for idx, cell in enumerate(cells):
            if idx in taken:
                continue
            if cell == alias:
                mapping[field] = idx
                taken.add(idx)
                break

    missing = [f for f in _REQUIRED_FIELDS if f not in mapping]
    if missing:
        raise RuntimeError(
            f"Could not find required column(s) {', '.join(missing)} in the "
            f"dsebd.org header row: {cells}. Update _FIELD_ALIASES in dse_client.py."
        )
    return mapping


def _parse_table(table) -> Dict[str, SharePrice]:
    rows = table.find_all("tr")
    if not rows:
        raise RuntimeError("Price table had no rows.")

    colmap = _map_columns(rows[0])

    def cell(cols: List[str], field: str) -> str:
        idx = colmap.get(field, -1)
        return cols[idx] if 0 <= idx < len(cols) else ""

    results: Dict[str, SharePrice] = {}
    for row in rows[1:]:
        cols = [_clean(c.get_text()) for c in row.find_all("td")]
        if len(cols) < 3:
            continue

        code = cell(cols, "trading_code").upper()
        ltp = _to_float(cell(cols, "ltp"))
        if not code or ltp is None:
            continue

        results[code] = SharePrice(
            trading_code=code,
            ltp=ltp,
            high=_to_float(cell(cols, "high")),
            low=_to_float(cell(cols, "low")),
            close_price=_to_float(cell(cols, "close_price")),
            ycp=_to_float(cell(cols, "ycp")),
            change=_to_float(cell(cols, "change")),
            change_pct=_to_float(cell(cols, "change_pct")),
            volume=_to_int(cell(cols, "volume")),
        )

    if not results:
        raise RuntimeError(
            "Parsed 0 rows from dsebd.org — layout likely changed, "
            "update the column mapping in dse_client.py."
        )
    return results


def _fetch_from(url: str) -> Dict[str, SharePrice]:
    resp = _session.get(url, timeout=20)
    if resp.status_code == 404:
        raise RuntimeError(f"404 — endpoint removed: {url}")
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = _find_price_table(soup)
    if table is None:
        raise RuntimeError(
            f"No table with a TRADING CODE header at {url} — the page "
            "layout may have changed."
        )
    return _parse_table(table)


def fetch_all_prices(force_refresh: bool = False) -> Dict[str, SharePrice]:
    """
    Returns a dict of trading_code -> SharePrice for every listed
    instrument currently on the DSE live price page. Cached for
    _CACHE_TTL_SECONDS to keep this polite to dsebd.org.
    """
    global _cache, _cache_ts, _last_good_url

    if not force_refresh and _cache and (time.time() - _cache_ts) < _CACHE_TTL_SECONDS:
        return _cache

    urls = list(DSE_LIVE_URLS)
    if _last_good_url in urls:
        urls.remove(_last_good_url)
        urls.insert(0, _last_good_url)

    failures = []
    for url in urls:
        try:
            results = _fetch_from(url)
        except requests.exceptions.SSLError as exc:
            raise RuntimeError(
                "TLS verification failed for dsebd.org. Either refresh your "
                "trust store (pip install -U certifi) or, if the site serves "
                "an incomplete chain, build a merged bundle and set "
                "DSE_CA_BUNDLE — see the notes at the top of dse_client.py."
            ) from exc
        except (RuntimeError, requests.RequestException) as exc:
            failures.append(f"  {url}\n    -> {exc}")
            continue

        _cache = results
        _cache_ts = time.time()
        _last_good_url = url
        return results

    raise RuntimeError(
        "Every DSE endpoint failed:\n" + "\n".join(failures) +
        "\n\nOpen one of these URLs in a browser to see what changed, then "
        "update DSE_LIVE_URLS / _FIELD_ALIASES in dse_client.py."
    )


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
