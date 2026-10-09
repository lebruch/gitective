"""members - public members and outside collaborators of one or more organizations.

Always targets organizations (-o only). Each name is still checked with
GitHub, so a user account given by mistake is reported clearly instead of
failing with a bare 404.

Public members only: GET /orgs/{org}/members lists the members who made
their membership public. Anyone who kept it private is invisible without
admin rights on the org - never treat this as a complete member list.

Outside collaborators (repo access without being a member) usually need a
token with admin rights on the org: without them, that part is skipped with
a note instead of failing.
"""

import sys

from rich.table import Table

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, get_account_type, iter_pages, make_session
from ..core.lists import parse_list_arg
from ..core.targets import offer_to_save_pivots

NAME = "members"
HELP = "List an organization's public members and outside collaborators"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    org TEXT, login TEXT, profile_url TEXT, type TEXT, outside_collaborator INTEGER
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-o", "--org", help="Comma-separated GitHub org names (or @file, one per line)")


def _resolve_orgs(args, ctx, session) -> list:
    """Return the org names to list: -o values, or the 'owner' targets that are organizations."""
    if getattr(args, "from_targets", False):
        candidates = ctx.db.target_values("owner")
    elif getattr(args, "org", None):
        candidates = parse_list_arg(args.org)
    else:
        sys.exit("[!] This module needs --org or --from-targets")

    orgs = []
    for name in candidates:
        real_type = get_account_type(session, name)
        if real_type == "Organization":
            orgs.append(name)
        else:
            console.print(f"[warn][!] Skipping {name}: {real_type or 'not found'}, not an organization[/warn]")
    if not orgs and getattr(args, "from_targets", False):
        sys.exit("[!] No organization found among 'owner' targets - see 'gitective db', or use --org")
    return orgs


def _fetch_accounts(session, org, endpoint, is_outside_collaborator):
    """Return the accounts listed by /orgs/{org}/{endpoint} as rows of the 'members' table."""
    accounts = []
    for account in iter_pages(session, f"{API}/orgs/{org}/{endpoint}", params={"per_page": 100}):
        accounts.append({"org": org, "login": account["login"], "profile_url": account.get("html_url"),
                         "type": account.get("type"), "outside_collaborator": int(is_outside_collaborator)})
    return accounts


def _print_members(members):
    """Print the members and collaborators as a table, sorted by org then login."""
    table = Table(title=f"{len(members)} member(s)/collaborator(s)", header_style="bold magenta")
    table.add_column("Org", style="ok")
    table.add_column("Login")
    table.add_column("Type", style="dim")
    table.add_column("Outside collaborator?", style="dim")
    for member in sorted(members, key=lambda m: (m["org"], m["login"])):
        login = member["login"]
        if member["profile_url"]:
            login = f"[link={member['profile_url']}]{login}[/link]"
        table.add_row(member["org"], login, member["type"] or "-",
                      "yes" if member["outside_collaborator"] else "-")
    console.print(table)
    console.print("\n[dim]Public members only - anyone who set their org membership to private "
                  "is invisible here without admin rights on the org. Never treat this as a "
                  "complete member list.[/dim]")


def run(args, ctx):
    """List, save and print the public members and outside collaborators of each org."""
    session = make_session(args.token)
    orgs = _resolve_orgs(args, ctx, session)

    members = []
    try:
        with console.status("[info]Listing members...[/info]") as status:
            for org in orgs:
                status.update(f"[info]Listing members of {org}[/info]")
                try:
                    members.extend(_fetch_accounts(session, org, "members", False))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping members of {org}: {exc}[/warn]")

                status.update(f"[info]Listing outside collaborators of {org}[/info]")
                try:
                    members.extend(_fetch_accounts(session, org, "outside_collaborators", True))
                except SkipTarget as exc:
                    console.print(f"[dim]Note: couldn't list outside collaborators of {org} "
                                  f"(likely needs admin rights on the org): {exc}[/dim]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("members", members)

    console.print()
    if members:
        _print_members(members)
    else:
        console.print("[warn]No members found (or none are public).[/warn]")

    offer_to_save_pivots(args, ctx, NAME, {"owner": sorted({member["login"] for member in members})})
