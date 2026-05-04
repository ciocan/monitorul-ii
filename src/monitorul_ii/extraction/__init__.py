"""Extraction pipeline (MD → structured JSON sidecar).

Step 1.5 of the build order in `docs/extraction-schema.md`. Public API kept
small; everything else is module-private.

Schema-version bumps and per-component versions all live next to the code
that produces them — bumping any version travels with the sidecars whose
body content depends on that component (see version-aware idempotency).
"""

from __future__ import annotations

from monitorul_ii.extraction.coverage import COVERAGE_VERSION, Claim
from monitorul_ii.extraction.envelope import EnvelopeMeta, split_md
from monitorul_ii.extraction.pipeline import (
    SCHEMA_VERSION,
    ExtractResult,
    extract,
)
from monitorul_ii.extraction.schema import SchemaError, validate

__all__ = [
    "COVERAGE_VERSION",
    "Claim",
    "EnvelopeMeta",
    "ExtractResult",
    "SCHEMA_VERSION",
    "SchemaError",
    "extract",
    "split_md",
    "validate",
]
