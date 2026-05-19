"""Cost estimation and spend tracking.

Verbatim's only paid moving part is the LLM call. This module:
1. Maps each model to a (input_$/MTok, output_$/MTok) price.
2. Estimates the cost of one extraction from its diagnostics (token counts).
3. Computes total spend across every session in the local DB.
4. Provides a `would_exceed_budget` predicate used by ingest commands to honor
   a `--max-cost-usd` budget cap.

Pricing is sourced from Anthropic's public price list as of 2026-05. Self-hosted
users with negotiated rates can override per model via the `VERBATIM_PRICING`
env var:
    VERBATIM_PRICING=claude-sonnet-4-6:2.5/12,claude-opus-4-7:12/60
"""
from __future__ import annotations

import os
import sqlite3

# (input price per 1M tokens, output price per 1M tokens) in USD.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _load_pricing() -> dict[str, tuple[float, float]]:
    """Merge $VERBATIM_PRICING overrides into the default table."""
    pricing = dict(DEFAULT_PRICING)
    override = os.environ.get("VERBATIM_PRICING")
    if not override:
        return pricing
    for entry in override.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            model, rates = entry.split(":", 1)
            inp, out = rates.split("/", 1)
            pricing[model.strip()] = (float(inp), float(out))
        except ValueError:
            continue
    return pricing


PRICING = _load_pricing()


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """Return the cost in USD for a single extraction.

    Returns 0.0 when the model is unknown — better to under-report than throw.
    """
    if not model:
        return 0.0
    rates = PRICING.get(model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0


def session_cost(session_row: dict | sqlite3.Row) -> float:
    """Cost for one row from the `sessions` table."""
    if isinstance(session_row, sqlite3.Row):
        session_row = dict(session_row)
    return estimate_cost(
        session_row.get("model"),
        int(session_row.get("input_tokens") or 0),
        int(session_row.get("output_tokens") or 0),
    )


def total_spend(conn: sqlite3.Connection) -> float:
    """Sum of estimated cost across every extraction session in the DB."""
    rows = conn.execute(
        "SELECT model, input_tokens, output_tokens FROM sessions"
    ).fetchall()
    return sum(session_cost(r) for r in rows)


def spend_by_model(conn: sqlite3.Connection) -> dict[str, float]:
    """Break down spend by model name."""
    rows = conn.execute(
        "SELECT model, input_tokens, output_tokens FROM sessions"
    ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        model = r["model"] or "unknown"
        out[model] = out.get(model, 0.0) + session_cost(r)
    return out


def spend_breakdown(conn: sqlite3.Connection) -> dict[str, float]:
    """Return {model: total_usd, ...}, sorted descending by amount."""
    breakdown = spend_by_model(conn)
    return dict(sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True))


def total_tokens(conn: sqlite3.Connection) -> tuple[int, int]:
    """Sum of (input_tokens, output_tokens) across every session."""
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS i, "
        "COALESCE(SUM(output_tokens), 0) AS o FROM sessions"
    ).fetchone()
    return int(row["i"]), int(row["o"])
