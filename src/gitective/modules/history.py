"""history - search a custom regex in the full history of repos and gists.

Every added/removed line of every commit (repos), or every file of every
revision (gists, which have no diffs - see core/git_history.py), is checked
against the regex. --scope (added/removed/both) only applies to repos.

Targets: -u/-o/-r/-g/-w, see resolve_repo_and_gist_scope() in core/pivots.py
(shared with 'secrets').
"""

import re
import sys

from rich.table import Table

from ..core.console import console
from ..core.git_history import content_pivots, finding_source, scan_all
from ..core.http import make_session
from ..core.pivots import add_repo_and_gist_arguments, resolve_repo_and_gist_scope
from ..core.targets import offer_to_save_pivots

NAME = "history"
HELP = "Regex search across a repo's (and optionally gists') full history (custom query)"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, repository TEXT, file TEXT, "commit" TEXT, url TEXT, match TEXT, line TEXT
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    add_repo_and_gist_arguments(parser)
    parser.add_argument("-q", "--query", required=True, help="Regex pattern to search for")
    parser.add_argument("--scope", choices=["added", "removed", "both"], default="added",
                        help="Repos only: search added lines (default), removed lines, or both")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="Case-insensitive match")


def _compile_query(args):
    """Compile the -q regex (case-insensitive with -i), exiting on an invalid pattern."""
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        return re.compile(args.query, flags)
    except re.error as exc:
        sys.exit(f"[!] Invalid regex: {exc}")


def _print_findings(findings, query):
    """Print the matches as a table."""
    table = Table(title=f"{len(findings)} match(es) for '{query}'", header_style="bold red")
    table.add_column("Source", style="ok")
    table.add_column("Commit/Rev", style="dim")
    table.add_column("File")
    table.add_column("Line", style="dim")
    for finding in findings:
        revision_link = f"[link={finding['url']}]{finding['sha'][:10]}[/link]"
        table.add_row(finding_source(finding), revision_link, finding["file"] or "-", finding["line"])
    console.print(table)


def run(args, ctx):
    """Scan the targets' history for the regex, then save and print the matches."""
    regex = _compile_query(args)

    def find_matches(text):
        """Yield the first regex match in the text, if any (no label for a custom query)."""
        match = regex.search(text)
        if match:
            yield "", match.group(0)

    session = make_session(args.token)
    repo_targets, gist_targets = resolve_repo_and_gist_scope(args, ctx, session)
    findings = scan_all(session, repo_targets, gist_targets, args.scope, find_matches,
                        action=f"Searching for '{args.query}' in")

    ctx.db.insert_many("history", [
        {"owner": f["owner"], "repository": f["repo"], "file": f["file"], "commit": f["sha"],
         "url": f["url"], "match": f["match"], "line": f["line"]}
        for f in findings
    ])

    console.print()
    if findings:
        _print_findings(findings, args.query)
    else:
        console.print("[warn]No match found.[/warn]")

    offer_to_save_pivots(args, ctx, NAME, content_pivots(repo_targets, gist_targets))
