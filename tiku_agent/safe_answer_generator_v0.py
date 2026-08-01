"""Offline-only safe-answer generator with deterministic fallback.

The generator owns no Agent state and exposes no tools.  It rechecks routing
eligibility before making at most one injected model call, validates the text,
and falls back to the reviewed fixed reply on every model failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Literal

from tiku_agent.safe_answer_contract_v0 import (
    SafeAnswerPromptV0,
    build_safe_answer_prompt_v0,
    validate_safe_answer_output_v0,
)
from tiku_agent.safe_answer_context_v0 import (
    SafeAnswerValidationFacts,
    SafeConversationContext,
)
from tiku_agent.safe_answer_policy_v0 import evaluate_safe_answer_policy
from tiku_agent.safe_answer_reply_v0 import render_safe_answer_v0


GenerationSourceV0 = Literal["model", "fixed_fallback", "not_called"]


@dataclass(frozen=True)
class SafeAnswerModelRequestV0:
    prompt: SafeAnswerPromptV0
    timeout_seconds: float = 5.0
    temperature: float = 0.2
    max_tokens: int = 120


@dataclass(frozen=True)
class SafeAnswerGenerationV0:
    text: str
    source: GenerationSourceV0
    category: str
    fallback_reason: str
    latency_ms: int


SafeAnswerModelClientV0 = Callable[[SafeAnswerModelRequestV0], str]


class SafeAnswerGeneratorV0:
    """Generate one bounded answer without any business runtime dependency."""

    def __init__(
        self,
        model_client: SafeAnswerModelClientV0,
        *,
        timeout_seconds: float = 5.0,
        temperature: float = 0.2,
        max_tokens: int = 120,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 0 <= temperature <= 1:
            raise ValueError("temperature must be between 0 and 1")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self.model_client = model_client
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.clock = clock

    def generate(
        self,
        user_text: str,
        context: SafeConversationContext | None = None,
        validation_facts: SafeAnswerValidationFacts | None = None,
    ) -> SafeAnswerGenerationV0:
        decision = evaluate_safe_answer_policy(user_text)
        if not decision.eligible:
            return SafeAnswerGenerationV0(
                text="",
                source="not_called",
                category=decision.category,
                fallback_reason=decision.reason,
                latency_ms=0,
            )

        request = SafeAnswerModelRequestV0(
            prompt=build_safe_answer_prompt_v0(
                decision.category,
                user_text,
                context,
            ),
            timeout_seconds=self.timeout_seconds,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        started = self.clock()
        try:
            output = self.model_client(request)
        except TimeoutError:
            return self._fallback(
                decision.category,
                "model_timeout",
                started,
                context,
                validation_facts,
            )
        except Exception:  # noqa: BLE001 - every provider failure must use the safe fallback.
            return self._fallback(
                decision.category,
                "model_error",
                started,
                context,
                validation_facts,
            )

        if not isinstance(output, str):
            return self._fallback(
                decision.category,
                "invalid_output_type",
                started,
                context,
                validation_facts,
            )
        validation = validate_safe_answer_output_v0(
            output,
            decision.category,
            context,
            validation_facts,
        )
        if not validation.accepted:
            return self._fallback(
                decision.category,
                f"output_{validation.reason}",
                started,
                context,
                validation_facts,
            )
        return SafeAnswerGenerationV0(
            text=validation.normalized_text,
            source="model",
            category=decision.category,
            fallback_reason="",
            latency_ms=self._elapsed_ms(started),
        )

    def _fallback(
        self,
        category: str,
        reason: str,
        started: float,
        context: SafeConversationContext | None = None,
        validation_facts: SafeAnswerValidationFacts | None = None,
    ) -> SafeAnswerGenerationV0:
        return SafeAnswerGenerationV0(
            text=render_safe_answer_v0(category, context, validation_facts),
            source="fixed_fallback",
            category=category,
            fallback_reason=reason,
            latency_ms=self._elapsed_ms(started),
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self.clock() - started) * 1000))
