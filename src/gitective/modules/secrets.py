"""secrets - search known secret patterns in the full history of repos and gists.

Same walk as 'history', but with ~33 built-in patterns at once
(core/secrets_patterns.py, from TruffleHog) instead of one custom regex.
--scope defaults to 'both': the point of scanning history is to find secrets
that were removed from the current code but still sit in an old commit.
--scope only applies to repos.

Targets: -u/-o/-r/-g/-w, see resolve_repo_and_gist_scope() in core/pivots.py
(shared with 'history').
"""

from rich.table import Table

from ..core.console import console
from ..core.git_history import content_pivots, finding_source, scan_all
from ..core.http import make_session
from ..core.pivots import add_repo_and_gist_arguments, resolve_repo_and_gist_scope
from ..core.secrets_patterns import find_secrets
from ..core.targets import offer_to_save_pivots

NAME = "secrets"
HELP = "Scan a repo's (and optionally gists') full history for known secret patterns (TruffleHog)"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, repository TEXT, file TEXT, "commit" TEXT, url TEXT, pattern TEXT, match TEXT, line TEXT
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    add_repo_and_gist_arguments(parser)
    parser.add_argument("--scope", choices=["added", "removed", "both"], default="both",
                        help="Repos only: scan added lines, removed lines, or both (default - "
                             "a secret rotated out of HEAD is still a finding)")


def _print_findings(findings):
    """Print the potential secrets as a table, with a reminder that patterns are broad."""
    table = Table(title=f"{len(findings)} potential secret(s) found", header_style="bold red")
    table.add_column("Source", style="ok")
    table.add_column("Pattern")
    table.add_column("Commit/Rev", style="dim")
    table.add_column("File")
    table.add_column("Match", style="dim")
    for finding in findings:
        revision_link = f"[link={finding['url']}]{finding['sha'][:10]}[/link]"
        table.add_row(finding_source(finding), finding["label"], revision_link,
                      finding["file"] or "-", finding["match"])
    console.print(table)
    console.print("\n[dim]Broad patterns, not proof - eyeball each match "
                  "(especially 'Generic API Key'/'Generic Secret').[/dim]")


def run(args, ctx):
    """Scan the targets' history for known secrets, then save and print them."""
    session = make_session(args.token)
    repo_targets, gist_targets = resolve_repo_and_gist_scope(args, ctx, session)
    findings = scan_all(session, repo_targets, gist_targets, args.scope, find_secrets,
                        action="Scanning for known secrets in")

    ctx.db.insert_many("secrets", [
        {"owner": f["owner"], "repository": f["repo"], "file": f["file"], "commit": f["sha"],
         "url": f["url"], "pattern": f["label"], "match": f["match"], "line": f["line"]}
        for f in findings
    ])

    console.print()
    if findings:
        _print_findings(findings)
    else:
        console.print("[warn]No known secret pattern matched.[/warn]")

    offer_to_save_pivots(args, ctx, NAME, content_pivots(repo_targets, gist_targets))
