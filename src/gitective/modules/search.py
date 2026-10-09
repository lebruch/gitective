"""search - search a string in the code indexed by GitHub.

GET /search/code is GitHub's legacy code search, not the engine behind
github.com/search. It only searches the default branch, ignores files over
384 KB, returns at most 1000 results, and never searches history (see
'history').

-u/-r/-l/-p are FILTERS added to the query, not a list of repos to walk:
  -u user       -> qualifier `user:user`
  -r owner/repo -> qualifier `repo:owner/repo`
  -l language   -> qualifier `language:x`
  -p path       -> qualifier `path:x` (a folder PREFIX, not a glob:
                   path:*.go returns nothing on this endpoint)
-r wins over -u when both are given. With several -u/-r values, one query is
sent per value (OR between qualifiers is unreliable on this endpoint).
-l/-p take one value, applied to every query.

--context decides what is fetched on top of the repo/file list:
  (absent)     -> just the list, no content request
  --context    -> + the exact matching line (1 request per match)
  --context N  -> + N lines before/after (stored in 'context')

/search/code has a much stricter limit (~30 requests/min) than the rest of
the API, so pages are fetched with a delay (SEARCH_PAGE_DELAY).
"""

import base64

import requests
from rich.panel import Panel
from rich.table import Table

from ..core.console import console
from ..core.http import (API, SEARCH_PAGE_DELAY, RunInterrupted, fetch_json_or_none, iter_pages,
                         make_session)
from ..core.lists import parse_list_arg
from ..core.targets import offer_to_save_pivots

NAME = "search"
HELP = "Search a string across GitHub's indexed code (legacy /search/code)"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS search (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, repository TEXT, file TEXT, url TEXT, match TEXT, line TEXT, context TEXT
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-q", "--query", required=True, help="Code search query")
    parser.add_argument("-u", "--user",
                        help="Comma-separated GitHub usernames (or @file, one per line) - filters "
                             "to their repos via the 'user:' qualifier. Ignored if -r is also given.")
    parser.add_argument("-r", "--repository",
                        help="Comma-separated 'owner/repo' (or @file, one per line) - filters via "
                             "the 'repo:' qualifier. Wins over -u if both are given.")
    parser.add_argument("-l", "--language", help="Filter by language (single value, e.g. python)")
    parser.add_argument("-p", "--path",
                        help="Filter by path (single value). On GitHub this is a directory PREFIX "
                             "match, not a glob - 'src' works, 'src/*.py' does not.")
    parser.add_argument("--context", nargs="?", const=0, type=int, default=None, metavar="N",
                        help="Without this flag: just list matching repos/files. With --context "
                             "alone: also fetch the matched line. With --context N: also fetch N "
                             "lines before/after (stored in 'context').")


def _build_queries(args, ctx) -> list:
    """Return one query string per -r (or else -u) value, each with the -l/-p qualifiers."""
    if getattr(args, "from_targets", False):
        repos = ctx.db.target_values("repo")
        users = ctx.db.target_values("owner")
    else:
        repos = parse_list_arg(args.repository)
        users = parse_list_arg(args.user)

    if repos:
        scope_qualifiers = [f"repo:{repo}" for repo in repos]
        if users and not getattr(args, "from_targets", False):
            console.print("[dim]Note: -r wins over -u when both are given (a full 'owner/repo' "
                          "already implies the owner).[/dim]")
    elif users:
        scope_qualifiers = [f"user:{user}" for user in users]
    else:
        scope_qualifiers = [""]  # one global query

    extra_qualifiers = []
    if args.language:
        extra_qualifiers.append(f"language:{args.language}")
    if args.path:
        extra_qualifiers.append(f"path:{args.path}")

    queries = []
    for scope_qualifier in scope_qualifiers:
        parts = [args.query, scope_qualifier] + extra_qualifiers
        queries.append(" ".join(part for part in parts if part))
    return queries


def _search_code(session, query):
    """Yield {owner, repo, path, html_url} for each file matching a /search/code query."""
    url = f"{API}/search/code"
    params = {"q": query, "per_page": 100}
    for item in iter_pages(session, url, params=params, items_key="items", delay=SEARCH_PAGE_DELAY):
        full_name = item.get("repository", {}).get("full_name", "")
        owner, _, repo = full_name.partition("/")
        yield {"owner": owner, "repo": repo, "path": item.get("path"), "html_url": item.get("html_url")}


def _download_text(session, owner, repo, path):
    """Return the text content of a file in a repo, or None if it can't be read."""
    data = fetch_json_or_none(session, f"{API}/repos/{owner}/{repo}/contents/{path}")
    if not data or data.get("encoding") != "base64" or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except ValueError:
        return None


def _find_in_text(text, query, lines_around):
    """Return (matched_text, line, context_block) for the first line containing `query`.

    lines_around: also return that many numbered lines before/after (0 = no context).
    Returns (None, None, None) when the query isn't found.
    """
    lines = text.splitlines()
    needle = query.lower()
    for index, line in enumerate(lines):
        position = line.lower().find(needle)
        if position == -1:
            continue
        context_block = None
        if lines_around:
            start = max(0, index - lines_around)
            end = min(len(lines), index + lines_around + 1)
            context_block = "\n".join(f"{number + 1}: {lines[number]}" for number in range(start, end))
        return line[position:position + len(query)], line, context_block
    return None, None, None


def _unique_by_file(matches):
    """Drop matches already seen for the same (owner, repo, path), e.g. found by two -u values."""
    unique = []
    seen = set()
    for match in matches:
        key = (match["owner"], match["repo"], match["path"])
        if key not in seen:
            seen.add(key)
            unique.append(match)
    return unique


def _print_matches(matches, repos, args):
    """Print the matches table, then each context block if --context N was given."""
    show_line = args.context is not None
    table = Table(title=f"{len(matches)} match(es) in {len(repos)} repo(s) for '{args.query}'",
                  header_style="bold magenta")
    table.add_column("Owner/Repo", style="ok")
    table.add_column("File", style="dim")
    if show_line:
        table.add_column("Line", style="dim")
    for match in matches:
        file_cell = match["path"]
        if match["html_url"]:
            file_cell = f"[link={match['html_url']}]{match['path']}[/link]"
        row = [f"{match['owner']}/{match['repo']}", file_cell]
        if show_line:
            row.append(match.get("line") or "-")
        table.add_row(*row)
    console.print(table)

    for match in matches:
        if match.get("context"):
            title = f"[link={match['html_url']}]{match['owner']}/{match['repo']}/{match['path']}[/link]"
            console.print()
            console.print(Panel(match["context"], title=title, title_align="left", border_style="dim"))


def run(args, ctx):
    """Run the code search(es), optionally fetch the matched lines, then save and print."""
    session = make_session(args.token)
    queries = _build_queries(args, ctx)

    matches = []
    try:
        with console.status(f"[info]Searching '{args.query}'...[/info]"):
            for query in queries:
                try:
                    matches.extend(_search_code(session, query))
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 422:
                        raise
                    console.print(f"[warn][!] Search stopped for '{query}' "
                                  f"(query too broad, or not indexed)[/warn]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")
    matches = _unique_by_file(matches)

    if matches and args.context is not None:
        try:
            with console.status("[info]Fetching matched lines...[/info]"):
                for match in matches:
                    text = _download_text(session, match["owner"], match["repo"], match["path"])
                    if text:
                        match["match"], match["line"], match["context"] = _find_in_text(
                            text, args.query, args.context)
        except RunInterrupted as exc:
            console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("search", [
        {"owner": m["owner"], "repository": m["repo"], "file": m["path"], "url": m["html_url"],
         "match": m.get("match"), "line": m.get("line"), "context": m.get("context")}
        for m in matches
    ])

    repos = sorted({(match["owner"], match["repo"]) for match in matches})
    console.print()
    if matches:
        _print_matches(matches, repos, args)
        console.print("\n[info]Tip:[/info] chain into another module with "
                      "[bold]gitective emails --from-targets[/bold]")
    else:
        console.print("[warn]No matches found.[/warn]")

    pivots = {"owner": sorted({owner for owner, _repo in repos}),
              "repo": [f"{owner}/{repo}" for owner, repo in repos]}
    offer_to_save_pivots(args, ctx, NAME, pivots)
