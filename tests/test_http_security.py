import unittest

from tiku_shared.http_security import (
    FailureRateLimiter,
    RequestBodyError,
    read_bounded_body,
    validated_client_key,
)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class _Request:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = dict(headers or {})

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


class _OversizedChunk:
    def __len__(self):
        return 8192

    def __iter__(self):
        raise AssertionError("oversized chunk must be rejected before buffer extension")


class HttpSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_body_checks_stream_even_without_content_length(self):
        request = _Request([b"abc", b"def"])
        with self.assertRaises(RequestBodyError) as raised:
            await read_bounded_body(request, max_bytes=5)
        self.assertEqual(raised.exception.status_code, 413)

        accepted = await read_bounded_body(
            _Request([b"abc"], {"content-length": "3"}),
            max_bytes=5,
        )
        self.assertEqual(accepted, b"abc")

    async def test_bounded_body_rejects_one_large_chunk_before_copying_it(self):
        with self.assertRaises(RequestBodyError) as raised:
            await read_bounded_body(_Request([_OversizedChunk()]), max_bytes=5)
        self.assertEqual(raised.exception.status_code, 413)

    async def test_bounded_body_rejects_invalid_declared_length(self):
        with self.assertRaises(RequestBodyError) as raised:
            await read_bounded_body(
                _Request([], {"content-length": "not-a-number"}),
                max_bytes=5,
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_failed_login_window_blocks_until_oldest_attempt_expires(self):
        clock = _Clock()
        limiter = FailureRateLimiter(
            attempts=2,
            window_seconds=10,
            max_keys=2,
            clock=clock,
        )
        first, retry_after = limiter.reserve_attempt("one")
        self.assertIsNotNone(first)
        self.assertEqual(retry_after, 0)
        self.assertTrue(limiter.complete_failure(first))
        second, retry_after = limiter.reserve_attempt("one")
        self.assertIsNotNone(second)
        self.assertEqual(retry_after, 0)
        self.assertTrue(limiter.complete_failure(second))

        blocked, retry_after = limiter.reserve_attempt("one")
        self.assertIsNone(blocked)
        self.assertEqual(retry_after, 10)
        clock.value += 4.2
        blocked, retry_after = limiter.reserve_attempt("one")
        self.assertIsNone(blocked)
        self.assertEqual(retry_after, 6)
        clock.value += 5.8
        admitted, retry_after = limiter.reserve_attempt("one")
        self.assertIsNotNone(admitted)
        self.assertEqual(retry_after, 0)

    def test_cancelled_and_expired_reservations_do_not_count_as_failures(self):
        clock = _Clock()
        limiter = FailureRateLimiter(
            attempts=1,
            window_seconds=10,
            clock=clock,
        )
        cancelled, _ = limiter.reserve_attempt("one")
        self.assertIsNotNone(cancelled)
        self.assertTrue(limiter.cancel_attempt(cancelled))
        admitted, _ = limiter.reserve_attempt("one")
        self.assertIsNotNone(admitted)
        clock.value += 11
        replacement, retry_after = limiter.reserve_attempt("one")
        self.assertIsNotNone(replacement)
        self.assertEqual(retry_after, 0)
        self.assertFalse(limiter.complete_failure(admitted))

    def test_success_clears_failures_but_keeps_other_in_flight_attempts(self):
        limiter = FailureRateLimiter(attempts=3, window_seconds=10)
        first, _ = limiter.reserve_attempt("one")
        successful, _ = limiter.reserve_attempt("one")
        still_running, _ = limiter.reserve_attempt("one")
        self.assertTrue(limiter.complete_failure(first))
        self.assertTrue(limiter.complete_success(successful))

        fourth, _ = limiter.reserve_attempt("one")
        fifth, _ = limiter.reserve_attempt("one")
        blocked, retry_after = limiter.reserve_attempt("one")
        self.assertIsNotNone(still_running)
        self.assertIsNotNone(fourth)
        self.assertIsNotNone(fifth)
        self.assertIsNone(blocked)
        self.assertGreater(retry_after, 0)

    def test_capacity_does_not_evict_keys_with_in_flight_attempts(self):
        limiter = FailureRateLimiter(
            attempts=2,
            window_seconds=10,
            max_keys=2,
        )
        first, _ = limiter.reserve_attempt("one")
        second, _ = limiter.reserve_attempt("two")
        blocked, retry_after = limiter.reserve_attempt("three")
        self.assertIsNone(blocked)
        self.assertGreater(retry_after, 0)
        self.assertEqual(limiter.tracked_keys, 2)

        self.assertTrue(limiter.cancel_attempt(first))
        third, retry_after = limiter.reserve_attempt("three")
        self.assertIsNotNone(third)
        self.assertEqual(retry_after, 0)
        self.assertEqual(limiter.tracked_keys, 2)
        self.assertTrue(limiter.cancel_attempt(second))
        self.assertTrue(limiter.cancel_attempt(third))

    def test_only_valid_cloudflare_client_ip_is_trusted(self):
        self.assertEqual(
            validated_client_key("203.0.113.7", "127.0.0.1"),
            "203.0.113.7",
        )
        self.assertEqual(
            validated_client_key("spoofed, 203.0.113.7", "127.0.0.1"),
            "127.0.0.1",
        )


if __name__ == "__main__":
    unittest.main()
