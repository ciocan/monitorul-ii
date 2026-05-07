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
    """
    if not config.verify_certs:
        warnings.filterwarnings("ignore", category=SecurityWarning)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return Elasticsearch(
        hosts=config.url,
        api_key=config.api_key,
        verify_certs=config.verify_certs,
    )
