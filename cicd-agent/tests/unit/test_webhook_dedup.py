"""Tests for the in-memory webhook delivery dedupe."""

from __future__ import annotations

from webhook.dedup import DeliveryDedup


def test_first_delivery_not_seen() -> None:
    d = DeliveryDedup()
    assert d.seen_before("abc") is False


def test_second_delivery_seen() -> None:
    d = DeliveryDedup()
    d.seen_before("abc")
    assert d.seen_before("abc") is True


def test_distinct_ids_independent() -> None:
    d = DeliveryDedup()
    d.seen_before("a")
    d.seen_before("b")
    assert d.seen_before("a") is True
    assert d.seen_before("b") is True
    assert d.seen_before("c") is False


def test_empty_delivery_id_never_seen() -> None:
    d = DeliveryDedup()
    assert d.seen_before("") is False
    assert d.seen_before("") is False  # still false; we don't track empty


def test_capacity_evicts_oldest() -> None:
    d = DeliveryDedup(capacity=3)
    for x in ("a", "b", "c"):
        d.seen_before(x)
    d.seen_before("d")  # 'a' should now be evicted
    assert d.seen_before("a") is False  # treated as new
    assert d.seen_before("d") is True
    assert d.seen_before("c") is True


def test_repeat_resets_recency() -> None:
    """Repeating an id moves it to the most-recently-seen position so it
    isn't evicted by subsequent new ids."""
    d = DeliveryDedup(capacity=3)
    d.seen_before("a")
    d.seen_before("b")
    d.seen_before("c")
    d.seen_before("a")  # touch a → now most recent
    d.seen_before("d")  # should evict 'b' (now oldest), not 'a'
    assert d.seen_before("a") is True
    assert d.seen_before("b") is False  # evicted
