"""Contradiction detection — flag decisions that disagree with each other.

A team decides "use Postgres" in one meeting and "use SQLite" two weeks
later, on the same topic, and nobody connects the two. Verbatim already
holds both decisions; this module surfaces the conflict.

# Heuristic

A contradiction is a pair of open decisions where:
  - the **topics** are similar (token-set ratio >= `topic_threshold`), and
  - the **outcomes** are NOT similar (ratio < `outcome_threshold`).

Same topic + same outcome is a duplicate — reconciliation's job, not this.
Same topic + different outcome is the conflict worth surfacing.

The thresholds are deliberately looser than reconciliation's (88): a
contradiction is a prompt for a human to look, not an automatic state
change, so a few false positives are cheap and a missed conflict is not.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import reconcile, store

DEFAULT_TOPIC_THRESHOLD = 75
DEFAULT_OUTCOME_THRESHOLD = 55


@dataclass
class Contradiction:
    """A pair of decisions that look like they disagree."""

    decision_a: dict[str, Any]
    decision_b: dict[str, Any]
    topic_score: int      # topic similarity, 0-100
    outcome_score: int    # outcome similarity, 0-100

    @property
    def topic(self) -> str:
        return (
            self.decision_a.get("primary_topic")
            or (self.decision_a.get("payload") or {}).get("topic")
            or "(untitled)"
        )


def _topic_of(decision: dict[str, Any]) -> str:
    return (
        decision.get("primary_topic")
        or (decision.get("payload") or {}).get("topic")
        or ""
    )


def _outcome_of(decision: dict[str, Any]) -> str:
    return (decision.get("payload") or {}).get("outcome") or ""


def find_contradictions(
    conn: sqlite3.Connection,
    *,
    topic_threshold: int = DEFAULT_TOPIC_THRESHOLD,
    outcome_threshold: int = DEFAULT_OUTCOME_THRESHOLD,
    limit: int = 100,
) -> list[Contradiction]:
    """Scan open decisions for same-topic / different-outcome pairs.

    O(n²) over open canonical decisions — fine for the local-scale corpora
    Verbatim works with (hundreds, not millions). Returns pairs sorted by
    topic similarity descending (most-confident conflicts first).
    """
    decisions = store.fetch_entities(
        conn, kind="decision", status="open",
        canonical_only=True, limit=1000,
    )
    out: list[Contradiction] = []
    for i, a in enumerate(decisions):
        a_topic, a_outcome = _topic_of(a), _outcome_of(a)
        if not a_topic or not a_outcome:
            continue
        for b in decisions[i + 1:]:
            b_topic, b_outcome = _topic_of(b), _outcome_of(b)
            if not b_topic or not b_outcome:
                continue
            topic_score = reconcile.topic_similarity(a_topic, b_topic)
            if topic_score < topic_threshold:
                continue
            outcome_score = reconcile.topic_similarity(a_outcome, b_outcome)
            if outcome_score >= outcome_threshold:
                continue  # same topic + same outcome = duplicate, not conflict
            out.append(Contradiction(
                decision_a=a, decision_b=b,
                topic_score=topic_score, outcome_score=outcome_score,
            ))
    out.sort(key=lambda c: c.topic_score, reverse=True)
    return out[:limit]
