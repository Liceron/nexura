"""Standalone executable example for B00: Typed module contracts.

Demonstrates a pure text normalization module running standalone in Python
without Studio, databases, or external network calls.
"""

from __future__ import annotations

import asyncio
import unicodedata
from typing import Any, Literal, cast

from nexura_runtime.contracts import (
    BaseModule,
    ExecutionContext,
    ModuleManifest,
    ModuleResult,
    PortDefinition,
)


class TextNormalizeModule(BaseModule):
    """Normalizes Unicode text using NFC/NFKC/NFD/NFKD forms."""

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            schema_version=1,
            module_id="nexura.text.normalize",
            version="1.0.0",
            kind="transform",
            title="Text Normalization Module",
            description=(
                "Pure transform normalizing Unicode text (including Indic scripts and diacritics)."
            ),
            effect_policy="pure",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            config_schema={
                "type": "object",
                "properties": {"form": {"type": "string", "enum": ["NFC", "NFKC", "NFD", "NFKD"]}},
                "required": ["form"],
                "additionalProperties": False,
            },
            named_ports=[
                PortDefinition(name="text", type="string", description="Text payload to normalize"),
            ],
            capabilities={"network": False, "streaming": False},
            required_credential_types=[],
            supported_locales=["en", "hi", "mr", "ta", "te", "bn"],
            supported_modalities=["text"],
            verification_status="mock_verified",
        )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        text: str = inputs["text"]
        form: str = config["form"]
        normalized = unicodedata.normalize(cast(Literal["NFC", "NFKC", "NFD", "NFKD"], form), text)
        return ModuleResult.success(outputs={"text": normalized})


async def main() -> None:
    module = TextNormalizeModule()
    manifest = module.manifest

    print(f"Loaded Module: {manifest.module_id} v{manifest.version}")
    print(f"Manifest Digest: {manifest.digest}")
    print(f"Effect Policy: {manifest.effect_policy}")

    # Set up execution context
    context = ExecutionContext(
        workspace_id="ws_local",
        principal_id="user_admin",
        run_id="run_b00_demo",
        node_id="node_normalize_1",
        attempt_id="att_1",
    )

    # Input containing combining accents and Indic Devanagari text
    raw_input = {"text": "e\u0301cole — नमस्ते दुनिया"}
    config = {"form": "NFC"}

    result = await module.execute(context, raw_input, config)

    print(f"Execution Status: {result.status}")
    print(f"Output: {result.outputs}")

    assert result.status == "success"
    assert result.outputs["text"] == "école — नमस्ते दुनिया"
    print("B00 standalone example executed and verified successfully.")


if __name__ == "__main__":
    asyncio.run(main())
