from __future__ import annotations

import aio_pika

from lawyer_agent.infrastructure.messaging.topology import RabbitTopologyV1


class AioPikaRuntimeTopology:
    """Runtime AMQP passive readiness check without management credentials."""

    def __init__(self, amqp_url: str) -> None:
        if not isinstance(amqp_url, str) or "://" not in amqp_url:
            raise ValueError("AMQP URL must be an absolute URL")
        self._amqp_url = amqp_url

    async def passive_check(self, topology: RabbitTopologyV1) -> bool:
        connection = await aio_pika.connect_robust(self._amqp_url)
        try:
            channel = await connection.channel()
            for exchange_name in topology.all_exchanges:
                if not await _exchange_exists(channel, exchange_name):
                    return False
            for queue_name in topology.all_queues:
                if not await _queue_exists(channel, queue_name):
                    return False
            return True
        finally:
            await connection.close()


async def _exchange_exists(channel: aio_pika.abc.AbstractChannel, name: str) -> bool:
    try:
        exchange = await channel.get_exchange(name, ensure=False)
        return exchange is not None
    except Exception:
        return False


async def _queue_exists(channel: aio_pika.abc.AbstractChannel, name: str) -> bool:
    try:
        queue = await channel.get_queue(name, ensure=False)
        return queue is not None
    except Exception:
        return False
