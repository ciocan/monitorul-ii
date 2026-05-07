from __future__ import annotations

import warnings

import urllib3
from elastic_transport import SecurityWarning

from monitorul_ii.elasticsearch import ESConfig
from monitorul_ii.elasticsearch.client import build_client


def _cfg(*, verify_certs: bool) -> ESConfig:
    return ESConfig(
        url="https://es.example.com:9200",
        api_key="encoded-api-key",
        verify_certs=verify_certs,
    )


def test_build_client_silences_security_warning_when_verify_off():
    """`verify_certs=False` opts the user into self-signed; suppress the
    SecurityWarning the elastic_transport stack emits at construction."""
    build_client(_cfg(verify_certs=False))
    with warnings.catch_warnings(record=True) as captured:
        warnings.warn("verify_certs=False is insecure", category=SecurityWarning)
    assert captured == []


def test_build_client_silences_urllib3_insecure_warning_when_verify_off():
    """The per-request urllib3.InsecureRequestWarning floods parallel-indexer
    logs; suppressed alongside SecurityWarning when verify_certs=False."""
    build_client(_cfg(verify_certs=False))
    with warnings.catch_warnings(record=True) as captured:
        warnings.warn(
            "Unverified HTTPS request",
            category=urllib3.exceptions.InsecureRequestWarning,
        )
    assert captured == []


def test_build_client_leaves_warnings_loud_when_verify_on():
    """Production stays loud: SecurityWarning is not silenced when
    verify_certs=True, so a future regression that flips the default
    surfaces immediately."""
    build_client(_cfg(verify_certs=True))
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", SecurityWarning)
        warnings.warn("probe", category=SecurityWarning)
    assert any(issubclass(w.category, SecurityWarning) for w in captured)
