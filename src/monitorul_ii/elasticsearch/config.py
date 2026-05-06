from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(value: str | None, default: bool) -> bool:
    """Parse a boolean-shaped env var (`1/0`, `true/false`, `yes/no`).

    Empty / missing values fall back to `default`. Anything not in the
    accepted vocabulary raises — silent typos here lead to surprising
    cert-verification flips in production.
    """
    if value is None or value == "":
        return default
    norm = value.strip().lower()
    if norm in ("1", "true", "yes", "on"):
        return True
    if norm in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"invalid boolean for ES_VERIFY_CERTS: {value!r} "
        "(accepted: 1/0, true/false, yes/no, on/off)"
    )


@dataclass(frozen=True)
class ESConfig:
    """Connection settings for the Elasticsearch client.

    Mirrors `S3Config.from_env()`'s shape: a single classmethod that
    reads `ES_*` env vars, returns `None` if any required var is
    missing so callers can decide whether ES is optional or fail-fast.
    """

    url: str
    api_key: str
    verify_certs: bool = True

    @classmethod
    def from_env(cls) -> ESConfig | None:
        """Build config from `ES_URL` / `ES_API_KEY` / `ES_VERIFY_CERTS`.

        `ES_VERIFY_CERTS` is opt-out and defaults to True. Use
        `ES_VERIFY_CERTS=0` for a self-signed dev cluster; production
        should always verify.

        Returns `None` when either of the two required vars is unset
        or empty — `ES_VERIFY_CERTS` alone never decides presence.
        """
        url = os.environ.get("ES_URL")
        api_key = os.environ.get("ES_API_KEY")
        if not (url and api_key):
            return None
        verify = _env_bool(os.environ.get("ES_VERIFY_CERTS"), default=True)
        return cls(url=url, api_key=api_key, verify_certs=verify)
