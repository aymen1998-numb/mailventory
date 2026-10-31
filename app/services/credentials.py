from __future__ import annotations

import re
from pathlib import Path

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Update keys in a dotenv file while preserving comments and other keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []

    for line in lines:
        match = KEY_RE.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            output.append(f"{key}={_format_value(remaining.pop(key))}")
        else:
            output.append(line)

    for key, value in remaining.items():
        output.append(f"{key}={_format_value(value)}")

    path.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _format_value(value: str) -> str:
    value = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not value:
        return ""
    if any(char in value for char in ' #"\t'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
