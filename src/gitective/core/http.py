"""GitHub API access shared by all modules: session, retries, pagination.

Error handling, in one place so modules don't have to care:
- rate limits (403 with Retry-After, X-RateLimit-Remaining: 0, or a body
  mentioning a rate limit) -> wait, then retry the same request;
- 404 (not found), 409 (empty repository) and any other 403 (missing
  permission) -> SkipTarget, which modules catch to skip that one target;
- timeout / connection error -> NetworkStopped, which modules catch to save
  what was collected so far and stop.

The search endpoints have a much stricter limit (~30 requests/min) than the
rest of the API: callers pass delay=SEARCH_PAGE_DELAY to iter_pages().

graphql() runs a GraphQL query (always needs a token); used where REST loses
information, e.g. the UTC offset of commit dates ('activity').
"""

import time

import requests

from .console import console

API = "https://api.github.com"
GRAPHQL_URL = f"{API}/graphql"
TIMEOUT = 30  # seconds, per HTTP request
SEARCH_PAGE_DELAY = 2  # seconds between two pages of a /search/* endpoint


class SkipTarget(Exception):
    """An expected failure on one target (repo, gist, user...): skip it, keep going."""


class RunInterrupted(Exception):
    """Stop the whole run early, but save what was collected so far."""


class NetworkStopped(RunInterrupted):
    """A request timed out or the connection failed."""


def make_session(token: str | None) -> requests.Session:
    """Create an HTTP session with the GitHub JSON headers and optional token."""
    session = requests.Session()
    session.headers.update({"Accept": "application/vnd.github+json"})
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _body_mentions_rate_limit(resp: requests.Response) -> bool:
    """Tell whether a 403 response body talks about a rate limit."""
    try:
        text = resp.text.lower()
    except Exception:
        return False
    return "rate limit" in text or "abuse detection" in text


def _seconds_to_wait(resp: requests.Response) -> float | None:
    """Return how long to wait if a 403 is a rate limit, or None if it isn't."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        return int(retry_after)
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        reset_at = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
        return max(reset_at - time.time(), 1)
    # The secondary limit doesn't always send the headers above.
    if _body_mentions_rate_limit(resp):
        return 60
    return None


def _send_with_retry(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Send a request, waiting out rate limits; raise SkipTarget or NetworkStopped on failure."""
    while True:
        try:
            resp = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            console.print(f"[err][!] Network error, stopping: {exc}[/err]")
            raise NetworkStopped(str(exc)) from exc

        if resp.status_code == 403:
            wait = _seconds_to_wait(resp)
            if wait is None:
                # Not a rate limit: almost always a missing permission
                # (e.g. an endpoint needing admin rights), retrying won't help.
                raise SkipTarget(f"403 (forbidden) for {url}")
            console.print(f"[warn][!] GitHub rate limit, sleeping {wait:.0f}s and retrying[/warn]")
            time.sleep(wait)
            continue

        if resp.status_code in (404, 409):
            # 404: not found, 409: empty repository (no commits yet)
            raise SkipTarget(f"{resp.status_code} for {url}")

        resp.raise_for_status()
        return resp


def request_with_retry(session: requests.Session, url: str, params=None) -> requests.Response:
    """GET a URL, waiting out rate limits; raise SkipTarget or NetworkStopped on failure."""
    return _send_with_retry(session, "GET", url, params=params)


def graphql(session: requests.Session, query: str, variables: dict) -> dict:
    """Run a GraphQL query (needs a token) and return its 'data'; raise SkipTarget on an error.

    Unlike REST, GraphQL reports most errors in a 200 response ('errors' list).
    """
    while True:
        payload = _send_with_retry(session, "POST", GRAPHQL_URL,
                                   json={"query": query, "variables": variables}).json()
        errors = payload.get("errors") or []
        if any(error.get("type") == "RATE_LIMITED" for error in errors):
            console.print("[warn][!] GitHub GraphQL rate limit, sleeping 60s and retrying[/warn]")
            time.sleep(60)
            continue
        if errors:
            raise SkipTarget("; ".join(error.get("message", "GraphQL error") for error in errors))
        return payload.get("data") or {}


def fetch_json_or_none(session: requests.Session, url: str):
    """GET a URL and return its JSON, or None if the target doesn't exist."""
    try:
        return request_with_retry(session, url).json()
    except SkipTarget:
        return None


def link_header_url(resp: requests.Response, rel: str) -> str | None:
    """Return the URL for `rel` ('next', 'last'...) from the Link header, if any."""
    link = resp.headers.get("Link")
    if not link:
        return None
    for part in link.split(","):
        url_part, rel_part = part.split(";", 1)
        if f'rel="{rel}"' in rel_part:
            return url_part.strip().strip("<>")
    return None


def iter_pages(session: requests.Session, url: str, params=None, items_key=None, delay: float = 0):
    """Yield every item of a paginated endpoint, following the Link header.

    items_key: set it when the items are wrapped, e.g. {"items": [...]} on /search/*.
    delay: seconds to wait between pages (use SEARCH_PAGE_DELAY for /search/*).
    """
    while url:
        resp = request_with_retry(session, url, params=params)
        params = None  # the "next" URL already carries the query string
        payload = resp.json()
        items = payload[items_key] if items_key else payload
        for item in items:
            yield item
        url = link_header_url(resp, "next")
        if url and delay:
            time.sleep(delay)


def get_account_type(session: requests.Session, name: str) -> str | None:
    """Return 'User' or 'Organization' for an account, or None if it doesn't exist."""
    data = fetch_json_or_none(session, f"{API}/users/{name}")
    return data.get("type") if data else None


def iter_non_fork_repos(session: requests.Session, name: str, is_org: bool):
    """Yield the names of an account's non-fork repos."""
    if is_org:
        url = f"{API}/orgs/{name}/repos"
    else:
        url = f"{API}/users/{name}/repos"
    for repo in iter_pages(session, url, params={"per_page": 100}):
        if not repo.get("fork"):
            yield repo["name"]


def iter_gist_ids(session: requests.Session, owner: str):
    """Yield the ids of a user's public gists."""
    for gist in iter_pages(session, f"{API}/users/{owner}/gists"):
        yield gist["id"]
