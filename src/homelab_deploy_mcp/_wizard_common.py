"""Small, dependency-free interactive prompt helpers shared by the two
wizards (config_wizard.py, target_wizard.py).

Deliberately plain stdlib input() rather than a prompt-toolkit-style
library: this project already keeps its dependency list short (mcp,
paramiko, pyyaml), and a wizard's whole point is reducing friction, not
adding a new library surface to learn or for something to go wrong in.
"""

from __future__ import annotations

import re
import sys


def prompt(message: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{message}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("  (required)")


def prompt_int(
    message: str,
    *,
    default: int,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    while True:
        raw = input(f"{message} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("  enter a whole number")
            continue
        if min_value is not None and value < min_value:
            print(f"  must be at least {min_value}")
            continue
        if max_value is not None and value > max_value:
            print(f"  must be at most {max_value}")
            continue
        return value


def prompt_yes_no(message: str, *, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{message} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  enter y or n")


def prompt_optional(message: str) -> str | None:
    """Returns None if the user just presses enter, otherwise the trimmed value."""
    value = input(f"{message} (leave blank to skip): ").strip()
    return value or None


def prompt_list(message: str, *, validator: "re.Pattern[str] | None" = None, item_hint: str = "") -> list[str]:
    """Comma-separated list, re-prompting the whole line until every item
    is non-empty and (if a validator is given) matches it."""
    while True:
        raw = input(f"{message}: ").strip()
        items = [item.strip() for item in raw.split(",") if item.strip()]
        if not items:
            print("  enter at least one value")
            continue
        if validator is not None:
            bad = [item for item in items if not validator.match(item)]
            if bad:
                print(f"  invalid: {', '.join(bad)}{(' — ' + item_hint) if item_hint else ''}")
                continue
        return items


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)
