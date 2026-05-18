# Changelog

All notable changes to Verbatim. This project follows [Semantic Versioning](https://semver.org/).

## [0.1.1] — 2026-05-18

### Added
- Unit test suite (42 tests) covering schema, transcripts, store, state, and renderers
- `CONTRIBUTING.md` with the two non-negotiables (verbatim quoting, calibrated confidence) and guides for adding transcript parsers, connectors, projections, and MCP tools
- GitHub Actions CI: lint (ruff) + pytest on Python 3.10–3.12 matrix

### Changed
- **Tightened the extraction prompt** based on observed over-extraction in v0.0:
  - `HIGH` confidence now requires substantive weight, not just explicitness — casual asides and offhand favors are `MEDIUM` at most
  - New rule: respect explicit speaker scope-flags ("that's separate from X") — skip or extract at `LOW` with a `notes` annotation
  - New rule: distinguish open questions (needs an answer) from stated concerns (contingency / "if X then Y" hedging) — concerns are not questions
  - Anti-fragmentation guidance: don't split a single deliverable into multiple commitments
- pytest config disables `pytest-asyncio` plugin to avoid collection conflicts from transitive `mcp` deps

### Why these changes
First real-API extraction on the included sample transcript flagged three calibration issues:
- A casual "just use my key, I'll cover it" was treated as a high-confidence commitment
- A statement the speaker explicitly scoped out ("that's separate from Verbatim") still ended up in the commitments list
- A contingency ("if quality is bad we may need to iterate") was extracted as an open question

The prompt changes target each issue specifically. Validate with `verbatim extract examples/sample_transcript.txt`; you should see fewer false positives without losing the real items.

## [0.1.0] — 2026-05-18

### Added (initial release)
- CLI commands: `extract` (file output), `ingest` (persist to DB), `query` (read state), `resolve` (mark resolved)
- Pydantic schema with `SourceReference` requirement on every extracted entity
- WebVTT + plain text + stdin transcript parsers
- Anthropic API extraction via forced tool use
- JSON + Markdown renderers
- SQLite-backed state graph (sessions, entities, entity_sources)
- MCP server (`verbatim-mcp`) exposing 7 tools to agent clients (Claude Code, Cursor, etc.)
- Self-hostable, BYO LLM key, MIT licensed
