import json
import re
from pathlib import Path
from typing import Any

import yaml


def load_compose() -> dict[str, Any]:
    path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_dockerfile() -> str:
    path = Path(__file__).parents[3] / "deploy" / "docker" / "backend.Dockerfile"
    return path.read_text(encoding="utf-8")


def test_compose_contains_required_services() -> None:
    services = load_compose()["services"]
    assert {"api", "mysql", "redis", "rabbitmq", "opensearch", "minio"} <= services.keys()


def test_stateful_services_have_healthchecks_and_volumes() -> None:
    services = load_compose()["services"]
    for name in {"mysql", "redis", "rabbitmq", "opensearch", "minio"}:
        healthcheck = services[name]["healthcheck"]
        assert healthcheck["test"]
        assert float(str(healthcheck["interval"]).removesuffix("s")) > 0
        assert float(str(healthcheck["timeout"]).removesuffix("s")) > 0
        assert healthcheck["retries"] > 0
        assert "volumes" in services[name]


def test_mysql_uses_the_configured_host_port() -> None:
    mysql = load_compose()["services"]["mysql"]
    assert mysql["ports"] == ["127.0.0.1:13306:3306"]


def test_opensearch_supplies_its_required_initial_admin_password() -> None:
    opensearch = load_compose()["services"]["opensearch"]
    expected_variable = "${" + "OPENSEARCH_INITIAL_ADMIN_PASSWORD}"
    assert opensearch["environment"]["OPENSEARCH_INITIAL_ADMIN_PASSWORD"] == (
        expected_variable
    )


def test_opensearch_resolves_bulk_audit_events_without_logging_request_bodies() -> None:
    environment = load_compose()["services"]["opensearch"]["environment"]

    assert environment["plugins.security.audit.config.resolve_bulk_requests"] == "true"
    assert environment["plugins.security.audit.config.log_request_body"] == "false"
    assert environment.get("plugins.security.audit.enabled") != "false"
    assert "REQUEST_AUDIT" not in environment.get(
        "plugins.security.audit.config.disabled_transport_categories", ""
    )
    assert "plugins.security.audit.config.ignore_requests" not in environment


def test_api_runs_non_privileged_and_read_only() -> None:
    api = load_compose()["services"]["api"]
    assert api["read_only"] is True
    assert api["security_opt"] == ["no-new-privileges:true"]


def test_api_ai_configuration_uses_internal_search_and_file_secret() -> None:
    compose = load_compose()
    api = compose["services"]["api"]
    environment = api["environment"]
    assert environment["LAWYER_OPENSEARCH_URL"] == (
        "${LAWYER_OPENSEARCH_URL:-http://opensearch:9200}"
    )
    assert environment["LAWYER_EMBEDDING_MODEL_REF"] == (
        "${LAWYER_EMBEDDING_MODEL_REF:-IEITYuan/Yuan-embedding-2.0-zh}"
    )
    assert environment["LAWYER_EMBEDDING_DIMENSION"] == "${LAWYER_EMBEDDING_DIMENSION:-1792}"
    assert "LAWYER_DEEPSEEK_API_KEY" not in environment
    assert environment["LAWYER_DEEPSEEK_API_KEY_FILE"] == (
        "/run/secrets/lawyer_deepseek_config"
    )
    assert {"source": "lawyer_deepseek_config", "target": "lawyer_deepseek_config"} in (
        api["secrets"]
    )
    assert compose["secrets"]["lawyer_deepseek_config"]["file"] == (
        "${LAWYER_DEEPSEEK_CONFIG_PATH:-./deepseek.config.example.json}"
    )
    template = Path(__file__).parents[3] / "deploy/deepseek.config.example.json"
    assert json.loads(template.read_text(encoding="utf-8")) == {"api_key": ""}


def test_backend_image_uses_uid_10001_before_its_command() -> None:
    dockerfile = load_dockerfile()
    user_instruction = "\nUSER 10001\n"
    user_position = dockerfile.rfind(user_instruction)
    command_position = dockerfile.index("\nCMD ")

    assert "useradd --create-home --uid 10001 app" in dockerfile
    assert user_position != -1
    assert user_position < command_position


def test_backend_image_installs_frozen_cpu_embedding_production_extra() -> None:
    dockerfile = load_dockerfile()
    assert "uv==0.12.7" in dockerfile
    assert "RUN uv sync --frozen --no-dev --extra embedding-cpu" in dockerfile
    assert "COPY artifacts" not in dockerfile


def test_api_model_cache_is_read_only_and_never_auto_created() -> None:
    api = load_compose()["services"]["api"]
    assert api["environment"]["LAWYER_EMBEDDING_DEVICE"] == "${LAWYER_EMBEDDING_DEVICE:-cpu}"
    assert api["environment"]["LAWYER_EMBEDDING_LOCAL_FILES_ONLY"] == (
        "${LAWYER_EMBEDDING_LOCAL_FILES_ONLY:-true}"
    )
    assert api["environment"]["HF_HOME"] == "/models/huggingface"
    mounts = [volume for volume in api["volumes"]
              if isinstance(volume, dict) and volume.get("target") == "/models/huggingface"]
    assert mounts == [{
        "type": "bind", "source": "${LAWYER_MODEL_CACHE_DIR:-../artifacts/model-cache}",
        "target": "/models/huggingface", "read_only": True,
        "bind": {"create_host_path": False},
    }]


def test_compose_example_declares_cpu_offline_model_cache_settings() -> None:
    path = Path(__file__).parents[3] / "deploy" / "compose.env.example"
    example = path.read_text(encoding="utf-8")
    assert "LAWYER_EMBEDDING_DEVICE=cpu\n" in example
    assert "LAWYER_EMBEDDING_LOCAL_FILES_ONLY=true\n" in example
    assert "LAWYER_MODEL_CACHE_DIR=../artifacts/model-cache\n" in example


def test_every_published_port_is_bound_to_loopback_only() -> None:
    services = load_compose()["services"]

    published_ports = [
        port
        for service in services.values()
        for port in service.get("ports", [])
    ]

    assert published_ports
    for port in published_ports:
        assert isinstance(port, str)
        port_parts = port.rsplit(":", 2)
        assert len(port_parts) == 3, f"published port lacks a host IP: {port}"
        host_ip, host_port, container_port = port_parts
        assert host_ip == "127.0.0.1"
        assert host_port.isdecimal()
        assert container_port.isdecimal()


def test_all_external_images_are_pinned_to_sha256_digests() -> None:
    digest_pattern = re.compile(r"@sha256:[0-9a-f]{64}$")
    dockerfile_from = next(
        line for line in load_dockerfile().splitlines() if line.startswith("FROM ")
    ).split()[1]
    stateful_images = [
        load_compose()["services"][name]["image"]
        for name in {"mysql", "redis", "rabbitmq", "opensearch", "minio"}
    ]

    assert digest_pattern.search(dockerfile_from)
    assert all(digest_pattern.search(image) for image in stateful_images)
