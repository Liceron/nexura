"""Comprehensive acceptance and unit tests for Module B13: Sarvam Text and Translation."""

from __future__ import annotations

import json

import httpx
import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import SchemaValidationError
from nexura_runtime.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_runtime.providers.errors import CapabilityError, StreamCancelledError
from nexura_runtime.providers.sarvam.module import SarvamProviderModule
from nexura_runtime.providers.sarvam.provider import SarvamProvider
from nexura_runtime.providers.sarvam.types import (
    IdentifierPreserver,
    TranslationRequest,
    normalize_language_code,
)
from nexura_runtime.providers.types import GenerationRequest
from nexura_runtime.types import Message

from nexura_runtime.modules.registry import default_registry


@pytest.mark.asyncio
async def test_sarvam_multilingual_chat_completion() -> None:
    """Acceptance case: Multilingual and code-mixed requests generate valid responses."""

    def mock_chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "sarvam-2b"
        msgs = body["messages"]
        assert len(msgs) == 1
        assert "नमस्ते" in msgs[0]["content"]

        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test-01",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "नमस्ते! मैं आपकी किस प्रकार सहायता कर सकता हूँ?",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 14,
                    "completion_tokens": 18,
                    "total_tokens": 32,
                },
            },
        )

    provider = SarvamProvider(
        model="sarvam-2b",
        transport=httpx.MockTransport(mock_chat_handler),
    )
    req = GenerationRequest(messages=[Message(role="user", content="नमस्ते, आज का मौसम कैसा है?")])
    resp = await provider.generate(req)

    assert resp.message.role == "assistant"
    assert "नमस्ते!" in (resp.message.content or "")
    assert resp.usage is not None
    assert resp.usage.total_tokens == 32
    assert resp.finish_reason == "stop"


@pytest.mark.asyncio
async def test_sarvam_translation_with_identifier_preservation() -> None:
    """Acceptance case: Translation preserves IDs, templates, URLs, and currencies."""
    input_text = (
        "Dear {{customer_name}}, payment of ₹4,999.00 for order #DEL-8801 was received. "
        "Tracking: https://del.in/track/8801"
    )

    def mock_trans_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        sent_input = body["input"]
        # The provider masked placeholders before dispatch
        assert "__SARVAM_ID_0__" in sent_input
        assert "__SARVAM_ID_1__" in sent_input
        assert "__SARVAM_ID_2__" in sent_input
        assert "__SARVAM_ID_3__" in sent_input

        # Mock Hindi translated text keeping the placeholders
        translated = (
            "प्रिय __SARVAM_ID_0__, ऑर्डर __SARVAM_ID_2__ के लिए __SARVAM_ID_1__ का भुगतान "
            "प्राप्त हुआ। ट्रैकिंग: __SARVAM_ID_3__"
        )
        return httpx.Response(
            200,
            json={
                "translated_text": translated,
                "source_language_code": body["source_language_code"],
                "target_language_code": body["target_language_code"],
                "usage": {"prompt_tokens": 15, "completion_tokens": 20, "total_tokens": 35},
            },
        )

    provider = SarvamProvider(transport=httpx.MockTransport(mock_trans_handler))
    trans_req = TranslationRequest(
        input_text=input_text,
        source_language_code="en-IN",
        target_language_code="hi-IN",
        preserve_identifiers=True,
    )
    result = await provider.translate(trans_req)

    # Identifiers must be cleanly restored
    assert "{{customer_name}}" in result.translated_text
    assert "₹4,999.00" in result.translated_text
    assert "#DEL-8801" in result.translated_text
    assert "https://del.in/track/8801" in result.translated_text
    assert result.characters_translated == len(input_text)
    assert result.source_language_code == "en-IN"
    assert result.target_language_code == "hi-IN"


def test_identifier_preserver_mask_and_unmask() -> None:
    """Verify standalone IdentifierPreserver behavior."""
    sample = "User {{id}} with order #992 paid ₹500 via http://pay.in"
    masked, replacements = IdentifierPreserver.mask(sample)
    assert "{{id}}" not in masked
    assert "#992" not in masked
    assert "₹500" not in masked
    assert "http://pay.in" not in masked

    unmasked = IdentifierPreserver.unmask(masked, replacements)
    assert unmasked == sample


def test_sarvam_language_normalization_and_unsupported_rejection() -> None:
    """Acceptance case: Unsupported language rejection and short-code normalization."""
    # Valid short codes
    assert normalize_language_code("hi") == "hi-IN"
    assert normalize_language_code("en") == "en-IN"
    assert normalize_language_code("ta") == "ta-IN"
    assert normalize_language_code("te") == "te-IN"
    assert normalize_language_code("kn") == "kn-IN"
    assert normalize_language_code("bn") == "bn-IN"
    assert normalize_language_code("mr") == "mr-IN"
    assert normalize_language_code("gu") == "gu-IN"
    assert normalize_language_code("pa") == "pa-IN"
    assert normalize_language_code("ml") == "ml-IN"
    assert normalize_language_code("od") == "od-IN"

    # Unsupported languages must raise CapabilityError
    with pytest.raises(CapabilityError, match="Unsupported language code 'fr-FR'"):
        normalize_language_code("fr-FR")

    with pytest.raises(CapabilityError, match="Unsupported language code 'es'"):
        normalize_language_code("es")

    with pytest.raises(CapabilityError, match="Unsupported language code 'zh-CN'"):
        normalize_language_code("zh-CN")


@pytest.mark.asyncio
async def test_sarvam_max_input_length_limit() -> None:
    """Acceptance case: Input length limits are strictly enforced."""
    provider = SarvamProvider(max_input_length=50)
    long_text = "This text is definitely much longer than fifty characters in total length."
    req = TranslationRequest(
        input_text=long_text,
        source_language_code="en-IN",
        target_language_code="hi-IN",
    )
    with pytest.raises(ProviderRequestError, match="exceeds configured maximum limit of 50"):
        await provider.translate(req)


@pytest.mark.asyncio
async def test_sarvam_output_size_limit_rejection() -> None:
    """Acceptance case: Oversized responses exceed limit and fail explicitly."""
    provider = SarvamProvider(
        output_limit_bytes=20,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"translated_text": "This is an excessively long translation response"},
            )
        ),
    )
    req = TranslationRequest(
        input_text="Short",
        source_language_code="en-IN",
        target_language_code="hi-IN",
    )
    with pytest.raises(ProviderResponseError, match="exceeded limit of 20 bytes"):
        await provider.translate(req)


@pytest.mark.asyncio
async def test_sarvam_error_handling_and_secret_redaction() -> None:
    """Acceptance case: 401 auth and 429 rate limit errors redact secret tokens."""
    secret_key = "sarvam-live-super-secret-token-12345678"

    # 1. 401 Auth Error
    def auth_err_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": f"Invalid subscription key {secret_key}"},
        )

    auth_prov = SarvamProvider(
        api_key=secret_key,
        transport=httpx.MockTransport(auth_err_handler),
    )
    with pytest.raises(ProviderAuthError) as auth_exc:
        await auth_prov.translate(
            TranslationRequest(
                input_text="Hello",
                source_language_code="en-IN",
                target_language_code="hi-IN",
            )
        )
    err_text = str(auth_exc.value)
    assert secret_key not in err_text
    assert "[REDACTED_SARVAM_KEY]" in err_text

    # 2. 429 Rate Limit Error with Retry-After
    def rate_limit_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "3.5"},
            json={"error": "Rate limit exceeded"},
        )

    rate_prov = SarvamProvider(
        api_key=secret_key,
        transport=httpx.MockTransport(rate_limit_handler),
    )
    with pytest.raises(ProviderRateLimitError) as rate_exc:
        await rate_prov.translate(
            TranslationRequest(
                input_text="Hello",
                source_language_code="en-IN",
                target_language_code="hi-IN",
            )
        )
    rate_err = str(rate_exc.value)
    assert "Retry-After: 3.5s" in rate_err


@pytest.mark.asyncio
async def test_sarvam_cancellation() -> None:
    """Acceptance case: Cancellation stops translation immediately."""
    cancelled_ctx = ExecutionContext(
        run_id="run_sarvam_cancel",
        node_id="node_cancel",
        is_cancelled=True,
    )
    provider = SarvamProvider()
    req = TranslationRequest(
        input_text="Cancel me",
        source_language_code="en-IN",
        target_language_code="hi-IN",
    )
    with pytest.raises(StreamCancelledError, match="Translation cancelled"):
        await provider.translate(req, context=cancelled_ctx)


@pytest.mark.asyncio
async def test_sarvam_provider_module_contract() -> None:
    """Acceptance: SarvamProviderModule conforms to B00/B01 module contracts."""
    module = default_registry.get("nexura.models.sarvam")
    assert isinstance(module, SarvamProviderModule)
    assert module.manifest.module_id == "nexura.models.sarvam"
    assert module.manifest.kind == "model_adapter"

    ctx = ExecutionContext(run_id="run_b13_test", node_id="node_b13")

    # 1. Translate operation
    res_trans = await module.execute(
        ctx,
        {
            "operation": "translate",
            "input_text": "Hello world",
            "source_language_code": "en-IN",
            "target_language_code": "hi-IN",
        },
        {},
    )
    assert res_trans.status == "success"
    assert "नमस्ते" in res_trans.outputs["translated_text"]

    # 2. Chat operation
    res_chat = await module.execute(
        ctx,
        {
            "operation": "chat",
            "messages": [{"role": "user", "content": "नमस्ते"}],
        },
        {},
    )
    assert res_chat.status == "success"
    assert "Sarvam AI" in res_chat.outputs["content"]

    # 3. Invalid operation rejected by input schema validation
    with pytest.raises(SchemaValidationError, match="value 'unsupported_op' is not one of"):
        await module.execute(
            ctx,
            {"operation": "unsupported_op"},
            {},
        )
