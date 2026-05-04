"""Reference-parser stub.

The full discriminated-union parser (12 variants per schema $defs) lands
when the `plenary_stenogram` extractor needs it. v0.1 ships only the
version constant so it can participate in `extractor_versions` and the
version-aware idempotency gate, with one helper for question_register's
incidental needs (none of which currently surface — the question_register
body shape carries no `references` array).

Promote individual reference shapes into this module as plenary needs
them; do not add stubs preemptively.
"""

from __future__ import annotations

REFERENCES_VERSION = "0.1.0"
