"""verbatim CLI — extract, persist (ingest), and query accumulated team state."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
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
from rich.prompt import Confirm
from rich.table import Table

from . import __version__, cost, email_digest, state, store, web
from . import ask as ask_lib
from . import contradictions as contradictions_lib
from . import reconcile as reconcile_lib
from . import simplify as simplify_lib
from . import slack_bot as slack_bot_lib
from .connectors import calendar as calendar_conn
from .connectors import github_pr, slack_api, slack_export
from .extractor import DEFAULT_MODEL, extract
from .projections import github_issues as gh_issues_proj
from .projections import jira as jira_proj
from .projections import linear as linear_proj
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

project_app = typer.Typer(name="project", help="Push extracted state to external trackers.")
app.add_typer(project_app)

slack_bot_app = typer.Typer(name="slack-bot", help="Run the Slack bot — slash commands + digest posting.")
app.add_typer(slack_bot_app)

digest_app = typer.Typer(name="digest", help="Send state digests over various channels.")
app.add_typer(digest_app)

watch_app = typer.Typer(
    name="watch",
    help="Long-running ingest daemons. Poll a source on an interval until interrupted.",
)
app.add_typer(watch_app)

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
    auto_reconcile: bool = typer.Option(
        False, "--auto-reconcile",
        help="Auto-merge new entities into existing canonicals when similarity ≥ threshold.",
    ),
    reconcile_threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD, "--reconcile-threshold",
        min=50, max=100,
        help=f"Similarity threshold for --auto-reconcile (default {reconcile_lib.DEFAULT_THRESHOLD}).",
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
            auto_reconcile=auto_reconcile,
            reconcile_threshold=reconcile_threshold,
        )
    finally:
        conn.close()

    if not quiet:
        total = sum(summary.counts.values())
        recon = f"  ·  reconciled: {summary.reconcile_links}" if auto_reconcile else ""
        body = (
            f"[bold]Saved {total} items[/bold] · "
            f"{summary.counts['commitment']} commitments · "
            f"{summary.counts['decision']} decisions · "
            f"{summary.counts['open_question']} open questions · "
            f"{summary.counts['blocker']} blockers\n"
            f"[dim]session_id: {summary.session_id}  ·  "
            f"db: {store.resolve_db_path(db)}{recon}[/dim]"
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
    auto_reconcile: bool = typer.Option(
        False, "--auto-reconcile",
        help="Auto-merge new entities into existing canonicals during ingest.",
    ),
    reconcile_threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD, "--reconcile-threshold",
        min=50, max=100,
        help=f"Similarity threshold for --auto-reconcile (default {reconcile_lib.DEFAULT_THRESHOLD}).",
    ),
    max_cost_usd: float | None = typer.Option(
        None, "--max-cost-usd",
        help="Stop ingesting once estimated spend in USD hits this number.",
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

    _run_slack_ingest(
        units, model=model, db=db,
        auto_reconcile=auto_reconcile, reconcile_threshold=reconcile_threshold,
        max_cost_usd=max_cost_usd,
    )


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


def _run_slack_ingest(
    units: list,
    *,
    model: str | None,
    db: Path | None,
    auto_reconcile: bool = False,
    reconcile_threshold: int = reconcile_lib.DEFAULT_THRESHOLD,
    max_cost_usd: float | None = None,
) -> None:
    conn = state.open_db(db)
    total_counts = {"commitment": 0, "decision": 0, "open_question": 0, "blocker": 0}
    failed = 0
    total_in_tokens = 0
    total_out_tokens = 0
    total_reconciled = 0
    run_cost = 0.0
    cost_aborted = False

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
                        auto_reconcile=auto_reconcile,
                        reconcile_threshold=reconcile_threshold,
                    )
                    for k, v in summary.counts.items():
                        total_counts[k] += v
                    total_in_tokens += diag.input_tokens
                    total_out_tokens += diag.output_tokens
                    total_reconciled += summary.reconcile_links
                    run_cost += cost.estimate_cost(
                        diag.model, diag.input_tokens, diag.output_tokens
                    )
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    err_console.print(f"[red]  failed {unit.source_label}: {e}[/red]")
                progress.advance(task)

                if max_cost_usd is not None and run_cost >= max_cost_usd:
                    err_console.print(
                        f"[yellow]Budget cap hit (${run_cost:.4f} ≥ ${max_cost_usd:.4f}). Stopping.[/yellow]"
                    )
                    cost_aborted = True
                    break
    finally:
        conn.close()

    total = sum(total_counts.values())
    recon = f"  ·  reconciled: {total_reconciled}" if auto_reconcile else ""
    cost_line = f"  ·  cost: ${run_cost:.4f}" if run_cost > 0 else ""
    aborted_note = "  ·  [yellow]budget capped[/yellow]" if cost_aborted else ""
    body = (
        f"[bold]Extracted {total} items across {len(units) - failed}/{len(units)} units[/bold]\n"
        f"{total_counts['commitment']} commitments · "
        f"{total_counts['decision']} decisions · "
        f"{total_counts['open_question']} open questions · "
        f"{total_counts['blocker']} blockers\n"
        f"[dim]tokens: {total_in_tokens:,} in / {total_out_tokens:,} out  ·  "
        f"failed: {failed}{recon}{cost_line}{aborted_note}[/dim]"
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
    auto_reconcile: bool = typer.Option(
        False, "--auto-reconcile",
        help="Auto-merge new entities into existing canonicals during ingest.",
    ),
    reconcile_threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD, "--reconcile-threshold",
        min=50, max=100,
        help=f"Similarity threshold for --auto-reconcile (default {reconcile_lib.DEFAULT_THRESHOLD}).",
    ),
    max_cost_usd: float | None = typer.Option(
        None, "--max-cost-usd",
        help="Stop ingesting once estimated spend in USD hits this number.",
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
    skipped: list[slack_api.ChannelNotAccessible] = []

    def on_skip(e: slack_api.ChannelNotAccessible) -> None:
        skipped.append(e)
        err_console.print(f"[yellow]skipping #{e.channel}[/yellow]: {e.hint}")

    try:
        units = list(
            client.iter_units(
                channels=channel,
                since=since_dt,
                until=until_dt,
                min_thread_messages=min_thread_messages,
                include_loose_messages=include_loose,
                include_private=include_private,
                on_channel_error=on_skip,
            )
        )
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Slack API call failed during message fetch: {e}[/red]")
        raise typer.Exit(code=1) from None

    if skipped and not units:
        err_console.print(
            f"[red]All {len(skipped)} requested channel(s) were inaccessible. "
            f"Nothing to ingest.[/red]"
        )
        raise typer.Exit(code=1)

    if limit is not None:
        units = units[:limit]

    if not units:
        err_console.print("[yellow]No units matched the filters.[/yellow]")
        return

    if dry_run:
        _print_slack_dry_run(units)
        return

    _run_slack_ingest(
        units, model=model, db=db,
        auto_reconcile=auto_reconcile, reconcile_threshold=reconcile_threshold,
        max_cost_usd=max_cost_usd,
    )


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
    auto_reconcile: bool = typer.Option(
        False, "--auto-reconcile",
        help="Auto-merge new entities into existing canonicals during ingest.",
    ),
    reconcile_threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD, "--reconcile-threshold",
        min=50, max=100,
        help=f"Similarity threshold for --auto-reconcile (default {reconcile_lib.DEFAULT_THRESHOLD}).",
    ),
    max_cost_usd: float | None = typer.Option(
        None, "--max-cost-usd",
        help="Stop ingesting once estimated spend in USD hits this number.",
    ),
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

    _run_unit_ingest(
        units, model=model, db=db, source_kind_default="github_pr",
        auto_reconcile=auto_reconcile, reconcile_threshold=reconcile_threshold,
        max_cost_usd=max_cost_usd,
    )


@app.command(name="ingest-calendar")
def ingest_calendar_cmd(
    provider: str = typer.Argument(
        ..., help="Calendar provider: google | outlook.",
    ),
    token: str | None = typer.Option(
        None, "--token",
        help="OAuth access token. Falls back to $GOOGLE_CALENDAR_TOKEN / "
             "$OUTLOOK_CALENDAR_TOKEN depending on provider.",
    ),
    calendar_id: str = typer.Option(
        "primary", "--calendar-id",
        help="Google calendar id to read (ignored for Outlook). Default: primary.",
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only events on/after this date (YYYY-MM-DD)."
    ),
    until: str | None = typer.Option(
        None, "--until", help="Only events on/before this date (YYYY-MM-DD)."
    ),
    include_empty: bool = typer.Option(
        False, "--include-empty",
        help="Also ingest events with no description and ≤1 attendee "
             "(skipped by default — they cost tokens for no signal).",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    auto_reconcile: bool = typer.Option(
        False, "--auto-reconcile",
        help="Auto-merge new entities into existing canonicals during ingest.",
    ),
    reconcile_threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD, "--reconcile-threshold",
        min=50, max=100,
    ),
    max_cost_usd: float | None = typer.Option(
        None, "--max-cost-usd",
        help="Stop ingesting once estimated spend in USD hits this number.",
    ),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Ingest meeting events from Google Calendar or Outlook.

    Each event's title, organizer, attendees, and description/agenda become
    one extraction session. Pass an OAuth access token with calendar read
    scope — see `verbatim.connectors.calendar` for how to obtain one.
    """
    provider = provider.lower()
    if provider not in {"google", "outlook"}:
        err_console.print(f"[red]provider must be google|outlook, got: {provider}[/red]")
        raise typer.Exit(code=2)

    if not token:
        env_var = (
            "GOOGLE_CALENDAR_TOKEN" if provider == "google"
            else "OUTLOOK_CALENDAR_TOKEN"
        )
        token = os.environ.get(env_var)
    if not token:
        env_var = (
            "GOOGLE_CALENDAR_TOKEN" if provider == "google"
            else "OUTLOOK_CALENDAR_TOKEN"
        )
        err_console.print(
            f"[red]Calendar token required.[/red] Set ${env_var} or pass --token. "
            "It must be an OAuth access token with calendar read scope."
        )
        raise typer.Exit(code=2)

    since_dt = _parse_iso_date(since, "--since")
    until_dt = _parse_iso_date(until, "--until")

    units: list = []
    try:
        if provider == "google":
            with calendar_conn.GoogleCalendarClient(token=token) as gcal:
                err_console.print("[dim]Fetching Google Calendar events…[/dim]")
                for event in gcal.iter_events(
                    calendar_id=calendar_id, since=since_dt, until=until_dt,
                ):
                    if not include_empty and not event.has_content:
                        continue
                    units.append(event)
                    if limit is not None and len(units) >= limit:
                        break
        else:
            with calendar_conn.OutlookCalendarClient(token=token) as ocal:
                err_console.print("[dim]Fetching Outlook calendar events…[/dim]")
                for event in ocal.iter_events(since=since_dt, until=until_dt):
                    if not include_empty and not event.has_content:
                        continue
                    units.append(event)
                    if limit is not None and len(units) >= limit:
                        break
    except httpx.HTTPStatusError as e:
        err_console.print(
            f"[red]Calendar API error: {e.response.status_code} "
            f"{e.response.text[:200]}[/red]"
        )
        raise typer.Exit(code=1) from None
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Calendar fetch failed: {e}[/red]")
        raise typer.Exit(code=1) from None

    if not units:
        err_console.print(
            "[yellow]No events matched the filters.[/yellow] "
            "Events with no description and ≤1 attendee are skipped unless "
            "you pass --include-empty."
        )
        return

    if dry_run:
        _print_calendar_dry_run(units)
        return

    _run_unit_ingest(
        units, model=model, db=db,
        source_kind_default=f"calendar_{provider}",
        auto_reconcile=auto_reconcile, reconcile_threshold=reconcile_threshold,
        max_cost_usd=max_cost_usd,
    )


def _print_calendar_dry_run(units: list) -> None:
    table = Table(
        show_header=True, header_style="bold cyan",
        title="Calendar ingest plan (dry run)",
    )
    table.add_column("when", no_wrap=True)
    table.add_column("event")
    table.add_column("organizer")
    table.add_column("attendees", justify="right")
    for u in units:
        table.add_row(
            u.start.strftime("%Y-%m-%d %H:%M"),
            u.title[:50] + ("…" if len(u.title) > 50 else ""),
            u.organizer[:30],
            str(len(u.attendees)),
        )
    console.print(table)
    err_console.print(
        f"[dim]{len(units)} events would be extracted. "
        f"Estimated cost at Sonnet pricing: ~${len(units) * 0.04:.2f}[/dim]"
    )


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


def _run_unit_ingest(
    units: list,
    *,
    model: str | None,
    db: Path | None,
    source_kind_default: str,
    auto_reconcile: bool = False,
    reconcile_threshold: int = reconcile_lib.DEFAULT_THRESHOLD,
    max_cost_usd: float | None = None,
) -> None:
    """Generic ingest loop usable for any unit type with .transcript / .source_label / .source_kind."""
    conn = state.open_db(db)
    total_counts = {"commitment": 0, "decision": 0, "open_question": 0, "blocker": 0}
    failed = 0
    total_in_tokens = 0
    total_out_tokens = 0
    total_reconciled = 0
    run_cost = 0.0
    cost_aborted = False

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
                        auto_reconcile=auto_reconcile,
                        reconcile_threshold=reconcile_threshold,
                    )
                    for k, v in summary.counts.items():
                        total_counts[k] += v
                    total_in_tokens += diag.input_tokens
                    total_out_tokens += diag.output_tokens
                    total_reconciled += summary.reconcile_links
                    run_cost += cost.estimate_cost(
                        diag.model, diag.input_tokens, diag.output_tokens
                    )
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    err_console.print(f"[red]  failed {unit.source_label}: {e}[/red]")
                progress.advance(task)

                if max_cost_usd is not None and run_cost >= max_cost_usd:
                    err_console.print(
                        f"[yellow]Budget cap hit (${run_cost:.4f} ≥ ${max_cost_usd:.4f}). Stopping.[/yellow]"
                    )
                    cost_aborted = True
                    break
    finally:
        conn.close()

    total = sum(total_counts.values())
    recon = f"  ·  reconciled: {total_reconciled}" if auto_reconcile else ""
    cost_line = f"  ·  cost: ${run_cost:.4f}" if run_cost > 0 else ""
    aborted_note = "  ·  [yellow]budget capped[/yellow]" if cost_aborted else ""
    body = (
        f"[bold]Extracted {total} items across {len(units) - failed}/{len(units)} units[/bold]\n"
        f"{total_counts['commitment']} commitments · "
        f"{total_counts['decision']} decisions · "
        f"{total_counts['open_question']} open questions · "
        f"{total_counts['blocker']} blockers\n"
        f"[dim]tokens: {total_in_tokens:,} in / {total_out_tokens:,} out  ·  "
        f"failed: {failed}{recon}{cost_line}{aborted_note}[/dim]"
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
    ungrouped: bool = typer.Option(
        False, "--ungrouped",
        help="Show every entity individually; do not fold merged siblings into canonicals.",
    ),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List commitments. By default, merged duplicates are folded into canonicals."""
    conn = state.open_db(db)
    try:
        items = state.list_commitments(
            conn, actor=actor, min_confidence=min_confidence,
            status=None if include_resolved else "open",
            canonical_only=not ungrouped,
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
    ungrouped: bool = typer.Option(False, "--ungrouped"),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List decisions. By default, merged duplicates are folded into canonicals."""
    conn = state.open_db(db)
    try:
        items = state.list_decisions(
            conn, min_confidence=min_confidence,
            status=None if include_resolved else "open",
            canonical_only=not ungrouped,
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
    ungrouped: bool = typer.Option(False, "--ungrouped"),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List open questions. By default, merged duplicates are folded into canonicals."""
    conn = state.open_db(db)
    try:
        items = state.list_open_questions(
            conn, raised_by=raised_by, min_confidence=min_confidence,
            canonical_only=not ungrouped, limit=limit,
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
    ungrouped: bool = typer.Option(False, "--ungrouped"),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List blockers. By default, merged duplicates are folded into canonicals."""
    conn = state.open_db(db)
    try:
        items = state.list_blockers(
            conn, owner=owner, min_confidence=min_confidence,
            canonical_only=not ungrouped, limit=limit,
        )
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
        total_spend = cost.total_spend(conn)
        in_tok, out_tok = cost.total_tokens(conn)
    finally:
        conn.close()
    cost_line = (
        f"\n[bold]${total_spend:.4f}[/bold] spent · "
        f"[dim]{in_tok:,} in / {out_tok:,} out tokens[/dim]"
        if total_spend > 0 else ""
    )
    body = (
        f"[bold]{s['sessions']}[/bold] sessions ingested\n"
        f"[bold]{s['commitments_open']}[/bold] open commitments · "
        f"[bold]{s['decisions_open']}[/bold] decisions · "
        f"[bold]{s['open_questions_open']}[/bold] open questions · "
        f"[bold]{s['blockers_open']}[/bold] blockers"
        f"{cost_line}"
    )
    console.print(Panel(body, title="verbatim state", border_style="cyan", expand=False))


@query_app.command("overdue")
def query_overdue(
    limit: int = typer.Option(200, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List open commitments whose deadline has already passed."""
    conn = state.open_db(db)
    try:
        items = state.overdue_commitments(conn, limit=limit)
    finally:
        conn.close()
    if not items:
        console.print("[green]Nothing overdue.[/green] Every dated commitment is still in time.")
        return
    _print_deadline_table(items, title="Overdue commitments")


@query_app.command("due-soon")
def query_due_soon(
    within: int = typer.Option(
        7, "--within", "-w", help="Day window counted as 'due soon'."
    ),
    limit: int = typer.Option(200, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List open commitments due today or within the next N days."""
    conn = state.open_db(db)
    try:
        items = state.due_soon_commitments(conn, within_days=within, limit=limit)
    finally:
        conn.close()
    if not items:
        console.print(
            f"[green]Nothing due in the next {within} days.[/green]"
        )
        return
    _print_deadline_table(items, title=f"Due within {within} days")


def _print_deadline_table(items: list[dict[str, Any]], *, title: str) -> None:
    table = Table(show_header=True, header_style="bold cyan", title=title)
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("when", no_wrap=True)
    table.add_column("actor")
    table.add_column("deliverable")
    table.add_column("deadline")
    for item in items:
        payload = item["payload"]
        days = item.get("days_until")
        status = item.get("due_status")
        if status == "overdue":
            when = f"[red]{abs(days)}d overdue[/red]"
        elif status == "due_today":
            when = "[yellow]today[/yellow]"
        elif days is not None:
            when = f"[yellow]in {days}d[/yellow]"
        else:
            when = "[dim]—[/dim]"
        table.add_row(
            item["id"][:8] + "…",
            when,
            payload.get("actor") or "—",
            payload.get("deliverable") or "—",
            payload.get("deadline") or "—",
        )
    console.print(table)


@query_app.command("contradictions")
def query_contradictions(
    topic_threshold: int = typer.Option(
        contradictions_lib.DEFAULT_TOPIC_THRESHOLD, "--topic-threshold",
        min=50, max=100, help="Min topic similarity to consider two decisions related.",
    ),
    outcome_threshold: int = typer.Option(
        contradictions_lib.DEFAULT_OUTCOME_THRESHOLD, "--outcome-threshold",
        min=0, max=100, help="Max outcome similarity to still count as a conflict.",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Flag decisions that look like they disagree — same topic, different outcome.

    A team that decides 'use Postgres' and later 'use SQLite' on the same
    topic has a contradiction nobody connected. This surfaces those pairs.
    """
    conn = state.open_db(db)
    try:
        pairs = contradictions_lib.find_contradictions(
            conn, topic_threshold=topic_threshold,
            outcome_threshold=outcome_threshold,
        )
    finally:
        conn.close()
    if not pairs:
        console.print("[green]No contradictions found.[/green] Open decisions are consistent.")
        return
    console.print(
        f"\n[bold yellow]{len(pairs)} possible contradiction"
        f"{'s' if len(pairs) != 1 else ''}[/bold yellow]\n"
    )
    for c in pairs:
        pa, pb = c.decision_a["payload"], c.decision_b["payload"]
        console.print(f"[bold]Topic:[/bold] {c.topic}  [dim](topic match {c.topic_score}%)[/dim]")
        console.print(
            f"  [cyan]VRB-{c.decision_a['id'][:8]}[/cyan] → {pa.get('outcome') or '—'}"
        )
        console.print(
            f"  [cyan]VRB-{c.decision_b['id'][:8]}[/cyan] → {pb.get('outcome') or '—'}"
        )
        console.print()


@query_app.command("stale")
def query_stale(
    days: int = typer.Option(
        30, "--days", "-d", help="Flag entities open and untouched this many days."
    ),
    kind: str | None = typer.Option(
        None, "--kind", "-k",
        help="Restrict to one kind: commitment | decision | open_question | blocker.",
    ),
    limit: int = typer.Option(200, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List open entities that have sat untouched too long.

    "Untouched" means no confirm / edit / reassign / dismiss activity since
    the cutoff — these are items quietly rotting in the state graph.
    """
    conn = state.open_db(db)
    try:
        items = state.stale_entities(conn, stale_after_days=days, kind=kind, limit=limit)
    finally:
        conn.close()
    if not items:
        console.print(
            f"[green]Nothing stale.[/green] Every open item has moved in the last {days} days."
        )
        return
    table = Table(
        show_header=True, header_style="bold cyan",
        title=f"Stale — open + untouched ≥ {days} days",
    )
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("idle", no_wrap=True)
    table.add_column("summary")
    for item in items:
        idle = item.get("idle_days")
        idle_txt = f"[red]{idle}d[/red]" if idle is not None else "—"
        payload = item["payload"]
        summary = (
            payload.get("deliverable") or payload.get("topic")
            or payload.get("question") or payload.get("blocked_thing") or "—"
        )
        table.add_row(item["id"][:8] + "…", item["kind"], idle_txt, summary)
    console.print(table)


@app.command(name="standup")
def standup_cmd(
    person: str = typer.Argument(..., help="Person's name (substring match)."),
    recent_days: int = typer.Option(
        7, "--recent-days", help="Window for the 'recently resolved' section."
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Generate a standup-style status summary for one person.

    Pulls from the state graph: what they're on the hook for, what's blocking
    them, open questions they raised, and what moved recently. Paste it
    straight into your standup channel.
    """
    conn = state.open_db(db)
    try:
        report = state.standup(conn, person, recent_days=recent_days)
    finally:
        conn.close()

    stats = report["stats"]
    if stats["total"] == 0 and not report["recently_resolved"]:
        console.print(
            f"[dim]Nothing recorded for '{person}'. "
            "Try `verbatim query people` to see known names.[/dim]"
        )
        return

    console.print(f"\n[bold cyan]Standup — {person}[/bold cyan]\n")

    owed = report["owed"]
    if owed:
        console.print("[bold violet]On the hook[/bold violet]")
        for c in owed:
            p = c["payload"]
            days = c.get("days_until")
            status = c.get("due_status")
            if status == "overdue":
                tag = f" [red](overdue {abs(days)}d)[/red]"
            elif status == "due_today":
                tag = " [yellow](due today)[/yellow]"
            elif days is not None:
                tag = f" [yellow](in {days}d)[/yellow]"
            else:
                tag = ""
            console.print(f"  • {p.get('deliverable') or '—'}{tag}")
        console.print()

    if report["blocked"]:
        console.print("[bold red]Blocked[/bold red]")
        for b in report["blocked"]:
            p = b["payload"]
            console.print(
                f"  • {p.get('blocked_thing') or '—'} "
                f"— blocked by {p.get('blocked_by') or '?'}"
            )
        console.print()

    if report["questions"]:
        console.print("[bold yellow]Open questions raised[/bold yellow]")
        for q in report["questions"]:
            p = q["payload"]
            console.print(f"  • {p.get('question') or p.get('topic') or '—'}")
        console.print()

    if report["recently_resolved"]:
        console.print("[bold green]Recently moved[/bold green]")
        for r in report["recently_resolved"]:
            ent = r["entity"]
            p = ent["payload"]
            summary = (
                p.get("deliverable") or p.get("topic")
                or p.get("question") or p.get("blocked_thing") or "—"
            )
            console.print(
                f"  • [{r['action']}] {ent['kind']}: {summary}"
            )
        console.print()


@query_app.command("person")
def query_person(
    name: str = typer.Argument(..., help="Person's name (substring match, case-insensitive)."),
    include_resolved: bool = typer.Option(False, "--all", help="Include resolved items."),
    limit_per_kind: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Show everything tied to a person — commitments owed, decisions involved
    in, questions raised, blockers owned. The single-pane answer to
    'what's Alice on the hook for?'.
    """
    conn = state.open_db(db)
    try:
        view = store.fetch_person(
            conn, name,
            include_resolved=include_resolved,
            limit_per_kind=limit_per_kind,
        )
    finally:
        conn.close()
    stats = view["stats"]
    if stats["total"] == 0:
        console.print(f"[dim]Nothing found for '{name}'. Try `verbatim query people` to see known names.[/dim]")
        return

    console.print(
        f"\n[bold cyan]{name}[/bold cyan] "
        f"[dim]· {stats['total']} items "
        f"({stats['commitments']} commitments · {stats['decisions']} decisions · "
        f"{stats['questions_raised']} questions · {stats['blockers_owned']} blockers)"
        f"[/dim]\n"
    )
    if view["commitments"]:
        console.print("[bold violet]Commitments owed[/bold violet]")
        _print_entity_table(view["commitments"], kind="commitment")
    if view["blockers_owned"]:
        console.print("\n[bold red]Blockers owned[/bold red]")
        _print_entity_table(view["blockers_owned"], kind="blocker")
    if view["questions_raised"]:
        console.print("\n[bold yellow]Questions raised[/bold yellow]")
        _print_entity_table(view["questions_raised"], kind="open_question")
    if view["decisions"]:
        console.print("\n[bold cyan]Decisions involved in[/bold cyan]")
        _print_entity_table(view["decisions"], kind="decision")


@query_app.command("people")
def query_people(
    limit: int = typer.Option(200, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List every distinct person who appears in the state graph, by frequency."""
    conn = state.open_db(db)
    try:
        people = store.list_known_people(conn, limit=limit)
    finally:
        conn.close()
    if not people:
        console.print("[dim]No people recorded yet. Ingest a transcript to populate state.[/dim]")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("person")
    table.add_column("items", justify="right")
    for p in people:
        table.add_row(p["name"], str(p["total"]))
    console.print(table)


@query_app.command("cost")
def query_cost(db: Path | None = typer.Option(None, "--db")) -> None:
    """Estimated total spend across every ingest session, broken down by model."""
    conn = state.open_db(db)
    try:
        total = cost.total_spend(conn)
        breakdown = cost.spend_breakdown(conn)
        in_tok, out_tok = cost.total_tokens(conn)
    finally:
        conn.close()
    if total == 0:
        console.print(
            "[dim]No spend recorded yet — try `verbatim ingest examples/sample_transcript.txt`.[/dim]"
        )
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("model")
    table.add_column("spend", justify="right")
    for model_name, spend in breakdown.items():
        table.add_row(model_name, f"${spend:.4f}")
    table.add_row("[bold]total[/bold]", f"[bold]${total:.4f}[/bold]")
    console.print(table)
    console.print(
        f"[dim]{in_tok:,} input tokens · {out_tok:,} output tokens. "
        f"Prices: see `verbatim.cost.PRICING` or override via $VERBATIM_PRICING.[/dim]"
    )


def _resolve_ref(conn, token: str) -> str | None:
    """Resolve `#330293`, `330293`, `VRB-330293`, or a UUID prefix → full id."""
    code = store.parse_entity_code(token)
    if code is not None:
        entity = store.fetch_entity_by_code(conn, code)
        if entity is not None:
            return entity["id"]
    return _resolve_id_prefix(conn, token.lstrip("#"))


@app.command(name="resolve")
def resolve_cmd(
    entity_ref: str = typer.Argument(
        ..., help="Entity reference: #330293, VRB-330293, or a UUID prefix.",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Mark an entity as resolved."""
    conn = state.open_db(db)
    try:
        full_id = _resolve_ref(conn, entity_ref)
        if full_id is None:
            err_console.print(f"[red]No entity matches '{entity_ref}'.[/red]")
            raise typer.Exit(code=1)
        ok = state.resolve_entity(conn, full_id)
    finally:
        conn.close()
    if ok:
        console.print(f"[green]✓[/green] resolved {full_id}")
    else:
        err_console.print("[yellow]Nothing changed.[/yellow]")


@app.command(name="del")
def del_cmd(
    entity_ref: str = typer.Argument(
        ..., help="Entity reference: #330293, VRB-330293, or a UUID prefix.",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Dismiss an item — removes it from active queries but keeps the audit trail.

    Matches the `/verbatim del #330293` Slack command. Soft delete: the row
    isn't removed, just marked `dismissed`; restore by updating its status
    via the web UI.
    """
    conn = state.open_db(db)
    try:
        full_id = _resolve_ref(conn, entity_ref)
        if full_id is None:
            err_console.print(f"[red]No entity matches '{entity_ref}'.[/red]")
            raise typer.Exit(code=1)
        ok = store.update_entity_status(conn, full_id, "dismissed")
        if ok:
            store.record_audit(
                conn, entity_id=full_id, action="dismiss",
                actor_id=None, actor_label="CLI",
                before={"status": "open"}, after={"status": "dismissed"},
            )
    finally:
        conn.close()
    if ok:
        console.print(f"[green]✓[/green] dismissed {full_id}")
    else:
        err_console.print("[yellow]Nothing changed.[/yellow]")


@app.command()
def version() -> None:
    """Print the verbatim version."""
    console.print(f"verbatim {__version__}")


# ----------------------- init (first-run wizard) -----------------------


@app.command("init")
def init_cmd(
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip prompts and accept all defaults (CI-friendly).",
    ),
    skip_sample: bool = typer.Option(
        False, "--skip-sample",
        help="Don't run the sample extraction step.",
    ),
) -> None:
    """First-run wizard. Validates env, sets up the DB, runs a sample extraction.

    Designed to take a brand-new user from `pip install verbatim-ai` to seeing
    real extracted output in under 60 seconds. Detects existing state so it's
    safe to run multiple times.
    """
    console.print(
        Panel(
            "[bold]Welcome to Verbatim.[/bold]\n"
            "[dim]The AI memory layer for engineering teams.[/dim]",
            border_style="cyan",
            expand=False,
        )
    )

    # Step 1 — DB
    db_path = store.resolve_db_path()
    if db_path.exists():
        console.print(f"[green]✓[/green] State DB found at [bold]{db_path}[/bold]")
    else:
        if yes or Confirm.ask(
            f"Create state DB at [bold]{db_path}[/bold]?", default=True
        ):
            conn = state.open_db()
            conn.close()
            console.print(f"[green]✓[/green] Created state DB at [bold]{db_path}[/bold]")
        else:
            console.print("[yellow]Skipped DB creation.[/yellow]")
            return

    # Step 2 — Anthropic API key
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if has_key:
        console.print("[green]✓[/green] [bold]ANTHROPIC_API_KEY[/bold] is set in your environment")
    else:
        err_console.print(
            "[yellow]![/yellow] [bold]ANTHROPIC_API_KEY[/bold] is not set."
        )
        console.print(
            "  Get one at https://console.anthropic.com/settings/keys, then add this\n"
            "  to your shell rc and re-source:\n"
            "    [dim]export ANTHROPIC_API_KEY=sk-ant-...[/dim]"
        )
        console.print("\n[dim]The wizard will continue, but extraction won't work until you set it.[/dim]")

    # Step 3 — optional sample extraction
    sample_path = Path(__file__).parent.parent.parent / "examples" / "sample_transcript.txt"
    if not sample_path.exists():
        # When installed via pip, the examples aren't in the package; fall back to a tiny inline sample.
        sample_path = None

    if skip_sample or not has_key:
        console.print("[dim]Skipping sample extraction.[/dim]")
    elif sample_path and (yes or Confirm.ask(
        "\nRun a sample extraction now? (~$0.07, ~30s)", default=True
    )):
        console.print(f"\n[dim]Extracting from {sample_path}…[/dim]")
        try:
            transcript = load_transcript(sample_path)
            result, diag = extract(transcript)
            conn = state.open_db()
            try:
                summary = state.save_extraction(
                    conn, result, diag, source_path=str(sample_path),
                )
            finally:
                conn.close()
            total = sum(summary.counts.values())
            console.print(
                f"[green]✓[/green] Extracted [bold]{total}[/bold] items: "
                f"{summary.counts['commitment']} commitments, "
                f"{summary.counts['decision']} decisions, "
                f"{summary.counts['open_question']} questions, "
                f"{summary.counts['blocker']} blockers"
            )
        except Exception as e:  # noqa: BLE001
            err_console.print(f"[red]Sample extraction failed: {e}[/red]")

    # Step 4 — what's next
    console.print(
        "\n"
        "[bold]Next steps:[/bold]\n"
        "  [dim]·[/dim] Browse what's in the state graph: [bold]verbatim query commitments[/bold]\n"
        "  [dim]·[/dim] Open the web UI:                   [bold]verbatim serve[/bold]\n"
        "  [dim]·[/dim] Ingest your own transcript:        [bold]verbatim ingest path/to/meeting.txt[/bold]\n"
        "  [dim]·[/dim] Wire up the Slack bot:             [bold]verbatim slack-bot run[/bold]\n"
        "  [dim]·[/dim] See all commands:                  [bold]verbatim --help[/bold]"
    )
    console.print(
        "\n[dim]Documentation: https://github.com/qatcod/verbatim[/dim]"
    )


# ----------------------- watch (daemon mode) -----------------------


def _watch_loop(
    *,
    label: str,
    interval_seconds: int,
    overlap_seconds: int,
    iterations: int | None,
    poll_once: callable,
) -> None:
    """Generic poll loop. `poll_once(since: datetime)` runs one iteration.

    `iterations=None` runs forever (until Ctrl-C). Finite `iterations` is for
    tests + one-shot manual checks.
    """
    import time

    err_console.print(
        Panel(
            f"[bold]{label}[/bold]\n"
            f"[dim]Polling every {interval_seconds}s "
            f"({overlap_seconds}s overlap to handle clock drift). "
            f"Ctrl-C to stop.[/dim]",
            title="watch", border_style="green", expand=False,
        )
    )

    count = 0
    try:
        while iterations is None or count < iterations:
            since = datetime.now(tz=timezone.utc).replace(microsecond=0)
            since = since - timedelta(seconds=interval_seconds + overlap_seconds)
            tick_label = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
            err_console.print(f"[dim][{tick_label}] polling…[/dim]")
            try:
                poll_once(since)
            except Exception as e:  # noqa: BLE001
                err_console.print(f"[red]  iteration failed: {e}[/red]")
            count += 1
            if iterations is None or count < iterations:
                time.sleep(interval_seconds)
    except KeyboardInterrupt:
        err_console.print("\n[dim]Stopping watch loop.[/dim]")


@watch_app.command("slack-api")
def watch_slack_api_cmd(
    token: str | None = typer.Option(None, "--token", envvar="SLACK_TOKEN"),
    channel: list[str] | None = typer.Option(
        None, "--channel", "-c",
        help="Channels to poll (repeatable). Default: all accessible channels.",
    ),
    interval: int = typer.Option(
        300, "--interval", "-i", min=30,
        help="Seconds between polls. Default 300 (5min). Minimum 30s to respect Slack rate limits.",
    ),
    overlap: int = typer.Option(
        60, "--overlap",
        help="Look-back overlap to handle clock drift / late messages. Auto-reconcile dedupes.",
    ),
    min_thread_messages: int = typer.Option(3, "--min-thread-messages"),
    include_loose: bool = typer.Option(False, "--include-loose"),
    include_private: bool = typer.Option(False, "--include-private"),
    auto_reconcile: bool = typer.Option(
        True, "--auto-reconcile/--no-auto-reconcile",
        help="Merge new entities into existing canonicals during ingest. Default ON for watch mode.",
    ),
    reconcile_threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD, "--reconcile-threshold", min=50, max=100,
    ),
    max_cost_usd: float | None = typer.Option(
        None, "--max-cost-usd",
        help="Per-iteration cost cap. Hit it, the iteration stops; loop continues next interval.",
    ),
    request_pause: float = typer.Option(0.5, "--request-pause"),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
    iterations: int | None = typer.Option(
        None, "--iterations",
        help="Run only this many iterations then stop. None = run forever. For testing.",
    ),
) -> None:
    """Continuously ingest a live Slack workspace.

    Polls `conversations.history` on an interval, ingesting threads that have
    new activity since the last poll. Each iteration is bounded by --interval
    + --overlap to handle clock drift; auto-reconcile dedupes anything we
    accidentally pick up twice. Ideal for cron-style "always-on" inboxing.
    """
    if not token:
        err_console.print("[red]Slack token required.[/red] Set $SLACK_TOKEN or pass --token.")
        raise typer.Exit(code=2)

    def poll_once(since: datetime) -> None:
        client = slack_api.SlackClient(token=token, request_pause=request_pause)
        skipped: list = []
        units = list(client.iter_units(
            channels=channel, since=since, until=None,
            min_thread_messages=min_thread_messages,
            include_loose_messages=include_loose,
            include_private=include_private,
            on_channel_error=skipped.append,
        ))
        if not units:
            err_console.print("[dim]  no new units this tick[/dim]")
            return
        err_console.print(f"[dim]  {len(units)} unit(s) to extract[/dim]")
        _run_slack_ingest(
            units, model=model, db=db,
            auto_reconcile=auto_reconcile, reconcile_threshold=reconcile_threshold,
            max_cost_usd=max_cost_usd,
        )

    _watch_loop(
        label="watch slack-api",
        interval_seconds=interval,
        overlap_seconds=overlap,
        iterations=iterations,
        poll_once=poll_once,
    )


@watch_app.command("github")
def watch_github_cmd(
    repo: str = typer.Argument(..., help="owner/name"),
    token: str | None = typer.Option(None, "--token", envvar="GITHUB_TOKEN"),
    state_filter: str = typer.Option(
        "all", "--state",
        help="PR state filter: open | closed | all.",
    ),
    interval: int = typer.Option(
        900, "--interval", "-i", min=60,
        help="Seconds between polls. Default 900 (15min). GitHub rate limits are looser than Slack.",
    ),
    overlap: int = typer.Option(120, "--overlap"),
    auto_reconcile: bool = typer.Option(True, "--auto-reconcile/--no-auto-reconcile"),
    reconcile_threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD, "--reconcile-threshold", min=50, max=100,
    ),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd"),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
    iterations: int | None = typer.Option(None, "--iterations"),
) -> None:
    """Continuously ingest GitHub PR discussions for a repo."""
    if not token:
        err_console.print("[red]GitHub token required.[/red] Set $GITHUB_TOKEN or pass --token.")
        raise typer.Exit(code=2)
    if "/" not in repo:
        err_console.print(f"[red]Repo must be owner/name form, got: {repo}[/red]")
        raise typer.Exit(code=2)
    if state_filter not in {"open", "closed", "all"}:
        err_console.print(f"[red]--state must be open|closed|all, got: {state_filter}[/red]")
        raise typer.Exit(code=2)

    def poll_once(since: datetime) -> None:
        units: list = []
        with github_pr.GitHubClient(token=token) as gh:
            for unit in gh.iter_pull_requests(
                repo, state=state_filter, since=since, until=None,
            ):
                units.append(unit)
        if not units:
            err_console.print("[dim]  no new PRs this tick[/dim]")
            return
        err_console.print(f"[dim]  {len(units)} PR(s) to extract[/dim]")
        _run_unit_ingest(
            units, model=model, db=db, source_kind_default="github_pr",
            auto_reconcile=auto_reconcile, reconcile_threshold=reconcile_threshold,
            max_cost_usd=max_cost_usd,
        )

    _watch_loop(
        label=f"watch github {repo}",
        interval_seconds=interval,
        overlap_seconds=overlap,
        iterations=iterations,
        poll_once=poll_once,
    )


# ----------------------- serve (web UI) -----------------------


@app.command("serve")
def serve_cmd(
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Bind address. Stays local by default — don't expose without auth.",
    ),
    port: int = typer.Option(8765, "--port", "-p", help="Port to bind."),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Run the Verbatim web UI.

    Read-mostly view of the state graph: dashboard, list pages per kind, entity
    detail with all source quotes, sessions, and active projections. Default
    binds to 127.0.0.1 — exposing this on a public interface is not safe yet
    (no auth, no CSRF, mutations would be needed for a multi-user flow).
    """
    try:
        import uvicorn
    except ImportError:
        err_console.print(
            "[red]uvicorn is required for `verbatim serve`. "
            "Reinstall with: pip install -e .[/red]"
        )
        raise typer.Exit(code=1) from None

    application = web.create_app(db_path=db)
    err_console.print(
        Panel(
            f"[bold]Verbatim web UI[/bold]\n"
            f"Open: [link=http://{host}:{port}]http://{host}:{port}[/link]\n"
            f"[dim]Local-only by default — see --host to change.[/dim]",
            title="serve",
            border_style="green",
            expand=False,
        )
    )
    uvicorn.run(application, host=host, port=port, log_level="warning")


# ----------------------- digest email -----------------------


@digest_app.command("email")
def digest_email_cmd(
    to: list[str] = typer.Option(
        ..., "--to", help="Recipient email(s). Repeatable.",
    ),
    smtp_host: str | None = typer.Option(
        None, "--smtp-host", envvar="SMTP_HOST",
        help="SMTP server. Reads $SMTP_HOST if not passed.",
    ),
    smtp_port: int = typer.Option(
        587, "--smtp-port", envvar="SMTP_PORT", help="587 for STARTTLS, 465 for SSL.",
    ),
    smtp_user: str | None = typer.Option(
        None, "--smtp-user", envvar="SMTP_USER",
        help="SMTP auth username. Reads $SMTP_USER.",
    ),
    smtp_password: str | None = typer.Option(
        None, "--smtp-password", envvar="SMTP_PASSWORD",
        help="SMTP auth password. Reads $SMTP_PASSWORD.",
    ),
    sender: str | None = typer.Option(
        None, "--from", envvar="SMTP_FROM",
        help="From: header email. Reads $SMTP_FROM. Falls back to --smtp-user.",
    ),
    sender_name: str = typer.Option(
        "Verbatim", "--sender-name",
        help="Display name shown next to From: address.",
    ),
    use_ssl: bool = typer.Option(
        False, "--ssl/--starttls",
        help="--ssl for SMTPS (port 465). Default is STARTTLS.",
    ),
    brand: str = typer.Option(
        "Verbatim", "--brand", help="Brand name in the digest subject/title.",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Render the current state as a digest email and send via SMTP.

    Useful from cron: weekly Monday-morning digest to the team, or daily digest
    to an exec. The same render is the basis for the Slack digest and the web
    dashboard, so consumers see consistent content across surfaces.
    """
    if not smtp_host:
        err_console.print("[red]SMTP host required (--smtp-host or $SMTP_HOST).[/red]")
        raise typer.Exit(code=2)
    sender_addr = sender or smtp_user
    if not sender_addr:
        err_console.print("[red]Sender required (--from / $SMTP_FROM, or --smtp-user).[/red]")
        raise typer.Exit(code=2)

    conn = state.open_db(db)
    try:
        content = email_digest.render_digest(conn, brand=brand)
    finally:
        conn.close()

    msg = email_digest.build_message(
        content, sender=sender_addr, recipients=to, sender_name=sender_name,
    )
    smtp_cfg = email_digest.SmtpConfig(
        host=smtp_host, port=smtp_port,
        username=smtp_user, password=smtp_password, use_ssl=use_ssl,
    )
    try:
        email_digest.send_via_smtp(msg, smtp_cfg)
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]SMTP send failed: {e}[/red]")
        raise typer.Exit(code=1) from None

    console.print(
        f"[green]✓[/green] digest sent to {len(to)} recipient(s) "
        f"via {smtp_host}:{smtp_port}"
    )


# ----------------------- slack-bot (consumer-facing Slack surface) -----------------------


@slack_bot_app.command("run")
def slack_bot_run_cmd(
    bot_token: str | None = typer.Option(
        None, "--bot-token", envvar="SLACK_BOT_TOKEN",
        help="Bot token (xoxb-...). Reads $SLACK_BOT_TOKEN if not passed.",
    ),
    app_token: str | None = typer.Option(
        None, "--app-token", envvar="SLACK_APP_TOKEN",
        help="App-level token (xapp-...) with connections:write. Reads $SLACK_APP_TOKEN.",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Start the Slack bot. Blocks until interrupted (Ctrl-C).

    Connects to Slack via Socket Mode — no public URL needed. Listens for
    /verbatim slash commands and replies with state queries. Posts ephemeral
    replies so command output is only visible to the invoker.

    Setup: see slack_bot.py module docstring or the README. tl;dr — turn on
    Socket Mode in your existing Slack App and register /verbatim as a slash
    command.
    """
    if not bot_token:
        err_console.print("[red]Bot token required.[/red] Set $SLACK_BOT_TOKEN or pass --bot-token.")
        raise typer.Exit(code=2)
    if not app_token:
        err_console.print("[red]App token required.[/red] Set $SLACK_APP_TOKEN or pass --app-token.")
        raise typer.Exit(code=2)

    try:
        bot = slack_bot_lib.VerbatimSlackBot(bot_token=bot_token, app_token=app_token, db_path=db)
    except ValueError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from None

    err_console.print(
        Panel(
            "[bold]Verbatim Slack bot running[/bold]\n"
            "[dim]Connected via Socket Mode. Listening for /verbatim slash commands. "
            "Ctrl-C to stop.[/dim]",
            title="slack-bot",
            border_style="green",
            expand=False,
        )
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        err_console.print("[dim]Shutting down.[/dim]")


@slack_bot_app.command("digest")
def slack_bot_digest_cmd(
    channel: str = typer.Argument(
        ..., help="Channel id (e.g. C0123456) or name (e.g. #engineering) to post to.",
    ),
    bot_token: str | None = typer.Option(
        None, "--bot-token", envvar="SLACK_BOT_TOKEN",
        help="Bot token (xoxb-...).",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Post a summary digest of current Verbatim state into a Slack channel.

    Useful from cron (e.g. weekly Monday morning digest) or after a batch
    ingest. Does not require Socket Mode (the bot does not need to be
    running) — uses only the Web API.
    """
    if not bot_token:
        err_console.print("[red]Bot token required.[/red] Set $SLACK_BOT_TOKEN or pass --bot-token.")
        raise typer.Exit(code=2)
    bot = slack_bot_lib.VerbatimSlackBot(bot_token=bot_token, app_token="not-needed-for-digest-only", db_path=db)
    try:
        result = bot.post_digest(channel)
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Digest post failed: {e}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]✓[/green] digest posted to {channel} (ts: {result.get('ts', '?')})")


@slack_bot_app.command("nudge")
def slack_bot_nudge_cmd(
    channel: str = typer.Argument(
        ..., help="Channel id or name to post the deadline nudge to.",
    ),
    within: int = typer.Option(
        7, "--within", "-w", help="Day window counted as 'due soon'."
    ),
    bot_token: str | None = typer.Option(
        None, "--bot-token", envvar="SLACK_BOT_TOKEN",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Post a deadline nudge — overdue + due-soon commitments — to a channel.

    Built for cron: a daily or Monday-morning `verbatim slack-bot nudge
    #team` keeps slipping commitments visible. Web API only — the bot does
    not need to be running.
    """
    if not bot_token:
        err_console.print("[red]Bot token required.[/red] Set $SLACK_BOT_TOKEN or pass --bot-token.")
        raise typer.Exit(code=2)
    bot = slack_bot_lib.VerbatimSlackBot(
        bot_token=bot_token, app_token="not-needed-for-nudge-only", db_path=db,
    )
    try:
        result = bot.post_nudge(channel, within_days=within)
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Nudge post failed: {e}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]✓[/green] deadline nudge posted to {channel} (ts: {result.get('ts', '?')})")


@slack_bot_app.command("post-card")
def slack_bot_post_card_cmd(
    channel: str = typer.Argument(..., help="Channel id or name to post to."),
    entity_id: str = typer.Argument(..., help="Verbatim entity id (or 8-char prefix)."),
    bot_token: str | None = typer.Option(
        None, "--bot-token", envvar="SLACK_BOT_TOKEN",
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Post an interactive extraction card (with Confirm / Dismiss / Edit / Reassign
    buttons) for one entity to a Slack channel.

    Button clicks fire `block_actions` events that the running `verbatim slack-bot
    run` daemon picks up and dispatches. If the daemon is not running, the buttons
    won't do anything — start it with `verbatim slack-bot run`.
    """
    if not bot_token:
        err_console.print("[red]Bot token required.[/red] Set $SLACK_BOT_TOKEN or pass --bot-token.")
        raise typer.Exit(code=2)

    # Resolve prefix → full id
    conn = state.open_db(db)
    try:
        full_id = entity_id
        if len(entity_id) < 32:
            row = conn.execute(
                "SELECT id FROM entities WHERE id LIKE ? LIMIT 2",
                (entity_id + "%",),
            ).fetchall()
            if len(row) != 1:
                err_console.print(
                    f"[red]Entity prefix '{entity_id}' didn't uniquely resolve "
                    f"({len(row)} matches).[/red]"
                )
                raise typer.Exit(code=1)
            full_id = row[0]["id"]
    finally:
        conn.close()

    bot = slack_bot_lib.VerbatimSlackBot(
        bot_token=bot_token,
        app_token="not-needed-for-card-post",
        db_path=db,
    )
    try:
        result = bot.post_extraction_card(channel, full_id)
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Card post failed: {e}[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        f"[green]✓[/green] extraction card posted to {channel} "
        f"(ts: {result.get('ts', '?')})"
    )


# ----------------------- project (push to external trackers) -----------------------


@project_app.command("linear")
def project_linear_cmd(
    team: str = typer.Option(
        ..., "--team",
        help="Linear team name or key (e.g. 'Engineering' or 'ENG'). Required.",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", envvar="LINEAR_API_KEY",
        help="Linear personal API key. Reads $LINEAR_API_KEY if not passed.",
    ),
    workflow_state: str | None = typer.Option(
        None, "--state",
        help="Workflow state name (e.g. 'Backlog' or 'Todo'). Defaults to Linear's team default.",
    ),
    min_confidence: str = typer.Option(
        "high", "--min-confidence", "-c",
        help="Only project commitments at this confidence or higher. Default: high.",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Stop after this many issues."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without creating issues."),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Push pending Verbatim commitments out as Linear issues. Idempotent."""
    if not api_key:
        err_console.print("[red]Linear API key required.[/red] Set $LINEAR_API_KEY or pass --api-key.")
        raise typer.Exit(code=2)

    try:
        client = linear_proj.LinearClient(api_key=api_key)
    except ValueError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from None

    with client:
        try:
            teams = client.list_teams()
        except Exception as e:  # noqa: BLE001
            err_console.print(f"[red]Linear API call failed listing teams: {e}[/red]")
            raise typer.Exit(code=1) from None

        team_match = _match_team(teams, team)
        if team_match is None:
            err_console.print(
                f"[red]No team matched '{team}'. Available: "
                f"{', '.join(t['name'] for t in teams)}[/red]"
            )
            raise typer.Exit(code=1)

        state_id: str | None = None
        if workflow_state:
            states = client.list_workflow_states(team_match["id"])
            ws = _match_workflow_state(states, workflow_state)
            if ws is None:
                err_console.print(
                    f"[red]No workflow state matched '{workflow_state}' in team "
                    f"'{team_match['name']}'. Available: {', '.join(s['name'] for s in states)}[/red]"
                )
                raise typer.Exit(code=1)
            state_id = ws["id"]

        try:
            users = client.list_users()
        except Exception as e:  # noqa: BLE001
            err_console.print(f"[yellow]Couldn't fetch Linear users for assignee resolution: {e}[/yellow]")
            users = []
        resolver = linear_proj.build_user_resolver(users)

        conn = state.open_db(db)
        try:
            commitments = state.list_commitments(
                conn, min_confidence=min_confidence, limit=limit or 200,
            )
            plans = [linear_proj.plan_projection(conn, c, assignee_resolver=resolver) for c in commitments]
            pending = [p for p in plans if not p.skip_reason]
            skipped = [p for p in plans if p.skip_reason]
            if limit is not None:
                pending = pending[:limit]

            if not pending:
                err_console.print(
                    f"[yellow]No commitments to project.[/yellow] "
                    f"({len(skipped)} skipped — already projected or non-canonical)"
                )
                return

            if dry_run:
                _print_linear_dry_run(pending, skipped, team_match, workflow_state)
                return

            created: list[dict[str, Any]] = []
            for plan in pending:
                try:
                    info = linear_proj.execute_projection(
                        conn, client, plan,
                        team_id=team_match["id"], state_id=state_id,
                    )
                    created.append(info)
                    err_console.print(
                        f"[green]✓[/green] {info.get('identifier') or '?'}: {plan.draft.title[:60]}"
                    )
                except Exception as e:  # noqa: BLE001
                    err_console.print(f"[red]  failed for entity {plan.entity['id'][:8]}: {e}[/red]")
        finally:
            conn.close()

    body = (
        f"[bold]Created {len(created)} Linear issue(s)[/bold]\n"
        f"[dim]skipped (already-projected/merged): {len(skipped)}  ·  team: {team_match['name']}[/dim]"
    )
    err_console.print(Panel(body, title="linear projection complete", border_style="green", expand=False))


@project_app.command("github")
def project_github_cmd(
    repo: str = typer.Option(
        ..., "--repo",
        help="Target GitHub repo in owner/name form (e.g. qatcod/verbatim).",
    ),
    token: str | None = typer.Option(
        None, "--token", envvar="GITHUB_TOKEN",
        help="GitHub PAT. Reads $GITHUB_TOKEN if not passed.",
    ),
    label: list[str] | None = typer.Option(
        None, "--label",
        help="Extra label(s) to apply to each issue (repeatable). 'verbatim' is always added.",
    ),
    min_confidence: str = typer.Option(
        "high", "--min-confidence", "-c",
        help="Only project commitments at this confidence or higher. Default: high.",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Push pending Verbatim commitments out as GitHub issues. Idempotent."""
    if not token:
        err_console.print("[red]GitHub token required.[/red] Set $GITHUB_TOKEN or pass --token.")
        raise typer.Exit(code=2)
    if "/" not in repo:
        err_console.print(f"[red]Repo must be owner/name form, got: {repo}[/red]")
        raise typer.Exit(code=2)

    conn = state.open_db(db)
    try:
        commits = state.list_commitments(conn, min_confidence=min_confidence, limit=limit or 200)
        plans = [gh_issues_proj.plan_projection(conn, c, extra_labels=label) for c in commits]
        pending = [p for p in plans if not p.skip_reason]
        skipped = [p for p in plans if p.skip_reason]
        if limit is not None:
            pending = pending[:limit]
        if not pending:
            err_console.print(
                f"[yellow]No commitments to project.[/yellow] "
                f"({len(skipped)} skipped — already projected, merged, or below confidence threshold)"
            )
            return

        if dry_run:
            table = Table(
                show_header=True, header_style="bold cyan",
                title=f"GitHub Issues projection plan (dry run) — {repo}",
            )
            table.add_column("entity")
            table.add_column("title")
            table.add_column("labels")
            for p in pending:
                table.add_row(
                    p.entity["id"][:8] + "…",
                    p.draft.title[:60] + ("…" if len(p.draft.title) > 60 else ""),
                    ", ".join(p.draft.labels),
                )
            console.print(table)
            err_console.print(f"[dim]{len(pending)} issues would be created; {len(skipped)} skipped[/dim]")
            return

        try:
            client = gh_issues_proj.GitHubIssuesClient(token=token)
        except ValueError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2) from None

        created: list[dict[str, Any]] = []
        with client:
            for plan in pending:
                try:
                    info = gh_issues_proj.execute_projection(conn, client, plan, repo=repo)
                    created.append(info)
                    err_console.print(
                        f"[green]✓[/green] #{info.get('number')}: {plan.draft.title[:60]}"
                    )
                except Exception as e:  # noqa: BLE001
                    err_console.print(
                        f"[red]  failed for entity {plan.entity['id'][:8]}: {e}[/red]"
                    )
    finally:
        conn.close()

    body = (
        f"[bold]Created {len(created)} GitHub issue(s)[/bold] in [cyan]{repo}[/cyan]\n"
        f"[dim]skipped (already-projected/merged/low-confidence): {len(skipped)}[/dim]"
    )
    err_console.print(Panel(body, title="github projection complete", border_style="green", expand=False))


@project_app.command("jira")
def project_jira_cmd(
    site: str = typer.Option(
        ..., "--site",
        help="Atlassian site (e.g. https://yourco.atlassian.net).",
    ),
    project_key: str = typer.Option(
        ..., "--project",
        help="Jira project key (e.g. ENG).",
    ),
    email: str | None = typer.Option(
        None, "--email", envvar="JIRA_EMAIL",
        help="Atlassian account email. Reads $JIRA_EMAIL.",
    ),
    api_token: str | None = typer.Option(
        None, "--api-token", envvar="JIRA_API_TOKEN",
        help="Atlassian API token. Reads $JIRA_API_TOKEN.",
    ),
    issuetype: str = typer.Option("Task", "--issuetype"),
    label: list[str] | None = typer.Option(None, "--label"),
    min_confidence: str = typer.Option("high", "--min-confidence", "-c"),
    limit: int | None = typer.Option(None, "--limit", "-n"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Push pending Verbatim commitments out as Jira issues. Idempotent."""
    if not email or not api_token:
        err_console.print(
            "[red]Jira email + API token required.[/red] Set $JIRA_EMAIL and $JIRA_API_TOKEN, "
            "or pass --email and --api-token."
        )
        raise typer.Exit(code=2)

    conn = state.open_db(db)
    try:
        commits = state.list_commitments(conn, min_confidence=min_confidence, limit=limit or 200)
        plans = [jira_proj.plan_projection(conn, c, extra_labels=label) for c in commits]
        pending = [p for p in plans if not p.skip_reason]
        skipped = [p for p in plans if p.skip_reason]
        if limit is not None:
            pending = pending[:limit]
        if not pending:
            err_console.print(
                f"[yellow]No commitments to project.[/yellow] "
                f"({len(skipped)} skipped — already projected or merged)"
            )
            return

        if dry_run:
            table = Table(
                show_header=True, header_style="bold cyan",
                title=f"Jira projection plan (dry run) — project {project_key}",
            )
            table.add_column("entity")
            table.add_column("summary")
            table.add_column("issuetype")
            table.add_column("labels")
            for p in pending:
                table.add_row(
                    p.entity["id"][:8] + "…",
                    p.draft.summary[:60] + ("…" if len(p.draft.summary) > 60 else ""),
                    issuetype,
                    ", ".join(p.draft.labels),
                )
            console.print(table)
            err_console.print(f"[dim]{len(pending)} issues would be created; {len(skipped)} skipped[/dim]")
            return

        try:
            client = jira_proj.JiraClient(site=site, email=email, api_token=api_token)
        except ValueError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2) from None

        created: list[dict[str, Any]] = []
        with client:
            for plan in pending:
                try:
                    info = jira_proj.execute_projection(
                        conn, client, plan,
                        project_key=project_key, issuetype=issuetype,
                    )
                    created.append(info)
                    err_console.print(
                        f"[green]✓[/green] {info.get('key')}: {plan.draft.summary[:60]}"
                    )
                except Exception as e:  # noqa: BLE001
                    err_console.print(
                        f"[red]  failed for entity {plan.entity['id'][:8]}: {e}[/red]"
                    )
    finally:
        conn.close()

    body = (
        f"[bold]Created {len(created)} Jira issue(s)[/bold] in [cyan]{project_key}[/cyan]\n"
        f"[dim]skipped (already-projected/merged/low-confidence): {len(skipped)}[/dim]"
    )
    err_console.print(Panel(body, title="jira projection complete", border_style="green", expand=False))


@project_app.command("status")
def project_status_cmd(
    target: str = typer.Option("linear_issue", "--target", help="Projection target kind to filter."),
    show_inactive: bool = typer.Option(False, "--all", help="Include inactive projections."),
    limit: int = typer.Option(100, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """List active projections — what's been pushed where."""
    conn = state.open_db(db)
    try:
        projections = store.list_projections(
            conn,
            target_kind=target,
            status=None if show_inactive else "active",
            limit=limit,
        )
    finally:
        conn.close()
    if not projections:
        console.print("[dim]No projections found.[/dim]")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("projection id", style="dim")
    table.add_column("kind")
    table.add_column("entity")
    table.add_column("external")
    table.add_column("url")
    table.add_column("status")
    for p in projections:
        meta = p.get("metadata") or {}
        ext = meta.get("identifier") or (p["external_id"] or "")[:10]
        table.add_row(
            p["id"][:8] + "…",
            p.get("entity_kind") or "—",
            f"{p.get('primary_actor') or '?'}: {(p.get('primary_topic') or '')[:40]}",
            ext,
            (p.get("external_url") or "")[:50],
            p["status"],
        )
    console.print(table)


@app.command(name="unproject")
def unproject_cmd(
    projection_id: str = typer.Argument(..., help="Projection id (or prefix)."),
    close_external: bool = typer.Option(
        False, "--close-external",
        help="Also close/archive the issue in the external system (Linear).",
    ),
    api_key: str | None = typer.Option(None, "--api-key", envvar="LINEAR_API_KEY"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Deactivate a projection. The external issue stays unless --close-external is passed."""
    conn = state.open_db(db)
    try:
        full_id = _resolve_projection_prefix(conn, projection_id)
        if full_id is None:
            err_console.print(f"[red]No projection matches prefix '{projection_id}'.[/red]")
            raise typer.Exit(code=1)
        client = None
        if close_external:
            if not api_key:
                err_console.print("[red]--close-external requires $LINEAR_API_KEY or --api-key.[/red]")
                raise typer.Exit(code=2)
            client = linear_proj.LinearClient(api_key=api_key)
        try:
            changed = linear_proj.deactivate_projection(
                conn, full_id, client=client, close_linear=close_external,
            )
        finally:
            if client is not None:
                client.close()
    finally:
        conn.close()
    if changed:
        msg = "; Linear issue archived" if close_external else ""
        console.print(f"[green]✓[/green] projection {full_id[:8]}… deactivated{msg}")
    else:
        err_console.print("[yellow]No change.[/yellow]")


def _match_team(teams: list[dict], query: str) -> dict | None:
    q = query.lower().strip()
    for t in teams:
        if t.get("id") == query:
            return t
        if (t.get("name") or "").lower() == q:
            return t
        if (t.get("key") or "").lower() == q:
            return t
    return None


def _match_workflow_state(states: list[dict], query: str) -> dict | None:
    q = query.lower().strip()
    for s in states:
        if s.get("id") == query:
            return s
        if (s.get("name") or "").lower() == q:
            return s
    return None


def _print_linear_dry_run(
    pending: list[linear_proj.ProjectionPlan],
    skipped: list[linear_proj.ProjectionPlan],
    team: dict,
    workflow_state: str | None,
) -> None:
    table = Table(
        show_header=True, header_style="bold cyan",
        title=f"Linear projection plan (dry run) — team {team['name']}"
              + (f" / state {workflow_state}" if workflow_state else ""),
    )
    table.add_column("entity")
    table.add_column("title")
    table.add_column("assignee")
    table.add_column("due")
    for p in pending:
        d = p.draft
        table.add_row(
            p.entity["id"][:8] + "…",
            d.title[:60] + ("…" if len(d.title) > 60 else ""),
            "(assigned)" if d.assignee_id else "(unassigned)",
            d.due_date or "—",
        )
    console.print(table)
    err_console.print(f"[dim]{len(pending)} issues would be created; {len(skipped)} skipped[/dim]")


def _resolve_projection_prefix(conn, prefix: str) -> str | None:
    rows = conn.execute(
        "SELECT id FROM projections WHERE id LIKE ? LIMIT 2",
        (prefix + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


# ----------------------- reconcile / link / unlink / show -----------------------


@app.command(name="reconcile")
def reconcile_cmd(
    threshold: int = typer.Option(
        reconcile_lib.DEFAULT_THRESHOLD,
        "--threshold", "-t", min=50, max=100,
        help=f"Minimum topic similarity (0–100) to auto-merge. Default: {reconcile_lib.DEFAULT_THRESHOLD}.",
    ),
    kind: list[str] | None = typer.Option(
        None, "--kind",
        help="Restrict to specific kinds: commitment | decision | open_question | blocker.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview merges without writing."),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Sweep the state graph and merge entities that look like duplicates.

    Same actor (case-insensitive), same kind, topic similarity ≥ threshold using
    rapidfuzz token-set comparison. Older canonical wins so audit history reads
    left-to-right in time.
    """
    conn = state.open_db(db)
    try:
        if dry_run:
            preview = _reconcile_preview(conn, threshold=threshold, kinds=kind)
            _print_reconcile_preview(preview, threshold=threshold)
            return
        result = reconcile_lib.reconcile_all(conn, threshold=threshold, kinds=kind)
    finally:
        conn.close()

    body = (
        f"[bold]Linked {result.linked} entities[/bold]\n"
        f"{result.no_match} had no match · {result.skipped_unchanged} already-merged\n"
        f"[dim]threshold: {threshold}[/dim]"
    )
    err_console.print(Panel(body, title="reconcile complete", border_style="green", expand=False))


@app.command(name="link")
def link_cmd(
    canonical_id: str = typer.Argument(..., help="The canonical entity (id or prefix). It keeps its identity."),
    member_id: str = typer.Argument(..., help="The entity to merge into the canonical (id or prefix)."),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Manually link two entities — `member` becomes a sibling of `canonical`."""
    conn = state.open_db(db)
    try:
        full_canonical = _resolve_id_prefix(conn, canonical_id)
        full_member = _resolve_id_prefix(conn, member_id)
        if full_canonical is None:
            err_console.print(f"[red]No entity matches canonical prefix '{canonical_id}'.[/red]")
            raise typer.Exit(code=1)
        if full_member is None:
            err_console.print(f"[red]No entity matches member prefix '{member_id}'.[/red]")
            raise typer.Exit(code=1)
        try:
            reconcile_lib.link_entities(conn, canonical_id=full_canonical, member_id=full_member)
        except ValueError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2) from None
    finally:
        conn.close()
    console.print(f"[green]✓[/green] linked {full_member[:8]}… → {full_canonical[:8]}…")


@app.command(name="unlink")
def unlink_cmd(
    entity_id: str = typer.Argument(..., help="Entity id (or prefix) to restore as standalone canonical."),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Restore a merged entity to standalone-canonical status."""
    conn = state.open_db(db)
    try:
        full_id = _resolve_id_prefix(conn, entity_id)
        if full_id is None:
            err_console.print(f"[red]No entity matches id prefix '{entity_id}'.[/red]")
            raise typer.Exit(code=1)
        changed = reconcile_lib.unlink_entity(conn, full_id)
    finally:
        conn.close()
    if changed:
        console.print(f"[green]✓[/green] {full_id[:8]}… is now its own canonical")
    else:
        err_console.print(f"[yellow]{full_id[:8]}… was already canonical[/yellow]")


@app.command(name="relate")
def relate_cmd(
    from_id: str = typer.Argument(..., help="The 'from' entity (id or prefix)."),
    to_id: str = typer.Argument(..., help="The 'to' entity (id or prefix)."),
    rel_type: str = typer.Option(
        "relates-to", "--type", "-t",
        help="resolves | answers | supersedes | blocks | relates-to.",
    ),
    note: str | None = typer.Option(None, "--note"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Create a typed relationship between two entities.

    Unlike `link` (which merges duplicates), `relate` records a typed edge
    between two *distinct* entities — e.g. a commitment that `resolves` a
    blocker, or a decision that `answers` an open question.
    """
    conn = state.open_db(db)
    try:
        full_from = _resolve_id_prefix(conn, from_id)
        full_to = _resolve_id_prefix(conn, to_id)
        if full_from is None:
            err_console.print(f"[red]No entity matches prefix '{from_id}'.[/red]")
            raise typer.Exit(code=1)
        if full_to is None:
            err_console.print(f"[red]No entity matches prefix '{to_id}'.[/red]")
            raise typer.Exit(code=1)
        try:
            store.add_relationship(
                conn, from_entity_id=full_from, to_entity_id=full_to,
                rel_type=rel_type, note=note,
            )
        except store.RelationshipError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2) from None
    finally:
        conn.close()
    console.print(
        f"[green]✓[/green] VRB-{full_from[:8]} [bold]{rel_type}[/bold] VRB-{full_to[:8]}"
    )


@app.command(name="unrelate")
def unrelate_cmd(
    from_id: str = typer.Argument(..., help="The 'from' entity (id or prefix)."),
    to_id: str = typer.Argument(..., help="The 'to' entity (id or prefix)."),
    rel_type: str | None = typer.Option(
        None, "--type", "-t", help="Only remove this type; omit to remove all."
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Remove relationship(s) between two entities."""
    conn = state.open_db(db)
    try:
        full_from = _resolve_id_prefix(conn, from_id)
        full_to = _resolve_id_prefix(conn, to_id)
        if full_from is None or full_to is None:
            err_console.print("[red]One or both entity prefixes didn't resolve.[/red]")
            raise typer.Exit(code=1)
        removed = store.remove_relationship(
            conn, from_entity_id=full_from, to_entity_id=full_to, rel_type=rel_type,
        )
    finally:
        conn.close()
    if removed:
        console.print(f"[green]✓[/green] removed {removed} relationship(s)")
    else:
        err_console.print("[yellow]No matching relationship to remove.[/yellow]")


@app.command(name="show")
def show_cmd(
    entity_id: str = typer.Argument(..., help="Entity id (or prefix) to display."),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Show a single entity with all its merged members and source quotes."""
    conn = state.open_db(db)
    try:
        full_id = _resolve_id_prefix(conn, entity_id)
        if full_id is None:
            err_console.print(f"[red]No entity matches id prefix '{entity_id}'.[/red]")
            raise typer.Exit(code=1)
        entity = state.show_entity(conn, full_id)
        rels = store.fetch_relationships(conn, full_id) if entity else None
    finally:
        conn.close()

    if entity is None:
        err_console.print(f"[red]Entity not found: {full_id}[/red]")
        raise typer.Exit(code=1)

    _print_entity_detail(entity)
    _print_relationships(rels)


def _print_relationships(rels: dict | None) -> None:
    if not rels or (not rels["outgoing"] and not rels["incoming"]):
        return

    def _summary(e: dict) -> str:
        p = e.get("payload") or {}
        return (
            p.get("deliverable") or p.get("topic")
            or p.get("question") or p.get("blocked_thing") or e["kind"]
        )

    console.print("\n[bold]Relationships:[/bold]")
    for item in rels["outgoing"]:
        e = item["entity"]
        console.print(
            f"  → [bold]{item['rel_type']}[/bold] "
            f"VRB-{e['id'][:8]} ({e['kind']}: {_summary(e)})"
        )
    for item in rels["incoming"]:
        e = item["entity"]
        console.print(
            f"  ← VRB-{e['id'][:8]} ({e['kind']}: {_summary(e)}) "
            f"[bold]{item['rel_type']}[/bold] this"
        )


@app.command(name="simplify")
def simplify_cmd(
    entity_id: str = typer.Argument(
        ..., help="Entity id (or prefix) to explain in plain language."
    ),
    audience: str | None = typer.Option(
        None, "--audience", "-a",
        help="Who it's for, e.g. 'a CFO', 'a new hire'. Default: general reader.",
    ),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Explain an entity in plain language — no jargon, acronyms expanded.

    For the non-technical reader (a CEO scanning commitments) and the
    technical one (an engineer reading a finance item). Same facts, plainer
    words.
    """
    conn = state.open_db(db)
    try:
        full_id = _resolve_id_prefix(conn, entity_id)
        if full_id is None:
            err_console.print(f"[red]No entity matches id prefix '{entity_id}'.[/red]")
            raise typer.Exit(code=1)
        try:
            with err_console.status("[dim]Simplifying…[/dim]"):
                result = simplify_lib.simplify_entity(
                    conn, full_id, audience=audience, model=model,
                )
        except Exception as e:  # noqa: BLE001
            err_console.print(f"[red]Simplify failed: {e}[/red]")
            raise typer.Exit(code=1) from None
    finally:
        conn.close()

    if result is None:
        err_console.print(f"[red]Entity not found: {full_id}[/red]")
        raise typer.Exit(code=1)

    console.print()
    console.print(result.text)
    spend = cost.estimate_cost(result.model, result.input_tokens, result.output_tokens)
    console.print(f"\n[dim]{result.model} · ${spend:.4f}[/dim]")


@app.command(name="audit")
def audit_cmd(
    entity_id: str = typer.Argument(..., help="Entity id (or prefix) to show history for."),
    limit: int = typer.Option(50, "--limit", "-n"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Show the audit trail for an entity — every confirm / dismiss / edit /
    reassign action, who did it, and when.
    """
    conn = state.open_db(db)
    try:
        full_id = _resolve_id_prefix(conn, entity_id)
        if full_id is None:
            err_console.print(f"[red]No entity matches id prefix '{entity_id}'.[/red]")
            raise typer.Exit(code=1)
        rows = store.fetch_audit(conn, full_id, limit=limit)
    finally:
        conn.close()

    if not rows:
        console.print(
            f"[dim]No recorded activity for `VRB-{full_id[:8]}`. "
            "Audit rows are written by Slack HITL actions and field edits.[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("action", no_wrap=True)
    table.add_column("by")
    table.add_column("note")
    for r in rows:
        table.add_row(
            (r["created_at"] or "")[:19].replace("T", " "),
            r["action"],
            r.get("actor_label") or r.get("actor_id") or "—",
            r.get("note") or "",
        )
    console.print(f"\n[bold]Activity for[/bold] [cyan]VRB-{full_id[:8]}[/cyan]")
    console.print(table)


@app.command(name="ask")
def ask_cmd(
    question: list[str] = typer.Argument(
        ..., help="The question, in plain English. Quote it or just type it.",
    ),
    simple: bool = typer.Option(
        False, "--simple", "-s",
        help="Answer in plain language — expand acronyms, drop jargon.",
    ),
    model: str | None = typer.Option(None, "--model", "-m"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Ask a natural-language question about your team's tracked state.

    Verbatim assembles the current open commitments, decisions, questions,
    and blockers, hands them to the LLM with your question, and answers —
    citing VRB-ids and verbatim quotes. It answers only from the state; if
    the answer isn't there, it says so. Pass --simple for a jargon-free
    answer anyone can follow.
    """
    question_text = " ".join(question).strip()
    if not question_text:
        err_console.print("[red]Ask a question, e.g. `verbatim ask \"what's overdue?\"`[/red]")
        raise typer.Exit(code=2)

    conn = state.open_db(db)
    try:
        with err_console.status("[dim]Thinking…[/dim]"):
            result = ask_lib.answer(
                conn, question_text, model=model, plain_language=simple,
            )
    except Exception as e:  # noqa: BLE001
        err_console.print(f"[red]Ask failed: {e}[/red]")
        raise typer.Exit(code=1) from None
    finally:
        conn.close()

    console.print()
    console.print(result.answer)
    spend = cost.estimate_cost(result.model, result.input_tokens, result.output_tokens)
    console.print(
        f"\n[dim]{result.entities_considered} items considered · "
        f"{result.model} · ${spend:.4f}[/dim]"
    )


def _reconcile_preview(
    conn,
    *,
    threshold: int,
    kinds: list[str] | None,
) -> list[tuple[dict, dict, int]]:
    """Return (entity, candidate, score) tuples that *would* be merged."""
    pairs: list[tuple[dict, dict, int]] = []
    target_kinds = kinds or ["commitment", "decision", "open_question", "blocker"]
    for k in target_kinds:
        rows = conn.execute(
            "SELECT id FROM entities WHERE kind = ? AND canonical_id IS NULL "
            "ORDER BY created_at ASC",
            (k,),
        ).fetchall()
        seen_canonicals: set[str] = set()
        for r in rows:
            entity = store.fetch_entity(conn, r["id"])
            if entity is None or entity["id"] in seen_canonicals:
                continue
            matches = reconcile_lib.find_candidates(conn, entity, threshold=threshold, limit=1)
            if matches:
                top = matches[0]
                # Don't preview pairs where the candidate already targets entity
                if top.candidate["id"] in seen_canonicals:
                    continue
                pairs.append((entity, top.candidate, top.score))
                # Pretend the merge happened so we don't double-suggest for this canonical
                seen_canonicals.add(top.candidate["id"])
    return pairs


def _print_reconcile_preview(pairs: list[tuple[dict, dict, int]], *, threshold: int) -> None:
    if not pairs:
        console.print(f"[dim]No merges would happen at threshold {threshold}.[/dim]")
        return
    table = Table(show_header=True, header_style="bold cyan", title=f"Reconcile plan (threshold {threshold})")
    table.add_column("score", justify="right")
    table.add_column("kind")
    table.add_column("would merge")
    table.add_column("into canonical")
    for entity, candidate, score in pairs:
        table.add_row(
            str(score),
            entity["kind"],
            f"{entity['id'][:8]}… {entity.get('primary_topic') or '—'}",
            f"{candidate['id'][:8]}… {candidate.get('primary_topic') or '—'}",
        )
    console.print(table)


def _print_entity_detail(entity: dict[str, Any]) -> None:
    badge = {
        "high": "[green]high[/green]",
        "medium": "[yellow]medium[/yellow]",
        "low": "[red]low[/red]",
    }.get(entity["confidence"], entity["confidence"])
    lines = [
        f"[bold]{entity['kind']}[/bold]  id={entity['id']}",
        f"confidence: {badge}  ·  status: {entity['status']}",
    ]
    if entity.get("canonical_id"):
        lines.append(f"[dim]merged into canonical: {entity['canonical_id']}[/dim]")
    if entity.get("merged_count", 0) > 0:
        n = entity["merged_count"]
        word = "source" if n == 1 else "sources"
        lines.append(f"[dim]merged with {n} other {word}[/dim]")
    payload = entity["payload"]
    if entity["kind"] == "commitment":
        lines.append(f"actor: {payload.get('actor') or '—'}")
        lines.append(f"deliverable: {payload.get('deliverable') or '—'}")
        if payload.get("deadline"):
            lines.append(f"deadline: {payload['deadline']}")
        if payload.get("to"):
            lines.append(f"to: {payload['to']}")
    elif entity["kind"] == "decision":
        lines.append(f"topic: {payload.get('topic') or '—'}")
        lines.append(f"outcome: {payload.get('outcome') or '—'}")
        if payload.get("rationale"):
            lines.append(f"rationale: {payload['rationale']}")
    elif entity["kind"] == "open_question":
        lines.append(f"topic: {payload.get('topic') or '—'}")
        lines.append(f"question: {payload.get('question') or '—'}")
        if payload.get("raised_by"):
            lines.append(f"raised_by: {payload['raised_by']}")
    elif entity["kind"] == "blocker":
        lines.append(f"blocked_thing: {payload.get('blocked_thing') or '—'}")
        lines.append(f"blocked_by: {payload.get('blocked_by') or '—'}")
    lines.append("")
    lines.append(f"[bold]Sources ({len(entity['sources'])}):[/bold]")
    for s in entity["sources"]:
        speaker = f"{s.get('speaker')}: " if s.get("speaker") else ""
        ts = f"[{s['approximate_timestamp']}] " if s.get("approximate_timestamp") else ""
        lines.append(f"  > {ts}{speaker}{s['verbatim_quote']}")
        if s.get("rationale"):
            lines.append(f"    [dim]rationale: {s['rationale']}[/dim]")
    console.print("\n".join(lines))


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
        merged = item.get("merged_count", 0)
        id_cell = item["id"][:8] + "…"
        if merged:
            id_cell += f" [dim](+{merged})[/dim]"
        row = [id_cell, conf_cell]
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
