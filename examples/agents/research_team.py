"""Research team: sequential pipeline plus parallel fan-out, with tracing.

Scenario: three source agents (web, papers, news) are queried in parallel and
their findings merged by a ``combine`` function. A SequentialAgent then runs a
Researcher (which uses the parallel fan-out as a tool) and passes its notes to a
Writer. If ``opentelemetry-sdk`` is installed, tracing is enabled with an
in-memory exporter and the span tree is printed; otherwise that part is skipped.

Demonstrates: ``SequentialAgent``, ``ParallelAgent(combine=...)``,
``team.as_tool()``, ``nexura_agents.tracing.instrument``.

Offline: all models are scripted ``FakeModel`` instances. To use a real model:
# model="openai:gpt-4.1-mini"
"""

from nexura_agents import Agent, FakeModel, ParallelAgent, SequentialAgent, ToolCall


def source(name: str, finding: str) -> Agent:
    return Agent(name=name, instructions=f"Search {name}.", model=FakeModel(responses=[finding]))


def build_team() -> SequentialAgent:
    fanout = ParallelAgent(
        "Sources",
        [
            source("Web", "Tides are driven by the moon."),
            source("Papers", "Spring tides follow new and full moons."),
            source("News", "Record high tide hit the coast."),
        ],
        combine=lambda results: "\n".join(f"[{n}] {r.final_output}" for n, r in results.items()),
        description="Query all sources at once.",
    )
    researcher = Agent(
        name="Researcher",
        instructions="Gather facts using the sources tool.",
        model=FakeModel(
            responses=[
                [ToolCall(id="s1", name="sources", arguments={"request": "tides"})],
                "Notes: moon-driven tides, spring tides, record high tide.",
            ]
        ),
        tools=[fanout.as_tool()],
    )
    writer = Agent(
        name="Writer",
        instructions="Write a short post from the notes.",
        model=FakeModel(
            responses=["Post: the moon drives the tides, strongest at new and full moon."]
        ),
    )
    return SequentialAgent("ResearchTeam", [researcher, writer])


def print_tree(spans) -> None:
    by_id = {s.context.span_id: s for s in spans}
    children: dict = {}
    for s in spans:
        parent = s.parent.span_id if s.parent and s.parent.span_id in by_id else None
        children.setdefault(parent, []).append(s)

    def walk(parent, depth: int) -> None:
        for s in sorted(children.get(parent, []), key=lambda x: x.start_time):
            print("  " * depth + s.name)
            walk(s.context.span_id, depth + 1)

    walk(None, 0)


def main() -> None:
    exporter = None
    try:
        from nexura_agents.tracing import instrument
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrument(tracer_provider=provider)
    except ImportError:
        print("opentelemetry-sdk not installed: skipping tracing")

    result = build_team().run("Write about tides")
    assert result.status == "completed"
    print("steps:", [(s.path, s.member) for s in result.steps])
    print("post:", result.output)

    if exporter is not None:
        from nexura_agents.tracing import uninstrument

        uninstrument()
        print("span tree:")
        print_tree(exporter.get_finished_spans())
    print("RESEARCH TEAM DONE")


if __name__ == "__main__":
    main()
