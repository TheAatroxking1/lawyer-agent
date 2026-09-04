from __future__ import annotations

import aio_pika
import orjson

from lawyer_agent.application.ai_job_runtime import (
    MessagePublisherPort,
    PublisherErrorCode,
    PublishReceipt,
)
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope, envelope_to_json
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


class AioPikaPublisher(MessagePublisherPort):
    """Publisher confirm + mandatory adapter; never consumes messages."""

    def __init__(
        self,
        amqp_url: str,
        *,
        topology: RabbitTopologyV1,
        confirm_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(amqp_url, str) or "://" not in amqp_url:
            raise ValueError("AMQP URL must be an absolute URL")
        self._amqp_url = amqp_url
        self._topology = topology
        self._confirm_timeout = confirm_timeout_seconds

    async def publish(
        self,
        envelope: AIJobEnvelope,
        routing_key: str,
    ) -> PublishReceipt:
        connection = await aio_pika.connect_robust(self._amqp_url)
        try:
            channel = await connection.channel(
                publisher_confirms=True,
                on_return_raises=True,
            )
            exchange_name = _exchange_for_routing_key(self._topology, routing_key)
            exchange = await channel.get_exchange(exchange_name)
            message = aio_pika.Message(
                body=orjson.dumps(envelope_to_json(envelope)),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=str(envelope.message_id),
                correlation_id=str(envelope.correlation_id),
                timestamp=envelope.issued_at,
                content_type="application/json",
            )
            try:
                confirmation = await exchange.publish(
                    message,
                    routing_key=routing_key,
                    mandatory=True,
                    timeout=self._confirm_timeout,
                )
                confirmation_name = type(confirmation).__name__ if confirmation else ""
                if confirmation_name in {"Nack", "Reject"}:
                    return PublishReceipt(False, PublisherErrorCode.RABBIT_NACK)
                return PublishReceipt(True)
            except aio_pika.exceptions.DeliveryError as exc:
                del exc
                return PublishReceipt(False, PublisherErrorCode.RABBIT_UNROUTABLE)
            except aio_pika.exceptions.ChannelClosed as exc:
                del exc
                return PublishReceipt(False, PublisherErrorCode.RABBIT_CHANNEL_CLOSED)
            except TimeoutError:
                return PublishReceipt(False, PublisherErrorCode.RABBIT_TIMEOUT)
            except aio_pika.exceptions.AMQPConnectionError as exc:
                del exc
                return PublishReceipt(False, PublisherErrorCode.RABBIT_CONNECTION_LOST)
        finally:
            await connection.close()


def _exchange_for_routing_key(
    topology: RabbitTopologyV1, routing_key: str
) -> str:
    if routing_key == topology.execute_key:
        return topology.main_exchange
    if routing_key.startswith("ai.job.retry."):
        return topology.retry_exchange
    if routing_key == topology.dead_key:
        return topology.dlx_exchange
    raise ValueError(f"unknown routing key for ai job dispatch: {routing_key}")


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
