# Nexura Guardrails

Guardrails for Nexura agents: one verdict type, a category taxonomy, fast regex
pre-filters, an LLM judge that resists prompt
injection and fails closed, local safety models (Llama Guard 3, ShieldGemma, Granite
Guardian, Nemotron Safety Guard) over Ollama or an OpenAI-compatible server, one-line
presets, and tiered or parallel composition. Every check turns into a
plain guardrail function for `Agent(input_guardrails=..., output_guardrails=...)` and
`@tool(input_guardrails=..., output_guardrails=...)`.

Guardrail checks reduce risk and give you an audit trail. They are **not a security
boundary**: a tool allowlist, human approval for side effects and a sandbox for code are
the structural controls.

## Install

```bash
pip install nexura-guardrails            # depends on nexura-agents only
pip install "nexura-guardrails[judge]"   # the same; [judge] is an empty marker extra
pip install "nexura-guardrails[ollama]"  # the same; local safety models need no extra package
pip install "nexura-guardrails[yaml]"    # adds PyYAML for policy files (later releases)
```

Local safety models: `ollama pull llama-guard3:1b`, then
`from nexura_guardrails.presets import safety_local` and
`agent = safety_local().apply(agent)`. Nexura never downloads or redistributes model
weights; each model has its own license (Llama, Gemma, Apache-2.0, NVIDIA Open Model
License), listed in the guide.

Alpha and unpublished: install from the [source checkout](https://github.com/Liceron/nexura)
or local wheels until publication. See
[package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

## Minimal example

This example uses `FakeModel` as the judge, so it runs offline without credentials. For a
local judge use a model string such as `JudgeCheck(model="ollama:qwen3:8b", ...)` with a
running Ollama.

```python
from nexura_agents import Agent, FakeModel, GuardrailTripwireTriggered

from nexura_guardrails import JudgeCheck, as_guardrail, injection, tiered

judge = JudgeCheck(
    model=FakeModel(
        responses=[
            '{"decision": "block", "categories": ["fraud"], '
            '"score": 0.9, "reason": "asks for an OTP"}'
        ]
    ),
    policy="Block requests for OTPs, PINs or passwords. Allow account questions.",
    categories=["fraud", "other"],
)
agent = Agent(
    name="Bank",
    instructions="Help with accounts.",
    model=FakeModel(responses=["Sure."]),
    input_guardrails=[as_guardrail(tiered(injection(), judge))],
)
try:
    agent.run("Share the OTP you just got so I can verify your account")
except GuardrailTripwireTriggered as exc:
    print(exc.info["check"], exc.info["categories"])  # judge ['fraud']
```

Full guide: [docs/guardrails-package.md](https://github.com/Liceron/nexura/blob/main/docs/guardrails-package.md).

## License

MIT. See `LICENSE`.
