"""
In-process asyncio Server-Sent Events (SSE) broadcaster.
Relies on Render's single-worker deployment (--workers 1) to broadcast
events to all connected SSE clients in the same process without external pub/sub.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Set

logger = logging.getLogger(__name__)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    """Format datetime as UTC ISO 8601 with trailing Z and no microseconds."""
    if dt is None:
        dt = datetime.now(timezone.utc).replace(tzinfo=None)
    elif dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat() + "Z"


class EventBroadcaster:
    """
    In-process SSE event broadcaster.
    Each subscriber receives an asyncio.Queue bounded at queue_maxsize (default 100).
    Slow subscribers whose queue becomes full are dropped and disconnected immediately,
    preventing unbounded memory growth.
    """
    def __init__(self, queue_maxsize: int = 100):
        self.queue_maxsize = queue_maxsize
        self._subscribers: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        """Subscribes a new client, returning its bounded message queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Unsubscribes a client and removes its queue."""
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(
        self,
        event_type: str,
        entity_id: str,
        status: str,
        updated_at: Optional[str] = None
    ) -> None:
        """
        Publishes a lightweight event payload: {type, id, status, updated_at}.
        Must ONLY be called after the database commit succeeds.
        Never raises: log and swallow exceptions so failures never turn committed operations into 500s.
        If a subscriber's queue is full, the subscriber is dropped immediately
        and sent a None sentinel to terminate its stream generator.
        """
        try:
            if not updated_at:
                updated_at = _utc_iso()

            payload = {
                "type": event_type,
                "id": entity_id,
                "status": status,
                "updated_at": updated_at,
            }
            data_str = json.dumps(payload)
            sse_message = f"event: {event_type}\ndata: {data_str}\n\n"

            subscribers = list(self._subscribers)
            dropped = []
            for q in subscribers:
                try:
                    q.put_nowait(sse_message)
                except asyncio.QueueFull:
                    logger.warning(
                        "Subscriber queue full (%d items); dropping slow subscriber.",
                        self.queue_maxsize
                    )
                    dropped.append(q)
                    # Drop an old item and insert None sentinel so generator terminates
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

            for q in dropped:
                self._subscribers.discard(q)
        except Exception as exc:
            logger.exception("Failed to publish SSE event: %s", exc)

    async def publish_payout_request(
        self,
        req_id: str,
        status: str,
        updated_at: Optional[str] = None
    ) -> None:
        await self.publish("payout_request", req_id, status, updated_at)

    async def publish_task(
        self,
        task_id: str,
        status: str,
        updated_at: Optional[str] = None
    ) -> None:
        await self.publish("task", task_id, status, updated_at)

    async def publish_agent(
        self,
        agent_id: str,
        status: str,
        updated_at: Optional[str] = None
    ) -> None:
        await self.publish("agent", agent_id, status, updated_at)


broadcaster = EventBroadcaster()
