"""How long one conversation and its media live - defined exactly once.

The browser countdown, the A2/A3 session store, the session-coordination
fence receipts, the durable execution sessions and the A3 crop media all
expire on the same clock.  They used to hard-code the same two hours
independently, so any single edit could silently pull them apart: a client
that still believed in a two-hour conversation could hand the server a fence
whose receipt had already been dropped, and the reset deadlocked.  One
constant here, imported by every consumer, is what keeps them in step.

The value is published to the browser in the session response
(``conversation_ttl_seconds``) so the interface never promises a lifetime the
server does not honour.
"""
from __future__ import annotations

from datetime import timedelta

CONVERSATION_TTL_SECONDS = 2 * 60 * 60
CONVERSATION_TTL = timedelta(seconds=CONVERSATION_TTL_SECONDS)
