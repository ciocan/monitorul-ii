"""Build an `Elasticsearch` client from `ESConfig`.

Kept separate from `bootstrap.py` so test fixtures can construct a
client (or mock one) without pulling in the bootstrap helpers.
"""

from __future__ import annotations

from elasticsearch import Elasticsearch

from monitorul_ii.elasticsearch.config import ESConfig


def build_client(config: ESConfig) -> Elasticsearch:
    """Construct an `Elasticsearch` from the ES_* env-derived config.

    The 8.x client takes the API key as an opaque encoded string (or
    a `(id, key)` tuple); we pass through whatever the user set in
    `ES_API_KEY`. `verify_certs=False` is the toggle for self-signed
    dev clusters; production should always leave it True.
    """
    return Elasticsearch(
        hosts=config.url,
        api_key=config.api_key,
        verify_certs=config.verify_certs,
    )
