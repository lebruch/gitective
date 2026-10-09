"""workflows - GitHub Actions workflows of repos, with the raw facts worth
checking first: triggers, and whether each job runs on a GitHub-hosted or a
self-hosted runner.

Facts only: no secret extraction ('secrets' does that), no combined
vulnerability detection (pwn request, script injection...) - correlating is
left to the user.

Targets: same as 'activity' (-u/-o/-r/--from-targets, see
resolve_repo_scope() in core/pivots.py). The author part of the scope is not
used: a workflow file isn't "authored" by one committer.

Runner classification:
- GitHub-hosted labels are documented: ubuntu-*, windows-*, macos-*;
- self-hosted runners carry the literal 'self-hosted' label, or use the
  {group, labels} object syntax; any other custom label is self-hosted too;
- a runs-on built from an expression (e.g. ${{ matrix.os }}) can't be
  resolved without running the workflow: reported as 'unresolved'.

YAML gotcha: PyYAML follows YAML 1.1, where the bare key `on` is parsed as
the boolean True, not the string "on" - both are checked.
"""

import base64

import yaml
from rich.table import Table

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, iter_pages, make_session, request_with_retry
from ..core.pivots import resolve_repo_scope
from ..core.targets import offer_to_save_pivots

NAME = "workflows"
HELP = "List a repo's GitHub Actions workflows: triggers, runner type"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, repository TEXT, path TEXT, name TEXT, state TEXT,
    triggers TEXT, runners TEXT
);
"""

GITHUB_HOSTED_PREFIXES = ("ubuntu-", "windows-", "macos-")


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file, one per line)")
    parser.add_argument("-o", "--org", help="Comma-separated GitHub org names (or @file, one per line)")
    parser.add_argument("-r", "--repository",
                        help="Comma-separated 'owner/repo' (or @file, one per line) - only these repos")


def _classify_runner(runs_on) -> str:
    """Return 'github-hosted', 'self-hosted' or 'unresolved' for a job's runs-on value."""
    if isinstance(runs_on, dict):
        return "self-hosted"  # {group, labels} syntax: always a self-hosted runner group
    if isinstance(runs_on, list):
        labels = [str(label) for label in runs_on]
    else:
        labels = [str(runs_on)]

    if "self-hosted" in labels:
        return "self-hosted"
    if any("${{" in label for label in labels):
        return "unresolved"
    if all(label.lower().startswith(GITHUB_HOSTED_PREFIXES) for label in labels):
        return "github-hosted"
    return "self-hosted"  # custom label matching none of the GitHub-hosted prefixes


def _extract_triggers(workflow: dict) -> list:
    """Return the event names of the workflow's `on:` key (string, list or mapping form)."""
    triggers = workflow.get("on", workflow.get(True))  # see the YAML gotcha in the module docstring
    if isinstance(triggers, str):
        return [triggers]
    if isinstance(triggers, list):
        return [str(trigger) for trigger in triggers]
    if isinstance(triggers, dict):
        return list(triggers.keys())
    return []


def _extract_runners(workflow: dict) -> list:
    """Return the sorted runner kinds used by the workflow's jobs."""
    runners = set()
    for job in (workflow.get("jobs") or {}).values():
        if isinstance(job, dict) and "runs-on" in job:
            runners.add(_classify_runner(job["runs-on"]))
    return sorted(runners)


def _fetch_workflow(session, owner, repo, path):
    """Download and parse a workflow file; return {} if it can't be read or parsed."""
    try:
        resp = request_with_retry(session, f"{API}/repos/{owner}/{repo}/contents/{path}")
        content = base64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")
        workflow = yaml.safe_load(content)
    except (SkipTarget, yaml.YAMLError, KeyError):
        return {}
    if isinstance(workflow, dict):
        return workflow
    return {}


def _list_workflows(session, owner, repo):
    """Return the workflows of a repo as rows of the 'workflows' table."""
    workflows = []
    url = f"{API}/repos/{owner}/{repo}/actions/workflows"
    for listed in iter_pages(session, url, items_key="workflows"):
        workflow = _fetch_workflow(session, owner, repo, listed["path"])
        workflows.append({
            "owner": owner, "repository": repo, "path": listed["path"], "name": listed.get("name"),
            "state": listed.get("state"), "triggers": ", ".join(_extract_triggers(workflow)),
            "runners": ", ".join(_extract_runners(workflow)),
        })
    return workflows


def _print_workflows(workflows):
    """Print the workflows as a table."""
    table = Table(title=f"{len(workflows)} workflow(s)", header_style="bold magenta")
    table.add_column("Repo", style="ok")
    table.add_column("Workflow")
    table.add_column("Triggers", style="dim")
    table.add_column("Runners")
    for workflow in workflows:
        table.add_row(f"{workflow['owner']}/{workflow['repository']}", workflow["name"] or workflow["path"],
                      workflow["triggers"] or "-", workflow["runners"] or "-")
    console.print(table)
    console.print("\n[dim]Facts only - correlate yourself (e.g. a self-hosted runner reachable "
                  "via an externally-triggerable event, is worth a closer look).[/dim]")


def run(args, ctx):
    """List the workflows of every target repo, then save and print them."""
    session = make_session(args.token)
    repos = sorted({(owner, repo) for owner, repo, _author in resolve_repo_scope(args, ctx, session)})

    workflows = []
    try:
        with console.status(f"[info]Listing workflows across {len(repos)} repo(s)...[/info]") as status:
            for owner, repo in repos:
                status.update(f"[info]Listing workflows for {owner}/{repo}[/info]")
                try:
                    workflows.extend(_list_workflows(session, owner, repo))
                except SkipTarget as exc:
                    console.print(f"[warn][!] Skipping {owner}/{repo}: {exc}[/warn]")
    except RunInterrupted as exc:
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("workflows", workflows)

    console.print()
    if workflows:
        _print_workflows(workflows)
    else:
        console.print("[warn]No workflows found.[/warn]")

    pivots = {
        "owner": sorted({owner for owner, _repo in repos}),
        "repo": sorted(f"{owner}/{repo}" for owner, repo in repos),
        # 'owner/repo:path' lets history/secrets scan just that file later.
        "workflow": sorted(f"{w['owner']}/{w['repository']}:{w['path']}" for w in workflows),
    }
    offer_to_save_pivots(args, ctx, NAME, pivots)
