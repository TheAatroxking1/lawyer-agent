from pathlib import Path
from typing import Any

import yaml


def load_compose() -> dict[str, Any]:
    path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_compose_contains_required_services() -> None:
    services = load_compose()["services"]
    assert {"api", "mysql", "redis", "rabbitmq", "opensearch", "minio"} <= services.keys()


def test_stateful_services_have_healthchecks_and_volumes() -> None:
    services = load_compose()["services"]
    for name in {"mysql", "redis", "rabbitmq", "opensearch", "minio"}:
        assert "healthcheck" in services[name]
        assert "volumes" in services[name]


def test_mysql_uses_the_configured_host_port() -> None:
    mysql = load_compose()["services"]["mysql"]
    assert mysql["ports"] == ["13306:3306"]


def test_opensearch_supplies_its_required_initial_admin_password() -> None:
    opensearch = load_compose()["services"]["opensearch"]
    expected_variable = "${" + "OPENSEARCH_INITIAL_ADMIN_PASSWORD}"
    assert opensearch["environment"]["OPENSEARCH_INITIAL_ADMIN_PASSWORD"] == (
        expected_variable
    )


def test_api_runs_non_privileged_and_read_only() -> None:
    api = load_compose()["services"]["api"]
    assert api["read_only"] is True
    assert api["security_opt"] == ["no-new-privileges:true"]
