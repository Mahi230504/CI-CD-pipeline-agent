"""
Webhook delivery dedupe — a small in-memory LRU keyed on X-GitHub-Delivery.

GitHub retries failed deliveries (and some networks duplicate them). Without
a guard, a retried delivery re-enqueues the same workflow run and the
pipeline runs again — wasting Gemini quota and producing extra audit lines.

The dedupe cache is process-local, so a server restart loses it. The
persistent queue (orchestrator/persistent_queue) catches anything that
slipped through restart.
"""

from __future__ import annotations

import threading
from collections import OrderedDict


class DeliveryDedup:
    def __init__(self, capacity: int = 1024) -> None:
        self._cap = max(1, int(capacity))
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def seen_before(self, delivery_id: str) -> bool:
        """Return True iff `delivery_id` was already recorded. Records it on first call."""
        if not delivery_id:
            return False
        with self._lock:
            if delivery_id in self._seen:
                # touch (most-recently-seen) so frequent retries don't get evicted
                self._seen.move_to_end(delivery_id)
                return True
            self._seen[delivery_id] = None
            if len(self._seen) > self._cap:
                self._seen.popitem(last=False)
            return False

    def __len__(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


_singleton: DeliveryDedup | None = None


def get_dedup() -> DeliveryDedup:
    global _singleton
    if _singleton is None:
        _singleton = DeliveryDedup()
    return _singleton


def reset_for_testing() -> None:
    global _singleton
    _singleton = None
