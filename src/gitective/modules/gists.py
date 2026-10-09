"""gists - public gists of one or more users.

Gists are not indexed by /search/code (see 'search'), and people often paste
throwaway scripts and configs there with less caution than in repos.
"""

from rich.table import Table

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, iter_pages, make_session
from ..core.pivots import resolve_users
from ..core.targets import offer_to_save_pivots

NAME = "gists"
HELP = "List public gists for one or more users"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS gists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, gist_id TEXT, url TEXT, description TEXT, created_at TEXT, files TEXT
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")


def _fetch_gists(session, owner):
    """Return the public gists of a user as rows of the 'gists' table."""
    gists = []
    for gist in iter_pages(session, f"{API}/users/{owner}/gists"):
        gists.append({
            "owner": owner, "gist_id": gist["id"], "url": gist.get("html_url"),
            "description": gist.get("description"), "created_at": gist.get("created_at"),
            "files": ", ".join(gist.get("files", {}).keys()),
        })
    return gists


def _print_gists(gists):
    """Print the gists as a table with a link on each id."""
    table = Table(title=f"{len(gists)} gist(s)", header_style="bold magenta")
    table.add_column("Owner", style="ok")
    table.add_column("Gist", style="dim")
    table.add_column("Description", style="dim")
    table.add_column("Files")
    for gist in gists:
        link = f"[link={gist['url']}]{gist['gist_id']}[/link]" if gist["url"] else gist["gist_id"]
        table.add_row(gist["owner"], link, gist["description"] or "-", gist["files"])
    console.print(table)


def run(args, ctx):
    """List, save and print the public gists of each target user."""
    session = make_session(args.token)
    owners = resolve_users(args, ctx)

    gists = []
    try:
        with console.status("[info]Listing gists...[/info]") as status:
            for owner in owners:
                status.update(f"[info]Listing gists for {owner}[/info]")
                try:
                    gists.extend(_fetch_gists(session, owner))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping gists for {owner}: {exc}[/warn]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("gists", gists)

    console.print()
    if gists:
        _print_gists(gists)
    else:
        console.print("[warn]No public gists found.[/warn]")

    pivots = {
        "owner": sorted({gist["owner"] for gist in gists}),
        "gist": sorted({gist["gist_id"] for gist in gists}),
    }
    offer_to_save_pivots(args, ctx, NAME, pivots)
