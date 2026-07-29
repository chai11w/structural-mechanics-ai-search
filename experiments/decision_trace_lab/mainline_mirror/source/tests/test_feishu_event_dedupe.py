from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from scripts.feishu_tiku_bot import RecentEventIdCache


class RecentEventIdCacheTest(unittest.TestCase):
    def test_duplicate_is_rejected_only_within_ttl(self):
        clock = [100.0]
        cache = RecentEventIdCache(ttl_seconds=30, max_entries=10, now=lambda: clock[0])

        self.assertFalse(cache.seen_or_add("event-1"))
        self.assertTrue(cache.seen_or_add("event-1"))
        clock[0] += 31
        self.assertFalse(cache.seen_or_add("event-1"))
        self.assertEqual(len(cache), 1)

    def test_cache_evicts_oldest_ids_at_capacity(self):
        clock = [0.0]
        cache = RecentEventIdCache(ttl_seconds=3600, max_entries=2, now=lambda: clock[0])

        self.assertFalse(cache.seen_or_add("oldest"))
        clock[0] += 1
        self.assertFalse(cache.seen_or_add("middle"))
        clock[0] += 1
        self.assertFalse(cache.seen_or_add("newest"))

        self.assertEqual(len(cache), 2)
        self.assertFalse(cache.seen_or_add("oldest"))

    def test_concurrent_duplicate_delivery_is_accepted_once(self):
        cache = RecentEventIdCache(ttl_seconds=60, max_entries=10)
        workers = 8
        barrier = threading.Barrier(workers)

        def submit_once() -> bool:
            barrier.wait()
            return cache.seen_or_add("same-event")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            duplicates = list(executor.map(lambda _index: submit_once(), range(workers)))

        self.assertEqual(duplicates.count(False), 1)
        self.assertEqual(duplicates.count(True), workers - 1)


if __name__ == "__main__":
    unittest.main()
