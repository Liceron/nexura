# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline handlers behind the starter templates.

Every handler here is deterministic and makes no network call. Steps that stand for an
LLM call run a real ``Agent`` on a scripted ``FakeModel`` (the reply is computed from the
workflow state), so the template runs the same code path an agent step would, without a
provider. Steps that stand for an external system (a payment API, a mail server) only
record what they would do. To use a template with real systems, register your own handlers
under the same names, or edit the instantiated workflow to point at your modules.
"""

from __future__ import annotations

import re
from typing import Any

from nexura_agents.agent import Agent
from nexura_agents.models.fake import FakeModel
from nexura_agents.runner import Runner
from pydantic import BaseModel, ValidationError

from nexura_runtime.studio.traces import traced_run
from nexura_runtime.templates.library import LIBRARY_HANDLERS
from nexura_runtime.workflow import HandlerOutput, ModuleRegistry

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "do",
        "does",
        "did",
        "can",
        "i",
        "my",
        "our",
        "your",
    }
)


async def _agent_says(name: str, instructions: str, prompt: str, reply: str) -> str:
    """Run one agent turn on a scripted FakeModel and return its final text."""
    agent = Agent(name=name, instructions=instructions, model=FakeModel(responses=[reply]))
    result = await traced_run(Runner(), agent, prompt)
    return str(result.final_output)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


# --- support desk ---


def tpl_support_classify(state: dict[str, Any]) -> HandlerOutput:
    """Route a message: refund requests need approval, everything else is answered."""
    message = str(state.get("message", ""))
    wants_refund = any(word in message.lower() for word in ("refund", "money back", "chargeback"))
    return HandlerOutput(
        state_update={"intent": "refund" if wants_refund else "question"},
        branch="refund" if wants_refund else "answer",
    )


def tpl_support_issue_refund(state: dict[str, Any]) -> dict[str, Any]:
    """Record the refund. Gated by approval; no payment system is called."""
    order_id = str(state.get("order_id", "UNKNOWN"))
    amount = float(state.get("amount", 0))
    return {
        "refund": {"order_id": order_id, "amount": amount, "status": "issued", "simulated": True}
    }


async def tpl_support_reply(state: dict[str, Any]) -> dict[str, Any]:
    """Write the customer reply (a FakeModel agent turn)."""
    customer = str(state.get("customer", "there"))
    refund = state.get("refund")
    if refund:
        reply = (
            f"Hi {customer}, your refund of {refund['amount']:g} for order "
            f"{refund['order_id']} has been approved and issued."
        )
    else:
        reply = f"Hi {customer}, thanks for getting in touch. Our team will answer shortly."
    text = await _agent_says(
        "SupportAgent", "Reply politely and briefly.", str(state.get("message", "")), reply
    )
    return {"reply": text}


# --- RAG with citations ---


def _passages(documents: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for index, doc in enumerate(documents):
        doc_id = str(doc.get("id", f"doc{index + 1}"))
        title = str(doc.get("title", doc_id))
        for part, chunk in enumerate(re.split(r"\n\s*\n", str(doc.get("text", "")))):
            if chunk.strip():
                out.append(
                    {"doc_id": doc_id, "title": title, "chunk": str(part), "text": chunk.strip()}
                )
    return out


def tpl_rag_retrieve(state: dict[str, Any]) -> HandlerOutput:
    """Rank passages of the supplied documents by word overlap with the question."""
    documents = state.get("documents") or []
    question_words = _tokens(str(state.get("question", "")))
    scored = []
    for passage in _passages(list(documents)):
        score = len(question_words & _tokens(passage["text"]))
        if score:
            scored.append((score, passage))
    scored.sort(key=lambda pair: -pair[0])
    top = [{**p, "score": s, "citation": f"[{i + 1}]"} for i, (s, p) in enumerate(scored[:3])]
    return HandlerOutput(state_update={"passages": top}, branch="found" if top else "none")


async def tpl_rag_answer(state: dict[str, Any]) -> dict[str, Any]:
    """Answer from the best passage and cite every passage used."""
    passages: list[dict[str, Any]] = state["passages"]
    reply = f"{passages[0]['text']} " + "".join(p["citation"] for p in passages)
    answer = await _agent_says(
        "RagAgent",
        "Answer only from the numbered passages and cite them.",
        str(state.get("question", "")),
        reply,
    )
    citations = [
        {"citation": p["citation"], "doc_id": p["doc_id"], "title": p["title"]} for p in passages
    ]
    return {"answer": answer, "citations": citations}


def tpl_rag_no_answer(state: dict[str, Any]) -> dict[str, Any]:
    """Refuse to guess when no passage matches."""
    return {
        "answer": "I could not find this in the uploaded documents.",
        "citations": [],
    }


# --- research team (sequential agents) ---


async def tpl_research_plan(state: dict[str, Any]) -> dict[str, Any]:
    topic = str(state.get("topic", "the topic"))
    questions = [
        f"What is {topic}?",
        f"Why does {topic} matter?",
        f"What are the risks of {topic}?",
    ]
    await _agent_says("Planner", "Split the topic into questions.", topic, "\n".join(questions))
    return {"questions": questions}


async def tpl_research_gather(state: dict[str, Any]) -> dict[str, Any]:
    sources = [str(s) for s in state.get("sources", [])]
    notes = [
        f"{q} -> {sources[i % len(sources)] if sources else 'no source supplied'}"
        for i, q in enumerate(state.get("questions", []))
    ]
    await _agent_says(
        "Researcher", "Collect notes per question.", "\n".join(notes), "\n".join(notes)
    )
    return {"notes": notes}


async def tpl_research_write(state: dict[str, Any]) -> dict[str, Any]:
    topic = str(state.get("topic", "the topic"))
    draft = f"# {topic}\n\n" + "\n".join(f"- {n}" for n in state.get("notes", []))
    return {"draft": await _agent_says("Writer", "Write a short brief.", topic, draft)}


async def tpl_research_review(state: dict[str, Any]) -> dict[str, Any]:
    draft = str(state.get("draft", ""))
    missing = "no source supplied" in draft
    verdict = "needs_sources" if missing else "approved"
    await _agent_says("Reviewer", "Check claims have sources.", draft, verdict)
    return {"verdict": verdict, "report": draft}


# --- approval workflow ---


def tpl_request_intake(state: dict[str, Any]) -> HandlerOutput:
    """Classify a request by amount; requests of 1000 or more need a human decision."""
    amount = float(state.get("amount", 0))
    risky = amount >= float(state.get("approval_threshold", 1000))
    return HandlerOutput(
        state_update={"risk": "high" if risky else "low"},
        branch="needs_approval" if risky else "auto",
    )


def tpl_request_execute(state: dict[str, Any]) -> dict[str, Any]:
    """Carry out the request. Gated by approval; nothing external is called."""
    return {"executed": True, "execution": {"title": state.get("title", ""), "simulated": True}}


def tpl_request_notify(state: dict[str, Any]) -> dict[str, Any]:
    outcome = "executed after approval" if state.get("executed") else "auto-approved (low risk)"
    return {"notification": f"Request '{state.get('title', '')}' {outcome}."}


# --- scheduled report ---

_DEFAULT_METRICS = [
    {"day": "Mon", "signups": 12, "revenue": 340.0},
    {"day": "Tue", "signups": 18, "revenue": 410.0},
    {"day": "Wed", "signups": 9, "revenue": 295.0},
]


def tpl_report_collect(state: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = state.get("metrics") or _DEFAULT_METRICS
    return {
        "totals": {
            "signups": sum(int(r.get("signups", 0)) for r in rows),
            "revenue": round(sum(float(r.get("revenue", 0)) for r in rows), 2),
            "days": len(rows),
        }
    }


async def tpl_report_summarize(state: dict[str, Any]) -> dict[str, Any]:
    totals = state["totals"]
    text = f"{totals['days']} days: {totals['signups']} signups, revenue {totals['revenue']:g}."
    return {"summary": await _agent_says("Analyst", "Summarise the numbers.", str(totals), text)}


def tpl_report_deliver(state: dict[str, Any]) -> dict[str, Any]:
    """Queue the report in a dry-run outbox. Nothing is sent."""
    return {
        "delivery": {
            "channel": str(state.get("channel", "outbox")),
            "subject": "Scheduled report",
            "body": state.get("summary", ""),
            "sent": False,
            "mode": "dry_run",
        }
    }


# --- structured extraction ---


class Invoice(BaseModel):
    """The structured output the extraction template produces."""

    invoice_number: str
    vendor: str
    date: str
    currency: str
    total: float


_INVOICE_PATTERNS = {
    "invoice_number": re.compile(r"invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9\-]+)", re.I),
    "vendor": re.compile(r"vendor\s*:\s*(.+)", re.I),
    "date": re.compile(r"date\s*:\s*(\d{4}-\d{2}-\d{2})", re.I),
    "total": re.compile(r"total\s*:\s*([A-Z]{3})?\s*([0-9][0-9,]*\.?[0-9]*)", re.I),
}


def tpl_extract_fields(state: dict[str, Any]) -> dict[str, Any]:
    text = str(state.get("text", ""))
    found: dict[str, Any] = {}
    for key, pattern in _INVOICE_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        if key == "total":
            found["currency"] = (match.group(1) or "USD").upper()
            found["total"] = match.group(2).replace(",", "")
        else:
            found[key] = match.group(1).strip()
    return {"extracted": found}


def tpl_extract_validate(state: dict[str, Any]) -> HandlerOutput:
    try:
        record = Invoice.model_validate(state.get("extracted", {}))
    except ValidationError as exc:
        errors = [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors()]
        return HandlerOutput(state_update={"errors": errors}, branch="invalid")
    return HandlerOutput(state_update={"record": record.model_dump()}, branch="valid")


def tpl_extract_finalize(state: dict[str, Any]) -> dict[str, Any]:
    return {"status": "extracted"}


def tpl_extract_flag(state: dict[str, Any]) -> dict[str, Any]:
    return {"status": "needs_review"}


HANDLERS: dict[str, Any] = {
    name: fn for name, fn in sorted(globals().items()) if name.startswith("tpl_") and callable(fn)
}
# The business templates (support, sales, HR, finance, engineering, content, data, governance).
HANDLERS.update(LIBRARY_HANDLERS)


def template_registry() -> ModuleRegistry:
    """A registry holding only the template handlers."""
    registry = ModuleRegistry()
    for name, handler in HANDLERS.items():
        registry.register(name, handler)
    return registry


def register_template_handlers(registry: ModuleRegistry) -> None:
    """Add any template handler the registry lacks; never replaces an existing one."""
    for name, handler in HANDLERS.items():
        if not registry.has_handler(name):
            registry.register(name, handler)
