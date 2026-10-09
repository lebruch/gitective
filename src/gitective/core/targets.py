"""End-of-run prompt offering to add a run's pivots to the 'targets' pool.

Every module calls offer_to_save_pivots() after displaying its results.
(O)ui adds everything, (E)diter lists them numbered so some can be dropped
first, (N)on adds nothing. The module's own result table is always saved
before this question, whatever the answer.

When a module is run by another one (e.g. 'report'), the caller sets
args.skip_targets_prompt = True: pivots are then added silently instead of
asking the same question once per sub-module.
"""

from rich.prompt import Prompt

from .console import console

ALLOWED_TARGET_KINDS = {"owner", "repo", "gist", "workflow"}


def offer_to_save_pivots(args, ctx, source_module: str, pivots: dict):
    """Ask whether to add the pivots ({kind: [values]}) to the targets pool, then add them."""
    pivots_to_offer = {}
    for kind, values in (pivots or {}).items():
        if values and kind in ALLOWED_TARGET_KINDS:
            pivots_to_offer[kind] = values
    if not pivots_to_offer:
        return

    if getattr(args, "skip_targets_prompt", False):
        added = _save(ctx, source_module, pivots_to_offer)
        if added:
            console.print(f"[dim][*] Added {added} new target(s).[/dim]")
        return

    total = sum(len(values) for values in pivots_to_offer.values())
    console.print()
    answer = Prompt.ask(f"Save {total} pivot(s) found to your targets pool? (o)ui / (e)diter / (N)on",
                        default="n")
    choice = answer.strip().lower()[:1]
    if choice not in ("o", "e", "n"):
        choice = "o"
    if choice == "n":
        return
    if choice == "e":
        pivots_to_offer = _let_user_remove_some(pivots_to_offer)

    added = _save(ctx, source_module, pivots_to_offer)
    if added:
        console.print(f"[ok]Added {added} new target(s).[/ok]")
    else:
        console.print("[dim]No new targets added (already present, or all excluded).[/dim]")


def _let_user_remove_some(pivots: dict) -> dict:
    """Show the pivots numbered, ask which indices to drop, return the remaining ones."""
    numbered = []
    for kind, values in pivots.items():
        for value in values:
            numbered.append((kind, value))

    console.print()
    for index, (kind, value) in enumerate(numbered, start=1):
        console.print(f"  [{index}] {kind}: {value}")
    answer = Prompt.ask("Retirer quels indices ? (séparés par des virgules, vide = garder tout)", default="")

    excluded = set()
    if answer.strip():
        try:
            excluded = {int(part.strip()) for part in answer.split(",") if part.strip()}
        except ValueError:
            console.print("[warn]Format non reconnu, tout est conservé.[/warn]")

    remaining = {}
    for index, (kind, value) in enumerate(numbered, start=1):
        if index not in excluded:
            remaining.setdefault(kind, []).append(value)
    return remaining


def _save(ctx, source_module: str, pivots: dict) -> int:
    """Add the pivots to the targets table and return how many were new."""
    added = 0
    for kind, values in pivots.items():
        added += ctx.db.add_targets(kind, values, source_module=source_module)
    return added
