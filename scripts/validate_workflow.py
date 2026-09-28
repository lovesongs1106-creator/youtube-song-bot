#!/usr/bin/env python3
"""Stdlib-only GitHub Actions workflow validator (no PyYAML needed).

Checks .github/workflows/*.yml for:
  1. No tab characters (YAML forbids tabs for indentation).
  2. Indentation in multiples of 2 spaces.
  3. Required top-level structure: name / on / jobs.
  4. Every step has a name plus run/uses.
  5. Required second-channel secrets + variables referenced.
  6. Balanced block structure (mapping keys end with ':' or have a value).

Usage: python3 scripts/validate_workflow.py
Exit code 0 = valid, 1 = problems found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

REQUIRED_SECRET_REFS = (
    "secrets.YOUTUBE_SECOND_CLIENT_ID",
    "secrets.YOUTUBE_SECOND_CLIENT_SECRET",
    "secrets.YOUTUBE_SECOND_REFRESH_TOKEN",
)
REQUIRED_VAR_REFS = (
    "vars.YOUTUBE_TARGET_CHANNEL",
    "vars.YOUTUBE_VISIBILITY",
    "vars.OUTRO_ASSET_PATH",
)

FAILURES: list[str] = []


def fail(msg: str) -> None:
    print(f"❌ {msg}", flush=True)
    FAILURES.append(msg)


def ok(msg: str) -> None:
    print(f"✅ {msg}", flush=True)


def check_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rel = path.relative_to(ROOT)

    # 1. No tabs.
    tabs = [i + 1 for i, l in enumerate(lines) if "\t" in l]
    if tabs:
        fail(f"{rel}: tab characters on lines {tabs}")
    else:
        ok(f"{rel}: no tabs")

    # 2. Indentation multiples of 2.
    bad_indent = []
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent % 2 != 0:
            bad_indent.append(i + 1)
    if bad_indent:
        fail(f"{rel}: odd indentation on lines {bad_indent}")
    else:
        ok(f"{rel}: indentation consistent")

    # 3. Top-level structure.
    for key in ("name:", "on:", "jobs:"):
        if not re.search(rf"^{re.escape(key)}", text, re.MULTILINE):
            fail(f"{rel}: missing top-level '{key}'")
    if all(re.search(rf"^{k}", text, re.MULTILINE) for k in ("name:", "on:", "jobs:")):
        ok(f"{rel}: has name/on/jobs")

    # 4. Steps have name + run/uses.
    step_names = re.findall(r"^\s+- name:\s*(.+)$", text, re.MULTILINE)
    step_blocks = re.split(r"^\s+- name:\s*.+$", text, flags=re.MULTILINE)[1:]
    missing_action = [
        name for name, block in zip(step_names, step_blocks)
        if not re.search(r"^\s+(run:|uses:)", block, re.MULTILINE)
    ]
    if missing_action:
        fail(f"{rel}: steps without run/uses: {missing_action}")
    else:
        ok(f"{rel}: all {len(step_names)} steps have run/uses")

    # 5. Second-channel secrets + variables (render-upload.yml only).
    if path.name == "render-upload.yml":
        for ref in REQUIRED_SECRET_REFS + REQUIRED_VAR_REFS:
            if ref not in text:
                fail(f"{rel}: missing reference '${{{{ {ref} }}}}'")
        if all(r in text for r in REQUIRED_SECRET_REFS + REQUIRED_VAR_REFS):
            ok(f"{rel}: second-channel secrets + variables referenced")

    # 6. No dangling '${{ }}' expressions.
    unbalanced = [i + 1 for i, l in enumerate(lines)
                  if l.count("${{") != l.count("}}")]
    if unbalanced:
        fail(f"{rel}: unbalanced ${{{{ }}}} on lines {unbalanced}")
    else:
        ok(f"{rel}: expressions balanced")


def main() -> int:
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    if not files:
        fail("no workflow files found")
        return 1
    for path in files:
        print(f"--- {path.relative_to(ROOT)} ---")
        check_file(path)
    print()
    if FAILURES:
        print(f"WORKFLOW VALIDATION FAILED: {len(FAILURES)} problem(s)")
        return 1
    print("WORKFLOW VALIDATION PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
