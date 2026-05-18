# Contributing to Verbatim

Thanks for thinking about contributing. Verbatim is built around two strong opinions, and contributions that hold those lines are the easiest to merge.

## The two non-negotiables

1. **Every extraction must carry a verbatim quote from the input.** No paraphrasing, no inferred speech, no summary in place of a quote. The product's name is the contract; the tests enforce it; reviewers will too.

2. **Confidence is calibrated, not aspirational.** `HIGH` means a colleague would track it. Casual asides, contingencies, and offhand favors are `MEDIUM` at most. Hypothetical talk gets `LOW` or gets skipped. Tuning the prompt is fair game; raising the floor for what counts as `HIGH` is not.

If a PR violates either of these, it'll need a strong story for why.

## Dev setup

```bash
git clone https://github.com/qatcod/verbatim.git
cd verbatim
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

`pip install -e ".[dev]"` includes `pytest` and `ruff`. The unit tests don't hit the network — they cover the deterministic logic (schema, transcripts, store, state, renderers). Extraction quality is validated manually against real transcripts with `verbatim extract`.

## Project layout

```
src/verbatim/
├── schema.py          # Pydantic models. SourceReference is the contract.
├── prompts.py         # System prompt + extraction tool definition.
├── transcripts.py     # Format parsers (.txt, .vtt, stdin).
├── extractor.py       # Anthropic API call with forced tool use.
├── renderers.py       # JSON + Markdown output.
├── store.py           # SQLite layer (schema, raw CRUD).
├── state.py           # Domain operations on top of store.
├── mcp_server.py      # MCP stdio server for agent clients.
└── cli.py             # Typer CLI: extract, ingest, query, resolve.

tests/
├── conftest.py        # Shared fixtures (temp DB, sample ExtractionResult).
└── test_*.py          # One per module under test.
```

## Adding a new transcript format

If you want to support a new input format (Otter JSON export, Granola export, Zoom recording API, etc.):

1. Add a parser to `transcripts.py` that takes raw input and returns a normalized string. The LLM does the interpretation — the parser just unpacks the wrapping format.
2. Route to it from `load_transcript()` based on file extension.
3. Add a test fixture in `tests/test_transcripts.py` with a small sample of the format.

The normalized string should preserve speaker labels and approximate timestamps when the source format has them.

## Adding a new connector (v1+)

Connectors pull from external sources (Slack history, GitHub audit, etc.) and feed the extractor. The general contract:

1. Connector lives at `src/verbatim/connectors/<name>.py`.
2. Exposes a `pull(...) -> Iterator[NormalizedEvent]` function.
3. Auth via env vars or a config file under `~/.verbatim/`. Never commit credentials.
4. The connector framework is part of the v1 roadmap — design discussions welcome in issues before code.

## Adding a projection target (v1+)

Projections push state out to existing tools (Linear, GitHub Issues, etc.). The contract:

1. Idempotent — running twice produces the same external state.
2. Source-linked — every projected item carries a back-reference to the Verbatim entity id.
3. Lives at `src/verbatim/projections/<name>.py`.

## Adding an MCP tool

If you want to expose a new tool to agent clients:

1. Add a `types.Tool(...)` entry in `mcp_server._list_tools()` with a clear description and JSON Schema.
2. Add a dispatch branch in `mcp_server._call_tool()` that calls into `state.py`.
3. Return `[types.TextContent(...)]` with a human-readable rendering of the result — agents read these like a human would.

## Style

- `ruff check src/` should pass.
- Keep functions small and named after their effect.
- Prefer Pydantic models over dicts at API boundaries.
- Comments explain *why* something is the way it is, not *what* the code does.

## Reporting issues

If you find a case where extraction quality is bad, include:

1. The transcript (or a minimal redacted version).
2. The full `*.verbatim.json` output.
3. What you expected vs. what you got.
4. The model used (visible in `diagnostics.model` in the JSON output).

Issues with concrete reproduction beat issues without.

## License

By contributing, you agree your contribution will be licensed under the project's MIT license.
