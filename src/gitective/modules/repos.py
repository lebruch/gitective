"""repos - repositories of one or more accounts, with fork status and stats.

Unlike 'emails'/'activity' (which skip forks to focus on a person's own
work), this module shows fork status explicitly: forks are listed by
default, --no-forks hides them.

Commit stats (count, first and last commit date) cost 1-2 extra requests
per repo (--no-commit-stats skips them): asking for the commits one per
page, the first page is the latest commit, the Link header's last page
number is the total count, and that last page is the oldest commit.
"""

from urllib.parse import parse_qs, urlparse

from rich.table import Table

from ..core.console import console
from ..core.http import (API, RunInterrupted, SkipTarget, iter_pages, link_header_url, make_session,
                         request_with_retry)
from ..core.pivots import check_account_kind, resolve_user_and_org_names
from ..core.targets import offer_to_save_pivots

NAME = "repos"
HELP = "List a user's repos with fork status, stars, and commit stats"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, repository TEXT, url TEXT, is_fork INTEGER, description TEXT,
    language TEXT, stars INTEGER, fork_count INTEGER, open_issues INTEGER,
    size_kb INTEGER, archived INTEGER, default_branch TEXT, license TEXT,
    created_at TEXT, updated_at TEXT, pushed_at TEXT,
    commit_count INTEGER, first_commit_at TEXT, last_commit_at TEXT
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")
    parser.add_argument("-o", "--org", help="Comma-separated GitHub org names (or @file, one per line)")
    parser.add_argument("--no-forks", action="store_true", help="Exclude forks from the listing")
    parser.add_argument("--no-commit-stats", action="store_true",
                        help="Skip fetching commit count / first / last commit date "
                             "(saves 1-2 requests per repo)")


def _commit_date(commits_page):
    """Return the author date of the first commit in a page of commits (None if empty)."""
    if not commits_page:
        return None
    author = commits_page[0].get("commit", {}).get("author") or {}
    return author.get("date")


def _commit_stats(session, owner, repo):
    """Return (commit_count, first_commit_at, last_commit_at) for a repo; SkipTarget if it's empty."""
    url = f"{API}/repos/{owner}/{repo}/commits"
    first_page = request_with_retry(session, url, params={"per_page": 1})
    latest_commits = first_page.json()
    if not latest_commits:
        return 0, None, None
    latest_date = _commit_date(latest_commits)

    last_page_url = link_header_url(first_page, "last")
    if not last_page_url:
        return 1, latest_date, latest_date
    commit_count = int(parse_qs(urlparse(last_page_url).query).get("page", ["1"])[0])
    if commit_count <= 1:
        return 1, latest_date, latest_date

    oldest_commits = request_with_retry(session, url, params={"per_page": 1, "page": commit_count}).json()
    return commit_count, _commit_date(oldest_commits), latest_date


def _list_repos(session, owner, expected_kind, include_forks):
    """Return [{"owner", "repo": GitHub repo payload}] for every repo of an account."""
    is_org = check_account_kind(session, owner, expected_kind)
    if is_org:
        url = f"{API}/orgs/{owner}/repos"
    else:
        url = f"{API}/users/{owner}/repos"
    repos = []
    for repo in iter_pages(session, url, params={"per_page": 100}):
        if repo.get("fork") and not include_forks:
            continue
        repos.append({"owner": owner, "repo": repo})
    return repos


def _add_commit_stats(session, entries):
    """Add commit_count / first_commit_at / last_commit_at to each repo entry."""
    with console.status("[info]Fetching commit stats...[/info]") as status:
        for entry in entries:
            owner, name = entry["owner"], entry["repo"]["name"]
            status.update(f"[info]Commit stats for {owner}/{name}[/info]")
            try:
                count, first_at, last_at = _commit_stats(session, owner, name)
            except SkipTarget:
                count, first_at, last_at = 0, None, None
            entry["commit_count"] = count
            entry["first_commit_at"] = first_at
            entry["last_commit_at"] = last_at


def _to_row(entry):
    """Convert a repo entry into a row of the 'repos' table."""
    repo = entry["repo"]
    return {
        "owner": entry["owner"], "repository": repo["name"], "url": repo.get("html_url"),
        "is_fork": int(bool(repo.get("fork"))), "description": repo.get("description"),
        "language": repo.get("language"), "stars": repo.get("stargazers_count"),
        "fork_count": repo.get("forks_count"), "open_issues": repo.get("open_issues_count"),
        "size_kb": repo.get("size"), "archived": int(bool(repo.get("archived"))),
        "default_branch": repo.get("default_branch"), "license": (repo.get("license") or {}).get("name"),
        "created_at": repo.get("created_at"), "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "commit_count": entry.get("commit_count"), "first_commit_at": entry.get("first_commit_at"),
        "last_commit_at": entry.get("last_commit_at"),
    }


def _print_repos(entries):
    """Print the repos as a table, most starred first."""
    table = Table(title=f"{len(entries)} repo(s)", header_style="bold magenta")
    table.add_column("Owner/Repo", style="ok")
    table.add_column("Fork?")
    table.add_column("★", justify="right")
    table.add_column("Forks", justify="right")
    table.add_column("Lang")
    table.add_column("Commits", justify="right")
    table.add_column("First commit", style="dim")
    table.add_column("Last commit", style="dim")
    for entry in sorted(entries, key=lambda e: e["repo"].get("stargazers_count") or 0, reverse=True):
        repo = entry["repo"]
        commit_count = entry.get("commit_count")
        table.add_row(
            f"[link={repo.get('html_url')}]{entry['owner']}/{repo['name']}[/link]",
            "yes" if repo.get("fork") else "-",
            str(repo.get("stargazers_count") or 0),
            str(repo.get("forks_count") or 0),
            repo.get("language") or "-",
            str(commit_count) if commit_count is not None else "-",
            (entry.get("first_commit_at") or "-")[:10],
            (entry.get("last_commit_at") or "-")[:10],
        )
    console.print(table)


def run(args, ctx):
    """List the repos of each target account (with commit stats), save and print them."""
    session = make_session(args.token)
    names = resolve_user_and_org_names(args, ctx)

    entries = []
    try:
        with console.status("[info]Listing repos...[/info]") as status:
            for owner, expected_kind in names:
                status.update(f"[info]Listing repos for {owner}[/info]")
                entries.extend(_list_repos(session, owner, expected_kind, include_forks=not args.no_forks))
        if entries and not args.no_commit_stats:
            _add_commit_stats(session, entries)
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("repos", [_to_row(entry) for entry in entries])

    console.print()
    if entries:
        _print_repos(entries)
    else:
        console.print("[warn]No repos found.[/warn]")

    non_fork_repos = [f"{e['owner']}/{e['repo']['name']}" for e in entries if not e["repo"].get("fork")]
    pivots = {"owner": [name for name, _ in names], "repo": sorted(non_fork_repos)}
    offer_to_save_pivots(args, ctx, NAME, pivots)
