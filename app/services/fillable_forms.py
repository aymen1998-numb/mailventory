"""Optional bridge to the external ``commonforms`` command.

CommonForms (https://github.com/jbarrow/commonforms) detects where form fields
*should* go in a digital PDF and writes a new PDF with empty fillable boxes. It is
not OCR: it does not read values, line items or totals.

The upstream project ships no license file, so nothing from it is vendored,
imported or pinned here. The user installs it in an isolated environment and this
module only shells out to the command:

    pipx install commonforms

Enable it with ``COMMONFORMS_ENABLED=true`` in ``.env``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 300
FILLABLE_SUFFIX = "-fillable.pdf"


class CommonFormsError(RuntimeError):
    """Raised when the optional commonforms command cannot produce a PDF."""


def resolve_command(command: str) -> str | None:
    return shutil.which(command)


def is_available(command: str) -> bool:
    return resolve_command(command) is not None


def fillable_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}{FILLABLE_SUFFIX}")


def prepare_fillable_form(
    source: Path,
    destination: Path | None = None,
    *,
    command: str = "commonforms",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    """Run ``commonforms <source> <destination>`` and return the written PDF."""
    executable = resolve_command(command)
    if executable is None:
        raise CommonFormsError(
            f"The '{command}' command was not found. Install it in isolation first: "
            "pipx install commonforms"
        )
    if not source.exists():
        raise CommonFormsError(f"Source PDF is missing: {source}")

    target = destination or fillable_path(source)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        completed = subprocess.run(
            [executable, str(source), str(target)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommonFormsError(
            f"commonforms timed out after {timeout}s (first run downloads model weights)."
        ) from exc
    except OSError as exc:
        raise CommonFormsError(f"Could not run commonforms: {exc}") from exc

    if completed.returncode != 0 or not target.exists():
        output = (completed.stderr or completed.stdout or "").strip().splitlines()
        detail = output[-1] if output else f"exit code {completed.returncode}"
        raise CommonFormsError(f"commonforms failed: {detail[:300]}")
    return target
