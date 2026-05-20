"""Relationship-graph assembly + layout for the visual graph view.

Entities are nodes; typed relationships (v0.12.1) are edges. This module
builds the node/edge set and computes 2-D positions with a small
force-directed layout — pure stdlib, deterministic, no JS-side physics.

Only entities that participate in at least one relationship are included.
An isolated entity is a dot with nothing to say; the graph view is about
the connections.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

from . import store


@dataclass
class GraphNode:
    entity_id: str
    kind: str
    label: str
    x: float = 0.0
    y: float = 0.0


@dataclass
class GraphEdge:
    from_id: str
    to_id: str
    rel_type: str


@dataclass
class Graph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.nodes


def _entity_label(entity: dict) -> str:
    payload = entity.get("payload") or {}
    text = (
        payload.get("deliverable") or payload.get("topic")
        or payload.get("question") or payload.get("blocked_thing")
        or entity["kind"]
    )
    text = " ".join(str(text).split())
    return text[:48] + ("…" if len(text) > 48 else "")


def build_graph(conn: sqlite3.Connection, *, limit: int = 300) -> Graph:
    """Assemble the relationship graph from `entity_relationships`.

    Walks every relationship row, collects the entities on both ends, and
    runs the layout. Entities with no relationships are excluded.
    """
    rows = conn.execute(
        """
        SELECT from_entity_id, to_entity_id, rel_type
        FROM entity_relationships
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return Graph()

    edges: list[GraphEdge] = []
    node_ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        edges.append(GraphEdge(
            from_id=r["from_entity_id"],
            to_id=r["to_entity_id"],
            rel_type=r["rel_type"],
        ))
        for eid in (r["from_entity_id"], r["to_entity_id"]):
            if eid not in seen:
                seen.add(eid)
                node_ids.append(eid)

    nodes: list[GraphNode] = []
    for eid in node_ids:
        entity = store.fetch_entity(conn, eid)
        if entity is None:
            continue
        nodes.append(GraphNode(
            entity_id=eid,
            kind=entity["kind"],
            label=_entity_label(entity),
        ))

    # Drop edges whose endpoints didn't resolve to a node.
    valid = {n.entity_id for n in nodes}
    edges = [e for e in edges if e.from_id in valid and e.to_id in valid]

    _layout(nodes, edges)
    return Graph(nodes=nodes, edges=edges)


def _layout(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    width: float = 1000.0,
    height: float = 640.0,
    iterations: int = 220,
) -> None:
    """Force-directed layout — mutates each node's x/y in place.

    Fruchterman-Reingold-ish: every node pair repels, every edge attracts.
    Initial positions are seeded deterministically from the entity id so the
    same graph always lays out the same way (stable across page reloads).
    """
    n = len(nodes)
    if n == 0:
        return
    if n == 1:
        nodes[0].x, nodes[0].y = width / 2, height / 2
        return

    # Deterministic seed: spread nodes on a circle by id hash.
    for i, node in enumerate(nodes):
        seed = int(node.entity_id[:8], 16) if _is_hex(node.entity_id[:8]) else i
        angle = (seed % 360) * math.pi / 180.0
        radius = 180.0 + (seed % 120)
        node.x = width / 2 + radius * math.cos(angle)
        node.y = height / 2 + radius * math.sin(angle)

    area = width * height
    k = math.sqrt(area / n)  # ideal edge length
    index = {node.entity_id: node for node in nodes}
    temp = width / 8.0
    cooling = temp / (iterations + 1)

    for _ in range(iterations):
        disp = {node.entity_id: [0.0, 0.0] for node in nodes}

        # Repulsion — every pair pushes apart.
        for i in range(n):
            a = nodes[i]
            for j in range(i + 1, n):
                b = nodes[j]
                dx, dy = a.x - b.x, a.y - b.y
                dist = math.hypot(dx, dy) or 0.01
                force = (k * k) / dist
                ux, uy = dx / dist, dy / dist
                disp[a.entity_id][0] += ux * force
                disp[a.entity_id][1] += uy * force
                disp[b.entity_id][0] -= ux * force
                disp[b.entity_id][1] -= uy * force

        # Attraction — edges pull endpoints together.
        for edge in edges:
            a, b = index[edge.from_id], index[edge.to_id]
            dx, dy = a.x - b.x, a.y - b.y
            dist = math.hypot(dx, dy) or 0.01
            force = (dist * dist) / k
            ux, uy = dx / dist, dy / dist
            disp[a.entity_id][0] -= ux * force
            disp[a.entity_id][1] -= uy * force
            disp[b.entity_id][0] += ux * force
            disp[b.entity_id][1] += uy * force

        # Apply, capped by the cooling temperature, clamped to the canvas.
        for node in nodes:
            dx, dy = disp[node.entity_id]
            d = math.hypot(dx, dy) or 0.01
            node.x += (dx / d) * min(d, temp)
            node.y += (dy / d) * min(d, temp)
            node.x = min(width - 40, max(40, node.x))
            node.y = min(height - 40, max(40, node.y))

        temp = max(temp - cooling, 1.0)


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False
