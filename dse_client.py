"""dse_client.py — DSE price feed backed by the `bdshare` library.

Public interface is unchanged from the scraper version:
    SharePrice, fetch_all_prices(), get_price(code), search_codes(query)

    pip install -U bdshare "numpy<2.4"
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import bdshare.stock.trading as _trading
from bdshare import get_current_trade_data

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60

# DSE launched a new website; the old price pages (latest_share_price_scroll_*)
# now live on old.dsebd.org, while dsebd.org / dse.com.bd return HTTP 410.
# bdshare builds its URL from vs.DSE_URL at call time, so repoint it.
# Override with the DSE_BASE_URL env var if the host changes again.
DSE_BASE_URL = os.environ.get("DSE_BASE_URL", "https://old.dsebd.org").rstrip("/")


def _point_bdshare_at_legacy_site() -> None:
    vs = getattr(_trading, "vs", None)
    orig = getattr(vs, "DSE_URL", None)
    if not isinstance(orig, str):
        log.warning("bdshare layout changed; could not override DSE_URL")
        return
    vs.DSE_URL = DSE_BASE_URL + ("/" if orig.endswith("/") else "")
    log.info("bdshare DSE_URL: %s -> %s", orig, vs.DSE_URL)


_point_bdshare_at_legacy_site()


# NOTE: adjust to match the original SharePrice in your project.
@dataclass
class SharePrice:
    code: str
    ltp: float
    high: float
    low: float
    close: float
    ycp: float
    change: float
    trade: int
    value_mn: float
    volume: int


_cache: Dict[str, SharePrice] = {}
_cache_ts: float = 0.0
_lock = threading.Lock()


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _from_dataframe(df) -> Dict[str, SharePrice]:
    # bdshare may return the symbol as the index or as a column
    if "symbol" not in df.columns:
        df = df.reset_index()
    df.columns = [str(c).strip().lower() for c in df.columns]

    out: Dict[str, SharePrice] = {}
    for row in df.to_dict("records"):
        code = str(row.get("symbol", "")).strip().upper()
        if not code:
            continue
        out[code] = SharePrice(
            code=code,
            ltp=_num(row.get("ltp")),
            high=_num(row.get("high")),
            low=_num(row.get("low")),
            close=_num(row.get("close")),
            ycp=_num(row.get("ycp")),
            change=_num(row.get("change")),
            trade=int(_num(row.get("trade"))),
            value_mn=_num(row.get("value")),
            volume=int(_num(row.get("volume"))),
        )
    return out


def fetch_all_prices(force: bool = False) -> Dict[str, SharePrice]:
    """Return {code: SharePrice} for every DSE instrument (cached 60s)."""
    global _cache, _cache_ts
    with _lock:
        fresh = (time.time() - _cache_ts) < CACHE_TTL_SECONDS
        if _cache and fresh and not force:
            return _cache
        try:
            df = get_current_trade_data()
            data = _from_dataframe(df)
            if not data:
                raise RuntimeError("bdshare returned no rows")
            _cache, _cache_ts = data, time.time()
        except Exception as exc:
            if _cache:  # serve stale data rather than break alerts/bot
                log.warning("DSE fetch failed (%s); serving stale cache", exc)
                return _cache
            raise
        return _cache


def get_price(code: str) -> Optional[SharePrice]:
    return fetch_all_prices().get(code.strip().upper())


def search_codes(query: str) -> List[str]:
    q = query.strip().upper()
    return sorted(c for c in fetch_all_prices() if q in c)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Using DSE_URL:", _trading.vs.DSE_URL)
    p = get_price("GP")
    print(p or "GP not found", f"| {len(fetch_all_prices())} instruments")