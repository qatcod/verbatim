"""verbatim CLI — extract, persist (ingest), and query accumulated team state."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__, state, store
from .connectors import github_pr, slack_api, slack_export
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


# ----------------------- ingest-slack (Slack export ZIP or dir) -----------------------


@app.command(name="ingest-slack")
def ingest_slack_cmd(
    source: Path = typer.Argument(
        ...,
        help="Path to a Slack export ZIP file or extracted directory.",
        exists=True,
    ),
    channel: list[str] | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="Restrict to specific channels (repeatable). Default: all channels.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only include messages after this ISO date (YYYY-MM-DD).",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Only include messages before this ISO date (YYYY-MM-DD).",
    ),
    min_thread_messages: int = typer.Option(
        3,
        "--min-thread-messages",
        help="Skip threads with fewer than this many messages.",
    ),
    include_loose: bool = typer.Option(
        False,
        "--include-loose",
        help="Also extract channel-day rollups of non-threaded messages (more noise).",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="Stop after extracting this many units.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List the units that would be extracted without making API calls.",
    ),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Ingest a Slack workspace export. Each thread becomes its own session."""
    since_dt = _parse_iso_date(since, "--since")
    until_dt = _parse_iso_date(until, "--until")

    try:
        export = slack_export.load(source)
    except (FileNotFoundError, ValueError) as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None

    err_console.print(
        f"[dim]Loaded export from {source}: "
        f"{len(export.users)} users, {len(export.channels)} channels[/dim]"
    )

    with export:
        units = list(
            export.iter_units(
                channels=channel,
                since=since_dt,
                until=until_dt,
                min_thread_messages=min_thread_messages,
                include_loose_messages=include_loose,
            )
        )

    if limit is not None:
        units = units[:limit]

    if not units:
        err_console.print("[yellow]No units matched the filters.[/yellow]")
        return

    if dry_run:
        _print_slack_dry_run(units)
        return

    _run_slack_ingest(units, model=model, db=db)


def _print_slack_dry_run(units: list) -> None:
    table = Table(show_header=True, header_style="bold cyan", title="Slack ingest plan (dry run)")
    table.add_column("kind")
    table.add_column("channel")
    table.add_column("start (UTC)")
    table.add_column("msgs", justify="right")
    table.add_column("title")
    for u in units:
        table.add_row(
            u.kind,
            f"#{u.channel}",
            u.start.strftime("%Y-%m-%d %H:%M"),
            str(len(u.messages)),
            u.title or "—",
        )
    console.print(table)
    err_console.print(
        f"[dim]{len(units)} units would be extracted. "
        f"Estimated cost at Sonnet pricing: ~${len(units) * 0.07:.2f}[/dim]"
    )


def _run_slack_ingest(units: list, *, model: str | None, db: Path | None) -> None:
    conn = state.open_db(db)
    total_counts = {"commitment": 0, "decision": 0, "open_question": 0, "blocker": 0}
    failed = 0
    total_in_tokens = 0
    total_out_tokens = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err_console,
    )

    try:
        with progress:
            task = progress.add_task(
                f"Extracting {len(units)} units",
                total=len(units),
            )
            for unit in units:
                progress.update(
                    task,
                    description=f"#{unit.channel} · {unit.kind} · {unit.start.strftime('%Y-%m-%d')}",
                )
                try:
                    result, diag = extract(unit.transcript, model=model)
                    summary = state.save_extraction(
                        conn, result, diag,
                        source_path=unit.source_label,
                        source_kind=unit.source_kind,
                    )
                    for k, v in summary.counts.items():
                        total_counts[k] += v
                    total_in_tokens += diag.input_tokens
                    total_out_tokens += diag.output_tokens
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    err_console.print(f"[red]  failed {unit.source_label}: {e}[/red]")
                progress.advance(task)
    finally:
        conn.close()

    total = sum(total_counts.values())
    body = (
        f"[bold]Extracted {total} items across {len(units) - failed}/{len(units)} units[/bold]\n"
        f"{total_counts['commitment']} commitments · "
        f"{total_counts['decision']} decisions · "
        f"{total_counts['open_question']} open questions · "
        f"{total_counts['blocker']} blockers\n"
        f"[dim]tokens: {total_in_tokens:,} in / {total_out_tokens:,} out  ·  "
        f"failed: {failed}[/dim]"
    )
    err_console.print(Panel(body, title="slack ingest complete", border_style="green", expand=False))


def _parse_iso_date(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        err_console.print(f"[red]{field_name} must be YYYY-MM-DD, got: {value}[/red]")
        raise typer.Exit(code=2) from None


# ----------------------- ingest-slack-api (live Slack workspace) -----------------------


@app.command(name="ingest-slack-api")
def ingest_slack_api_cmd(
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="SLACK_TOKEN",
        help="Slack OAuth token (xoxb-... or xoxp-...). Reads $SLACK_TOKEN if not passed.",
    ),
    channel: list[str] | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="Restrict to specific channels (repeatable). Default: all accessible channels.",
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only include messages after this ISO date (YYYY-MM-DD).",
    ),
    until: str | None = typer.Option(
        None, "--until", help="Only include messages before this ISO date (YYYY-MM-DD).",
    ),
    min_thread_messages: int = typer.Option(
        3, "--min-thread-messages", help="Skip threads with fewer than this many messages.",
    ),
    include_loose: bool = typer.Option(
        False, "--include-loose",
        help="Also extract channel-day rollups of non-threaded messages.",
    ),
    include_private: bool = typer.Option(
        False, "--include-private",
        help="Include private channels the token has access to.",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Stop after this many units."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only, no API calls to Claude."),
    request_pause: float = typer.Option(
        0.0, "--request-pause",
        help="Pause N seconds between Slack API calls to avoid rate limits.",
    ),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Ingest a live Slack workspace via the Web API.

    Requires a Slack App OAuth token (see slack_api.py docstring for setup).
    """
    if not token:
        err_console.print(
            "[red]Slack token required.[/red] Set $SLACK_TOKEN or pass --token. "
            "See: https://api.slack.com/apps"
        )
        raise typer.Exit(code=2)

    since_dt = _parse_iso_date(since, "--since")
    until_dt = _parse_iso_date(until, "--until")

    try:
        client = slack_api.SlackClient(token=token, request_pause=request_pause)
    except ValueError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from None

    err_console.print("[dim]Pulling user map + channel list from Slack…[/dim]")
    try:
        users = client.get_users()
        channels_available = client.list_channel_names(include_private=include_private)
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Slack API call failed: {e}[/red]")
        raise typer.Exit(code=1) from None

    err_console.print(
        f"[dim]{len(users)} users · {len(channels_available)} channels accessible[/dim]"
    )
    if channel:
        unknown = [c for c in channel if c not in channels_available]
        if unknown:
            err_console.print(
                f"[yellow]warn:[/yellow] not accessible / not in channel list: "
                f"{', '.join(unknown)}"
            )

    err_console.print("[dim]Fetching messages…[/dim]")
    try:
        units = list(
            client.iter_units(
                channels=channel,
                since=since_dt,
                until=until_dt,
                min_thread_messages=min_thread_messages,
                include_loose_messages=include_loose,
                include_private=include_private,
            )
        )
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Slack API call failed during message fetch: {e}[/red]")
        raise typer.Exit(code=1) from None

    if limit is not None:
        units = units[:limit]

    if not units:
        err_console.print("[yellow]No units matched the filters.[/yellow]")
        return

    if dry_run:
        _print_slack_dry_run(units)
        return

    _run_slack_ingest(units, model=model, db=db)


# ----------------------- ingest-github (GitHub PR discussions) -----------------------


@app.command(name="ingest-github")
def ingest_github_cmd(
    repo: str = typer.Argument(
        ..., help="Repository in owner/name form (e.g. qatcod/verbatim)."
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="GITHUB_TOKEN",
        help="GitHub PAT. Reads $GITHUB_TOKEN if not passed.",
    ),
    pr: list[int] | None = typer.Option(
        None,
        "--pr",
        help="Specific PR numbers to ingest (repeatable). If not given, list by state/date.",
    ),
    state: str = typer.Option(
        "all", "--state",
        help="PR state filter: open | closed | all. Only used if --pr not given.",
    ),
    since: str | None = typer.Option(
        None, "--since",
        help="Only PRs updated after this date (YYYY-MM-DD). Only used if --pr not given.",
    ),
    until: str | None = typer.Option(
        None, "--until", help="Only PRs updated before this date (YYYY-MM-DD).",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Ingest GitHub PR discussion threads.

    Pulls each PR's body plus all issue and review comments, sorts chronologically,
    and treats the whole thread as one extraction session.
    """
    if not token:
        err_console.print(
            "[red]GitHub token required.[/red] Set $GITHUB_TOKEN or pass --token."
        )
        raise typer.Exit(code=2)
    if "/" not in repo:
        err_console.print(f"[red]Repo must be owner/name form, got: {repo}[/red]")
        raise typer.Exit(code=2)
    if state not in {"open", "closed", "all"}:
        err_console.print(f"[red]--state must be open|closed|all, got: {state}[/red]")
        raise typer.Exit(code=2)

    since_dt = _parse_iso_date(since, "--since")
    until_dt = _parse_iso_date(until, "--until")

    units: list = []
    try:
        with github_pr.GitHubClient(token=token) as gh:
            err_console.print(f"[dim]Fetching PRs from {repo}…[/dim]")
            for unit in gh.iter_pull_requests(
                repo, state=state, since=since_dt, until=until_dt, numbers=pr,
            ):
                units.append(unit)
                if limit is not None and len(units) >= limit:
                    break
    except httpx.HTTPStatusError as e:
        err_console.print(f"[red]GitHub API error: {e.response.status_code} {e.response.text[:200]}[/red]")
        raise typer.Exit(code=1) from None
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]GitHub fetch failed: {e}[/red]")
        raise typer.Exit(code=1) from None

    if not units:
        err_console.print("[yellow]No PRs matched the filters.[/yellow]")
        return

    if dry_run:
        _print_github_dry_run(units)
        return

    _run_unit_ingest(units, model=model, db=db, source_kind_default="github_pr")


def _print_github_dry_run(units: list) -> None:
    table = Table(show_header=True, header_style="bold cyan", title="GitHub PR ingest plan (dry run)")
    table.add_column("repo")
    table.add_column("PR", justify="right")
    table.add_column("title")
    table.add_column("state")
    table.add_column("comments", justify="right")
    for u in units:
        table.add_row(
            u.repo,
            f"#{u.number}",
            u.title[:60] + ("…" if len(u.title) > 60 else ""),
            u.state,
            str(len(u.comments)),
        )
    console.print(table)
    err_console.print(
        f"[dim]{len(units)} PRs would be extracted. "
        f"Estimated cost at Sonnet pricing: ~${len(units) * 0.07:.2f}[/dim]"
    )


def _run_unit_ingest(units: list, *, model: str | None, db: Path | None, source_kind_default: str) -> None:
    """Generic ingest loop usable for any unit type with .transcript / .source_label / .source_kind."""
    conn = state.open_db(db)
    total_counts = {"commitment": 0, "decision": 0, "open_question": 0, "blocker": 0}
    failed = 0
    total_in_tokens = 0
    total_out_tokens = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err_console,
    )

    try:
        with progress:
            task = progress.add_task(f"Extracting {len(units)} units", total=len(units))
            for unit in units:
                progress.update(task, description=unit.source_label[:60])
                try:
                    result, diag = extract(unit.transcript, model=model)
                    summary = state.save_extraction(
                        conn, result, diag,
                        source_path=unit.source_label,
                        source_kind=getattr(unit, "source_kind", source_kind_default),
                    )
                    for k, v in summary.counts.items():
                        total_counts[k] += v
                    total_in_tokens += diag.input_tokens
                    total_out_tokens += diag.output_tokens
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    err_console.print(f"[red]  failed {unit.source_label}: {e}[/red]")
                progress.advance(task)
    finally:
        conn.close()

    total = sum(total_counts.values())
    body = (
        f"[bold]Extracted {total} items across {len(units) - failed}/{len(units)} units[/bold]\n"
        f"{total_counts['commitment']} commitments · "
        f"{total_counts['decision']} decisions · "
        f"{total_counts['open_question']} open questions · "
        f"{total_counts['blocker']} blockers\n"
        f"[dim]tokens: {total_in_tokens:,} in / {total_out_tokens:,} out  ·  "
        f"failed: {failed}[/dim]"
    )
    err_console.print(Panel(body, title="ingest complete", border_style="green", expand=False))


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
        first_quote = srcs[0]["verbatim_quote"] if srcs else ""
        quote = (first_quote[:60] + "…") if len(first_quote) > 60 else first_quote
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
