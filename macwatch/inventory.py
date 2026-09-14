"""Sklad a ekonomika: nakoupené kusy, náklady, marže, ROI a vázaný kapitál."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from macwatch.config import TravelSettings

COST_KINDS = (("dily", "Díly"), ("oprava", "Oprava"), ("cesta", "Cesta"), ("poplatky", "Poplatky"),
              ("jine", "Jiné"))


@dataclass(frozen=True, slots=True)
class Economics:
    status: str                  # skladem, vystaveno, prodano
    days_held: int
    costs_czk: int               # díly, opravy a ostatní zadané náklady
    travel_czk: int              # cesta podle ujetých km a nastavení spotřeby
    invested_czk: int            # nákup + náklady + cesta
    gross_margin_czk: int | None # prodej - nákup
    net_margin_czk: int | None   # prodej - nákup - náklady - cesta - poplatky
    roi: float | None            # čistá marže / investice
    annualized_roi: float | None
    capital_tied_czk: int        # jen u neprodaných kusů


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def travel_cost(km: float | None, settings: TravelSettings) -> int:
    """Ujeté km zadané u kusu jsou celá cesta, proto se nenásobí dvěma."""
    return round((km or 0) * settings.czk_per_km)


def economics(item: sqlite3.Row | dict, costs_czk: int, settings: TravelSettings,
              today: date) -> Economics:
    bought = _as_date(item["bought_at"])
    sold = _as_date(item["sold_at"])
    end = sold or today
    days = max((end - bought).days, 0) if bought else 0
    travel = travel_cost(item["travel_km"], settings)
    buy = item["buy_price_czk"] or 0
    invested = buy + costs_czk + travel
    status = "prodano" if sold else ("vystaveno" if item["listed_at"] else "skladem")

    gross = net = roi = annualized = None
    if sold and item["sale_price_czk"] is not None:
        sale = item["sale_price_czk"]
        fees = item["sale_fees_czk"] or 0
        gross = sale - buy
        net = sale - buy - costs_czk - travel - fees
        roi = net / invested if invested else None
        if roi is not None and roi > -1:
            # anualizace z minimálně jednoho dne, jinak by rychlá obrátka dala nekonečno
            annualized = (1 + roi) ** (365 / max(days, 1)) - 1
    return Economics(status, days, costs_czk, travel, invested, gross, net, roi, annualized,
                     0 if sold else invested)


@dataclass(frozen=True, slots=True)
class Summary:
    items: int
    in_stock: int
    sold: int
    capital_tied_czk: int
    capital_cap_czk: int
    over_cap: bool
    net_profit_czk: int
    revenue_czk: int
    avg_days_to_sell: float | None
    total_roi: float | None


class Inventory:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def items(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT i.*, COALESCE((SELECT SUM(amount_czk) FROM inventory_costs c
                                     WHERE c.inventory_id = i.id), 0) AS costs_czk
               FROM inventory i ORDER BY (i.sold_at IS NOT NULL), i.bought_at DESC, i.id DESC""").fetchall()

    def get(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT i.*, COALESCE((SELECT SUM(amount_czk) FROM inventory_costs c
                                     WHERE c.inventory_id = i.id), 0) AS costs_czk
               FROM inventory i WHERE id = ?""", (item_id,)).fetchone()

    def costs(self, item_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM inventory_costs WHERE inventory_id = ? ORDER BY spent_at, id",
            (item_id,)).fetchall()

    _COLUMNS = ("title", "family", "chip", "ram_gb", "ssd_gb", "bought_at", "buy_price_czk",
                "bought_from", "listing_url", "source", "source_id", "travel_km", "listed_at",
                "list_price_czk", "sold_at", "sale_price_czk", "sale_fees_czk", "note")

    def add(self, values: dict, now: datetime) -> int:
        data = {k: values.get(k) for k in self._COLUMNS}
        data["sale_fees_czk"] = data["sale_fees_czk"] or 0
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO inventory ({', '.join(data)}, created_at) "
                f"VALUES ({', '.join('?' for _ in data)}, ?)",
                (*data.values(), now.isoformat(timespec="seconds")))
        return int(cur.lastrowid)

    def update(self, item_id: int, values: dict) -> None:
        data = {k: values.get(k) for k in self._COLUMNS}
        data["sale_fees_czk"] = data["sale_fees_czk"] or 0
        with self.conn:
            self.conn.execute(
                f"UPDATE inventory SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                (*data.values(), item_id))

    def delete(self, item_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM inventory_costs WHERE inventory_id = ?", (item_id,))
            self.conn.execute("DELETE FROM inventory WHERE id = ?", (item_id,))

    def add_cost(self, item_id: int, spent_at: str, kind: str, description: str | None,
                 amount_czk: int) -> None:
        if kind not in dict(COST_KINDS):
            raise ValueError(f"neznámý druh nákladu {kind!r}")
        with self.conn:
            self.conn.execute(
                """INSERT INTO inventory_costs (inventory_id, spent_at, kind, description, amount_czk)
                   VALUES (?, ?, ?, ?, ?)""", (item_id, spent_at, kind, description, amount_czk))

    def delete_cost(self, cost_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM inventory_costs WHERE id = ?", (cost_id,))

    def summary(self, settings: TravelSettings, capital_cap_czk: int, today: date) -> Summary:
        rows = self.items()
        econ = [economics(row, row["costs_czk"], settings, today) for row in rows]
        sold = [(row, e) for row, e in zip(rows, econ) if e.status == "prodano"]
        tied = sum(e.capital_tied_czk for e in econ)
        invested_sold = sum(e.invested_czk for _, e in sold)
        net = sum(e.net_margin_czk or 0 for _, e in sold)
        return Summary(
            items=len(rows),
            in_stock=len(rows) - len(sold),
            sold=len(sold),
            capital_tied_czk=tied,
            capital_cap_czk=capital_cap_czk,
            over_cap=bool(capital_cap_czk) and tied > capital_cap_czk,
            net_profit_czk=net,
            revenue_czk=sum(row["sale_price_czk"] or 0 for row, _ in sold),
            avg_days_to_sell=round(sum(e.days_held for _, e in sold) / len(sold), 1) if sold else None,
            total_roi=net / invested_sold if invested_sold else None,
        )
