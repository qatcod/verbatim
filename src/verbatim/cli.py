"""verbatim CLI — extract, persist (ingest), and query accumulated team state."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__, state, store
from .extractor import DEFAULT_MODEL, extract
from .renderers import to_json, to_markdown
from .transcripts import load_transcript

app = typer.Typer(
    name="verbatim",
    help="Extract, persist, and query structured operational state from team communications.",
    add_completion=False,
    no_args_is_help=True,
)

query_app = typer.Typer(name="query", help="Read accumulated state from the local store.")
app.add_typer(query_app)

console = Console()
err_console = Console(stderr=True)


# ----------------------- extract (one-shot, file output) -----------------------


@app.command(name="extract")
def extract_cmd(
    source: str = typer.Argument(
        ...,
        metavar="TRANSCRIPT",
        help="Path to a transcript file (.txt or .vtt). Use '-' to read from stdin.",
    ),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", "-o", help="Directory for output files. Defaults to alongside input."
    ),
    model: str | None = typer.Option(
        None, "--model", "-m",
        help=f"Anthropic model ID. Defaults to $VERBATIM_MODEL or {DEFAULT_MODEL}.",
    ),
    json_only: bool = typer.Option(False, "--json-only", help="Emit only JSON."),
    markdown_only: bool = typer.Option(False, "--markdown-only", help="Emit only Markdown."),
    stdout: bool = typer.Option(False, "--stdout", help="Print Markdown to stdout, write no files."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Extract structured operational state from a transcript and write files."""
    if json_only and markdown_only:
        err_console.print("[red]Cannot pass both --json-only and --markdown-only.[/red]")
        raise typer.Exit(code=2)

    transcript = _load_or_die(source)
    if not quiet:
        err_console.print(
            f"[dim]Extracting from {source if source != '-' else '<stdin>'} "
            f"({len(transcript):,} chars)…[/dim]"
        )

    result, diag = _extract_or_die(transcript, model)
    if not quiet:
        _print_extraction_summary(result, diag.model, diag.input_tokens, diag.output_tokens)

    md = to_markdown(result, source_path=source if source != "-" else None)
    if stdout:
        console.print(md, markup=False, highlight=False)
        return

    paths = _resolve_output_paths(source, output_dir)
    if not markdown_only:
        paths["json"].write_text(
            to_json(result, diag, source_path=source if source != "-" else None),
            encoding="utf-8",
        )
        if not quiet:
            err_console.print(f"[green]✓[/green] wrote {paths['json']}")
    if not json_only:
        paths["md"].write_text(md, encoding="utf-8")
        if not quiet:
            err_console.print(f"[green]✓[/green] wrote {paths['md']}")


# ----------------------- ingest (extract + persist to DB) -----------------------


@app.command(name="ingest")
def ingest_cmd(
    source: str = typer.Argument(
        ...,
        metavar="TRANSCRIPT",
        help="Path to a transcript file (.txt or .vtt). Use '-' to read from stdin.",
    ),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(
        None, "--db",
        help="SQLite DB path. Defaults to $VERBATIM_DB_PATH or ~/.verbatim/state.db.",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Extract and persist into the local state store."""
    transcript = _load_or_die(source)
    if not quiet:
        err_console.print(
            f"[dim]Ingesting from {source if source != '-' else '<stdin>'} "
            f"({len(transcript):,} chars)…[/dim]"
        )

    result, diag = _extract_or_die(transcript, model)
    conn = state.open_db(db)
    try:
        summary = state.save_extraction(
            conn, result, diag,
            source_path=source if source != "-" else None,
        )
    finally:
        conn.close()

    if not quiet:
        total = sum(summary.counts.values())
        body = (
            f"[bold]Saved {total} items[/bold] · "
            f"{summary.counts['commitment']} commitments · "
            f"{summary.counts['decision']} decisions · "
            f"{summary.counts['open_question']} open questions · "
            f"{summary.counts['blocker']} blockers\n"
            f"[dim]session_id: {summary.session_id}  ·  "
            f"db: {store.resolve_db_path(db)}[/dim]"
        )
        err_console.print(Panel(body, title="ingested", border_style="green", expand=False))


# ----------------------- query subcommands -----------------------


@query_app.command("commitments")
def query_commitments(
    actor: str | None = typer.Option(None, "--actor", "-a", help="Filter by committer name."),
    min_confidence: str | None = typer.Option(
        None, "--min-confidence", "-c",
        help="Minimum confidence: low | medium | high.",
    ),
    include_resolved: bool = typer.Option(False, "--all", help="Include resolved items."),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List commitments."""
    conn = state.open_db(db)
    try:
        items = state.list_commitments(
            conn, actor=actor, min_confidence=min_confidence,
            status=None if include_resolved else "open",
            limit=limit,
        )
    finally:
        conn.close()
    if not items:
        console.print("[dim]No commitments matched.[/dim]")
        return
    _print_entity_table(items, kind="commitment")


@query_app.command("decisions")
def query_decisions(
    min_confidence: str | None = typer.Option(None, "--min-confidence", "-c"),
    include_resolved: bool = typer.Option(False, "--all"),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List decisions."""
    conn = state.open_db(db)
    try:
        items = state.list_decisions(
            conn, min_confidence=min_confidence,
            status=None if include_resolved else "open",
            limit=limit,
        )
    finally:
        conn.close()
    if not items:
        console.print("[dim]No decisions matched.[/dim]")
        return
    _print_entity_table(items, kind="decision")


@query_app.command("open-questions")
def query_open_questions(
    raised_by: str | None = typer.Option(None, "--raised-by"),
    min_confidence: str | None = typer.Option(None, "--min-confidence", "-c"),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List open questions."""
    conn = state.open_db(db)
    try:
        items = state.list_open_questions(
            conn, raised_by=raised_by, min_confidence=min_confidence, limit=limit,
        )
    finally:
        conn.close()
    if not items:
        console.print("[dim]No open questions matched.[/dim]")
        return
    _print_entity_table(items, kind="open_question")


@query_app.command("blockers")
def query_blockers(
    owner: str | None = typer.Option(None, "--owner"),
    min_confidence: str | None = typer.Option(None, "--min-confidence", "-c"),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List blockers."""
    conn = state.open_db(db)
    try:
        items = state.list_blockers(conn, owner=owner, min_confidence=min_confidence, limit=limit)
    finally:
        conn.close()
    if not items:
        console.print("[dim]No blockers matched.[/dim]")
        return
    _print_entity_table(items, kind="blocker")


@query_app.command("sessions")
def query_sessions(
    limit: int = typer.Option(20, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List recent extraction sessions."""
    conn = state.open_db(db)
    try:
        sessions = state.recent_sessions(conn, limit=limit)
    finally:
        conn.close()
    if not sessions:
        console.print("[dim]No sessions yet — run `verbatim ingest` first.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("session_id", style="dim", no_wrap=True)
    table.add_column("when (UTC)")
    table.add_column("source")
    table.add_column("model")
    table.add_column("items", justify="right")
    for s in sessions:
        table.add_row(
            s["id"][:8] + "…",
            s["extracted_at"][:19],
            s["source_path"] or "<stdin>",
            s["model"],
            str(s["entity_count"]),
        )
    console.print(table)


@query_app.command("stats")
def query_stats(db: Path | None = typer.Option(None, "--db")) -> None:
    """Counts of open items in the local state."""
    conn = state.open_db(db)
    try:
        s = state.stats(conn)
    finally:
        conn.close()
    body = (
        f"[bold]{s['sessions']}[/bold] sessions ingested\n"
        f"[bold]{s['commitments_open']}[/bold] open commitments · "
        f"[bold]{s['decisions_open']}[/bold] decisions · "
        f"[bold]{s['open_questions_open']}[/bold] open questions · "
        f"[bold]{s['blockers_open']}[/bold] blockers"
    )
    console.print(Panel(body, title="verbatim state", border_style="cyan", expand=False))


@app.command(name="resolve")
def resolve_cmd(
    entity_id: str = typer.Argument(..., help="Entity ID prefix (8+ chars) or full ID."),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Mark an entity as resolved."""
    conn = state.open_db(db)
    try:
        full_id = _resolve_id_prefix(conn, entity_id)
        if full_id is None:
            err_console.print(f"[red]No entity matches id prefix '{entity_id}'.[/red]")
            raise typer.Exit(code=1)
        ok = state.resolve_entity(conn, full_id)
    finally:
        conn.close()
    if ok:
        console.print(f"[green]✓[/green] resolved {full_id}")
    else:
        err_console.print("[yellow]Nothing changed.[/yellow]")


@app.command()
def version() -> None:
    """Print the verbatim version."""
    console.print(f"verbatim {__version__}")


# ----------------------- helpers -----------------------


def _load_or_die(source: str) -> str:
    try:
        transcript = load_transcript(source)
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    if not transcript.strip():
        err_console.print("[red]Transcript is empty.[/red]")
        raise typer.Exit(code=1)
    return transcript


def _extract_or_die(transcript: str, model: str | None):
    try:
        return extract(transcript, model=model)
    except Exception as e:
        err_console.print(f"[red]Extraction failed: {e}[/red]")
        raise typer.Exit(code=1) from None


def _resolve_output_paths(source: str, output_dir: Path | None) -> dict[str, Path]:
    if source == "-":
        base = "stdin"
        directory = output_dir or Path.cwd()
    else:
        src_path = Path(source)
        base = src_path.with_suffix("").name
        directory = output_dir or src_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "json": directory / f"{base}.verbatim.json",
        "md": directory / f"{base}.verbatim.md",
    }


def _print_extraction_summary(result, model: str, in_tokens: int, out_tokens: int) -> None:
    counts = (
        f"{len(result.commitments)} commitments · "
        f"{len(result.decisions)} decisions · "
        f"{len(result.open_questions)} open questions · "
        f"{len(result.blockers)} blockers"
    )
    body = (
        f"[bold]{counts}[/bold]\n"
        f"[dim]model: {model}  ·  tokens: {in_tokens:,} in / {out_tokens:,} out[/dim]"
    )
    err_console.print(Panel(body, title="verbatim", border_style="cyan", expand=False))


def _print_entity_table(items: list[dict[str, Any]], *, kind: str) -> None:
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("conf", no_wrap=True)
    if kind == "commitment":
        table.add_column("actor")
        table.add_column("deliverable")
        table.add_column("deadline")
    elif kind == "decision":
        table.add_column("topic")
        table.add_column("outcome")
    elif kind == "open_question":
        table.add_column("raised by")
        table.add_column("question")
    elif kind == "blocker":
        table.add_column("blocked thing")
        table.add_column("blocked by")
        table.add_column("owner")
    table.add_column("first source")

    for item in items:
        payload = item["payload"]
        srcs = item["sources"]
        quote = (srcs[0]["verbatim_quote"][:60] + "…") if srcs and len(srcs[0]["verbatim_quote"]) > 60 else (srcs[0]["verbatim_quote"] if srcs else "")
        conf_color = {"high": "green", "medium": "yellow", "low": "red"}.get(item["confidence"], "white")
        conf_cell = f"[{conf_color}]{item['confidence']}[/{conf_color}]"
        row = [item["id"][:8] + "…", conf_cell]
        if kind == "commitment":
            row += [payload.get("actor") or "—", payload.get("deliverable") or "—", payload.get("deadline") or "—"]
        elif kind == "decision":
            row += [payload.get("topic") or "—", payload.get("outcome") or "—"]
        elif kind == "open_question":
            row += [payload.get("raised_by") or "—", payload.get("question") or payload.get("topic") or "—"]
        elif kind == "blocker":
            row += [payload.get("blocked_thing") or "—", payload.get("blocked_by") or "—", payload.get("owner") or "—"]
        row.append(quote)
        table.add_row(*row)

    console.print(table)


def _resolve_id_prefix(conn, prefix: str) -> str | None:
    rows = conn.execute(
        "SELECT id FROM entities WHERE id LIKE ? LIMIT 2",
        (prefix + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


if __name__ == "__main__":  # pragma: no cover
    app()
