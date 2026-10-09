"""keys - public SSH and GPG keys of one or more users.

GPG key UIDs often expose extra email addresses. Keys are also a strong
identity link: the same key on two accounts means the same person. A
48-character slice from the middle of each key is shown as a search tip
(feed it to 'search' to find the key reused elsewhere); it is not stored.

SSH and GPG keys share the 'keys' table: key_type is 'gpg' for GPG keys and
the SSH algorithm (e.g. 'ssh-ed25519') for SSH keys.
"""

from rich.panel import Panel
from rich.table import Table

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, iter_pages, make_session
from ..core.pivots import resolve_users
from ..core.targets import offer_to_save_pivots

NAME = "keys"
HELP = "Fetch public SSH/GPG keys for one or more users (identity correlation, key reuse)"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, key_type TEXT, key TEXT, created_at TEXT, expires_at TEXT, email TEXT
);
"""

SEARCH_FRAGMENT_LENGTH = 48


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")


def _middle_slice(text: str) -> str | None:
    """Return SEARCH_FRAGMENT_LENGTH characters from the middle of a key body (None if empty)."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text) <= SEARCH_FRAGMENT_LENGTH:
        return text
    start = (len(text) - SEARCH_FRAGMENT_LENGTH) // 2
    return text[start:start + SEARCH_FRAGMENT_LENGTH]


def _ssh_search_fragment(key_line: str) -> str | None:
    """Return a search fragment of an SSH key line ('algorithm base64 [comment]')."""
    parts = (key_line or "").split()
    if len(parts) > 1:
        return _middle_slice(parts[1])
    if parts:
        return _middle_slice(parts[0])
    return None


def _gpg_search_fragment(armored_key: str) -> str | None:
    """Return a search fragment of an armored GPG key, ignoring its header lines."""
    body_lines = []
    for line in (armored_key or "").splitlines():
        if not line or line.startswith(("-----", "Version", "Comment")):
            continue
        body_lines.append(line)
    return _middle_slice("".join(body_lines))


def _fetch_ssh_keys(session, owner):
    """Return the SSH keys of a user as [{"owner", "key", "fragment"}]."""
    keys = []
    for key in iter_pages(session, f"{API}/users/{owner}/keys"):
        key_line = key.get("key")
        keys.append({"owner": owner, "key": key_line, "fragment": _ssh_search_fragment(key_line)})
    return keys


def _fetch_gpg_keys(session, owner):
    """Return the GPG keys of a user with their UID emails and dates."""
    keys = []
    for key in iter_pages(session, f"{API}/users/{owner}/gpg_keys"):
        emails = [uid.get("email") for uid in key.get("emails", []) if uid.get("email")]
        keys.append({
            "owner": owner, "key_id": key.get("key_id"), "emails": emails,
            "created_at": key.get("created_at"), "expires_at": key.get("expires_at"),
            "public_key": key.get("public_key"), "fragment": _gpg_search_fragment(key.get("public_key")),
        })
    return keys


def _to_rows(ssh_keys, gpg_keys):
    """Convert the keys into rows of the 'keys' table (one row per GPG UID email)."""
    rows = []
    for key in ssh_keys:
        key_type = key["key"].split()[0] if key["key"] else None
        rows.append({"owner": key["owner"], "key_type": key_type, "key": key["key"],
                     "created_at": None, "expires_at": None, "email": None})
    for key in gpg_keys:
        for email in (key["emails"] or [None]):
            rows.append({"owner": key["owner"], "key_type": "gpg", "key": key["public_key"],
                         "created_at": key["created_at"], "expires_at": key["expires_at"], "email": email})
    return rows


def _print_keys(ssh_keys, gpg_keys):
    """Print the SSH keys as a table and each GPG key as a panel."""
    console.print()
    if ssh_keys:
        table = Table(title="SSH keys", header_style="bold magenta")
        table.add_column("Owner", style="ok")
        table.add_column("Full key", style="dim")
        for key in ssh_keys:
            table.add_row(key["owner"], key["key"] or "-")
        console.print(table)
    else:
        console.print("[warn]No public SSH keys found.[/warn]")

    console.print()
    if not gpg_keys:
        console.print("[warn]No public GPG keys found.[/warn]")
    for key in gpg_keys:
        lines = [
            f"Key ID: {key['key_id'] or '-'}",
            f"UID email(s): {', '.join(key['emails']) or '-'}",
            f"Created: {key['created_at']}   Expires: {key['expires_at']}",
            f"Search fragment: {key['fragment'] or '-'}",
            "",
            key["public_key"] or "(no public key body)",
        ]
        console.print(Panel("\n".join(lines), title=f"{key['owner']} GPG key", border_style="dim"))


def run(args, ctx):
    """Fetch, save and print the SSH and GPG keys of each target user."""
    session = make_session(args.token)
    owners = resolve_users(args, ctx)

    ssh_keys, gpg_keys = [], []
    try:
        with console.status("[info]Fetching keys...[/info]") as status:
            for owner in owners:
                status.update(f"[info]Fetching keys for {owner}[/info]")
                try:
                    ssh_keys.extend(_fetch_ssh_keys(session, owner))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping SSH keys for {owner}: {exc}[/warn]")
                try:
                    gpg_keys.extend(_fetch_gpg_keys(session, owner))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping GPG keys for {owner}: {exc}[/warn]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("keys", _to_rows(ssh_keys, gpg_keys))
    _print_keys(ssh_keys, gpg_keys)

    fragments = [key["fragment"] for key in ssh_keys + gpg_keys if key["fragment"]]
    if fragments:
        console.print(f"\n[info]Tip:[/info] re-search a key fragment with "
                      f"[bold]gitective search -q \"{fragments[0]}\"[/bold]")

    offer_to_save_pivots(args, ctx, NAME, {"owner": owners})
