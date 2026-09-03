from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
import pytest_asyncio

from lawyer_agent.infrastructure.messaging.topology import (
    RabbitManagementClient,
    is_safe_test_vhost,
)


@dataclass(frozen=True, slots=True)
class RabbitTestVhost:
    name: str
    amqp_url: str
    management: RabbitManagementClient
    runner_user: str
    runner_password: str


def _admin_credentials() -> tuple[str, str, str]:
    base_url = os.getenv("LAWYER_TEST_RABBITMQ_MANAGEMENT_URL", "http://127.0.0.1:15672")
    user = os.getenv("LAWYER_TEST_RABBITMQ_ADMIN_USER")
    password = os.getenv("LAWYER_TEST_RABBITMQ_ADMIN_PASSWORD")
    if not user or not password:
        env_path = Path(__file__).parents[4] / "deploy" / ".env"
        if not env_path.is_file():
            pytest.skip("deploy/.env is required unless admin credentials are configured")
        user = "lawyer"
        password = ""
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = raw_line.partition("=")
            if separator and key.strip() == "RABBITMQ_PASSWORD":
                password = value.strip()
                break
        if not password:
            pytest.skip("RABBITMQ_PASSWORD is missing from deploy/.env")
    assert user is not None and password is not None
    return base_url, user, password


async def create_isolated_vhost(name: str) -> RabbitTestVhost:
    base_url, admin_user, admin_password = _admin_credentials()
    admin = RabbitManagementClient(base_url, admin_user, admin_password)
    await admin.create_vhost(name)
    runner_user = f"runner_{uuid4().hex[:16]}"
    runner_password = secrets.token_urlsafe(24)
    try:
        await admin.create_user(runner_user, runner_password)
        await admin.grant_permissions(name, runner_user)
    except BaseException:
        await admin.delete_user(runner_user)
        await admin.delete_vhost(name)
        raise
    amqp_url = (
        f"amqp://{quote(runner_user, safe='')}:{quote(runner_password, safe='')}"
        f"@127.0.0.1:5672/{quote(name, safe='')}"
    )
    return RabbitTestVhost(name, amqp_url, admin, runner_user, runner_password)


async def delete_exact_vhost(resource: RabbitTestVhost) -> None:
    name = resource.name
    suffix = name.removeprefix("lawyer_test_")
    if not is_safe_test_vhost(name) or name != f"lawyer_test_{suffix}":
        raise RuntimeError("refusing to delete an unsafe RabbitMQ vhost name")
    await resource.management.delete_vhost(name)
    await resource.management.delete_user(resource.runner_user)


@pytest_asyncio.fixture
async def rabbit_vhost() -> AsyncIterator[RabbitTestVhost]:
    suffix = uuid4().hex
    name = f"lawyer_test_{suffix}"
    if not is_safe_test_vhost(name) or name != f"lawyer_test_{suffix}":
        raise RuntimeError("refusing unsafe RabbitMQ vhost name")
    resource = await create_isolated_vhost(name)
    try:
        yield resource
    finally:
        await delete_exact_vhost(resource)
