"""
dse_client.py
Fetches live share prices from the Dhaka Stock Exchange (DSE) via the
`bdshare` library.

Public interface is identical to the original scraper version, so bot.py,
portfolio.py and alerts.py need no changes:

    SharePrice(trading_code, ltp, high, low, close_price, ycp,
               change, change_pct, volume)
    fetch_all_prices(force_refresh=False) -> Dict[str, SharePrice]
    get_price(trading_code)               -> Optional[SharePrice]
    search_codes(substring, limit=15)     -> list[str]

    pip install -U bdshare "numpy<2.4"
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

import bdshare.stock.trading as _trading
from bdshare import get_current_trade_data

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Host / TLS configuration
# ---------------------------------------------------------------------------
# DSE launched a new website; the legacy price pages
# (latest_share_price_scroll_*) now live on old.dsebd.org, while dsebd.org and
# dse.com.bd return HTTP 410. bdshare builds its URL from vs.DSE_URL at call
# time, so repoint it. Override with DSE_BASE_URL if the host changes again.
DSE_BASE_URL = os.environ.get("DSE_BASE_URL", "https://old.dsebd.org").rstrip("/")

# Optional CA chain for the "unable to get local issuer certificate" error.
# bdshare uses `requests`, which reads REQUESTS_CA_BUNDLE.
_ca = os.environ.get("DSE_CA_BUNDLE")
if _ca:
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)


def _point_bdshare_at_legacy_site() -> None:
    vs = getattr(_trading, "vs", None)
    orig = getattr(vs, "DSE_URL", None)
    if not isinstance(orig, str):
        log.warning("bdshare layout changed; could not override DSE_URL")
        return
    vs.DSE_URL = DSE_BASE_URL + ("/" if orig.endswith("/") else "")
    log.info("bdshare DSE_URL: %s -> %s", orig, vs.DSE_URL)


_point_bdshare_at_legacy_site()

# ---------------------------------------------------------------------------
# Cache (60s, as before)
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 60
_cache: Dict[str, "SharePrice"] = {}
_cache_ts: float = 0.0
_lock = threading.Lock()


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


def _to_float(value) -> Optional[float]:
    text = str(value).strip().replace(",", "")
    if text in ("", "--", "-", "N/A", "nan", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _price(value) -> Optional[float]:
    """A price of 0 means 'no trade yet' on DSE, so treat it as missing."""
    v = _to_float(value)
    return v if v is not None and v > 0 else None


def _from_dataframe(df) -> Dict[str, SharePrice]:
    # bdshare may return the symbol as the index or as a column
    if "symbol" not in df.columns:
        df = df.reset_index()
    df.columns = [str(c).strip().lower() for c in df.columns]

    results: Dict[str, SharePrice] = {}
    for row in df.to_dict("records"):
        code = str(row.get("symbol", "")).strip()
        if not code:
            continue

        ycp = _price(row.get("ycp"))
        ltp = _price(row.get("ltp"))
        traded = ltp is not None
        if ltp is None:
            # No trade yet today: fall back to yesterday's close so the bot
            # shows the last known price and 'below' alerts don't fire on 0.
            ltp = ycp
        if ltp is None:
            continue  # nothing usable, same as the old `ltp is None` skip

        change = _to_float(row.get("change")) if traded else None
        if change is None and traded and ycp:
            change = round(ltp - ycp, 2)
        change_pct = round((ltp - ycp) / ycp * 100, 2) if traded and ycp else None

        vol = _to_float(row.get("volume"))
        results[code.upper()] = SharePrice(
            trading_code=code,
            ltp=ltp,
            high=_price(row.get("high")),
            low=_price(row.get("low")),
            close_price=_price(row.get("close")),
            ycp=ycp,
            change=change,
            change_pct=change_pct,
            volume=int(vol) if vol is not None else None,
        )
    return results


def fetch_all_prices(force_refresh: bool = False) -> Dict[str, SharePrice]:
    """
    Returns a dict of trading_code -> SharePrice for every listed
    instrument on the DSE live price page. Cached for _CACHE_TTL_SECONDS.
    If a refresh fails and an older cache exists, the stale data is served
    so alerts and the bot keep working.
    """
    global _cache, _cache_ts
    with _lock:
        fresh = (time.time() - _cache_ts) < _CACHE_TTL_SECONDS
        if not force_refresh and _cache and fresh:
            return _cache
        try:
            results = _from_dataframe(get_current_trade_data())
            if not results:
                raise RuntimeError("bdshare returned 0 usable rows")
            _cache, _cache_ts = results, time.time()
        except Exception as exc:
            if _cache:
                log.warning("DSE fetch failed (%s); serving stale cache", exc)
                return _cache
            raise
        return _cache


def get_price(trading_code: str) -> Optional[SharePrice]:
    """Look up a single trading code, e.g. get_price('BRACBANK')."""
    return fetch_all_prices().get(trading_code.upper())


def search_codes(substring: str, limit: int = 15) -> list[str]:
    """Find trading codes containing `substring` (case-insensitive)."""
    sub = substring.upper()
    return [code for code in fetch_all_prices() if sub in code][:limit]


if __name__ == "__main__":
    # Quick manual test: python dse_client.py BRACBANK
    import sys

    logging.basicConfig(level=logging.INFO)
    print("Using DSE_URL:", _trading.vs.DSE_URL)
    prices = fetch_all_prices()
    print(f"Fetched {len(prices)} instruments from DSE.")
    code = sys.argv[1].upper() if len(sys.argv) > 1 else "BRACBANK"
    p = prices.get(code)
    print(p if p else f"{code} not found")