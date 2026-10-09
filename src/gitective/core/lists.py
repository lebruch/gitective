"""Parsing of list arguments such as -u/-o/-r.

Values are comma-separated. Any entry starting with '@' is a path to a file
with one value per line (blank lines and '#' comments are ignored). Both can
be mixed:

    -u alice,bob
    -u @users.txt
    -u alice,@more_users.txt,bob
"""

import sys


def parse_list_arg(raw: str | None) -> list:
    """Split a comma-separated argument into values, expanding '@file' entries."""
    values = []
    if not raw:
        return values
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith("@"):
            values.extend(_read_values_file(entry[1:]))
        else:
            values.append(entry)
    return values


def _read_values_file(path: str) -> list:
    """Read one value per line from a file, skipping blank lines and comments."""
    values = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    values.append(line)
    except OSError as exc:
        sys.exit(f"[!] Couldn't read file '{path}': {exc}")
    return values
