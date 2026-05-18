"""Projections — push Verbatim state outward into the tools teams already use.

Each projection target (Linear, GitHub Issues, Jira, …) is its own module here.
The `projections` table in the store tracks which entities have been projected
where, keyed by `(entity_id, target_kind)`. This makes projections idempotent
(re-running doesn't duplicate) and reversible (we can mark `status='inactive'`
without touching the external system).
"""
