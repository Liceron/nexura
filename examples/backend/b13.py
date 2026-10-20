"""B13: Sarvam Text and Translation Standalone Example.

Demonstrates:
1. Keyless SarvamProvider initialized with deterministic httpx.MockTransport.
2. High-fidelity Indic translation with template identifier & URL preservation.
3. Chat generation in Hindi / code-mixed Hinglish via Sarvam LLM.
4. Eager rejection of unsupported non-Indic language codes.
5. Explicit notice that synthetic mock responses claim zero semantic quality.
6. Documented live credentialed smoke invocation.
7. Execution via B01 Module Interface and CLI.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.errors import CapabilityError
from nexura_runtime.providers.sarvam.module import SarvamProviderModule
from nexura_runtime.providers.sarvam.provider import SarvamProvider
from nexura_runtime.providers.sarvam.types import TranslationRequest
from nexura_runtime.providers.types import GenerationRequest
from nexura_runtime.types import Message

from nexura_runtime.modules.registry import default_registry


def create_sarvam_demo_mock_transport() -> httpx.MockTransport:
    """Deterministic mock transport handling both translation and chat."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8"))

        if "/translate" in path:
            input_text = body.get("input", "")
            tgt = body.get("target_language_code")
            # Synthetic translation preserving placeholders
            if tgt == "hi-IN":
                translated = (
                    input_text.replace("Hello", "नमस्ते")
                    .replace("your order", "आपका ऑर्डर")
                    .replace("has shipped via", "के माध्यम से भेज दिया गया है")
                    .replace("for", "के लिए")
                )
            else:
                translated = input_text.replace("नमस्ते", "Hello").replace("सहायता", "assistance")

            return httpx.Response(
                200,
                json={
                    "translated_text": translated,
                    "source_language_code": body.get("source_language_code"),
                    "target_language_code": tgt,
                    "usage": {
                        "prompt_tokens": len(input_text.split()),
                        "completion_tokens": len(translated.split()),
                        "total_tokens": len(input_text.split()) + len(translated.split()),
                    },
                },
            )

        # Chat completions
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-sarvam-demo-01",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": ("हाँ, मैं आपकी सहायता के लिए उपस्थित हूँ। कृपया अपना प्रश्न पूछें।"),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 16,
                    "total_tokens": 28,
                },
            },
        )

    return httpx.MockTransport(handler)


async def run_b13_demo() -> None:
    print("=== B13: Sarvam Text and Translation Demonstration ===\n")

    # 1. Keyless Provider Initialization
    print("1. Initializing SarvamProvider with MockTransport (Keyless)...")
    mock_transport = create_sarvam_demo_mock_transport()
    provider = SarvamProvider(
        model="sarvam-2b",
        transport=mock_transport,
    )
    print(f"  Provider initialized: {provider}")

    # 2. Translation with Identifier Preservation
    print("\n2. Executing Indic Translation with Identifier Preservation...")
    support_snippet = (
        "Hello {{customer_name}}, your order #BLR-9042 for ₹1,499.00 has shipped "
        "via https://track.delivery.in."
    )
    print(f"  Source Input (en-IN): {support_snippet}")

    trans_req = TranslationRequest(
        input_text=support_snippet,
        source_language_code="en-IN",
        target_language_code="hi-IN",
        preserve_identifiers=True,
    )
    trans_res = await provider.translate(trans_req)

    print(f"  Translated Output (hi-IN): {trans_res.translated_text}")
    print(f"  Characters Processed: {trans_res.characters_translated}")
    print(f"  Reported Usage: {trans_res.usage}")

    # Verify that template variable, order id, currency, and url were 100% preserved
    assert "{{customer_name}}" in trans_res.translated_text
    assert "#BLR-9042" in trans_res.translated_text
    assert "₹1,499.00" in trans_res.translated_text
    assert "https://track.delivery.in" in trans_res.translated_text
    print("  [SUCCESS] All template identifiers, order IDs, and URLs preserved intact.")

    # 3. Chat Generation in Hindi
    print("\n3. Executing Sarvam Indic Chat Completion...")
    chat_req = GenerationRequest(
        messages=[Message(role="user", content="नमस्ते, क्या आप मेरी मदद कर सकते हैं?")]
    )
    chat_resp = await provider.generate(chat_req)
    print(f"  Assistant Response: {chat_resp.message.content}")
    print(f"  Chat Usage: {chat_resp.usage}")
    print(f"  Finish Reason: {chat_resp.finish_reason}")

    # 4. Eager Rejection of Unsupported Languages
    print("\n4. Demonstrating Eager Rejection of Unsupported Languages...")
    bad_req = TranslationRequest(
        input_text="Bonjour le monde",
        source_language_code="fr-FR",
        target_language_code="hi-IN",
    )
    try:
        await provider.translate(bad_req)
    except CapabilityError as exc:
        print(f"  Correctly rejected unsupported language 'fr-FR': {exc.message}")

    # 5. Semantic Quality Disclaimer
    print("\n5. Verification Disclaimer:")
    print("   [NOTE] All integration verification above uses local labeled mock transports.")
    print("   No claims are made regarding semantic model quality from synthetic fixtures.")

    # 6. Production Credentialed Smoke Invocation
    print("\n6. Production Credentialed Smoke Invocation:")
    print("   ==============================================================")
    print("   export SARVAM_API_KEY='sarvam-...'")
    print('   python -c "')
    print("   import os, asyncio")
    print("   from nexura_runtime.providers.sarvam import SarvamProvider, TranslationRequest")
    print("   async def run():")
    print("       key = os.environ['SARVAM_API_KEY']")
    print("       p = SarvamProvider(api_key=key)")
    print(
        "       req = TranslationRequest(input_text='Hello world', "
        "source_language_code='en-IN', target_language_code='hi-IN')"
    )
    print("       r = await p.translate(req)")
    print("       print(r.translated_text)")
    print("   asyncio.run(run())")
    print('   "')
    print("   ==============================================================\n")

    # 7. Execute via B01 Module Interface & CLI
    print("7. Executing SarvamProviderModule via B01 Module Registry...")
    module = default_registry.get("nexura.models.sarvam")
    assert isinstance(module, SarvamProviderModule)
    ctx = ExecutionContext(run_id="run_b13_demo", node_id="node_b13")
    mod_result = await module.execute(
        ctx,
        {
            "operation": "translate",
            "input_text": "Order #550 ready",
            "source_language_code": "en-IN",
            "target_language_code": "hi-IN",
        },
        {},
    )
    print(f"  Module invocation status: {mod_result.status}")
    assert mod_result.status == "success"

    print("\n8. Executing via B01 CLI...")
    from nexura_runtime.modules.cli import main as cli_main

    cli_args = [
        "run",
        "nexura.models.sarvam",
        "--input",
        json.dumps(
            {
                "operation": "translate",
                "input_text": "Welcome to Nexura",
                "source_language_code": "en-IN",
                "target_language_code": "hi-IN",
            }
        ),
    ]
    orig_argv = sys.argv
    sys.argv = ["nexura-module"] + cli_args
    try:
        cli_code = cli_main()
        print(f"\nCLI Execution: {'SUCCESS' if cli_code == 0 else 'FAILURE'} (code {cli_code})")
        assert cli_code == 0
    finally:
        sys.argv = orig_argv

    print("\n=== B13 Example Completed Successfully ===")


def main() -> None:
    asyncio.run(run_b13_demo())


if __name__ == "__main__":
    main()
