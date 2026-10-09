"""emails - author/committer emails of the commits of non-fork repos.

Targets: -u/-o (every non-fork repo of these accounts; orgs are never used
as an author filter), -r ('owner/repo', or bare names combined with -u/-o),
or --from-targets.

gh_login/gh_id: the GitHub account the API links to the commit (the
top-level 'author'/'committer' fields, free in the same response). It ties
an email or author name to a stable account id.

No 'sha' column: the same email shows up on dozens of commits of a repo,
which says nothing more than "this email was seen on this repo". Rows are
deduplicated on (owner, repo, role, email) with a UNIQUE constraint and
INSERT OR IGNORE.
"""

import sys

from rich.table import Table

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, iter_pages, make_session
from ..core.lists import parse_list_arg
from ..core.pivots import iter_owner_repos
from ..core.targets import offer_to_save_pivots

NAME = "emails"
HELP = "Extract commit author/committer emails from non-fork repos"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, repo TEXT, role TEXT, name TEXT, email TEXT, gh_login TEXT, gh_id INTEGER,
    UNIQUE(owner, repo, role, email)
);
CREATE INDEX IF NOT EXISTS idx_emails_email ON emails(email);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")
    parser.add_argument("-o", "--org", help="Comma-separated GitHub org names (or @file, one per line)")
    parser.add_argument("-r", "--repository",
                        help="Comma-separated repos (or @file, one per line). 'owner/repo' alone, "
                             "or bare repo names combined with -u/-o")
    parser.add_argument("--repos-only", action="store_true",
                        help="With --from-targets: only scan the exact repos found, instead "
                             "of every repo owned by the owners found")


def commit_identities(commit, owner, repo):
    """Yield one entry per role (author, committer) of a commit that has an email.

    Shared with 'aliases'. Entry: {owner, repo, role, name, email, gh_login, gh_id}.
    """
    git_data = commit.get("commit", {})
    for role in ("author", "committer"):
        identity = git_data.get(role) or {}
        linked_account = commit.get(role) or {}  # GitHub account linked to this email, if any
        email = identity.get("email")
        if email:
            yield {"owner": owner, "repo": repo, "role": role,
                   "name": identity.get("name"), "email": email,
                   "gh_login": linked_account.get("login"), "gh_id": linked_account.get("id")}


def iter_repo_identities(session, owner, repo):
    """Yield the author/committer identities of every commit of a repo (shared with 'aliases')."""
    for commit in iter_pages(session, f"{API}/repos/{owner}/{repo}/commits", params={"per_page": 100}):
        yield from commit_identities(commit, owner, repo)


def _save(ctx, identities):
    """Save identities to the 'emails' table.

    Rows already known from an older run are skipped by INSERT OR IGNORE, so
    their linked account (gh_login/gh_id) is filled in afterwards if missing.
    """
    ctx.db.insert_many("emails", identities, or_ignore=True)
    updates = []
    for identity in identities:
        if identity["gh_id"]:
            updates.append((identity["gh_login"], identity["gh_id"], identity["owner"], identity["repo"],
                            identity["role"], identity["email"]))
    ctx.db.execute_many(
        "UPDATE emails SET gh_login = ?, gh_id = ? WHERE owner = ? AND repo = ? AND role = ? "
        "AND email = ? AND gh_id IS NULL", updates)


def _all_repos_of(session, names):
    """Return (owner, repo) for every non-fork repo of each (name, 'user'|'org')."""
    repos = []
    for name, kind in names:
        for repo, _author in iter_owner_repos(session, name, kind):
            repos.append((name, repo))
    return repos


def _resolve_repos(args, ctx, session):
    """Return the (owner, repo) pairs to scan, from -u/-o/-r or --from-targets."""
    if getattr(args, "from_targets", False):
        owners = ctx.db.target_values("owner")
        exact_repos = ctx.db.target_values("repo")
        if not owners and not exact_repos:
            sys.exit("[!] No 'owner'/'repo' targets saved yet - see 'gitective db'")
        if args.repos_only:
            if not exact_repos:
                sys.exit("[!] No 'repo' targets saved for --repos-only")
            return [tuple(entry.split("/", 1)) for entry in exact_repos]
        console.print(f"[info][*] {len(owners)} owner(s) from targets, "
                      f"expanding to their full repo list[/info]")
        return _all_repos_of(session, [(owner, "user") for owner in owners])

    names = [(user, "user") for user in parse_list_arg(args.user)]
    names += [(org, "org") for org in parse_list_arg(getattr(args, "org", None))]
    repo_names = parse_list_arg(args.repository)

    if names and repo_names:
        # Bare repo names: that repo of each -u/-o account.
        return [(name, repo) for name, _kind in names for repo in repo_names]
    if names:
        return _all_repos_of(session, names)
    if repo_names:
        repos = []
        for entry in repo_names:
            if "/" not in entry:
                sys.exit(f"[!] --repository entries must be 'owner/repo' when used without "
                         f"--user/--org: {entry}")
            repos.append(tuple(entry.split("/", 1)))
        return repos

    sys.exit("[!] emails module needs --user, --org, --repository, or --from-targets")


def _print_emails(identities):
    """Print each unique email with the repos it was seen in."""
    repos_by_email = {}
    for identity in identities:
        repos_by_email.setdefault(identity["email"], set()).add((identity["owner"], identity["repo"]))

    table = Table(title=f"{len(repos_by_email)} unique email(s)", header_style="bold magenta")
    table.add_column("Email", style="ok")
    table.add_column("Seen in repo(s)", style="dim")
    for email in sorted(repos_by_email):
        links = []
        for owner, repo in sorted(repos_by_email[email]):
            links.append(f"[link=https://github.com/{owner}/{repo}]{owner}/{repo}[/link]")
        table.add_row(email, "\n".join(links))
    console.print(table)


def run(args, ctx):
    """Collect the commit emails of every target repo, then save and print them."""
    session = make_session(args.token)
    repos = _resolve_repos(args, ctx, session)

    identities = []
    try:
        with console.status("[info]Scanning repos...[/info]") as status:
            for owner, repo in repos:
                status.update(f"[info]Scanning {owner}/{repo}[/info]")
                try:
                    identities.extend(iter_repo_identities(session, owner, repo))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping {owner}/{repo}: {exc}[/warn]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    _save(ctx, identities)

    console.print()
    if identities:
        _print_emails(identities)
    else:
        console.print("[warn]No emails found.[/warn]")

    pivots = {
        "owner": sorted({owner for owner, _repo in repos}),
        "repo": sorted(f"{owner}/{repo}" for owner, repo in repos),
    }
    offer_to_save_pivots(args, ctx, NAME, pivots)
