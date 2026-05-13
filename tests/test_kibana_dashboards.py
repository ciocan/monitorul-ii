from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "kibana_dashboards.py"
SPEC = importlib.util.spec_from_file_location("kibana_dashboards", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
kibana_dashboards = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = kibana_dashboards
SPEC.loader.exec_module(kibana_dashboards)


def _env(**overrides: str) -> dict[str, str]:
    env = {"QUERY_LOG_INDEX": "mo_query_log"}
    env.update(overrides)
    return env


def test_render_dashboard_template_replaces_query_log_index_and_defaults() -> None:
    dashboard = kibana_dashboards.render_dashboard_template(env=_env())

    rendered = json.dumps(dashboard)
    assert "${" not in rendered
    assert "mo_query_log" in rendered
    assert "FROM mo_query_log" in rendered
    assert "FROM `mo_query_log`" not in rendered
    assert "`timestamp`" in rendered
    assert "COUNT(CASE(`error` IS NOT NULL, 1, null))" in rendered
    assert "BY `op`, `surface`" in rendered
    assert "PERCENTILE(`took_ms`, 99)" in rendered
    assert "app_overhead_ms = `took_ms` - `es_took_ms`" in rendered
    assert '"id": "surface-health"' in rendered
    assert '"id": "retrieval-mode-health"' in rendered
    assert '"id": "quality-rates-over-time"' in rendered
    assert '"type": "duration"' not in rendered
    assert '"suffix": " ms"' in rendered
    assert dashboard["title"] == "Monitorul Query Logs"
    assert len(dashboard["panels"]) == 18


def test_render_dashboard_template_allows_field_overrides() -> None:
    dashboard = kibana_dashboards.render_dashboard_template(
        env=_env(
            QUERY_LOG_DURATION_FIELD="latency_ms",
            QUERY_LOG_NAME_FIELD="query.name",
        )
    )

    rendered = json.dumps(dashboard)
    assert "PERCENTILE(`latency_ms`, 95)" in rendered
    assert "BY `query.name`" in rendered


def test_render_dashboard_template_requires_query_log_index() -> None:
    with pytest.raises(ValueError, match="QUERY_LOG_INDEX"):
        kibana_dashboards.render_dashboard_template(env={})


def test_render_dashboard_template_rejects_esql_injection_in_index() -> None:
    with pytest.raises(ValueError, match="QUERY_LOG_INDEX"):
        kibana_dashboards.render_dashboard_template(
            env={"QUERY_LOG_INDEX": "mo_query_log | LIMIT 1"}
        )


def test_kibana_config_requires_kibana_url_or_cloud_id() -> None:
    with pytest.raises(ValueError, match="KIBANA_URL"):
        kibana_dashboards.kibana_config_from_env({})


def test_declarative_dashboard_api_rejects_kibana_before_9_4() -> None:
    status = {"version": {"number": "9.2.1"}}

    with pytest.raises(ValueError, match="Upgrade Kibana to 9.4"):
        kibana_dashboards.assert_declarative_dashboard_api_supported(status)


def test_declarative_dashboard_api_allows_kibana_9_4() -> None:
    status = {"version": {"number": "9.4.0"}}

    kibana_dashboards.assert_declarative_dashboard_api_supported(status)


def test_upsert_dashboard_accepts_created_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"data": {"title": "Dashboard"}}

    monkeypatch.setattr(kibana_dashboards, "kibana_request", fake_request)

    config = kibana_dashboards.KibanaConfig(url="https://kibana.example")
    response = kibana_dashboards.upsert_dashboard(
        config,
        "my dashboard/id",
        {"id": "ignored", "spaces": ["default"], "title": "Dashboard"},
    )

    assert response == {"data": {"title": "Dashboard"}}
    assert calls == [
        (
            (
                config,
                "PUT",
                "/api/dashboards/my%20dashboard%2Fid",
            ),
            {
                "body": {"title": "Dashboard"},
                "ok_statuses": {200, 201},
            },
        )
    ]
