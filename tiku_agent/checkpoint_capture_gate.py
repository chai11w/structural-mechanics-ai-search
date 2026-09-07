"""Admission gate for the future A2 Checkpoint capture path.

The gate is deliberately independent from the Store and Agent runtime. It
answers only whether a request is eligible to attempt best-effort evidence
capture; Store capacity and write failures remain the responsibility of 4.2.
"""

from __future__ import annotations

from dataclasses import dataclass


EXCLUDED_REQUEST_KINDS = frozenset({"health", "login", "quota", "queue", "media"})
BUSINESS_REQUEST_KIND = "search"


@dataclass(frozen=True)
class A2CaptureAdmissionV1:
    request_kind: str
    authenticated: bool
    quota_admitted: bool
    queue_admitted: bool
    upload_admitted: bool
    entered_business_processing: bool


@dataclass(frozen=True)
class A2CaptureDecisionV1:
    permitted: bool
    reason_code: str


class A2CheckpointCaptureGateV1:
    """Pure, default-closed admission gate for A2 evidence attempts."""

    def __init__(self, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise TypeError("capture gate enabled must be boolean")
        self.enabled = enabled

    def decide(self, admission: A2CaptureAdmissionV1) -> A2CaptureDecisionV1:
        if type(admission) is not A2CaptureAdmissionV1:
            raise TypeError("admission must be A2CaptureAdmissionV1")
        if not self.enabled:
            return A2CaptureDecisionV1(False, "CAPTURE_DISABLED")
        if admission.request_kind in EXCLUDED_REQUEST_KINDS:
            return A2CaptureDecisionV1(False, "REQUEST_KIND_EXCLUDED")
        if admission.request_kind != BUSINESS_REQUEST_KIND:
            return A2CaptureDecisionV1(False, "REQUEST_KIND_UNSUPPORTED")
        checks = (
            (admission.authenticated, "AUTHENTICATION_REQUIRED"),
            (admission.quota_admitted, "QUOTA_NOT_ADMITTED"),
            (admission.queue_admitted, "QUEUE_NOT_ADMITTED"),
            (admission.upload_admitted, "UPLOAD_NOT_ADMITTED"),
            (admission.entered_business_processing, "BUSINESS_PROCESSING_NOT_ENTERED"),
        )
        for passed, reason_code in checks:
            if not passed:
                return A2CaptureDecisionV1(False, reason_code)
        return A2CaptureDecisionV1(True, "CAPTURE_ADMITTED")


__all__ = [
    "A2CaptureAdmissionV1",
    "A2CaptureDecisionV1",
    "A2CheckpointCaptureGateV1",
    "BUSINESS_REQUEST_KIND",
    "EXCLUDED_REQUEST_KINDS",
]
