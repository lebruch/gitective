"""report - run profile, keys, gists, emails, repos and activity for
one or more users, then build one self-contained HTML file per user.

Each sub-module runs exactly as when called alone (same output, same table
writes) with a hand-built arguments object; report only orchestrates, then
reads back every row stored for that user. Lookups are case-insensitive
(GitHub's canonical case may differ from what was typed) and have no date
limit: deduplicated tables ('emails', 'activity') keep the first found_at
of a row, so a date limit would hide rows found again today.

The sub-modules' targets prompts are skipped (args.skip_targets_prompt):
their pivots are added silently instead of asking six questions in a row.

HTML rather than PDF: colored cells and CSS bars are simpler and more
faithful than drawing charts in a PDF.
"""

import base64
import html
import sys
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import requests

from ..core.console import console
from ..core.lists import parse_list_arg
from . import activity, emails, gists, keys, profile, repos

NAME = "report"
HELP = "Run profile/keys/gists/emails/repos/activity for one user and build an HTML summary"

# Sub-modules run in this order for each user.
SUB_MODULES = [profile, keys, gists, emails, repos, activity]


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user",
                        help="Comma-separated GitHub usernames (or @file, one per line) - "
                             "one HTML file per user")
    parser.add_argument("--output",
                        help="Output HTML path (default: <user>_report.html in the current "
                             "directory); ignored when reporting on more than one user")


def _sub_module_args(token, user):
    """Build the arguments object the sub-modules expect, targeting one user, with default options."""
    return SimpleNamespace(
        token=token, from_targets=False, skip_targets_prompt=True,
        user=user, repository=None, repos_only=False,
        no_forks=False, no_commit_stats=False,
    )


def _avatar_data_uri(url):
    """Download the avatar and return it as a data: URI (so the HTML has no external file), or None."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    content_type = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
    return f"data:{content_type};base64,{base64.b64encode(resp.content).decode('ascii')}"


def _escape(value) -> str:
    """HTML-escape a value; None and '' become ''."""
    if value in (None, ""):
        return ""
    return html.escape(str(value))


def _key_value_rows(fields) -> str:
    """Build the <tr> rows of a label/value table, skipping empty values."""
    rows = []
    for label, value in fields:
        if value in (None, "", "None"):
            continue
        rows.append(f"<tr><th>{_escape(label)}</th><td>{_escape(value)}</td></tr>")
    return "\n".join(rows) or "<tr><td class='empty'>No data.</td></tr>"


def _table_html(headers, rows) -> str:
    """Build an HTML table (empty values shown as '-'), or a 'No data found.' line."""
    if not rows:
        return "<p class='empty'>No data found.</p>"
    head = "".join(f"<th>{_escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        cells = ""
        for value in row:
            cells += f"<td>{_escape(value) if value not in (None, '') else '-'}</td>"
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _heatmap_html(times) -> str:
    """Build the hour x weekday heatmap as an HTML table of colored cells."""
    grid = activity.hour_weekday_grid(times)
    max_value = max(max(row) for row in grid)
    header = "<th></th>" + "".join(f"<th>{hour:02d}</th>" for hour in range(24))
    rows = []
    for day_index, day_name in enumerate(activity.WEEKDAYS):
        cells = []
        for hour in range(24):
            count = grid[day_index][hour]
            style = f"background:{activity.color_for(count, max_value)};" if count else ""
            title = f"{day_name} {hour:02d}:00 - {count} commit(s)"
            cells.append(f"<td class='cell' style='{style}' title='{title}'></td>")
        rows.append(f"<tr><th>{day_name}</th>{''.join(cells)}</tr>")
    return (f"<table class='heatmap'><thead><tr>{header}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _bar_chart_html(times) -> str:
    """Build the commits-per-hour bar chart as CSS bars."""
    grid = activity.hour_weekday_grid(times)
    hour_totals = [sum(grid[day][hour] for day in range(7)) for hour in range(24)]
    max_total = max(hour_totals)
    if max_total == 0:
        return ""
    bars = []
    for hour, total in enumerate(hour_totals):
        if total:
            style = f"height:{round(total / max_total * 100)}%; background:{activity.color_for(total, max_total)};"
        else:
            style = "height:2px; background:#e5e7eb;"
        bars.append(f"<div class='bar-col' title='{hour:02d}:00 - {total} commit(s)'>"
                    f"<div class='bar' style='{style}'></div>"
                    f"<span class='bar-label'>{hour:02d}</span></div>")
    return f"<div class='barchart'>{''.join(bars)}</div>"


def _estimate_html(utc_times) -> str:
    """Build the statistical UTC offset estimate (shown only when no real offset is known)."""
    top_offsets, confidence = activity.estimate_utc_offsets(Counter(time.hour for time in utc_times),
                                                            len(utc_times))
    items = []
    for rank, (offset, percent_of_best) in enumerate(top_offsets):
        label = "best fit" if rank == 0 else f"{percent_of_best:.0f}% as good a fit as the best match"
        items.append(f"<li><strong>{offset:+d}</strong> - {label}</li>")
    return ("<h3>Estimated UTC offset (no real offset known)</h3>"
            f"<ul class='tz-list'>{''.join(items)}</ul>"
            f"<p class='dim'>Overall confidence: <strong>{confidence:.0f}%</strong>. Statistical "
            "estimate from the hourly activity shape, not a fact - run 'activity' with a token to "
            "get the real offsets recorded by git.</p>")


def _offsets_html(local_times) -> str:
    """Build the table of real UTC offsets recorded by git, with a reminder of what they are."""
    rows = []
    for offset, count, first, last in activity.offset_summary(local_times):
        rows.append([offset, count, f"{count / len(local_times) * 100:.0f}%", first, last])
    return ("<h3>Committer UTC offsets (recorded by git)</h3>"
            + _table_html(["Offset", "Commits", "%", "First seen", "Last seen"], rows)
            + "<p class='dim'>From the clock of the committing machine: a raw fact, possibly wrong "
              "(VM, CI) or changed on purpose.</p>")


def _activity_html(rows) -> str:
    """Build the whole Activity section body: span, real offsets, heatmap and bars, estimate."""
    utc_times, local_times = activity.times_from_rows(rows)
    if not utc_times:
        return "<p class='empty'>No commit activity found.</p>"
    parts = [f"<p class='dim'>{len(utc_times)} commit(s), spanning "
             f"{min(utc_times).date()} to {max(utc_times).date()}</p>"]
    if local_times:
        parts.append(_offsets_html(local_times))
    # Local time when git's offsets are known (activity run with a token), UTC otherwise.
    if local_times:
        parts.append("<h3>Local time</h3>" + _heatmap_html(local_times) + _bar_chart_html(local_times))
    else:
        parts.append("<h3>UTC</h3>" + _heatmap_html(utc_times) + _bar_chart_html(utc_times))
    if not activity.offsets_are_informative(local_times):
        parts.append(_estimate_html(utc_times))
    return "\n  ".join(parts)


REPORT_CSS = """
:root { --bg:#ffffff; --fg:#1a1a1a; --muted:#6b7280; --border:#e5e7eb; }
body { font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; background:var(--bg);
       color:var(--fg); margin:0; padding:32px 40px; max-width:900px; margin-left:auto; margin-right:auto; }
header { display:flex; gap:20px; align-items:center; }
.avatar { width:96px; height:96px; border-radius:10px; object-fit:cover; flex-shrink:0; }
h1 { margin:0; font-size:26px; }
.handle { color:var(--muted); margin:4px 0 0; }
.generated { color:var(--muted); font-size:12px; margin:18px 0 28px; }
section { margin:32px 0; }
h3 { font-size:14px; margin:20px 0 6px; }
h2 { font-size:17px; border-bottom:1px solid var(--border); padding-bottom:6px; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--border); }
table.kv th { width:160px; color:var(--muted); font-weight:600; border:none; }
table.kv td { border:none; }
.empty { color:var(--muted); font-style:italic; }
.dim { color:var(--muted); font-size:13px; }
table.heatmap { font-size:10px; margin-top:8px; }
table.heatmap th { color:var(--muted); font-weight:400; text-align:center; }
table.heatmap td.cell { width:16px; height:16px; background:#f2f2f2; border:none; border-radius:3px;
                          padding:0; }
.barchart { display:flex; align-items:flex-end; gap:3px; height:110px; margin-top:18px; }
.bar-col { display:flex; flex-direction:column; justify-content:flex-end; align-items:center;
           flex:1; height:100%; }
.bar { width:100%; border-radius:2px 2px 0 0; min-height:2px; }
.bar-label { font-size:9px; color:var(--muted); margin-top:3px; }
.tz-list { padding-left:20px; margin-bottom:4px; }
a { color:#0969da; text-decoration:none; }
a:hover { text-decoration:underline; }
"""


def _build_html(user, data, avatar_data_uri, output_path):
    """Write the HTML report of one user from the rows read back from the database."""
    profile_row = data["profile"]
    name = profile_row.get("name") or user
    profile_url = profile_row.get("profile_url") or f"https://github.com/{user}"
    unique_emails = sorted({row["email"] for row in data["emails"] if row.get("email")})
    avatar_html = f"<img class='avatar' src='{avatar_data_uri}' alt='avatar'>" if avatar_data_uri else ""
    keys_rows, gists_rows, repos_rows = data["keys"], data["gists"], data["repos"]

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_escape(name)} - gitective report</title>
<style>{REPORT_CSS}</style>
</head>
<body>
<header>
  {avatar_html}
  <div>
    <h1>{_escape(name)}</h1>
    <p class="handle">@{_escape(user)} &middot; <a href="{_escape(profile_url)}">{_escape(profile_url)}</a></p>
  </div>
</header>
<p class="generated">Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} - gitective</p>

<section>
  <h2>Profile</h2>
  <table class="kv"><tbody>{_key_value_rows([
        ("GitHub ID", profile_row.get("github_id")),
        ("Company", profile_row.get("company")),
        ("Location", profile_row.get("location")),
        ("Blog", profile_row.get("blog")),
        ("Twitter", profile_row.get("twitter")),
        ("Public email", profile_row.get("email")),
        ("Bio", profile_row.get("bio")),
        ("Public repos", profile_row.get("public_repos")),
        ("Public gists", profile_row.get("public_gists")),
        ("Followers", profile_row.get("followers")),
        ("Following", profile_row.get("following")),
        ("Account created", profile_row.get("created_at")),
        ("Hireable", profile_row.get("hireable")),
        ("Organizations", profile_row.get("orgs")),
        ("Social accounts", profile_row.get("social_accounts")),
    ])}</tbody></table>
</section>

<section>
  <h2>Keys ({len(keys_rows)})</h2>
  {_table_html(["Type", "Value", "Email"], [[k.get("key_type"), k.get("key"), k.get("email")] for k in keys_rows])}
</section>

<section>
  <h2>Repos ({len(repos_rows)})</h2>
  {_table_html(["Repo", "Fork", "Stars", "Language", "Commits"],
                [[r.get("repository"), "yes" if r.get("is_fork") else "-",
                  r.get("stars"), r.get("language"), r.get("commit_count")] for r in repos_rows])}
</section>

<section>
  <h2>Emails ({len(unique_emails)})</h2>
  {_table_html(["Email"], [[addr] for addr in unique_emails])}
</section>

<section>
  <h2>Gists ({len(gists_rows)})</h2>
  {_table_html(["Gist ID", "Description", "Files"],
                [[g.get("gist_id"), g.get("description"), g.get("files")] for g in gists_rows])}
</section>

<section>
  <h2>Activity</h2>
  {_activity_html(data["activity"])}
</section>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)


def _read_back(ctx, user) -> dict:
    """Read every row stored for the user in each table (case-insensitive, no date limit)."""
    def rows_of(table):
        """Return the rows of `table` whose owner is the user, as dicts."""
        return [dict(row) for row in ctx.db.query(f"SELECT * FROM {table} WHERE owner = ? COLLATE NOCASE", (user,))]

    latest_profile = ctx.db.query(
        "SELECT * FROM profile WHERE login = ? COLLATE NOCASE ORDER BY id DESC LIMIT 1", (user,))
    profile_row = dict(latest_profile[0]) if latest_profile else {}
    return {
        "profile": profile_row,
        "keys": rows_of("keys"), "gists": rows_of("gists"), "repos": rows_of("repos"),
        "emails": rows_of("emails"), "activity": rows_of("activity"),
    }


def _report_one(args, ctx, user, output_path):
    """Run every sub-module for one user, then write their HTML report."""
    console.print(f"\n[info]=== Building report for {user} ===[/info]")
    sub_args = _sub_module_args(args.token, user)
    for module in SUB_MODULES:
        console.rule(module.NAME)
        module.run(sub_args, ctx)
    console.rule()

    data = _read_back(ctx, user)
    console.print("[info]Building HTML report...[/info]")
    _build_html(user, data, _avatar_data_uri(data["profile"].get("avatar_url")), output_path)
    console.print(f"[ok]Report saved to {output_path}[/ok]")


def run(args, ctx):
    """Build one report per target user."""
    if getattr(args, "from_targets", False):
        users = ctx.db.target_values("owner")
    else:
        users = parse_list_arg(args.user)
    if not users:
        sys.exit("[!] report module needs --user or --from-targets")

    for user in users:
        if args.output and len(users) == 1:
            output_path = args.output
        else:
            output_path = f"{user}_report.html"
        _report_one(args, ctx, user, output_path)
