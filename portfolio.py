"""
portfolio.py
Tracks buy/sell transactions, dividends and current holdings for one user,
persisted to a plain JSON file (data/portfolio.json). No database server needed.

Data shape:
{
  "transactions": [
    {"ts": "2026-09-17T10:30:00", "code": "BRACBANK", "side": "buy",
     "qty": 100, "price": 45.5},
    {"ts": "2026-10-01T09:00:00", "code": "BRACBANK", "side": "bonus",
     "qty": 10, "price": 0.0},          # stock dividend (bonus shares)
    ...
  ],
  "dividends": [                         # cash dividends
    {"ts": "2026-10-05T12:00:00", "code": "BRACBANK",
     "per_share": 1.5, "qty": 110},
    ...
  ]
}

Holdings (qty, avg cost) are derived from transactions on the fly, so the
transaction log is always the single source of truth — good for reports
and auditing. Stock dividends are logged as zero-cost "bonus" transactions
so they flow through the same average-cost maths; cash dividends live in
their own list because they don't change what you hold.

Files written before dividends existed have no "dividends" key and load fine.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_FILE = os.path.join(DATA_DIR, "portfolio.json")


@dataclass
class Transaction:
    ts: str
    code: str
    side: str  # "buy" or "sell"
    qty: float
    price: float

    @property
    def total(self) -> float:
        return self.qty * self.price


@dataclass
class Dividend:
    """A cash dividend received: `qty` shares x `per_share` taka."""
    ts: str
    code: str
    per_share: float
    qty: float

    @property
    def total(self) -> float:
        return self.qty * self.per_share


@dataclass
class Holding:
    code: str
    qty: float = 0.0
    avg_cost: float = 0.0
    realized_pl: float = 0.0  # profit/loss already locked in from sells


class Portfolio:
    def __init__(self, path: str = DATA_FILE):
        self.path = path
        self._transactions: List[Transaction] = []
        self._dividends: List[Dividend] = []
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(self.path):
            self._save()
            return
        with open(self.path, "r") as f:
            raw = json.load(f)
        self._transactions = [Transaction(**t) for t in raw.get("transactions", [])]
        self._dividends = [Dividend(**d) for d in raw.get("dividends", [])]

    def _save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {
            "transactions": [vars(t) for t in self._transactions],
            "dividends": [vars(d) for d in self._dividends],
        }
        with open(self.path, "w") as f:
            json.dump(payload, f, indent=2)

    # ---------- mutations ----------

    def buy(self, code: str, qty: float, price: float, ts: Optional[str] = None) -> Transaction:
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        tx = Transaction(ts=ts or datetime.now().isoformat(timespec="seconds"),
                          code=code.upper(), side="buy", qty=qty, price=price)
        self._transactions.append(tx)
        self._save()
        return tx

    def sell(self, code: str, qty: float, price: float, ts: Optional[str] = None) -> Transaction:
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        holding = self.get_holding(code)
        if holding.qty < qty:
            raise ValueError(
                f"Cannot sell {qty} of {code.upper()} — you only hold {holding.qty}"
            )
        tx = Transaction(ts=ts or datetime.now().isoformat(timespec="seconds"),
                          code=code.upper(), side="sell", qty=qty, price=price)
        self._transactions.append(tx)
        self._save()
        return tx

    # ---------- dividends ----------

    def add_cash_dividend(self, code: str, per_share: float, qty: Optional[float] = None,
                          ts: Optional[str] = None) -> Dividend:
        """Record a cash dividend. `qty` defaults to the shares you hold now."""
        code = code.upper()
        if per_share <= 0:
            raise ValueError("dividend per share must be positive")
        if qty is None:
            qty = self.get_holding(code).qty
        if qty <= 0:
            raise ValueError(
                f"You don't hold any {code}. If you held it on the record date, "
                f"pass the share count explicitly."
            )
        div = Dividend(ts=ts or datetime.now().isoformat(timespec="seconds"),
                       code=code, per_share=per_share, qty=qty)
        self._dividends.append(div)
        self._save()
        return div

    def add_stock_dividend(self, code: str, pct: float, qty: Optional[float] = None,
                           ts: Optional[str] = None) -> Transaction:
        """
        Record a stock dividend of `pct` percent (10 => 1 bonus share per 10
        held). Whole shares only: the bonus is rounded down. `qty` defaults to
        the shares you hold now. Logged as a zero-cost "bonus" transaction,
        which lowers your average cost per share.
        """
        code = code.upper()
        if pct <= 0:
            raise ValueError("stock dividend % must be positive")
        if qty is None:
            qty = self.get_holding(code).qty
        if qty <= 0:
            raise ValueError(
                f"You don't hold any {code}. If you held it on the record date, "
                f"pass the share count explicitly."
            )
        bonus = math.floor(qty * pct / 100 + 1e-9)
        if bonus < 1:
            raise ValueError(
                f"{pct:g}% of {qty:g} shares is less than one whole bonus share."
            )
        tx = Transaction(ts=ts or datetime.now().isoformat(timespec="seconds"),
                         code=code, side="bonus", qty=float(bonus), price=0.0)
        self._transactions.append(tx)
        self._save()
        return tx

    def get_dividends(self, code: Optional[str] = None, limit: Optional[int] = None) -> List[Dividend]:
        divs = self._dividends
        if code:
            divs = [d for d in divs if d.code == code.upper()]
        divs = sorted(divs, key=lambda d: d.ts, reverse=True)
        return divs[:limit] if limit else divs

    def get_dividend_total(self, code: Optional[str] = None) -> float:
        return sum(d.total for d in self.get_dividends(code))

    def get_dividend_totals_by_code(self) -> Dict[str, float]:
        totals: Dict[str, float] = {}
        for d in self._dividends:
            totals[d.code] = totals.get(d.code, 0.0) + d.total
        return totals

    # ---------- derived views ----------

    def _replay(self) -> Dict[str, Holding]:
        """Replay the log into per-code holdings (closed positions included)."""
        holdings: Dict[str, Holding] = {}
        for tx in self._transactions:
            h = holdings.setdefault(tx.code, Holding(code=tx.code))
            if tx.side in ("buy", "bonus"):  # bonus = a buy at price 0
                new_qty = h.qty + tx.qty
                h.avg_cost = ((h.avg_cost * h.qty) + (tx.price * tx.qty)) / new_qty if new_qty else 0
                h.qty = new_qty
            else:  # sell
                h.realized_pl += (tx.price - h.avg_cost) * tx.qty
                h.qty -= tx.qty
        return holdings

    def get_holdings(self) -> Dict[str, Holding]:
        # drop fully-closed positions from "current holdings"
        return {c: h for c, h in self._replay().items() if h.qty > 1e-9}

    def get_holding(self, code: str) -> Holding:
        return self.get_holdings().get(code.upper(), Holding(code=code.upper()))

    def get_realized_pl_total(self) -> float:
        return sum(h.realized_pl for h in self._replay().values())

    def get_transactions(self, code: Optional[str] = None, limit: Optional[int] = None) -> List[Transaction]:
        txs = self._transactions
        if code:
            txs = [t for t in txs if t.code == code.upper()]
        txs = sorted(txs, key=lambda t: t.ts, reverse=True)
        return txs[:limit] if limit else txs
