"""db - interactive SQL REPL on the gitective database.

Type any SQL statement and it runs as-is - SELECT results render as a
table, anything else reports how many rows were affected. No guardrails:
this is your database, do what you want with it (including DROP TABLE,
if that's really what you mean).

Three special commands on top of raw SQL:

    !show                 list every table with its row count
    !show <table>         equivalent to SELECT * FROM <table>
    !flush <table|all>    delete all rows from one table, or every table
    !export <table|all>   write one table (or every table) to <table>.csv
                           in the current directory

Ctrl-D, Ctrl-C, or typing 'exit'/'quit' leaves the REPL.
"""

import csv

from rich.prompt import Confirm
from rich.table import Table

from ..core.console import console

NAME = "db"
HELP = "Open an interactive SQL REPL against .gitective.db"


def add_arguments(parser):
    """Declare the module's command-line options (none: it's a REPL)."""


def run(args, ctx):
    """Read commands until exit: '!' commands are handled here, anything else runs as SQL."""
    console.print(f"[info]gitective db[/info] - {ctx.db.path}")
    console.print("Type SQL directly, or [bold]!show[/bold] / [bold]!flush[/bold] / "
                  "[bold]!export[/bold] <table|all>. Ctrl-D or 'exit' to leave.\n")
    while True:
        try:
            line = console.input("[bold]db>[/bold] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        if line.startswith("!show"):
            _show(line, ctx)
        elif line.startswith("!flush"):
            _flush(line, ctx)
        elif line.startswith("!export"):
            _export(line, ctx)
        else:
            _run_sql(line, ctx)


def _command_argument(line: str) -> str | None:
    """Return what follows a '!command', or None if nothing does."""
    parts = line.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip()


def _resolve_tables(name: str, ctx) -> list:
    """Return [name] if the table exists, every table for 'all', or [] (with a warning) otherwise."""
    known = sorted(ctx.db.table_names())
    if name == "all":
        return known
    if name not in known:
        console.print(f"[warn]Unknown table '{name}'. Known tables: {', '.join(known)}[/warn]")
        return []
    return [name]


def _show(line, ctx):
    """!show: list the tables, or !show <table>: print all its rows."""
    name = _command_argument(line)
    if name is None:
        _list_tables(ctx)
        return
    for table in _resolve_tables(name, ctx):
        _run_sql(f"SELECT * FROM {table}", ctx)


def _list_tables(ctx):
    """Print every table with its row count."""
    table = Table(title="Tables in .gitective.db", header_style="bold magenta")
    table.add_column("Table", style="ok")
    table.add_column("Rows", justify="right")
    for name in sorted(ctx.db.table_names()):
        count = ctx.db.query(f"SELECT COUNT(*) AS row_count FROM {name}")[0]["row_count"]
        table.add_row(name, str(count))
    console.print(table)


def _flush(line, ctx):
    """!flush <table|all>: delete all rows of a table (or every table) after confirmation."""
    name = _command_argument(line)
    if name is None:
        console.print("[warn]Usage: !flush <table|all>[/warn]")
        return
    tables = _resolve_tables(name, ctx)
    if not tables:
        return
    label = "ALL tables" if name == "all" else f"table '{tables[0]}'"
    if not Confirm.ask(f"Flush {label}? This cannot be undone.", default=False):
        console.print("[err]Aborted.[/err]")
        return
    for table in tables:
        ctx.db.clear_table(table)
    console.print(f"[ok]Flushed {label}.[/ok]")


def _export(line, ctx):
    """!export <table|all>: write each table to <table>.csv in the current folder."""
    name = _command_argument(line)
    if name is None:
        console.print("[warn]Usage: !export <table|all>[/warn]")
        return
    for table in _resolve_tables(name, ctx):
        rows = [dict(row) for row in ctx.db.query(f"SELECT * FROM {table}")]
        if not rows:
            console.print(f"[dim]'{table}' is empty, skipping.[/dim]")
            continue
        path = f"{table}.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"[ok]Exported {len(rows)} row(s) from '{table}' to {path}[/ok]")


def _run_sql(sql, ctx):
    """Run any SQL: print the rows of a SELECT, or how many rows a change affected."""
    try:
        cursor = ctx.db.conn.execute(sql)
    except Exception as exc:
        console.print(f"[err][!] {exc}[/err]")
        return

    if not cursor.description:  # not a SELECT: commit the change
        ctx.db.conn.commit()
        console.print(f"[ok]OK[/ok] ({max(cursor.rowcount, 0)} row(s) affected)")
        return

    rows = cursor.fetchall()
    if not rows:
        console.print("[dim](0 rows)[/dim]")
        return
    table = Table(header_style="bold magenta")
    for column in cursor.description:
        table.add_column(column[0])
    for row in rows:
        table.add_row(*[str(value) if value is not None else "-" for value in row])
    console.print(table)
