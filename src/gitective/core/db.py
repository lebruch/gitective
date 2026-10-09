"""SQLite storage: one database file per user (not per working directory).

Two kinds of tables:
- one table per module, named after it ('keys', 'emails'...), declared by
  the module's RESULT_SCHEMA and created by ensure_schema() at startup;
  each run appends to it, so a table is that module's full history;
- 'targets': the curated pool of owners/repos/gists/workflows that
  --from-targets reads back.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import platformdirs

_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.DOTALL)
_TABLE_CONSTRAINT_KEYWORDS = ("UNIQUE", "PRIMARY", "FOREIGN", "CHECK", "CONSTRAINT")

TARGETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    source_module TEXT,
    added_at TEXT NOT NULL,
    note TEXT,
    UNIQUE(kind, value)
);

CREATE INDEX IF NOT EXISTS idx_targets_kind ON targets(kind);
"""


def _default_db_path() -> Path:
    """Return the database path in the user's data directory, creating the folder."""
    folder = Path(platformdirs.user_data_dir("gitective", appauthor=False))
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "gitective.db"


def _split_column_definitions(body: str) -> list:
    """Split a CREATE TABLE body on commas, ignoring commas inside parentheses."""
    parts = []
    current = ""
    depth = 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return parts


class Database:
    """Thin wrapper around the SQLite connection, used by modules through ctx.db."""

    def __init__(self, path: Path = None):
        """Open (or create) the database and make sure 'targets' exists."""
        self.path = path or _default_db_path()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row  # rows behave like dicts: row["column"]
        self.conn.executescript(TARGETS_SCHEMA)
        self.conn.commit()

    # -- module tables --------------------------------------------------------

    def ensure_schema(self, sql: str):
        """Create a module's tables, and add any column missing from an existing table."""
        self.conn.executescript(sql)
        self.conn.commit()
        self._add_missing_columns(sql)

    def _add_missing_columns(self, sql: str):
        """ALTER TABLE ADD COLUMN for every column declared in `sql` but absent from the table."""
        for match in _CREATE_TABLE_RE.finditer(sql):
            table, body = match.group(1), match.group(2)
            existing = self.column_names(table)
            for definition in _split_column_definitions(body):
                words = definition.split()
                if not words:
                    continue
                first_word = words[0].split("(")[0].upper()  # 'UNIQUE(owner,' -> 'UNIQUE'
                if first_word in _TABLE_CONSTRAINT_KEYWORDS:
                    continue  # a table constraint, not a column
                name = words[0].strip('"')
                if name in existing:
                    continue
                # Constraints (NOT NULL, UNIQUE...) can't always be added to
                # existing rows, so the column is added as plain nullable.
                column_type = words[1] if len(words) > 1 else "TEXT"
                self.conn.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {column_type}')
        self.conn.commit()

    def column_names(self, table: str) -> set:
        """Return the column names of a table."""
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def insert_many(self, table: str, rows: list, or_ignore: bool = False):
        """Insert rows (dicts), filling 'found_at' automatically when the table has it.

        or_ignore: silently skip rows that would break a UNIQUE constraint
        (used by tables that deduplicate, such as 'emails' or 'activity').
        `table` and the row keys must come from module code, never from user input.
        """
        if not rows:
            return
        columns = list(rows[0].keys())
        if "found_at" not in columns and "found_at" in self.column_names(table):
            columns.append("found_at")
        found_at = datetime.now(timezone.utc).isoformat()
        values = []
        for row in rows:
            values.append(tuple(row.get(column, found_at if column == "found_at" else None)
                                for column in columns))

        # Column names are quoted so SQL keywords (e.g. 'commit') work as names.
        column_list = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        verb = "INSERT OR IGNORE INTO" if or_ignore else "INSERT INTO"
        self.conn.executemany(f"{verb} {table} ({column_list}) VALUES ({placeholders})", values)
        self.conn.commit()

    def execute_many(self, sql: str, params_list: list):
        """Run one SQL statement for each parameter tuple (e.g. a batch of UPDATEs)."""
        if not params_list:
            return
        self.conn.executemany(sql, params_list)
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list:
        """Run a SELECT and return all rows."""
        return self.conn.execute(sql, params).fetchall()

    def table_names(self) -> set:
        """Return the names of all tables in the database."""
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        return {row["name"] for row in rows}

    def clear_table(self, table: str):
        """Delete every row of a table (`table` must have been checked against table_names())."""
        self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    # -- targets pool ---------------------------------------------------------

    def add_targets(self, kind: str, values: list, source_module: str | None = None,
                    note: str | None = None) -> int:
        """Add values to the targets pool (already present ones are ignored); return how many were new."""
        added = 0
        now = datetime.now(timezone.utc).isoformat()
        for value in values:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO targets (kind, value, source_module, added_at, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, value, source_module, now, note),
            )
            added += cursor.rowcount
        self.conn.commit()
        return added

    def target_values(self, kind: str) -> list:
        """Return every value of one kind ('owner', 'repo'...) from the targets pool."""
        rows = self.query("SELECT value FROM targets WHERE kind = ? ORDER BY value", (kind,))
        return [row["value"] for row in rows]
