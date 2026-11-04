"""In-process fan-out of measurement events to live SSE streams.

The database is the source of truth: events are written there first and published here only
after the transaction commits. A stream that falls behind is dropped and the browser reconnects
with its last event id, so a slow client can never hold up a measurement or lose an event.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

TERMINAL_EVENTS = frozenset({"measurement.finished"})


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=2000)
    )
    overflowed: bool = False


class EventBus:
    def __init__(self) -> None:
        self._subscriptions: dict[str, set[Subscription]] = defaultdict(set)

    def subscribe(self, measurement_id: str) -> Subscription:
        subscription = Subscription()
        self._subscriptions[measurement_id].add(subscription)
        return subscription

    def unsubscribe(self, measurement_id: str, subscription: Subscription) -> None:
        subscriptions = self._subscriptions.get(measurement_id)
        if subscriptions is not None:
            subscriptions.discard(subscription)
            if not subscriptions:
                del self._subscriptions[measurement_id]

    def publish(self, measurement_id: str, event: dict[str, Any]) -> None:
        for subscription in list(self._subscriptions.get(measurement_id, ())):
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscription.overflowed = True

    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._subscriptions.values())
