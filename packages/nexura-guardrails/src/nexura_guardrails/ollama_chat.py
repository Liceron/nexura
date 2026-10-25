# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Ollama's native chat API (``POST /api/chat``) as a small model object for judges.

``JudgeCheck("ollama:qwen3:8b", ...)`` uses this instead of Ollama's OpenAI-compatible
endpoint (P39 G6), because only the native API takes a per-request context size:
``options.num_ctx`` is set to the judge's ``context_tokens``, so the prompt-size cap the
judge enforces is the context the server really uses (no ``OLLAMA_CONTEXT_LENGTH`` needed),
and ``truncate``/``shift`` are off, so an over-long prompt fails instead of losing its
middle. The verdict's JSON schema goes in ``format`` (Ollama's structured output),
temperature is 0 and ``num_predict`` caps the reply.

The call goes through ``nexura_agents.usage.admission`` like any model call: inside a run
the run's budget admits and settles it under the model id ``ollama:<model>`` (judges never
get a zero price; only the named local safety models do). Transport is the same hardened
HTTP client as the safety models: no proxies from the environment, no redirects, no
compressed bodies, a 256 KB reply cap, and a reply from a different model is refused.
"""

from __future__ import annotations

from typing import Any

from nexura_agents.types import Message, ModelResponse

__all__ = ["OllamaChatError", "OllamaChatModel"]


class OllamaChatError(RuntimeError):
    """The Ollama chat call failed; ``error_type`` says how (``TimeoutError``,
    ``HTTPStatusError``, ``MalformedResponse``, ``PromptTruncated``, ``ModelMismatch``...)."""

    def __init__(self, error_type: str, detail: str) -> None:
        super().__init__(f"{error_type}: {detail}")
        self.error_type = error_type
        self.detail = detail


class OllamaChatModel:
    """A model object for ``JudgeCheck`` that calls Ollama's ``/api/chat``.

    ``model`` is the Ollama tag (``qwen3:8b``); ``context_tokens`` is sent as ``num_ctx``.
    ``transport`` is an ``httpx`` transport (tests use ``httpx.MockTransport``).
    """

    supports_response_schema = True
    accepts_model_settings = True

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        context_tokens: int = 4096,
        timeout: float = 10.0,
        keep_alive: str | int | None = None,
        transport: Any = None,
    ) -> None:
        from nexura_guardrails.classifiers._base import DEFAULT_OLLAMA_URL

        name = model.removeprefix("ollama:").strip()
        if not name:
            raise ValueError("OllamaChatModel needs an Ollama model tag.")
        self.model = name
        self.model_id = f"ollama:{name}"
        self.base_url = (base_url or DEFAULT_OLLAMA_URL).rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http:// or https:// URL.")
        self.context_tokens = context_tokens
        self.timeout = float(timeout)
        self.keep_alive = keep_alive
        self.transport = transport

    def __repr__(self) -> str:
        return f"OllamaChatModel(model={self.model!r}, base_url={self.base_url!r})"

    def payload(
        self,
        messages: list[Message],
        *,
        response_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """The ``/api/chat`` request body (public for tests and audits)."""
        options: dict[str, Any] = {"temperature": 0, "num_ctx": self.context_tokens}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content or ""} for m in messages],
            "stream": False,
            # Never truncate or shift: an over-long prompt must fail, not lose its middle.
            "truncate": False,
            "shift": False,
            "options": options,
        }
        if response_schema is not None:
            body["format"] = response_schema
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        return body

    async def generate(
        self,
        messages: list[Message],
        tools: Any = None,
        *,
        response_schema: dict[str, Any] | None = None,
        model_settings: Any = None,
        **_: Any,
    ) -> ModelResponse:
        """One non-streaming chat call; raises ``OllamaChatError`` on any failure."""
        from nexura_guardrails.classifiers._base import (
            _CallError,
            _reported_model,
            _truncated,
            _usage,
            post_json,
        )

        max_tokens = getattr(model_settings, "max_tokens", None)
        body = self.payload(messages, response_schema=response_schema, max_tokens=max_tokens)
        try:
            data = await post_json(
                self.base_url + "/api/chat",
                body,
                transport=self.transport,
                timeout=self.timeout,
            )
        except _CallError as exc:
            raise OllamaChatError(exc.error_type, exc.detail) from None
        if not isinstance(data, dict):
            raise OllamaChatError("MalformedResponse", "response body is not a JSON object")
        if "error" in data:
            raise OllamaChatError("ServerError", "the server returned an error")
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaChatError("MalformedResponse", "no message content")
        if data.get("done") is not True:
            raise OllamaChatError("MalformedResponse", "response is not complete")
        if _truncated(data):
            raise OllamaChatError("PromptTruncated", "the server truncated the prompt")
        reported = _reported_model(data)
        if reported is not None and reported.removesuffix(":latest") != self.model.removesuffix(
            ":latest"
        ):
            raise OllamaChatError("ModelMismatch", "the reply came from a different model")
        reason = data.get("done_reason")
        return ModelResponse(
            message=Message(role="assistant", content=content),
            usage=_usage(data.get("prompt_eval_count"), data.get("eval_count")),
            finish_reason=reason if isinstance(reason, str) else None,
        )
