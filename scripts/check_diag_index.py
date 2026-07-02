#!/usr/bin/env python3
"""Assert that the ``## Sessions`` table in ``docs/diag/README.md`` matches
the actual subdirectory list under ``docs/diag/``.

Fails (exit 1) and prints a unified diff if either side is missing a row.
Run before opening a session PR; see ``docs/diag/README.md``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DIAG_DIR = Path(__file__).resolve().parent.parent / "docs" / "diag"
README = DIAG_DIR / "README.md"

# Subdirectory name shape: YYYY-MM-DD_<bug-id>_<topic>
SUBDIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[A-Za-z0-9-]+_[A-Za-z0-9-]+$")

# Markdown link in the Link column: [text](path) — we pull `path` and grep
# the YYYY-MM-DD_<bug>_<topic> token out of it.
LINK_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}_[A-Za-z0-9-]+_[A-Za-z0-9-]+")


def list_subdirs() -> set[str]:
    return {
        p.name for p in DIAG_DIR.iterdir() if p.is_dir() and SUBDIR_RE.match(p.name)
    }


def list_table_rows() -> set[str]:
    """Extract subdir tokens from the ``## Sessions`` table.

    A row contributes its first matching ``YYYY-MM-DD_<bug>_<topic>`` token,
    typically found in the Link column. Rows that say ``_none yet_`` or
    similar placeholders contribute nothing.
    """
    text = README.read_text(encoding="utf-8")
    sessions_header = "## Sessions"
    next_header_re = re.compile(r"^## ", re.MULTILINE)

    start = text.find(sessions_header)
    if start == -1:
        sys.exit(f"missing `## Sessions` heading in {README}")

    after_header = start + len(sessions_header)
    match = next_header_re.search(text, after_header)
    end = match.start() if match else len(text)
    section = text[after_header:end]

    rows: set[str] = set()
    for raw in section.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        if line.startswith("|--") or line.startswith("|---"):
            continue
        if "Date" in line and "Question" in line:
            continue
        token = LINK_TOKEN_RE.search(line)
        if token:
            rows.add(token.group(0))
    return rows


def main() -> int:
    if not DIAG_DIR.is_dir():
        # Empty diag tree is fine — nothing to check.
        return 0

    subdirs = list_subdirs()
    rows = list_table_rows()

    missing_from_table = subdirs - rows
    missing_from_disk = rows - subdirs

    if not missing_from_table and not missing_from_disk:
        return 0

    print(f"docs/diag/README.md ## Sessions table is out of sync with {DIAG_DIR}:")
    for m in sorted(missing_from_table):
        print(f"  + directory exists but no table row: {m}")
    for m in sorted(missing_from_disk):
        print(f"  - table row exists but no directory: {m}")
    print()
    print("Add a row for each new session directory (or remove the row if")
    print("the directory was removed). See docs/diag/README.md § Drift-proof index.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
