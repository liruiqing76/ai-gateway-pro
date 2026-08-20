import pytest
from datetime import datetime, timedelta
from backend.api.usage_tracker import UsageTracker

def test_record_request_basic():
    tracker = UsageTracker()
    tracker.record_request(user_id=1, channel="test", model="m1", tokens=10, latency=0.5, success=True)
    stats = tracker.get_user_stats(1)
    assert stats["request_count"] == 1
    assert stats["total_tokens"] == 10
    assert stats["success_count"] == 1
    assert stats["failure_count"] == 0
    assert stats["total_latency"] == 0.5

def test_record_request_failure():
    tracker = UsageTracker()
    tracker.record_request(user_id=2, channel="test", model="m2", tokens=5, latency=0.2, success=False)
    stats = tracker.get_user_stats(2)
    assert stats["request_count"] == 1
    assert stats["total_tokens"] == 5
    assert stats["success_count"] == 0
    assert stats["failure_count"] == 1
    assert stats["total_latency"] == 0.2

def test_multiple_users():
    tracker = UsageTracker()
    tracker.record_request(1, "c", "m", 3, 0.1, True)
    tracker.record_request(2, "c", "m", 7, 0.3, False)
    tracker.record_request(1, "c", "m", 2, 0.2, True)
    stats1 = tracker.get_user_stats(1)
    stats2 = tracker.get_user_stats(2)
    assert stats1["request_count"] == 2
    assert stats1["total_tokens"] == 5
    assert stats1["success_count"] == 2
    assert stats1["failure_count"] == 0
    assert stats1["total_latency"] == 0.3
    assert stats2["request_count"] == 1
    assert stats2["total_tokens"] == 7
    assert stats2["success_count"] == 0
    assert stats2["failure_count"] == 1
    assert stats2["total_latency"] == 0.3

def test_get_channel_stats():
    tracker = UsageTracker()
    tracker.record_request(1, "chanA", "m", 4, 0.1, True)
    tracker.record_request(2, "chanA", "m", 6, 0.2, False)
    tracker.record_request(3, "chanB", "m", 2, 0.5, True)
    chanA = tracker.get_channel_stats("chanA")
    assert chanA["request_count"] == 2
    assert chanA["total_tokens"] == 10
    assert chanA["success_count"] == 1
    assert chanA["failure_count"] == 1
    assert chanA["total_latency"] == 0.3
    chanB = tracker.get_channel_stats("chanB")
    assert chanB["request_count"] == 1
    assert chanB["total_tokens"] == 2
    assert chanB["success_count"] == 1
    assert chanB["failure_count"] == 0
    assert chanB["total_latency"] == 0.5
    # unknown channel returns zeros
    unknown = tracker.get_channel_stats("unknown")
    assert unknown == {"request_count":0,"total_tokens":0,"success_count":0,"failure_count":0,"total_latency":0.0}

def test_cleanup_older_than():
    tracker = UsageTracker()
    now = datetime.utcnow()
    tracker.record_request(1, "c", "m", 1, 0.1, True)
    # manipulate timestamp to be old
    tracker._requests[0]["timestamp"] = now - timedelta(days=10)
    tracker.record_request(2, "c", "m", 2, 0.2, True)  # recent
    tracker.cleanup_older_than(days=5)
    assert len(tracker._requests) == 1
    assert tracker._requests[0]["user_id"] == 2

def test_get_user_stats_unknown():
    tracker = UsageTracker()
    stats = tracker.get_user_stats(999)
    assert stats == {"request_count":0,"total_tokens":0,"success_count":0,"failure_count":0,"total_latency":0.0}