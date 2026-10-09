"""Walk the full history of a repo or a gist, shared by 'history' and 'secrets'.

Both modules give a find_matches(text) function, which yields
(label, matched_text) for every hit in a piece of text: one custom regex
for 'history', ~33 known secret patterns for 'secrets'.

- Repos expose real diffs (one API request per commit), so each added or
  removed line is checked on its own, and --scope can keep only one side.
- Gists don't expose diffs: each revision is the full file content, so the
  whole content of every revision is checked and --scope doesn't apply.

Each hit is returned as a finding dict:
{owner, repo (None for a gist), sha, url, file, label, match, line}.

Modules normally only call scan_all() and content_pivots().
"""

from .console import console
from .http import API, RunInterrupted, SkipTarget, iter_pages, request_with_retry

MAX_MATCH_LENGTH = 120
MAX_LINE_LENGTH = 200


def iter_diff_lines(session, owner: str, repo: str, author: str | None = None):
    """Yield (sha, filename, '+' or '-', text) for every added/removed line in a repo's history.

    author: only walk that user's commits (filtered by GitHub itself).
    """
    params = {"per_page": 100}
    if author:
        params["author"] = author
    for commit in iter_pages(session, f"{API}/repos/{owner}/{repo}/commits", params=params):
        sha = commit["sha"]
        try:
            details = request_with_retry(session, f"{API}/repos/{owner}/{repo}/commits/{sha}").json()
        except SkipTarget:
            continue
        for changed_file in details.get("files", []):
            patch = changed_file.get("patch")
            if not patch:
                continue
            for line in patch.splitlines():
                if line.startswith("+++") or line.startswith("---"):
                    continue  # file header lines, not content
                if line.startswith("+") or line.startswith("-"):
                    yield sha, changed_file.get("filename"), line[0], line[1:]


def iter_gist_revisions(session, gist_id: str):
    """Yield (revision_sha, filename, full_content) for every file of every revision of a gist."""
    for revision in iter_pages(session, f"{API}/gists/{gist_id}/commits"):
        sha = revision["version"]
        try:
            snapshot = request_with_retry(session, f"{API}/gists/{gist_id}/{sha}").json()
        except SkipTarget:
            continue
        for filename, info in snapshot.get("files", {}).items():
            content = info.get("content")
            if content:
                yield sha, filename, content


def scan_repo_history(session, target: dict, scope: str, find_matches):
    """Yield a finding for each match in a repo's diff lines.

    target: {"owner", "repo", "author", "path"} (path: only scan that file, or None).
    scope: 'added', 'removed' or 'both'.
    """
    owner, repo = target["owner"], target["repo"]
    for sha, filename, sign, text in iter_diff_lines(session, owner, repo, author=target["author"]):
        if target["path"] and filename != target["path"]:
            continue
        if scope == "added" and sign != "+":
            continue
        if scope == "removed" and sign != "-":
            continue
        for label, matched in find_matches(text):
            yield {"owner": owner, "repo": repo, "sha": sha,
                   "url": f"https://github.com/{owner}/{repo}/commit/{sha}",
                   "file": filename, "label": label, "match": matched[:MAX_MATCH_LENGTH],
                   "line": text.strip()[:MAX_LINE_LENGTH]}


def scan_gist_history(session, target: dict, find_matches):
    """Yield a finding for each match in any revision of a gist.

    target: {"gist_id", "owner"} (owner may be None).
    """
    gist_id = target["gist_id"]
    for sha, filename, content in iter_gist_revisions(session, gist_id):
        for label, matched in find_matches(content):
            yield {"owner": target["owner"], "repo": None, "sha": sha,
                   "url": f"https://gist.github.com/{gist_id}/{sha}",
                   "file": filename, "label": label, "match": matched[:MAX_MATCH_LENGTH],
                   "line": _line_containing(content, matched)[:MAX_LINE_LENGTH]}


def _line_containing(content: str, matched: str) -> str:
    """Return the line of `content` where `matched` first appears (stripped)."""
    position = content.find(matched)
    if position < 0:
        return matched.strip()
    line_index = content.count("\n", 0, position)
    lines = content.splitlines()
    if line_index < len(lines):
        return lines[line_index].strip()
    return matched.strip()


def scan_all(session, repo_targets, gist_targets, scope, find_matches, action="Scanning") -> list:
    """Scan every repo and gist target with a progress line; return all findings.

    A target that can't be read (e.g. an empty repo) is skipped; a network
    failure stops the scan but keeps the findings collected so far.
    """
    findings = []
    try:
        with console.status(f"[info]{action}...[/info]") as status:
            for target in repo_targets:
                label = f"{target['owner']}/{target['repo']}"
                status.update(f"[info]{action} {label}...[/info]")
                try:
                    findings.extend(scan_repo_history(session, target, scope, find_matches))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping {label}: {exc}[/warn]")
            for target in gist_targets:
                status.update(f"[info]{action} gist {target['gist_id']}...[/info]")
                try:
                    findings.extend(scan_gist_history(session, target, find_matches))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping gist {target['gist_id']}: {exc}[/warn]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")
    return findings


def content_pivots(repo_targets, gist_targets) -> dict:
    """Return the owners, repos and gists that were scanned, as pivots for the targets pool."""
    owners = {target["owner"] for target in repo_targets}
    owners.update(target["owner"] for target in gist_targets if target["owner"])
    return {
        "owner": sorted(owners),
        "repo": sorted(f"{target['owner']}/{target['repo']}" for target in repo_targets),
        "gist": sorted({target["gist_id"] for target in gist_targets}),
    }


def finding_source(finding) -> str:
    """Return 'owner/repo' for a repo finding, or 'gist:owner' for a gist finding."""
    if finding["repo"]:
        return f"{finding['owner']}/{finding['repo']}"
    return f"gist:{finding['owner'] or '?'}"
