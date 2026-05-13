#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD_ID = "monitorul-query-log-overview"
DEFAULT_TEMPLATE = ROOT / "kibana" / "dashboards" / "query-log-overview.json"

PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
INDEX_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.@*-]+$")
FIELD_NAME_RE = re.compile(r"^@?[A-Za-z_][A-Za-z0-9_.]*$")

QUERY_LOG_FIELD_DEFAULTS = {
    "QUERY_LOG_TIMESTAMP_FIELD": "timestamp",
    "QUERY_LOG_NAME_FIELD": "op",
    "QUERY_LOG_DURATION_FIELD": "took_ms",
    "QUERY_LOG_ES_DURATION_FIELD": "es_took_ms",
    "QUERY_LOG_ERROR_FIELD": "error",
    "QUERY_LOG_TOTAL_FIELD": "hits_total",
    "QUERY_LOG_SURFACE_FIELD": "surface",
    "QUERY_LOG_RANK_FUSION_FIELD": "mode",
}

MIN_DASHBOARD_API_VERSION = (9, 4)


class KibanaError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


@dataclass(frozen=True)
class KibanaConfig:
    url: str
    api_key: str | None = None
    username: str | None = None
    password: str | None = None
    space_id: str | None = None
    insecure: bool = False

    @property
    def base_url(self) -> str:
        base = self.url.rstrip("/")
        if self.space_id and self.space_id != "default":
            return f"{base}/s/{urllib.parse.quote(self.space_id)}"
        return base


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def parse_version(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def assert_declarative_dashboard_api_supported(status: dict[str, Any]) -> None:
    version = status.get("version", {}).get("number", "unknown")
    parsed = parse_version(version)
    if parsed is None:
        return

    if parsed[:2] < MIN_DASHBOARD_API_VERSION:
        min_version = ".".join(str(part) for part in MIN_DASHBOARD_API_VERSION)
        raise ValueError(
            f"Kibana {version} does not expose the declarative Dashboards API "
            f"used by this dashboard bundle. Upgrade Kibana to {min_version}+ "
            "or create the ES|QL panels manually in the Kibana UI."
        )


def _validate_index_pattern(value: str, var_name: str) -> str:
    if not value or not INDEX_PATTERN_RE.fullmatch(value):
        raise ValueError(
            f"{var_name} must be a single ES index name or wildcard pattern "
            "using only letters, numbers, '.', '_', '-', '@', and '*'"
        )
    return value


def _validate_field_name(value: str, var_name: str) -> str:
    if not value or not FIELD_NAME_RE.fullmatch(value):
        raise ValueError(
            f"{var_name} must be an ES field name using letters, numbers, "
            "'.', '_', and an optional leading '@'"
        )
    return value


def dashboard_environment(env: dict[str, str] | os._Environ[str]) -> dict[str, str]:
    query_log_index = env.get("QUERY_LOG_INDEX", "").strip()
    values = {
        "QUERY_LOG_INDEX": _validate_index_pattern(query_log_index, "QUERY_LOG_INDEX")
    }

    for name, default in QUERY_LOG_FIELD_DEFAULTS.items():
        values[name] = _validate_field_name((env.get(name) or default).strip(), name)

    return values


def render_dashboard_template(
    template_path: Path = DEFAULT_TEMPLATE,
    *,
    env: dict[str, str] | os._Environ[str] = os.environ,
) -> dict[str, Any]:
    values = dashboard_environment(env)
    text = template_path.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"unknown dashboard placeholder: {name}")
        return values[name]

    rendered = PLACEHOLDER_RE.sub(replace, text)
    if "${" in rendered:
        raise ValueError("dashboard template still contains unresolved placeholders")
    return json.loads(rendered)


def _cloud_id_to_kibana_url(cloud_id: str) -> str | None:
    parts = cloud_id.split(":", 1)
    if len(parts) != 2:
        return None
    decoded = base64.b64decode(parts[1]).decode("utf-8")
    decoded_parts = decoded.split("$")
    if len(decoded_parts) < 3 or not decoded_parts[2]:
        return None
    domain = decoded_parts[0]
    kibana_uuid = decoded_parts[2]
    host, _, port = domain.partition(":")
    port = f":{port}" if port else ":443"
    return f"https://{kibana_uuid}.{host}{port}"


def kibana_config_from_env(env: dict[str, str] | os._Environ[str]) -> KibanaConfig:
    url = (env.get("KIBANA_URL") or "").strip()
    cloud_id = (
        env.get("KIBANA_CLOUD_ID") or env.get("ELASTICSEARCH_CLOUD_ID") or ""
    ).strip()
    if not url and cloud_id:
        url = _cloud_id_to_kibana_url(cloud_id) or ""
    if not url:
        raise ValueError(
            "set KIBANA_URL, or KIBANA_CLOUD_ID with Kibana credentials, before "
            "using live Kibana commands"
        )

    return KibanaConfig(
        url=url,
        api_key=(env.get("KIBANA_API_KEY") or env.get("ELASTICSEARCH_API_KEY") or None),
        username=(
            env.get("KIBANA_USERNAME") or env.get("ELASTICSEARCH_USERNAME") or None
        ),
        password=(
            env.get("KIBANA_PASSWORD") or env.get("ELASTICSEARCH_PASSWORD") or None
        ),
        space_id=(env.get("KIBANA_SPACE_ID") or None),
        insecure=_parse_bool(env.get("KIBANA_INSECURE"), default=False),
    )


def _headers(config: KibanaConfig) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Elastic-Api-Version": "2023-10-31",
        "User-Agent": "monitorul-kibana-dashboard-helper",
        "kbn-xsrf": "true",
        "x-elastic-internal-origin": "kibana",
    }
    if config.api_key:
        headers["Authorization"] = f"ApiKey {config.api_key}"
    elif config.username and config.password:
        raw = f"{config.username}:{config.password}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    return headers


def _decode_response(data: bytes, content_type: str) -> Any:
    text = data.decode("utf-8")
    if "application/json" in content_type:
        return json.loads(text)
    return text


def kibana_request(
    config: KibanaConfig,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    ok_statuses: set[int] | None = None,
) -> Any:
    expected = ok_statuses or {200}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{config.base_url}{path}",
        data=data,
        headers=_headers(config),
        method=method,
    )
    context = ssl._create_unverified_context() if config.insecure else None

    try:
        with urllib.request.urlopen(request, context=context, timeout=30) as response:
            payload = _decode_response(
                response.read(), response.headers.get("content-type", "")
            )
            if response.status not in expected:
                raise KibanaError(
                    f"Kibana returned HTTP {response.status}",
                    status=response.status,
                    details=payload,
                )
            return payload
    except urllib.error.HTTPError as exc:
        payload = _decode_response(exc.read(), exc.headers.get("content-type", ""))
        if exc.code in expected:
            return payload
        message = payload.get("message") if isinstance(payload, dict) else str(payload)
        raise KibanaError(message, status=exc.code, details=payload) from exc
    except urllib.error.URLError as exc:
        raise KibanaError(str(exc.reason)) from exc


def test_kibana_connection(config: KibanaConfig) -> dict[str, Any]:
    status = kibana_request(config, "GET", "/api/status")
    assert_declarative_dashboard_api_supported(status)
    kibana_request(
        config,
        "GET",
        f"/api/dashboards/{urllib.parse.quote('__monitorul_missing_dashboard__', safe='')}",
        ok_statuses={200, 404},
    )
    return status


def upsert_dashboard(
    config: KibanaConfig, dashboard_id: str, dashboard: dict[str, Any]
) -> dict[str, Any]:
    body = {
        key: value for key, value in dashboard.items() if key not in {"id", "spaces"}
    }
    return kibana_request(
        config,
        "PUT",
        f"/api/dashboards/{urllib.parse.quote(dashboard_id, safe='')}",
        body=body,
        ok_statuses={200, 201},
    )


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render and deploy Kibana dashboards for Monitorul query logs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="print the rendered dashboard JSON")
    render.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)

    subparsers.add_parser(
        "test", help="test Kibana connectivity and dashboard API access"
    )

    upsert = subparsers.add_parser("upsert", help="upsert the query log dashboard")
    upsert.add_argument("--id", default=DEFAULT_DASHBOARD_ID)
    upsert.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)

    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "render":
            dashboard = render_dashboard_template(args.template)
            print(json.dumps(dashboard, indent=2, ensure_ascii=False))
            return 0

        config = kibana_config_from_env(os.environ)
        status = test_kibana_connection(config)
        version = status.get("version", {}).get("number", "unknown")
        name = status.get("name", "unknown")

        if args.command == "test":
            print(f"connected to Kibana {name} ({version})")
            return 0

        if args.command == "upsert":
            dashboard = render_dashboard_template(args.template)
            response = upsert_dashboard(config, args.id, dashboard)
            title = response.get("data", {}).get("title", dashboard.get("title"))
            print(f"upserted dashboard {args.id}: {title}")
            return 0
    except (KibanaError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, KibanaError) and exc.details:
            print(json.dumps(exc.details, indent=2), file=sys.stderr)
        return 1

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
