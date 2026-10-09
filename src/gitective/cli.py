#!/usr/bin/env python3
"""gitective - GitHub OSINT recon framework, à la nxc.

Usage:
    gitective <module> [options]

Chaining:
    Every module has its own table in the SQLite database, appended to run
    after run. Once results are displayed, you are asked whether to add the
    pivots found to the "targets" pool: (O)ui adds them all, (E)diter lists
    them numbered so you can drop some first, (N)on adds nothing.

        gitective search -q "AKIA"
        gitective emails --from-targets
            -> reuses the owners/repos saved in the pool.

Exploring the database (see 'gitective db -h'):
    gitective db
        -> SQL REPL on the database (one table per module, plus "targets").
           Type SQL, or !show / !flush / !export <table|all>.

Adding a module:
    Drop a file in gitective/modules/ defining NAME, HELP, add_arguments()
    and run(args, ctx) - it is discovered automatically. ctx.db is the
    Database.
"""

import argparse
import os
import sys

import requests

from . import modules as modules_pkg
from .core.console import console
from .core.db import Database
from .core.http import NetworkStopped


class Context:
    """Shared state handed to every module's run(args, ctx): just the database for now."""

    def __init__(self, db: Database):
        """Keep a reference to the database."""
        self.db = db


def build_parser(modules: dict) -> argparse.ArgumentParser:
    """Build the argument parser: one subcommand per module, plus the common options."""
    parser = argparse.ArgumentParser(
        prog="gitective",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="module")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-t", "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token (or set GITHUB_TOKEN env var)",
    )
    common.add_argument(
        "--from-targets", nargs="?", const="rgw", default=None, metavar="KINDS",
        help="Reuse owners/repos/etc from the curated target pool - see 'gitective db' "
             "(!show targets). Modules that scan both repos and gists (history, secrets) "
             "accept an optional letter suffix to restrict which kinds, e.g. 'rg' for "
             "repos+gists only; no suffix means everything. Ignored by other modules.",
    )

    for name, module in sorted(modules.items()):
        subparser = subparsers.add_parser(name, parents=[common], help=module.HELP)
        module.add_arguments(subparser)

    return parser


def print_module_list(modules: dict):
    """Print a table of the available modules and their one-line help."""
    from rich.table import Table
    table = Table(title="Available modules", header_style="bold magenta")
    table.add_column("Module", style="ok")
    table.add_column("Description")
    for name, module in sorted(modules.items()):
        table.add_row(name, module.HELP)
    console.print(table)
    console.print("[dim]Run 'gitective <module> -h' for module-specific options.[/dim]")


def _main():
    """Parse the command line, prepare the database, and run the chosen module."""
    modules = modules_pkg.discover()
    parser = build_parser(modules)
    args = parser.parse_args()

    if not args.module:
        parser.print_help()
        console.print()
        print_module_list(modules)
        sys.exit(1)

    db = Database()
    for module in modules.values():
        schema = getattr(module, "RESULT_SCHEMA", None)
        if schema:
            db.ensure_schema(schema)
    ctx = Context(db)

    if args.module != "db" and not args.token:
        console.print("[warn][!] Warning: no token set, you'll hit low unauthenticated rate limits "
                      "(search API in particular requires auth for decent limits)[/warn]")

    modules[args.module].run(args, ctx)


def main():
    """Console-script entry point: run _main() and turn known errors into clean exits."""
    try:
        _main()
    except KeyboardInterrupt:
        console.print("\n[warn]Interrupted.[/warn]")
        sys.exit(130)
    except requests.HTTPError as exc:
        console.print(f"[err][!] GitHub API error: {exc}[/err]")
        sys.exit(1)
    except NetworkStopped as exc:
        console.print(f"[err][!] Network error before anything could be collected: {exc}[/err]")
        sys.exit(1)


if __name__ == "__main__":
    main()
