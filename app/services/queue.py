"""Review queue adapters (synchronous SDKs; call through asyncio.to_thread).

- `sqs`:   AWS SQS. Locally this points at a moto server (PB_AWS_ENDPOINT_URL=http://localhost:5000).
- `azure`: Azure Storage Queues, authenticated with managed identity (account URL) or a connection string.
"""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueMessage:
    body: dict[str, Any]
    receipt: Any
    attempts: int


class ReviewQueue(Protocol):
    def send(self, body: dict[str, Any]) -> str: ...
    def send_many(self, bodies: list[dict[str, Any]]) -> int: ...
    def receive(self, max_messages: int, wait_seconds: int) -> list[QueueMessage]: ...
    def delete(self, message: QueueMessage) -> None: ...


class SqsQueue:
    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        s = get_settings()
        kwargs: dict[str, Any] = {
            "region_name": s.aws_region,
            "config": Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=3, read_timeout=25),
        }
        if s.aws_endpoint_url:
            kwargs.update(endpoint_url=s.aws_endpoint_url, aws_access_key_id=s.aws_access_key_id,
                          aws_secret_access_key=s.aws_secret_access_key)
        self.client = boto3.client("sqs", **kwargs)
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            name = get_settings().review_queue_name
            try:
                self._url = self.client.get_queue_url(QueueName=name)["QueueUrl"]
            except self.client.exceptions.QueueDoesNotExist:
                self._url = self.client.create_queue(QueueName=name, Attributes={"VisibilityTimeout": "300"})[
                    "QueueUrl"]
        return self._url

    def send(self, body: dict[str, Any]) -> str:
        return self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(body, default=str))["MessageId"]

    def send_many(self, bodies: list[dict[str, Any]]) -> int:
        """SendMessageBatch: 10 messages per call instead of one call per message."""
        for i in range(0, len(bodies), 10):
            chunk = bodies[i:i + 10]
            entries = [{"Id": str(n), "MessageBody": json.dumps(b, default=str)} for n, b in enumerate(chunk)]
            failed = self.client.send_message_batch(QueueUrl=self.url, Entries=entries).get("Failed", [])
            if failed:
                raise RuntimeError(f"{len(failed)} review messages were not enqueued")
        return len(bodies)

    def receive(self, max_messages: int = 5, wait_seconds: int = 5) -> list[QueueMessage]:
        resp = self.client.receive_message(QueueUrl=self.url, MaxNumberOfMessages=max_messages,
                                           WaitTimeSeconds=wait_seconds, AttributeNames=["ApproximateReceiveCount"])
        return [QueueMessage(json.loads(m["Body"]), m["ReceiptHandle"],
                             int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1)))
                for m in resp.get("Messages", [])]

    def delete(self, message: QueueMessage) -> None:
        self.client.delete_message(QueueUrl=self.url, ReceiptHandle=message.receipt)


class AzureStorageQueue:
    def __init__(self) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.queue import QueueClient

        s = get_settings()
        if s.azure_queue_connection_string:
            self.client = QueueClient.from_connection_string(s.azure_queue_connection_string, s.review_queue_name)
        elif s.azure_queue_account_url:
            from azure.identity import DefaultAzureCredential

            self.client = QueueClient(s.azure_queue_account_url, s.review_queue_name,
                                      credential=DefaultAzureCredential())
        else:
            raise RuntimeError("Set PB_AZURE_QUEUE_ACCOUNT_URL or PB_AZURE_QUEUE_CONNECTION_STRING")
        try:
            self.client.create_queue()
        except ResourceExistsError:
            pass

    def send(self, body: dict[str, Any]) -> str:
        return self.client.send_message(json.dumps(body, default=str)).id

    def send_many(self, bodies: list[dict[str, Any]]) -> int:
        # Storage Queues have no batch send; messages go out over one pooled HTTPS connection.
        for b in bodies:
            self.send(b)
        return len(bodies)

    def receive(self, max_messages: int = 5, wait_seconds: int = 5) -> list[QueueMessage]:
        # Storage Queues have no long polling; the worker sleeps between empty polls instead.
        msgs = self.client.receive_messages(messages_per_page=max_messages, visibility_timeout=300,
                                            max_messages=max_messages)
        return [QueueMessage(json.loads(m.content), m, m.dequeue_count or 1) for m in msgs]

    def delete(self, message: QueueMessage) -> None:
        self.client.delete_message(message.receipt)


@lru_cache
def review_queue() -> ReviewQueue:
    backend = get_settings().queue_backend
    return AzureStorageQueue() if backend == "azure" else SqsQueue()
