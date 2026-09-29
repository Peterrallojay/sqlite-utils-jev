"""The sqlite-utils command adapter; the runner itself uses the standard library."""

from functools import wraps
import json
from pathlib import Path
import sqlite3

import click
import sqlite_utils

from .client import MODEL, PRICE, JevError, allow_retry, set_budget, status
from .pipeline import classify_table
from .validation import response_json


def errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (JevError, OSError, ValueError, sqlite3.Error) as exc:
            raise click.ClickException(str(exc)) from None
    return wrapped


@click.group()
def jev():
    """Classify SQLite text with Jev; save decisions and spending locally."""


@jev.command("classify")
@click.argument("database", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("table")
@click.option("--key", required=True, help="Unique, non-null integer or text column.")
@click.option("--text", "text_columns", multiple=True, required=True, help="Text column to send; repeat for multiple columns.")
@click.option("--question", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="JSON containing one Choice question.")
@click.option("--questions", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="JSON mapping names to Choice questions.")
@click.option("--workers", type=click.IntRange(1, 16), default=8, show_default=True, help="Maximum concurrent requests; each contains one record.")
@click.option("--quiet", is_flag=True, help="Suppress progress on stderr.")
@click.option("--state", type=click.Path(dir_okay=False, path_type=Path), required=True, help="Separate SQLite journal and results file.")
@click.option("--budget-usd", help="Initial total allowance, shared by all runs using this state file.")
@click.option("--model", default=MODEL, show_default=True)
@click.option("--input-price", default=PRICE, show_default=True, help="USD per million input tokens; used for estimates/accounting.")
@click.option("--min-probability", type=click.FloatRange(0, 1), default=0.75, show_default=True)
@click.option("--min-confidence", type=click.FloatRange(0, 1), default=0.60, show_default=True)
@click.option("--limit", type=click.IntRange(min=1), help="Process the first N rows ordered by key.")
@click.option("--offline", is_flag=True, help="Reuse saved answers only; fail on a cache miss.")
@errors
def classify(database, table, question, questions, text_columns, quiet, **kwargs):
    """Classify a table or view. Repeat the command to resume."""
    def progress(update):
        click.echo(f"{update['rows']}/{update['total']} rows | {update['cached_rows']} cached rows | "
                   f"{update['requests']} requests | ${update['accounted_usd']:.6f} accounted "
                   f"(includes reservations) | {update['remaining']} remaining", err=True)

    if (question is None) == (questions is None):
        raise JevError("Supply exactly one of --question or --questions")
    selected = {"question" if question is not None else "questions":
                response_json((question or questions).read_text(encoding="utf-8"))}
    result = classify_table(database, table, text_columns=list(text_columns),
                            progress=None if quiet else progress, **selected, **kwargs)
    click.echo(json.dumps(result, indent=2))


@jev.command("status")
@click.argument("state", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@errors
def show_status(state):
    """Show spending and blocked requests without making API calls."""
    click.echo(json.dumps(status(state), indent=2))


@jev.command("budget")
@click.argument("state", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--usd", required=True, help="Total allowance, including all earlier costs/reservations.")
@errors
def budget(state, usd):
    """Explicitly set or change the persistent total allowance."""
    set_budget(state, usd)
    click.echo(json.dumps(status(state), indent=2))


@jev.command("retry")
@click.argument("state", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("request_hash")
@errors
def retry(state, request_hash):
    """Authorize one retry; earlier charges/reservations remain. May charge again."""
    allow_retry(state, request_hash)
    click.echo("One retry authorized. Earlier costs remain. Repeat classify to dispatch it.")


@sqlite_utils.hookimpl
def register_commands(cli):
    cli.add_command(jev)
