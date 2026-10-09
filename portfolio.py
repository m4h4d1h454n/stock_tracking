"""
portfolio.py
Tracks buy/sell transactions and current holdings for one user, persisted
to a plain JSON file (data/portfolio.json). No database server needed.

Data shape:
{
  "transactions": [
    {"ts": "2026-09-17T10:30:00", "code": "BRACBANK", "side": "buy",
     "qty": 100, "price": 45.5},
    ...
  ]
}

Holdings (qty, avg cost) are derived from transactions on the fly, so the
transaction log is always the single source of truth — good for reports
and auditing.
"""

from __future__ import annotations

import json
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
class Holding:
    code: str
    qty: float = 0.0
    avg_cost: float = 0.0
    realized_pl: float = 0.0  # profit/loss already locked in from sells


class Portfolio:
    def __init__(self, path: str = DATA_FILE):
        self.path = path
        self._transactions: List[Transaction] = []
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

    def _save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {"transactions": [vars(t) for t in self._transactions]}
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

    # ---------- derived views ----------

    def get_holdings(self) -> Dict[str, Holding]:
        holdings: Dict[str, Holding] = {}
        for tx in self._transactions:
            h = holdings.setdefault(tx.code, Holding(code=tx.code))
            if tx.side == "buy":
                new_qty = h.qty + tx.qty
                h.avg_cost = ((h.avg_cost * h.qty) + (tx.price * tx.qty)) / new_qty if new_qty else 0
                h.qty = new_qty
            else:  # sell
                h.realized_pl += (tx.price - h.avg_cost) * tx.qty
                h.qty -= tx.qty
        # drop fully-closed positions from "current holdings"
        return {c: h for c, h in holdings.items() if h.qty > 1e-9}

    def get_holding(self, code: str) -> Holding:
        return self.get_holdings().get(code.upper(), Holding(code=code.upper()))

    def get_realized_pl_total(self) -> float:
        holdings: Dict[str, Holding] = {}
        for tx in self._transactions:
            h = holdings.setdefault(tx.code, Holding(code=tx.code))
            if tx.side == "buy":
                new_qty = h.qty + tx.qty
                h.avg_cost = ((h.avg_cost * h.qty) + (tx.price * tx.qty)) / new_qty if new_qty else 0
                h.qty = new_qty
            else:
                h.realized_pl += (tx.price - h.avg_cost) * tx.qty
                h.qty -= tx.qty
        return sum(h.realized_pl for h in holdings.values())

    def get_transactions(self, code: Optional[str] = None, limit: Optional[int] = None) -> List[Transaction]:
        txs = self._transactions
        if code:
            txs = [t for t in txs if t.code == code.upper()]
        txs = sorted(txs, key=lambda t: t.ts, reverse=True)
        return txs[:limit] if limit else txs
