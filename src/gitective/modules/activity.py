"""activity - when does an account commit? Committer UTC offsets, hour x weekday
heatmap and hourly bar chart (in local time when offsets are known, UTC
otherwise), and a fallback offset estimate.

Commits only: /users/{user}/events/public is capped at ~90 days, whereas the
commit history of a repo goes back to its first commit.

Where the dates come from:
- with a token: the GraphQL API. Its commit dates (GitTimestamp) keep the UTC
  offset recorded by git on the author's machine (e.g. +02:00), 100 commits
  per request. Stored in the 'utc_offset' column.
- without a token: the REST API, which converts every date to UTC, so the
  offset is unknown (GraphQL always needs a token).

The offset comes from the clock of the machine that made the commit: a raw
fact, which can be wrong (VM, CI set to UTC) or deliberately changed. A mix
of two offsets one hour apart (e.g. +01:00 / +02:00) usually means daylight
saving time, hence a region.

Fallback estimate, only when no real offset is known (no token, or every
offset is +00:00, which may be real UTC or a machine set to UTC): each
candidate offset is scored by how well the hourly activity matches a typical
day shape (quiet night, morning ramp-up, work plateau, evening taper). It is
a statistical guess, not a fact.

Targets (see resolve_repo_scope() in core/pivots.py):
- -u alone: every non-fork repo of the user, only their own commits;
- -r alone: those repos, every committer;
- -u and -r: those repos, only the user's commits.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

from rich.panel import Panel
from rich.table import Table

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, fetch_json_or_none, graphql, iter_pages, make_session
from ..core.pivots import resolve_repo_scope
from ..core.targets import offer_to_save_pivots

NAME = "activity"
HELP = "Commit times of users/repos: real UTC offsets (with a token), heatmaps, offset estimate"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, repo TEXT, event_time TEXT, utc_offset TEXT,
    UNIQUE(owner, repo, event_time)
);
"""

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Cells without activity use a neutral grey, so they read as "nothing"
# rather than "a little" (the lowest color of the palette).
NO_ACTIVITY_COLOR = "grey15"
ACTIVITY_COLORS = ["#FCE1B4", "#FABF7B", "#FBB146", "#FBAE24", "#F87509", "#F75B0E"]  # low -> high

# Relative activity of a typical person for each local hour (0h..23h).
TYPICAL_DAY_PROFILE = [
    0.05, 0.02, 0.02, 0.02, 0.03, 0.05, 0.15, 0.40,
    0.75, 0.95, 1.00, 1.00, 0.95, 0.90, 0.90, 0.95,
    1.00, 0.90, 0.70, 0.50, 0.35, 0.25, 0.15, 0.08,
]

BAR_BLOCKS = " ▁▂▃▄▅▆▇█"  # index 0 = empty, 1..8 = eighths of a full block
BAR_HEIGHT = 8  # rows of the bar chart

HISTORY_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $author: CommitAuthor) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor, author: $author) {
            pageInfo { hasNextPage endCursor }
            nodes { author { date } committer { date } }
          }
        }
      }
    }
  }
}
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")
    parser.add_argument("-o", "--org", help="Comma-separated GitHub org names (or @file, one per line)")
    parser.add_argument("-r", "--repository",
                        help="Comma-separated 'owner/repo' (or @file, one per line). Alone: "
                             "activity from anyone on these repos. Combined with -u: only these "
                             "repos, restricted to -u's commits")


# -- collecting commit times ----------------------------------------------------

def _parse_date(date: str) -> datetime:
    """Parse an ISO date from GitHub ('Z' or '+02:00' suffix) into an aware datetime."""
    return datetime.fromisoformat(date.replace("Z", "+00:00"))


def _commit_times_rest(session, owner, repo, author=None):
    """Return the commit dates of a repo from REST (converted to UTC by GitHub: offset unknown)."""
    params = {"per_page": 100}
    if author:
        params["author"] = author  # filtered by GitHub on the linked account
    times = []
    for commit in iter_pages(session, f"{API}/repos/{owner}/{repo}/commits", params=params):
        git_data = commit.get("commit", {})
        date = (git_data.get("author") or {}).get("date") or (git_data.get("committer") or {}).get("date")
        if date:
            times.append(_parse_date(date))
    return times


def _commit_times_graphql(session, owner, repo, author_node_id=None):
    """Return the commit dates of a repo from GraphQL, with the offset recorded by git."""
    times = []
    variables = {"owner": owner, "name": repo, "cursor": None,
                 "author": {"id": author_node_id} if author_node_id else None}
    while True:
        data = graphql(session, HISTORY_QUERY, variables)
        branch = (data.get("repository") or {}).get("defaultBranchRef")
        if not branch:
            return times  # empty repository: no default branch yet
        history = branch["target"]["history"]
        for node in history["nodes"]:
            date = (node.get("author") or {}).get("date") or (node.get("committer") or {}).get("date")
            if date:
                times.append(_parse_date(date))
        if not history["pageInfo"]["hasNextPage"]:
            return times
        variables["cursor"] = history["pageInfo"]["endCursor"]


def _author_node_id(session, login, cache):
    """Return the GraphQL node id of a user (needed to filter history by author), cached."""
    if login not in cache:
        account = fetch_json_or_none(session, f"{API}/users/{login}")
        cache[login] = account.get("node_id") if account else None
    return cache[login]


def format_offset(time: datetime) -> str:
    """Return the UTC offset of an aware datetime as '+02:00'."""
    total_minutes = int(time.utcoffset().total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _parse_offset(offset: str) -> timezone:
    """Turn '+02:00' into a timezone object."""
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def times_from_rows(rows):
    """Return (utc_times, local_times) from 'activity' rows; local only for rows with a known offset.

    Also used by 'report'.
    """
    utc_times, local_times = [], []
    for row in rows:
        if not row.get("event_time"):
            continue
        utc_time = _parse_date(row["event_time"]).astimezone(timezone.utc)
        utc_times.append(utc_time)
        if row.get("utc_offset"):
            local_times.append(utc_time.astimezone(_parse_offset(row["utc_offset"])))
    return utc_times, local_times


def offset_summary(local_times) -> list:
    """Return [(offset, commit count, first date, last date)], most used offset first; also used by 'report'."""
    by_offset = {}
    for time in local_times:
        by_offset.setdefault(format_offset(time), []).append(time)
    summary = []
    for offset, times in by_offset.items():
        summary.append((offset, len(times), min(times).date(), max(times).date()))
    summary.sort(key=lambda item: item[1], reverse=True)
    return summary


def offsets_are_informative(local_times) -> bool:
    """Tell whether real offsets are known and not all +00:00 (ambiguous: UTC or a machine set to UTC)."""
    return any(format_offset(time) != "+00:00" for time in local_times)


# -- analysis helpers (also used by 'report') -----------------------------------

def hour_weekday_grid(times):
    """Count commits per [weekday][hour], in the timezone the datetimes carry."""
    grid = [[0] * 24 for _ in range(7)]
    for time in times:
        grid[time.weekday()][time.hour] += 1
    return grid


def color_for(value, max_value):
    """Return the palette color for a count, relative to the maximum."""
    if max_value == 0 or value == 0:
        return NO_ACTIVITY_COLOR
    index = int(value / max_value * len(ACTIVITY_COLORS))
    return ACTIVITY_COLORS[min(index, len(ACTIVITY_COLORS) - 1)]


def estimate_utc_offsets(hour_counts: dict, total: int, top_n: int = 3):
    """Return ([(offset, % of best score)] for the top_n offsets, overall confidence %).

    Confidence grows when the best offset stands out from the others and
    when there are more commits (full weight from 100 commits).
    """
    scores = []
    for offset in range(-12, 15):
        score = 0
        for hour in range(24):
            score += hour_counts.get(hour, 0) * TYPICAL_DAY_PROFILE[(hour + offset) % 24]
        scores.append((offset, score))
    scores.sort(key=lambda item: item[1], reverse=True)

    best_score = scores[0][1]
    if not best_score:
        return [(offset, 0.0) for offset, _ in scores[:top_n]], 0.0
    mean_score = sum(score for _, score in scores) / len(scores)
    stands_out = (best_score - mean_score) / best_score
    enough_data = min(1.0, total / 100)
    confidence = 100 * stands_out * enough_data

    top = [(offset, score / best_score * 100) for offset, score in scores[:top_n]]
    return top, confidence


# -- console output -------------------------------------------------------------

def _offsets_table(local_times):
    """Build the table of real UTC offsets seen in the commits."""
    table = Table(title="Committer UTC offsets (recorded by git)", header_style="bold magenta")
    table.add_column("Offset", style="ok")
    table.add_column("Commits", justify="right")
    table.add_column("%", justify="right")
    table.add_column("First seen", style="dim")
    table.add_column("Last seen", style="dim")
    for offset, count, first, last in offset_summary(local_times):
        table.add_row(offset, str(count), f"{count / len(local_times) * 100:.0f}", str(first), str(last))
    return table


def _heatmap_table(grid, title):
    """Build an hour x weekday heatmap as a rich table."""
    max_value = max(max(row) for row in grid)
    table = Table(title=title, header_style="bold magenta", padding=(0, 0))
    table.add_column("", style="dim")
    for hour in range(24):
        table.add_column(f"{hour:02d}", justify="center")
    for day_index, day_name in enumerate(WEEKDAYS):
        cells = []
        for hour in range(24):
            count = grid[day_index][hour]
            symbol = "█" if count else "·"
            cells.append(f"[{color_for(count, max_value)}]{symbol}[/]")
        table.add_row(day_name, *cells)
    return table


def _bar_chart_table(hour_totals, title):
    """Build a vertical bar chart of commits per hour (all days), or None if there are none."""
    max_total = max(hour_totals) if hour_totals else 0
    if max_total == 0:
        return None

    table = Table(title=title, header_style="bold magenta", padding=(0, 0), show_lines=False)
    table.add_column("", style="dim")
    for hour in range(24):
        table.add_column(f"{hour:02d}", justify="center")

    # Bar height of each hour, in eighths of a row.
    heights = [round(total / max_total * BAR_HEIGHT * 8) for total in hour_totals]
    for row in range(BAR_HEIGHT, 0, -1):  # top row first
        cells = []
        for hour, height in enumerate(heights):
            full_rows, partial_eighths = divmod(height, 8)
            if row <= full_rows:
                block = BAR_BLOCKS[8]
            elif row == full_rows + 1 and partial_eighths:
                block = BAR_BLOCKS[partial_eighths]
            else:
                cells.append(" ")
                continue
            cells.append(f"[{color_for(hour_totals[hour], max_total)}]{block}[/]")
        table.add_row("", *cells)
    return table


def _print_heatmap_and_bars(times, label):
    """Print the heatmap and the hourly bar chart of a set of times ('UTC' or 'local time')."""
    grid = hour_weekday_grid(times)
    console.print()
    console.print(_heatmap_table(grid, f"Activity heatmap - {label} hour x weekday"))
    hour_totals = [sum(grid[day][hour] for day in range(7)) for hour in range(24)]
    bar_chart = _bar_chart_table(hour_totals, f"Hourly activity ({label}) - all days combined")
    if bar_chart:
        console.print()
        console.print(bar_chart)


def _print_estimate(utc_times):
    """Print the statistical UTC offset estimate (used when no real offset is known)."""
    top_offsets, confidence = estimate_utc_offsets(Counter(time.hour for time in utc_times), len(utc_times))
    lines = []
    for rank, (offset, percent_of_best) in enumerate(top_offsets):
        if rank == 0:
            lines.append(f"[bold]{offset:+d}[/bold]  (best fit)")
        else:
            lines.append(f"[bold]{offset:+d}[/bold]  ({percent_of_best:.0f}% as good a fit as the best match)")
    console.print()
    console.print(Panel("\n".join(lines) + f"\n\nOverall confidence: [bold]{confidence:.0f}%[/bold]",
                        title="Estimated UTC offset (no real offset known)", border_style="dim"))
    console.print("\n[dim]Estimate only: each offset is scored by how well the hourly activity matches "
                  "a typical quiet-night/work-day/quiet-evening shape. Run with a token to get the "
                  "real offsets recorded by git.[/dim]")


def _print_activity(utc_times, local_times):
    """Print the date span, real offsets, the heatmap and bars, and the estimate if needed."""
    console.print(f"[dim]{len(utc_times)} commit(s), spanning {min(utc_times).date()} to "
                  f"{max(utc_times).date()}[/dim]")

    if local_times:
        console.print()
        console.print(_offsets_table(local_times))
        console.print("[dim]Offsets come from the clock of the committing machine: a raw fact, "
                      "possibly wrong (VM, CI) or changed on purpose.[/dim]")

    # Local time when git's offsets are known (GraphQL, with a token), UTC otherwise.
    if local_times:
        _print_heatmap_and_bars(local_times, "local time")
    else:
        _print_heatmap_and_bars(utc_times, "UTC")

    if not offsets_are_informative(local_times):
        _print_estimate(utc_times)


# -- module entry point ------------------------------------------------------------

def run(args, ctx):
    """Collect the commit times of every target repo, then save and print the activity."""
    session = make_session(args.token)
    scope = resolve_repo_scope(args, ctx, session)
    use_graphql = bool(args.token)
    if not use_graphql:
        console.print("[dim]No token: using the REST API, which only gives UTC times "
                      "(real offsets need GraphQL, hence a token).[/dim]")

    events = []  # [{"owner", "repo", "time"}]; time keeps its real offset with GraphQL
    node_ids = {}
    try:
        with console.status(f"[info]Walking commits of {len(scope)} repo(s)...[/info]") as status:
            for owner, repo, author in scope:
                label = f"{owner}/{repo}" + (f" (author={author})" if author else "")
                status.update(f"[info]Walking commits of {label}[/info]")
                try:
                    if use_graphql:
                        author_id = _author_node_id(session, author, node_ids) if author else None
                        if author and not author_id:
                            continue  # unknown user: no commits to attribute
                        times = _commit_times_graphql(session, owner, repo, author_id)
                    else:
                        times = _commit_times_rest(session, owner, repo, author)
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping {label}: {exc}[/warn]")
                    continue
                for time in times:
                    # Stored under the author when filtering, so 'report' finds it per user.
                    events.append({"owner": author or owner, "repo": repo, "time": time})
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    rows = []
    for event in events:
        rows.append({"owner": event["owner"], "repo": event["repo"],
                     "event_time": event["time"].astimezone(timezone.utc).isoformat(),
                     "utc_offset": format_offset(event["time"]) if use_graphql else None})
    ctx.db.insert_many("activity", rows, or_ignore=True)
    # Commits already stored by a REST run get their offset now that it's known.
    ctx.db.execute_many(
        "UPDATE activity SET utc_offset = ? WHERE owner = ? AND repo = ? AND event_time = ? AND utc_offset IS NULL",
        [(row["utc_offset"], row["owner"], row["repo"], row["event_time"]) for row in rows if row["utc_offset"]])

    console.print()
    if rows:
        utc_times, local_times = times_from_rows(rows)
        _print_activity(utc_times, local_times)
    else:
        console.print("[warn]No commit activity found.[/warn]")

    pivots = {
        "owner": sorted({owner for owner, _repo, _author in scope}),
        "repo": sorted(f"{owner}/{repo}" for owner, repo, _author in scope),
    }
    offer_to_save_pivots(args, ctx, NAME, pivots)
