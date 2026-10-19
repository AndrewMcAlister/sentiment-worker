"""The file's own schema, enforced before anything is written.

A file that fails its own schema is worse than a file that was never written:
the pipeline reads these files without validating them, so a wrong shape becomes
a wrong number somewhere else.  So the rule here is simple — the curated payload
is validated in memory, the bytes are written to a temporary file in the same
directory, **those bytes are parsed and validated again**, and only then is the
temporary file renamed into place.

`jsonschema` is a dependency of this worker (it is installed for Python 3.12 on
this machine); if it is missing the worker refuses to run rather than writing
files it could not check.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, List

import config


class SchemaError(RuntimeError):
    """A payload, or the schema itself, is not acceptable."""


@lru_cache(maxsize=1)
def _schema() -> dict:
    try:
        with open(config.SCHEMA_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise SchemaError(f"cannot read the schema at {config.SCHEMA_PATH}: {exc}") from None
    except ValueError as exc:
        raise SchemaError(f"the schema at {config.SCHEMA_PATH} is not JSON: {exc}") from None


@lru_cache(maxsize=1)
def _validator():
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - depends on the interpreter
        raise SchemaError(
            "the jsonschema package is not installed; refusing to write unvalidated files"
        ) from exc
    schema = _schema()
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:  # pragma: no cover - a development error
        raise SchemaError(f"schema.json is not a valid schema: {exc.message}") from None
    return jsonschema.Draft202012Validator(schema)


def schema() -> dict:
    """The parsed schema (a copy is not made; do not mutate it)."""
    return _schema()


def errors(payload: Any) -> List[str]:
    """Every reason *payload* does not match, as readable one-liners."""
    import jsonschema

    found: List[str] = []
    for error in sorted(_validator().iter_errors(payload), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(part) for part in error.absolute_path) or "<root>"
        found.append(f"{where}: {error.message}")
    return found


def validate(payload: Any, *, what: str = "payload") -> None:
    """Raise :class:`SchemaError` unless *payload* matches the schema."""
    problems = errors(payload)
    if not problems:
        return
    shown = "; ".join(problems[:5])
    more = "" if len(problems) <= 5 else f" (and {len(problems) - 5} more)"
    raise SchemaError(f"{what} does not match schema.json: {shown}{more}")


def validate_text(text: str, *, what: str = "payload") -> None:
    """Parse and validate JSON *text* — used on the bytes about to be renamed."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise SchemaError(f"{what} is not valid JSON: {exc}") from None
    validate(payload, what=what)


def validate_file(path: Path, *, what: str | None = None) -> None:
    """Parse and validate the file at *path*."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot read {path}: {exc}") from None
    validate_text(text, what=what or str(path))
