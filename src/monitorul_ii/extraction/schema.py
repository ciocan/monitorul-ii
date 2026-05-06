"""JSON Schema validation for extraction sidecars.

Loads the canonical schema once at import. Validation runs *pre-write*
(see Q3): a failure means the dispatcher refuses to write the sidecar and
optionally dumps the rejected dict to `<basename>.rejected.json` for
inspection. The error message is formatted with path + offending value so
you don't have to decode jsonschema's default messages.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as _JSValidationError

# Schema file lives next to this module, one level up.
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "extraction_schema.json"


class SchemaError(Exception):
    """Raised when a sidecar fails JSON Schema validation.

    `args[0]` is a single human-readable string with the path and offending
    value already formatted; iterate `errors` for the full list.
    """

    def __init__(self, message: str, errors: list[str]) -> None:
        super().__init__(message)
        self.errors = errors


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_path() -> Path:
    """Filesystem path to the canonical schema (test/debug helper)."""
    return _SCHEMA_PATH


def schema_dict() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(sidecar: dict[str, Any]) -> None:
    """Validate a sidecar dict; raise SchemaError on first violation found.

    All errors collected up front so you see every problem in one pass
    instead of fix-rerun-fix-rerun.
    """
    v = _validator()
    errors = sorted(v.iter_errors(sidecar), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    formatted = [_format_error(e) for e in errors]
    raise SchemaError(formatted[0], formatted)


def _format_error(err: _JSValidationError) -> str:
    path = "/".join(str(p) for p in err.absolute_path) or "<root>"
    val = _truncate(err.instance)
    return f"{path}: {err.message}  [value={val}]"


def _truncate(value: Any, limit: int = 80) -> str:
    s = repr(value)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s
