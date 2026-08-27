"""Small shared HTTP guards for public login endpoints."""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
import ipaddress
import math
from threading import Lock
import time
from typing import Protocol


class BoundedRequest(Protocol):
    headers: object

    def stream(self) -> AsyncIterator[bytes]: ...


class RequestBodyError(ValueError):
    def __init__(self, detail: str, *, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def read_bounded_body(request: BoundedRequest, *, max_bytes: int) -> bytes:
    """Read an ASGI request body without trusting Content-Length as the bound."""

    limit = max(1, int(max_bytes))
    raw_length = str(request.headers.get("content-length") or "").strip()  # type: ignore[attr-defined]
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise RequestBodyError("invalid content length", status_code=400) from exc
        if content_length < 0:
            raise RequestBodyError("invalid content length", status_code=400)
        if content_length > limit:
            raise RequestBodyError("request is too large", status_code=413)

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RequestBodyError("request is too large", status_code=413)
        body.extend(chunk)
    return bytes(body)


def validated_client_key(cf_connecting_ip: str, direct_host: str) -> str:
    """Trust only a syntactically valid Cloudflare client address."""

    forwarded = str(cf_connecting_ip or "").strip()
    if forwarded:
        try:
            return ipaddress.ip_address(forwarded).compressed
        except ValueError:
            pass
    fallback = str(direct_host or "unknown").strip() or "unknown"
    try:
        return ipaddress.ip_address(fallback).compressed
    except ValueError:
        return fallback[:256]


@dataclass(frozen=True)
class FailureAttemptReservation:
    """One admitted login attempt that must be completed or cancelled once."""

    key: str
    token: int


@dataclass
class _FailureEntry:
    failures: deque[float] = field(default_factory=deque)
    reservations: dict[int, float] = field(default_factory=dict)


class FailureRateLimiter:
    """Bound completed failures and concurrent login attempts per client key."""

    def __init__(
        self,
        *,
        attempts: int,
        window_seconds: float,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.attempts = max(1, int(attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(1, int(max_keys))
        self._clock = clock
        self._entries: OrderedDict[str, _FailureEntry] = OrderedDict()
        self._next_token = 1
        self._lock = Lock()

    def reserve_attempt(
        self,
        key: str,
    ) -> tuple[FailureAttemptReservation | None, int]:
        """Atomically check the limit and reserve one attempt slot."""

        clean = str(key or "unknown")
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(clean)
            if entry is None:
                if not self._make_capacity():
                    return None, self._capacity_retry_after(now)
                entry = _FailureEntry()
                self._entries[clean] = entry
            if len(entry.failures) + len(entry.reservations) >= self.attempts:
                self._entries.move_to_end(clean)
                return None, self._entry_retry_after(entry, now)
            token = self._next_token
            self._next_token += 1
            entry.reservations[token] = now
            self._entries.move_to_end(clean)
            return FailureAttemptReservation(clean, token), 0

    def complete_failure(self, reservation: FailureAttemptReservation) -> bool:
        """Convert one live reservation into a completed failed attempt."""

        now = self._clock()
        with self._lock:
            entry = self._live_reservation_entry(reservation, now)
            if entry is None:
                return False
            entry.reservations.pop(reservation.token)
            entry.failures.append(now)
            self._entries.move_to_end(reservation.key)
            return True

    def complete_success(self, reservation: FailureAttemptReservation) -> bool:
        """Clear completed failures without discarding other in-flight attempts."""

        now = self._clock()
        with self._lock:
            entry = self._live_reservation_entry(reservation, now)
            if entry is None:
                return False
            entry.reservations.pop(reservation.token)
            entry.failures.clear()
            self._drop_or_touch(reservation.key, entry)
            return True

    def cancel_attempt(self, reservation: FailureAttemptReservation) -> bool:
        """Release an admitted attempt that did not reach an auth verdict."""

        now = self._clock()
        with self._lock:
            entry = self._live_reservation_entry(reservation, now)
            if entry is None:
                return False
            entry.reservations.pop(reservation.token)
            self._drop_or_touch(reservation.key, entry)
            return True

    @property
    def tracked_keys(self) -> int:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            return len(self._entries)

    def _live_reservation_entry(
        self,
        reservation: FailureAttemptReservation,
        now: float,
    ) -> _FailureEntry | None:
        self._trim_key(reservation.key, now)
        entry = self._entries.get(reservation.key)
        if entry is None or reservation.token not in entry.reservations:
            return None
        return entry

    def _trim_key(self, key: str, now: float) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        cutoff = now - self.window_seconds
        while entry.failures and entry.failures[0] <= cutoff:
            entry.failures.popleft()
        for token, started_at in tuple(entry.reservations.items()):
            if started_at <= cutoff:
                entry.reservations.pop(token, None)
        if not entry.failures and not entry.reservations:
            self._entries.pop(key, None)

    def _purge_expired(self, now: float) -> None:
        for key in list(self._entries):
            self._trim_key(key, now)

    def _make_capacity(self) -> bool:
        if len(self._entries) < self.max_keys:
            return True
        for key, entry in tuple(self._entries.items()):
            if not entry.reservations:
                self._entries.pop(key, None)
                return True
        return False

    def _entry_retry_after(self, entry: _FailureEntry, now: float) -> int:
        timestamps = list(entry.failures) + list(entry.reservations.values())
        oldest = min(timestamps, default=now)
        return max(1, int(math.ceil(oldest + self.window_seconds - now)))

    def _capacity_retry_after(self, now: float) -> int:
        reservations = [
            started_at
            for entry in self._entries.values()
            for started_at in entry.reservations.values()
        ]
        oldest = min(reservations, default=now)
        return max(1, int(math.ceil(oldest + self.window_seconds - now)))

    def _drop_or_touch(self, key: str, entry: _FailureEntry) -> None:
        if not entry.failures and not entry.reservations:
            self._entries.pop(key, None)
        else:
            self._entries.move_to_end(key)
