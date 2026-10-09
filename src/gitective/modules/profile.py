"""profile - public profile of one or more GitHub accounts.

One GET /users/{user} (bio, company, location, blog, Twitter, public email,
counters, creation date, numeric id) plus the account's public
organizations and linked social accounts: 3 requests per user, a quick
first look before deciding where to dig.

The numeric id (github_id) never changes, even when the login does: two
runs seeing the same id under two logins prove a rename.
"""

from rich.panel import Panel

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, iter_pages, make_session, request_with_retry
from ..core.pivots import resolve_users
from ..core.targets import offer_to_save_pivots

NAME = "profile"
HELP = "Fetch basic public profile info for one or more GitHub users"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    login TEXT, github_id INTEGER, profile_url TEXT, avatar_url TEXT, name TEXT, company TEXT, location TEXT,
    blog TEXT, twitter TEXT, twitter_url TEXT, email TEXT, bio TEXT, public_repos INTEGER,
    public_gists INTEGER, followers INTEGER, following INTEGER, created_at TEXT,
    updated_at TEXT, hireable TEXT, orgs TEXT, social_accounts TEXT
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")


def _list_or_empty(session, url, field):
    """Return `field` of every item of a paginated endpoint, or [] if it can't be read."""
    values = []
    try:
        for item in iter_pages(session, url, params={"per_page": 100}):
            if item.get(field):
                values.append(item[field])
    except SkipTarget:
        pass
    return values


def _fetch_profile(session, user):
    """Return the /users payload of a user, with its 'orgs' and 'social' lists added."""
    profile = request_with_retry(session, f"{API}/users/{user}").json()
    profile["orgs"] = _list_or_empty(session, f"{API}/users/{user}/orgs", "login")
    profile["social"] = _list_or_empty(session, f"{API}/users/{user}/social_accounts", "url")
    return profile


def _to_row(profile):
    """Convert a fetched profile into a row of the 'profile' table."""
    twitter = profile.get("twitter_username")
    return {
        "login": profile.get("login"), "github_id": profile.get("id"),
        "profile_url": profile.get("html_url"), "avatar_url": profile.get("avatar_url"),
        "name": profile.get("name"), "company": profile.get("company"), "location": profile.get("location"),
        "blog": profile.get("blog"), "twitter": twitter,
        "twitter_url": f"https://x.com/{twitter}" if twitter else None,
        "email": profile.get("email"), "bio": profile.get("bio"),
        "public_repos": profile.get("public_repos"), "public_gists": profile.get("public_gists"),
        "followers": profile.get("followers"), "following": profile.get("following"),
        "created_at": profile.get("created_at"), "updated_at": profile.get("updated_at"),
        "hireable": str(profile.get("hireable")),
        "orgs": ", ".join(profile["orgs"]), "social_accounts": ", ".join(profile["social"]),
    }


def _print_profile(profile):
    """Print one profile as a panel with clickable links."""
    profile_url = profile.get("html_url") or f"https://github.com/{profile['login']}"
    twitter_name = profile.get("twitter_username")
    twitter = f"[link=https://x.com/{twitter_name}]@{twitter_name}[/link]" if twitter_name else "-"
    blog = f"[link={profile['blog']}]{profile['blog']}[/link]" if profile.get("blog") else "-"
    org_links = ", ".join(f"[link=https://github.com/{org}]{org}[/link]" for org in profile["orgs"])
    social_links = ", ".join(f"[link={url}]{url}[/link]" for url in profile["social"])
    lines = [
        f"GitHub: [link={profile_url}]{profile_url}[/link]   ID: {profile.get('id')}",
        f"Name: {profile.get('name') or '-'}",
        f"Company: {profile.get('company') or '-'}",
        f"Location: {profile.get('location') or '-'}",
        f"Blog: {blog}",
        f"Twitter: {twitter}",
        f"Public email: {profile.get('email') or '-'}",
        f"Bio: {profile.get('bio') or '-'}",
        f"Repos: {profile.get('public_repos', 0)}   Gists: {profile.get('public_gists', 0)}   "
        f"Followers: {profile.get('followers', 0)}   Following: {profile.get('following', 0)}",
        f"Created: {profile.get('created_at')}   Updated: {profile.get('updated_at')}",
        f"Hireable: {profile.get('hireable')}",
        f"Orgs: {org_links or '-'}",
        f"Social: {social_links or '-'}",
    ]
    console.print(Panel("\n".join(lines), title=profile["login"], border_style="dim"))


def run(args, ctx):
    """Fetch, save and print the profile of each target user."""
    session = make_session(args.token)
    users = resolve_users(args, ctx)

    profiles = []
    try:
        for user in users:
            try:
                profiles.append(_fetch_profile(session, user))
            except SkipTarget as exc:
                console.print(f"[warn][!] Skipping {user}: {exc}[/warn]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("profile", [_to_row(profile) for profile in profiles])

    console.print()
    for profile in profiles:
        _print_profile(profile)

    # The users themselves and their organizations are worth pivoting on.
    owners = set()
    for profile in profiles:
        owners.add(profile["login"])
        owners.update(profile["orgs"])
    offer_to_save_pivots(args, ctx, NAME, {"owner": sorted(owners)})
