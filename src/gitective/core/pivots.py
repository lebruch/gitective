"""Target resolution: turn -u/-o/-r/-g/-w/--from-targets into what to scan.

Pick the helper matching what your module scans:
- resolve_users(): accounts only, -u (profile, keys, gists);
- resolve_user_and_org_names(): accounts with their flag, -u/-o (repos);
- resolve_repo_scope(): repos, optionally restricted to one author's
  commits, -u/-o/-r (activity, workflows);
- resolve_repo_and_gist_scope(): repos and gists, -u/-o/-r/-g/-w
  (history, secrets).

The user/org guardrail (check_account_kind): whichever flag supplied a name,
GitHub's real account type decides which endpoint is used and whether an
author filter applies. An org never filters commits by author (orgs don't
author commits). A note is printed when the flag didn't match reality.

Every list value goes through parse_list_arg(), so '@file' works everywhere.
"""

import sys

from .console import console
from .http import SkipTarget, get_account_type, iter_gist_ids, iter_non_fork_repos
from .lists import parse_list_arg


def resolve_users(args, ctx) -> list:
    """Return the usernames given with -u, or the 'owner' targets with --from-targets."""
    if getattr(args, "from_targets", False):
        owners = ctx.db.target_values("owner")
        if not owners:
            sys.exit("[!] No 'owner' targets saved yet - see 'gitective db', or use --user instead")
        return owners

    if not getattr(args, "user", None):
        sys.exit("[!] This module needs --user or --from-targets")
    return parse_list_arg(args.user)


def resolve_user_and_org_names(args, ctx) -> list:
    """Return [(name, 'user'|'org')] from -u/-o, or from 'owner' targets (tagged 'user')."""
    if getattr(args, "from_targets", False):
        owners = ctx.db.target_values("owner")
        if not owners:
            sys.exit("[!] No 'owner' targets saved yet - see 'gitective db', or use --user/--org instead")
        # 'user' is just a neutral guess: check_account_kind() corrects it.
        return [(owner, "user") for owner in owners]

    names = _names_from_user_and_org_flags(args)
    if not names:
        sys.exit("[!] This module needs --user, --org, or --from-targets")
    return names


def _names_from_user_and_org_flags(args) -> list:
    """Return [(name, 'user')] for every -u value followed by [(name, 'org')] for every -o value."""
    names = []
    for user in parse_list_arg(getattr(args, "user", None)):
        names.append((user, "user"))
    for org in parse_list_arg(getattr(args, "org", None)):
        names.append((org, "org"))
    return names


def check_account_kind(session, name: str, expected: str) -> bool:
    """Return True if `name` is really an org, printing a note if the -u/-o flag was wrong."""
    real_type = get_account_type(session, name)
    is_org = real_type == "Organization"
    if expected == "user" and is_org:
        console.print(f"[dim]Note: {name} is an organization, not a user - using org repos.[/dim]")
    elif expected == "org" and real_type == "User":
        console.print(f"[dim]Note: {name} is a user, not an organization - using user repos.[/dim]")
    return is_org


def iter_owner_repos(session, name: str, expected: str):
    """Yield (repo_name, author) for each non-fork repo of an account; author is None for an org."""
    is_org = check_account_kind(session, name, expected)
    author = None if is_org else name
    for repo in iter_non_fork_repos(session, name, is_org):
        yield repo, author


def _iter_gist_ids_if_any(session, owner: str):
    """Yield a user's gist ids, or nothing for an account without gists (e.g. an org)."""
    try:
        yield from iter_gist_ids(session, owner)
    except SkipTarget:
        return


def _parse_owner_repo_list(raw: str) -> list:
    """Parse 'owner/repo' values into (owner, repo) tuples, exiting on a value without '/'."""
    pairs = []
    for entry in parse_list_arg(raw):
        if "/" not in entry:
            sys.exit(f"[!] --repository entries must be 'owner/repo': {entry}")
        owner, repo = entry.split("/", 1)
        pairs.append((owner, repo))
    return pairs


def resolve_repo_scope(args, ctx, session) -> list:
    """Return [(owner, repo, author_or_None)] from -u/-o/-r or --from-targets.

    - -u/-o alone: every non-fork repo of each account (author = the user,
      None for an org);
    - -r 'owner/repo': exactly those repos, never expanded; combined with -u,
      each -u becomes an author filter on them (-o never does).
    """
    if getattr(args, "from_targets", False):
        names = [(owner, "user") for owner in ctx.db.target_values("owner")]
        direct_repos = [tuple(entry.split("/", 1)) for entry in ctx.db.target_values("repo")]
    else:
        names = _names_from_user_and_org_flags(args)
        repo_arg = getattr(args, "repository", None)
        direct_repos = _parse_owner_repo_list(repo_arg) if repo_arg else []

    if not names and not direct_repos:
        sys.exit("[!] This module needs --user, --org, --repository, or --from-targets")

    scope = []
    if direct_repos:
        authors = [name for name, kind in names if kind == "user"]
        for owner, repo in direct_repos:
            if authors:
                for author in authors:
                    scope.append((owner, repo, author))
            else:
                scope.append((owner, repo, None))
        return scope

    for name, kind in names:
        for repo, author in iter_owner_repos(session, name, kind):
            scope.append((name, repo, author))
    return scope


def add_repo_and_gist_arguments(parser):
    """Declare the -u/-o/-r/-g/-w options read by resolve_repo_and_gist_scope()."""
    parser.add_argument("-u", "--user",
                        help="Comma-separated GitHub usernames (or @file, one per line)")
    parser.add_argument("-o", "--org",
                        help="Comma-separated GitHub org names (or @file, one per line) - never "
                             "used as an author filter (orgs don't author commits)")
    parser.add_argument("-r", "--repository", nargs="?", const=True, default=None,
                        help="Bare: include repos in scope. With a value: 'owner/repo', or a "
                             "bare name combined with -u for the owner.")
    parser.add_argument("-g", "--gist", nargs="?", const=True, default=None,
                        help="Bare: include gists in scope. With a value: that gist's id "
                             "(globally unique, -u not needed).")
    parser.add_argument("-w", "--workflow",
                        help="Restrict repo scanning to this workflow file (requires -r). "
                             "A bare name is prefixed with .github/workflows/.")


class _ContentScope:
    """Collects repo and gist targets for resolve_repo_and_gist_scope(), without duplicates."""

    def __init__(self):
        """Start with no targets."""
        self.repo_targets = []
        self.gist_targets = []
        self._seen_repos = set()
        self._seen_gists = set()

    def add_repo(self, owner, repo, author, path):
        """Add a repo target (path: only scan this file, or None)."""
        key = (owner, repo, author, path)
        if key not in self._seen_repos:
            self._seen_repos.add(key)
            self.repo_targets.append({"owner": owner, "repo": repo, "author": author, "path": path})

    def add_gist(self, gist_id, owner):
        """Add a gist target (owner may be None when only the id is known)."""
        if gist_id not in self._seen_gists:
            self._seen_gists.add(gist_id)
            self.gist_targets.append({"gist_id": gist_id, "owner": owner})


def resolve_repo_and_gist_scope(args, ctx, session):
    """Return (repo_targets, gist_targets) from -u/-o/-r/-g/-w or --from-targets.

    -r and -g work as checkboxes when given without a value (only that kind
    of content is scanned; neither given = both), or name exact targets
    when given a value:
    - -u/-o alone: every non-fork repo (user's own commits only) and every
      public gist of each account (orgs have no gists);
    - -r 'owner/repo': that repo; with -u, -u becomes an author filter;
    - -r 'name' (no slash): that repo of each -u/-o account;
    - -g 'id': that gist;
    - -w 'path': only scan that file in every repo target (needs -r); a bare
      file name gets '.github/workflows/' in front.

    --from-targets [letters]: 'r' repos, 'g' gists, 'w' workflows (any order,
    none = all). 'owner' targets are expanded like -u, direct 'repo'/'gist'/
    'workflow' targets are used as they are.

    repo_targets: [{"owner", "repo", "author", "path"}]
    gist_targets: [{"gist_id", "owner"}]
    """
    repo_arg = getattr(args, "repository", None)  # None = not given, True = given without value
    gist_arg = getattr(args, "gist", None)
    workflow_path = getattr(args, "workflow", None)
    if workflow_path and not repo_arg:
        sys.exit("[!] -w/--workflow requires -r/--repository (which repo is it in?)")
    if workflow_path and "/" not in workflow_path:
        workflow_path = f".github/workflows/{workflow_path}"

    scope = _ContentScope()
    from_targets = getattr(args, "from_targets", None)
    if from_targets:
        _add_from_targets(ctx, session, scope, from_targets.lower(), workflow_path)
        if not scope.repo_targets and not scope.gist_targets:
            sys.exit("[!] No matching targets saved yet - see 'gitective db'")
        return scope.repo_targets, scope.gist_targets

    names = _names_from_user_and_org_flags(args)
    if not names and repo_arg in (None, True) and gist_arg in (None, True):
        sys.exit("[!] This module needs --user, --org, --repository, --gist, or --from-targets")
    # Neither -r nor -g given: scan both kinds of content.
    want_repos = repo_arg is not None or gist_arg is None
    want_gists = gist_arg is not None or repo_arg is None

    if isinstance(repo_arg, str):
        _add_named_repos(scope, repo_arg, names, workflow_path)
    elif want_repos:
        if repo_arg and not names:
            sys.exit("[!] -r with no value needs -u/-o to know whose repos to expand")
        for name, kind in names:
            for repo, author in iter_owner_repos(session, name, kind):
                scope.add_repo(name, repo, author, workflow_path)

    if isinstance(gist_arg, str):
        for gist_id in parse_list_arg(gist_arg):
            scope.add_gist(gist_id, None)
    elif want_gists:
        if gist_arg and not names:
            sys.exit("[!] -g with no value needs -u/-o to know whose gists to list")
        for name, kind in names:
            if kind == "org":
                continue  # orgs don't have gists
            for gist_id in _iter_gist_ids_if_any(session, name):
                scope.add_gist(gist_id, name)

    return scope.repo_targets, scope.gist_targets


def _add_named_repos(scope, repo_arg: str, names: list, workflow_path):
    """Add the repos named by -r: 'owner/repo' as is, a bare name for each -u/-o account."""
    user_names = [name for name, kind in names if kind == "user"]
    for entry in parse_list_arg(repo_arg):
        if "/" in entry:
            owner, repo = entry.split("/", 1)
            for author in (user_names or [None]):
                scope.add_repo(owner, repo, author, workflow_path)
            continue
        if not names:
            sys.exit(f"[!] -r '{entry}' has no owner - combine with -u/-o, or use 'owner/repo'")
        for name, kind in names:
            author = name if kind == "user" else None
            scope.add_repo(name, entry, author, workflow_path)


def _add_from_targets(ctx, session, scope, letters: str, workflow_path):
    """Fill the scope from the targets pool, for the kinds selected by `letters` ('r', 'g', 'w')."""
    owners = ctx.db.target_values("owner")
    if "r" in letters:
        for name in owners:
            for repo, author in iter_owner_repos(session, name, "user"):
                scope.add_repo(name, repo, author, workflow_path)
        for entry in ctx.db.target_values("repo"):
            owner, repo = entry.split("/", 1)
            scope.add_repo(owner, repo, None, workflow_path)
    if "g" in letters:
        for name in owners:
            for gist_id in _iter_gist_ids_if_any(session, name):
                scope.add_gist(gist_id, name)
        for gist_id in ctx.db.target_values("gist"):
            scope.add_gist(gist_id, None)
    if "w" in letters:
        # Workflow targets carry their own repo: 'owner/repo:path'.
        for entry in ctx.db.target_values("workflow"):
            repo_part, path = entry.split(":", 1)
            owner, repo = repo_part.split("/", 1)
            scope.add_repo(owner, repo, None, path)
