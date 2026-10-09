"""
alerts.py
Simple price-alert store: "tell me when CODE goes above/below PRICE".
Persisted to data/alerts.json. Checked periodically by bot.py's job queue.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_FILE = os.path.join(DATA_DIR, "alerts.json")


@dataclass
class Alert:
    code: str
    direction: str  # "above" or "below"
    target_price: float
    triggered: bool = False


class AlertStore:
    def __init__(self, path: str = DATA_FILE):
        self.path = path
        self._alerts: List[Alert] = []
        self._load()

    def _load(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(self.path):
            self._save()
            return
        with open(self.path, "r") as f:
            raw = json.load(f)
        self._alerts = [Alert(**a) for a in raw.get("alerts", [])]

    def _save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"alerts": [vars(a) for a in self._alerts]}, f, indent=2)

    def add(self, code: str, direction: str, target_price: float) -> Alert:
        if direction not in ("above", "below"):
            raise ValueError("direction must be 'above' or 'below'")
        alert = Alert(code=code.upper(), direction=direction, target_price=target_price)
        self._alerts.append(alert)
        self._save()
        return alert

    def list_active(self) -> List[Alert]:
        return [a for a in self._alerts if not a.triggered]

    def list_all(self) -> List[Alert]:
        return list(self._alerts)

    def mark_triggered(self, alert: Alert) -> None:
        alert.triggered = True
        self._save()

    def clear_triggered(self) -> None:
        self._alerts = [a for a in self._alerts if not a.triggered]
        self._save()

    def remove(self, alert) -> None:
        self._alerts.remove(alert)
        self._save()  # or however you persist
