"""Build an `Elasticsearch` client from `ESConfig`.

Kept separate from `bootstrap.py` so test fixtures can construct a
client (or mock one) without pulling in the bootstrap helpers.
"""

from __future__ import annotations

import warnings

import urllib3
from elastic_transport import SecurityWarning
from elasticsearch import Elasticsearch

from monitorul_ii.elasticsearch.config import ESConfig

# Transport-level resilience defaults. The indexer's `-j N` parallel
# path issues bulk + delete_by_query round-trips concurrently; under
# load the cluster occasionally times out a slow shard or briefly
# returns 429 (es_rejected_execution_exception). Both are transient,
# both are safe to retry — bulk upserts are idempotent because they
# key on `_id`. Pre-fix: a `monitorul-ii index pdfs/ --force -j 16`
# run on 5,556 sidecars produced 3 transient failures with no auto-
# recovery; post-fix the same shape of transient is auto-retried by
# the transport before reaching the indexer's bulk-error branch.
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_ON_TIMEOUT = True
DEFAULT_REQUEST_TIMEOUT = 30  # seconds; 8.x default is 10s


def build_client(config: ESConfig) -> Elasticsearch:
    """Construct an `Elasticsearch` from the ES_* env-derived config.

    The 8.x client takes the API key as an opaque encoded string (or
    a `(id, key)` tuple); we pass through whatever the user set in
    `ES_API_KEY`. `verify_certs=False` is the toggle for self-signed
    dev clusters; production should always leave it True.

    When `verify_certs=False` is in effect, silence the two warnings
    the stack emits on every request — `elastic_transport.SecurityWarning`
    (one-shot at client construction) and `urllib3.InsecureRequestWarning`
    (one per HTTPS request, drowns parallel-indexer output). The user
    has explicitly opted in via `ES_VERIFY_CERTS=0`; production paths
    keep `verify_certs=True` and stay loud.

    Wires up transport-level retries (`max_retries=3`,
    `retry_on_timeout=True`, `request_timeout=30s`) so the parallel
    indexer auto-recovers from the connection-timeout / brief-429
    class of transient failures. Bulk-helper-level 429 retries are
    layered on top in `indexer._bulk_upsert(...)`.
    """
    if not config.verify_certs:
        warnings.filterwarnings("ignore", category=SecurityWarning)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return Elasticsearch(
        hosts=config.url,
        api_key=config.api_key,
        verify_certs=config.verify_certs,
        max_retries=DEFAULT_MAX_RETRIES,
        retry_on_timeout=DEFAULT_RETRY_ON_TIMEOUT,
        request_timeout=DEFAULT_REQUEST_TIMEOUT,
    )
