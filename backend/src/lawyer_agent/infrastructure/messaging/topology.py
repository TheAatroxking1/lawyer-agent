from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

_VHOST_PATTERN = re.compile(r"lawyer_test_[a-f0-9]{32}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class RabbitTopologyV1:
    main_exchange: str = "lawyer.ai.jobs.v1"
    retry_exchange: str = "lawyer.ai.jobs.retry.v1"
    dlx_exchange: str = "lawyer.ai.jobs.dlx.v1"
    main_queue: str = "lawyer.ai.jobs.main.v1"
    retry_queues: tuple[tuple[str, int, str], ...] = (
        ("lawyer.ai.jobs.retry.5s.v1", 5_000, "ai.job.retry.5s"),
        ("lawyer.ai.jobs.retry.30s.v1", 30_000, "ai.job.retry.30s"),
        ("lawyer.ai.jobs.retry.120s.v1", 120_000, "ai.job.retry.120s"),
    )
    dlq_queue: str = "lawyer.ai.jobs.dlq.v1"
    execute_key: str = "ai.job.execute"
    dead_key: str = "ai.job.dead"
    policy_name: str = "ai-job-v1-policy"

    @property
    def all_queues(self) -> tuple[str, ...]:
        return (self.main_queue, self.dlq_queue, *(name for name, _, _ in self.retry_queues))

    @property
    def all_exchanges(self) -> tuple[str, ...]:
        return (self.main_exchange, self.retry_exchange, self.dlx_exchange)


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    name: str
    durable: bool
    arguments: dict[str, object]

    @property
    def is_quorum(self) -> bool:
        return self.arguments.get("x-queue-type") == "quorum"


@dataclass(frozen=True, slots=True)
class ExchangeSnapshot:
    name: str
    type: str
    durable: bool


@dataclass(frozen=True, slots=True)
class TopologySnapshot:
    queues: tuple[QueueSnapshot, ...]
    exchanges: tuple[ExchangeSnapshot, ...]


class RabbitManagementClient:
    """HTTP management-API client used only by preflight and tests."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        if not base_url.startswith("http://") and not base_url.startswith("https://"):
            raise ValueError("management base URL must be absolute HTTP(S)")
        self._base_url = base_url.rstrip("/")
        self._auth = (username, password)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
    ) -> Any:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                f"{self._base_url}/api/{path}",
                json=json,
                auth=self._auth,
                timeout=15.0,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    def _vhost_segment(self, vhost: str) -> str:
        return quote(vhost, safe="")

    async def create_vhost(self, vhost: str) -> None:
        await self._request("PUT", f"vhosts/{self._vhost_segment(vhost)}")

    async def delete_vhost(self, vhost: str) -> None:
        await self._request("DELETE", f"vhosts/{self._vhost_segment(vhost)}")

    async def create_user(self, username: str, password: str) -> None:
        await self._request(
            "PUT",
            f"users/{quote(username, safe='')}",
            json={"password": password, "tags": "none"},
        )

    async def grant_permissions(self, vhost: str, username: str) -> None:
        await self._request(
            "PUT",
            f"permissions/{self._vhost_segment(vhost)}/{quote(username, safe='')}",
            json={"configure": ".*", "write": ".*", "read": ".*"},
        )

    async def delete_user(self, username: str) -> None:
        await self._request("DELETE", f"users/{quote(username, safe='')}")

    async def declare_exchange(
        self,
        vhost: str,
        name: str,
        *,
        exchange_type: str,
        durable: bool = True,
        arguments: dict[str, object] | None = None,
    ) -> None:
        await self._request(
            "PUT",
            f"exchanges/{self._vhost_segment(vhost)}/{quote(name, safe='')}",
            json={
                "type": exchange_type,
                "durable": durable,
                "arguments": arguments or {},
                "auto_delete": False,
                "internal": False,
            },
        )

    async def declare_queue(
        self,
        vhost: str,
        name: str,
        *,
        arguments: dict[str, object] | None = None,
    ) -> None:
        await self._request(
            "PUT",
            f"queues/{self._vhost_segment(vhost)}/{quote(name, safe='')}",
            json={"durable": True, "arguments": arguments or {}, "auto_delete": False},
        )

    async def bind(
        self,
        vhost: str,
        exchange: str,
        queue: str,
        routing_key: str,
    ) -> None:
        await self._request(
            "POST",
            (
                f"bindings/{self._vhost_segment(vhost)}/e/{quote(exchange, safe='')}"
                f"/q/{quote(queue, safe='')}"
            ),
            json={"routing_key": routing_key, "arguments": {}},
        )

    async def declare_policy(
        self,
        vhost: str,
        name: str,
        *,
        pattern: str,
        definition: dict[str, object],
        apply_to: str = "queues",
        priority: int = 100,
    ) -> None:
        await self._request(
            "PUT",
            f"policies/{self._vhost_segment(vhost)}/{quote(name, safe='')}",
            json={
                "pattern": pattern,
                "definition": definition,
                "apply-to": apply_to,
                "priority": priority,
            },
        )

    async def snapshot(self, vhost: str) -> TopologySnapshot:
        queues_data = await self._request("GET", f"queues/{self._vhost_segment(vhost)}")
        exchanges_data = await self._request("GET", f"exchanges/{self._vhost_segment(vhost)}")
        assert isinstance(queues_data, list)
        assert isinstance(exchanges_data, list)
        queues = tuple(
            QueueSnapshot(
                name=str(item["name"]),
                durable=bool(item.get("durable")),
                arguments={
                    str(key): value
                    for key, value in (item.get("arguments") or {}).items()
                },
            )
            for item in queues_data
        )
        exchanges = tuple(
            ExchangeSnapshot(
                name=str(item["name"]),
                type=str(item.get("type") or ""),
                durable=bool(item.get("durable")),
            )
            for item in exchanges_data
            if not item.get("name", "").startswith("amq.")
        )
        return TopologySnapshot(queues, exchanges)


def is_safe_test_vhost(name: str) -> bool:
    return isinstance(name, str) and _VHOST_PATTERN.fullmatch(name) is not None


class TopologyInitializer:
    """One-shot management-API preflight that applies and verifies the topology."""

    def __init__(self, management: RabbitManagementClient, amqp_url: str) -> None:
        if not isinstance(management, RabbitManagementClient):
            raise ValueError("management client must be strongly typed")
        self._management = management
        self._vhost = vhost_from_amqp_url(amqp_url)

    async def apply_and_verify(self, topology: RabbitTopologyV1) -> None:
        for exchange in topology.all_exchanges:
            await self._management.declare_exchange(
                self._vhost,
                exchange,
                exchange_type="direct",
            )
        main_args: dict[str, object] = {
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": topology.dlx_exchange,
            "x-dead-letter-routing-key": topology.dead_key,
            "x-overflow": "reject-publish",
            "x-max-length": 100_000,
        }
        await self._management.declare_queue(
            self._vhost,
            topology.main_queue,
            arguments=main_args,
        )
        for queue_name, ttl_ms, _routing_key in topology.retry_queues:
            await self._management.declare_queue(
                self._vhost,
                queue_name,
                arguments={
                    "x-queue-type": "quorum",
                    "x-message-ttl": ttl_ms,
                    "x-dead-letter-exchange": topology.main_exchange,
                    "x-dead-letter-routing-key": topology.execute_key,
                    "x-overflow": "reject-publish",
                    "x-max-length": 100_000,
                },
            )
        await self._management.declare_queue(
            self._vhost,
            topology.dlq_queue,
            arguments={
                "x-queue-type": "quorum",
                "x-overflow": "reject-publish",
            },
        )
        await self._management.bind(
            self._vhost,
            topology.main_exchange,
            topology.main_queue,
            topology.execute_key,
        )
        for queue_name, _ttl_ms, routing_key in topology.retry_queues:
            await self._management.bind(
                self._vhost,
                topology.retry_exchange,
                queue_name,
                routing_key,
            )
        await self._management.bind(
            self._vhost,
            topology.dlx_exchange,
            topology.dlq_queue,
            topology.dead_key,
        )
        await self._management.declare_policy(
            self._vhost,
            topology.policy_name,
            pattern="^lawyer\\.ai\\.jobs\\..*$",
            definition={
                "dead-letter-strategy": "at-least-once",
                "overflow": "reject-publish",
            },
        )
        snapshot = await self._management.snapshot(self._vhost)
        declared_queues = {queue.name for queue in snapshot.queues}
        if not set(topology.all_queues).issubset(declared_queues):
            raise RuntimeError("ai job topology queues are missing after preflight")
        by_name = {queue.name: queue for queue in snapshot.queues}
        for queue_name in topology.all_queues:
            queue = by_name.get(queue_name)
            if queue is None or not queue.durable or not queue.is_quorum:
                raise RuntimeError(
                    f"ai job topology queue {queue_name} is not quorum durable"
                )


def vhost_from_amqp_url(url: str) -> str:
    if not isinstance(url, str) or "://" not in url:
        raise ValueError("AMQP URL must be an absolute URL")
    rest = url.split("://", 1)[1]
    if "/" not in rest:
        return "/"
    path = rest.split("/", 1)[1]
    vhost = path.split("?", 1)[0].split("#", 1)[0]
    if not vhost:
        return "/"
    from urllib.parse import unquote

    return unquote(vhost)
