"""aliases - former usernames of one or more users, found in their commits.

GitHub exposes no rename history, but noreply commit emails freeze the login
used at commit time:
- noreply-id: 'ID+login@users.noreply.github.com' carries the account's
  numeric id (which never changes) and the login of that time;
- noreply-legacy: 'login@users.noreply.github.com' (format used before
  2017, no id) - kept only when GitHub linked that commit to the account.

For each -u user, every commit of their non-fork repos is walked (and, with
--extended, their commits across all of GitHub via search/commits, split by
year past the 1000-result cap). Only identities provably theirs are kept:
linked to their id, or a noreply email carrying their id. Any login found
there that differs from the current one is a former username, with the repos
it was seen in as evidence.

Limit: a commit linked to an account always shows its CURRENT login, so a
rename leaves no trace unless noreply emails were used before it.

Author names seen in the user's commits are printed as raw facts, never as
proof. --check looks up each former username: it may now belong to someone
else, which is why only current logins are offered as pivots, never aliases.
"""

import re
from datetime import datetime, timezone

from rich.table import Table

from ..core.console import console
from ..core.http import (API, SEARCH_PAGE_DELAY, RunInterrupted, SkipTarget, fetch_json_or_none,
                         iter_non_fork_repos, iter_pages, make_session, request_with_retry)
from ..core.pivots import resolve_users
from ..core.targets import offer_to_save_pivots
from .emails import commit_identities, iter_repo_identities

NAME = "aliases"
HELP = "Find former usernames of users in their commits (noreply emails frozen at commit time)"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    github_id INTEGER, current_login TEXT, alias TEXT, source TEXT, evidence TEXT,
    alias_status TEXT,
    UNIQUE(github_id, alias, source)
);
"""

NOREPLY_WITH_ID_RE = re.compile(r"^(\d+)\+([^@]+)@users\.noreply\.github\.com$", re.IGNORECASE)
NOREPLY_LEGACY_RE = re.compile(r"^([^+@]+)@users\.noreply\.github\.com$", re.IGNORECASE)
SEARCH_RESULT_CAP = 1000  # GitHub never returns more results for one search query


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")
    parser.add_argument("--extended", action="store_true",
                        help="Also search the user's commits across all of GitHub (search/commits)")
    parser.add_argument("--check", action="store_true",
                        help="Check whether each former username is free or now held by another account")


def _login_in_identity(identity, github_id):
    """Return (login, source) frozen in a commit identity of the account, or None.

    Only identities provably the account's count: a noreply email carrying its
    id, or a legacy noreply email on a commit GitHub linked to its id.
    """
    email = identity["email"]
    noreply_with_id = NOREPLY_WITH_ID_RE.match(email)
    if noreply_with_id:
        if int(noreply_with_id.group(1)) == github_id:
            return noreply_with_id.group(2), "noreply-id"
        return None
    noreply_legacy = NOREPLY_LEGACY_RE.match(email)
    if noreply_legacy and identity["gh_id"] == github_id:
        return noreply_legacy.group(1), "noreply-legacy"
    return None


def _belongs_to_account(identity, github_id) -> bool:
    """Tell whether a commit identity provably belongs to the account: linked id, or noreply with its id."""
    if identity["gh_id"] is not None:
        return identity["gh_id"] == github_id
    match = NOREPLY_WITH_ID_RE.match(identity["email"])
    return bool(match) and int(match.group(1)) == github_id


def _search_commit_identities(session, query, since_year):
    """Yield the identities of every commit matching a search/commits query.

    Past the 1000-result cap, the query is split into one query per year since `since_year`.
    """
    url = f"{API}/search/commits"
    total = request_with_retry(session, url, params={"q": query, "per_page": 1}).json().get("total_count", 0)
    if not total:
        return
    if total <= SEARCH_RESULT_CAP:
        queries = [query]
    else:
        queries = []
        for year in range(since_year, datetime.now(timezone.utc).year + 1):
            queries.append(f"{query} author-date:{year}-01-01..{year}-12-31")

    for yearly_query in queries:
        params = {"q": yearly_query, "per_page": 100}
        for item in iter_pages(session, url, params=params, items_key="items", delay=SEARCH_PAGE_DELAY):
            owner, repo = item["repository"]["full_name"].split("/", 1)
            yield from commit_identities(item, owner, repo)


def _user_identities(args, session, user, profile):
    """Return the commit identities provably the user's: their repos, plus GitHub-wide with --extended."""
    github_id = profile["id"]
    identities = []
    try:
        with console.status(f"[info]Walking {user}'s commits...[/info]") as status:
            for repo in iter_non_fork_repos(session, user, is_org=False):
                status.update(f"[info]Walking commits of {user}/{repo}[/info]")
                try:
                    for identity in iter_repo_identities(session, user, repo):
                        if _belongs_to_account(identity, github_id):
                            identities.append(identity)
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping {user}/{repo}: {exc}[/warn]")

            if args.extended:
                status.update(f"[info]Searching {user}'s commits across GitHub...[/info]")
                since_year = int((profile.get("created_at") or "2008")[:4])
                for identity in _search_commit_identities(session, f"author:{user}", since_year):
                    if _belongs_to_account(identity, github_id):
                        identities.append(identity)
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Using what was collected so far.[/warn]")
    return identities


def _former_logins(profile, identities) -> list:
    """Return one alias row per (former login, source), with the repos it was seen in as evidence."""
    current_login = profile["login"]
    found = {}  # (login lowercase, source) -> {"login", "repos": set}
    for identity in identities:
        login_and_source = _login_in_identity(identity, profile["id"])
        if not login_and_source:
            continue
        login, source = login_and_source
        if login.lower() == current_login.lower():
            continue  # that's the current login, not a former one
        key = (login.lower(), source)
        if key not in found:
            found[key] = {"login": login, "repos": set()}
        found[key]["repos"].add(f"{identity['owner']}/{identity['repo']}")

    rows = []
    for (_login, source), entry in sorted(found.items()):
        rows.append({"github_id": profile["id"], "current_login": current_login, "alias": entry["login"],
                     "source": source, "evidence": ", ".join(sorted(entry["repos"])), "alias_status": None})
    return rows


def _check_alias_status(session, rows):
    """Set alias_status on each row: 'free', 'same account', or who holds that login now."""
    status_by_alias = {}
    for row in rows:
        alias = row["alias"].lower()
        if alias not in status_by_alias:
            account = fetch_json_or_none(session, f"{API}/users/{row['alias']}")
            if not account:
                status_by_alias[alias] = "free"
            elif account["id"] == row["github_id"]:
                status_by_alias[alias] = "same account"
            else:
                status_by_alias[alias] = f"taken by {account['login']} (id {account['id']})"
        row["alias_status"] = status_by_alias[alias]


def _save_aliases(ctx, rows):
    """Insert new alias rows, and refresh current login, evidence and status of known ones."""
    ctx.db.insert_many("aliases", rows, or_ignore=True)
    ctx.db.execute_many(
        "UPDATE aliases SET current_login = ?, evidence = ? WHERE github_id = ? AND alias = ? AND source = ?",
        [(r["current_login"], r["evidence"], r["github_id"], r["alias"], r["source"]) for r in rows])
    ctx.db.execute_many(
        "UPDATE aliases SET alias_status = ? WHERE github_id = ? AND alias = ?",
        [(r["alias_status"], r["github_id"], r["alias"]) for r in rows if r["alias_status"]])


def _print_user_result(profile, rows, identities, check):
    """Print the former usernames of one user (or why none were found) and their author names."""
    console.print()
    if not rows:
        noreply_count = sum(1 for identity in identities if _login_in_identity(identity, profile["id"]))
        console.print(f"[warn]{profile['login']}: no former username found - {len(identities)} commit "
                      f"identity(ies) checked, {noreply_count} with a noreply email, all under the "
                      f"current login.[/warn]")
    else:
        table = Table(title=f"{profile['login']} (id {profile['id']}): {len(rows)} former username(s)",
                      header_style="bold magenta")
        columns = ["Alias", "Source", "Seen in"]
        if check:
            columns.append("Status")
        for column in columns:
            table.add_column(column)
        for row in rows:
            cells = [f"[ok]{row['alias']}[/ok]", row["source"], f"[dim]{row['evidence']}[/dim]"]
            if check:
                cells.append(row["alias_status"] or "-")
            table.add_row(*cells)
        console.print(table)

    author_names = sorted({identity["name"] for identity in identities if identity["name"]})
    if author_names:
        console.print(f"[dim]Author names seen in {profile['login']}'s commits: {', '.join(author_names)}[/dim]")


def run(args, ctx):
    """Walk each user's commits, then save and print their former usernames."""
    session = make_session(args.token)
    users = resolve_users(args, ctx)

    current_logins = []
    for user in users:
        profile = fetch_json_or_none(session, f"{API}/users/{user}")
        if not profile:
            console.print(f"[warn][!] Skipping {user}: no such account[/warn]")
            continue
        if profile.get("type") != "User":
            console.print(f"[warn][!] Skipping {user}: {profile.get('type')}, not a user[/warn]")
            continue

        identities = _user_identities(args, session, user, profile)
        rows = _former_logins(profile, identities)
        if args.check:
            _check_alias_status(session, rows)
        _save_aliases(ctx, rows)
        _print_user_result(profile, rows, identities, args.check)
        current_logins.append(profile["login"])

    offer_to_save_pivots(args, ctx, NAME, {"owner": sorted(set(current_logins))})
