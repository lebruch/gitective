"""Shared rich console, so every module prints with the same color styles.

Styles: [info] cyan, [warn] yellow, [err] bold red, [ok] bold green.
"""

from rich.console import Console
from rich.theme import Theme

console = Console(theme=Theme({
    "info": "cyan",
    "warn": "yellow",
    "err": "bold red",
    "ok": "bold green",
}))
