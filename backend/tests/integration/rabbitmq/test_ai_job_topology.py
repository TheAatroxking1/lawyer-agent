from __future__ import annotations

import pytest

from lawyer_agent.infrastructure.messaging.rabbitmq import AioPikaRuntimeTopology
from lawyer_agent.infrastructure.messaging.topology import (
    RabbitTopologyV1,
    TopologyInitializer,
    vhost_from_amqp_url,
)

pytestmark = [pytest.mark.integration]


async def test_topology_is_quorum_durable_and_runtime_passive(rabbit_vhost) -> None:
    initializer = TopologyInitializer(rabbit_vhost.management, rabbit_vhost.amqp_url)
    await initializer.apply_and_verify(RabbitTopologyV1())

    ok = await AioPikaRuntimeTopology(rabbit_vhost.amqp_url).passive_check(
        RabbitTopologyV1()
    )
    assert ok is True

    vhost = vhost_from_amqp_url(rabbit_vhost.amqp_url)
    actual = await rabbit_vhost.management.snapshot(vhost)
    assert actual.queues
    assert all(queue.durable and queue.is_quorum for queue in actual.queues)
    declared_names = {queue.name for queue in actual.queues}
    assert set(RabbitTopologyV1().all_queues).issubset(declared_names)


async def test_preflight_is_idempotent(rabbit_vhost) -> None:
    initializer = TopologyInitializer(rabbit_vhost.management, rabbit_vhost.amqp_url)
    await initializer.apply_and_verify(RabbitTopologyV1())
    await initializer.apply_and_verify(RabbitTopologyV1())
    ok = await AioPikaRuntimeTopology(rabbit_vhost.amqp_url).passive_check(
        RabbitTopologyV1()
    )
    assert ok is True
