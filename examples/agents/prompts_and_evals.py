"""Prompt versions promoted by label, compared on the same cases.

Scenario: a support prompt has two versions in a ``PromptRegistry``. Version 1
is wordy; version 2 asks for brevity. Labels "production" and "candidate" point
at them. The same eval cases run against both labels and a simple scoring
function (must mention the keyword, stay under a length limit) compares them.
Because the model is scripted, each version gets canned replies that mimic what
its prompt would produce; with a real model you would drop that scripting.

Demonstrates: ``PromptRegistry.register`` / ``set_label`` / ``instructions``,
``RunResult.prompt`` (name and version used), a tiny eval loop.

To use a real model instead of the fake one:
# model="openai:gpt-4.1-mini"
"""

from nexura_agents import Agent, FakeModel, PromptRegistry, Runner

CASES = [
    {"question": "How do I reset my password?", "keyword": "reset link"},
    {"question": "Where is my invoice?", "keyword": "billing page"},
]

REPLIES = {
    1: [
        "Thank you so much for contacting us today! We are delighted to help. To get going, "
        "please open the login screen, then choose Forgot password, and we will email you a "
        "reset link which you can follow to choose a brand new password for your account.",
        "Hello and welcome! Your invoice can be found by signing in and visiting the billing "
        "page, where every past invoice is listed and can be downloaded as a PDF document.",
    ],
    2: [
        "Use Forgot password to get a reset link.",
        "Open the billing page to download invoices.",
    ],
}


def score(reply: str, keyword: str) -> float:
    """1 point for the keyword, 1 point for brevity (under 120 chars), out of 2."""
    return float(keyword in reply) + float(len(reply) < 120)


def evaluate(registry: PromptRegistry, label: str, version_replies: list[str]) -> tuple[int, float]:
    agent = Agent(
        name="support",
        model=FakeModel(responses=list(version_replies)),
        instructions=registry.instructions("support", label=label, variables={"product": "Nexura"}),
    )
    runner = Runner()
    total, version = 0.0, 0
    for case in CASES:
        result = runner.run(agent, case["question"])
        version = result.prompt["version"]
        total += score(result.final_output, case["keyword"])
    return version, total / len(CASES)


def main() -> None:
    registry = PromptRegistry()
    registry.register("support", "You are a friendly {{ product }} support agent. Be thorough.")
    registry.register("support", "You are a {{ product }} support agent. Answer in one sentence.")
    registry.set_label("support", "production", 1)
    registry.set_label("support", "candidate", 2)

    results = {}
    for label in ("production", "candidate"):
        version = registry.get("support", label=label).version
        results[label] = evaluate(registry, label, REPLIES[version])
        print(f"{label}: version {results[label][0]} mean score {results[label][1]:.2f}")

    winner = max(results, key=lambda k: results[k][1])
    print("winner:", winner)
    if winner == "candidate":
        registry.set_label(
            "support", "production", registry.get("support", label="candidate").version
        )
        print("promoted: production now ->", registry.get("support", label="production").version)
    assert registry.get("support", label="production").version == 2
    print("PROMPT EVALS OK")


if __name__ == "__main__":
    main()
