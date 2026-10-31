# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline handlers behind the business starter templates (support, sales, HR, finance,
engineering, content, data and governance).

The same rules as ``handlers.py`` apply: every handler is deterministic and makes no network
call. Steps that stand for an LLM call run a real ``Agent`` on a scripted ``FakeModel`` whose reply
is computed from the workflow state; steps that stand for an external system (a CRM, a payroll
system, a pager, a social network) only record what they would do, marked ``simulated``. Replace
them with your own handlers under the same names, or point the instantiated workflow at your
modules, to use a template with real systems.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from typing import Any

from nexura_agents.agent import Agent
from nexura_agents.models.fake import FakeModel
from nexura_agents.runner import Runner

from nexura_runtime.studio.traces import traced_run
from nexura_runtime.workflow import HandlerOutput

_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?(?:\(\d{2,4}\)[ -]?)?\d{3,5}[ -]?\d{3,5}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_STOP = frozenset(
    [
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
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "my",
        "me",
        "do",
        "does",
        "did",
        "can",
        "not",
        "no",
        "yes",
        "but",
        "so",
        "if",
        "than",
        "then",
        "there",
        "their",
        "they",
        "them",
        "he",
        "she",
        "his",
        "her",
    ]
)
_POSITIVE = frozenset(
    [
        "great",
        "good",
        "love",
        "excellent",
        "amazing",
        "happy",
        "thanks",
        "thank",
        "helpful",
        "fast",
        "easy",
        "perfect",
        "awesome",
        "fantastic",
        "pleased",
        "smooth",
    ]
)
_NEGATIVE = frozenset(
    [
        "bad",
        "terrible",
        "awful",
        "hate",
        "angry",
        "broken",
        "slow",
        "worst",
        "disappointed",
        "useless",
        "refund",
        "frustrated",
        "annoying",
        "cancel",
        "rude",
        "late",
        "never",
        "wrong",
        "poor",
    ]
)


async def _agent_says(name: str, instructions: str, prompt: str, reply: str) -> str:
    """Run one agent turn on a scripted FakeModel and return its final text."""
    agent = Agent(name=name, instructions=instructions, model=FakeModel(responses=[reply]))
    result = await traced_run(Runner(), agent, prompt)
    return str(result.final_output)


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def _sentiment(text: str) -> float:
    """A lexicon score in [-1, 1]: (positive - negative) / matched words."""
    words = _WORD.findall(text.lower())
    pos = sum(w in _POSITIVE for w in words)
    neg = sum(w in _NEGATIVE for w in words)
    return 0.0 if pos + neg == 0 else round((pos - neg) / (pos + neg), 2)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _cards(text: str) -> list[str]:
    found = []
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append(match.group(0))
    return found


# --- FAQ chatbot ---

_DEFAULT_FAQ = [
    {
        "question": "How do I reset my password?",
        "answer": (
            "Open Settings, choose Security and click Reset password; a link arrives by email."
        ),
    },
    {
        "question": "What are your opening hours?",
        "answer": "Support is open Monday to Friday, 9:00 to 18:00.",
    },
    {
        "question": "How long does shipping take?",
        "answer": "Standard shipping takes 3 to 5 working days; express takes 1 to 2.",
    },
]


def tpl_faq_match(state: dict[str, Any]) -> HandlerOutput:
    """Find the closest FAQ entry by word overlap; escalate when nothing is close enough."""
    question = set(_words(str(state.get("question", ""))))
    faq = state.get("faq") or _DEFAULT_FAQ
    best, best_score = None, 0.0
    for entry in faq:
        words = set(_words(str(entry.get("question", ""))))
        score = len(question & words) / max(1, len(question | words))
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < 0.2:
        return HandlerOutput(state_update={"match": None}, branch="escalate")
    match = {"question": best["question"], "answer": best["answer"], "score": round(best_score, 2)}
    return HandlerOutput(state_update={"match": match}, branch="answer")


async def tpl_faq_answer(state: dict[str, Any]) -> dict[str, Any]:
    answer = state["match"]["answer"]
    text = await _agent_says(
        "FaqAgent", "Answer from the FAQ only.", str(state.get("question", "")), answer
    )
    return {"reply": text, "answered_from_faq": True}


def tpl_faq_escalate(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "answered_from_faq": False,
        "escalation": {"queue": "support", "reason": "no FAQ entry matched", "simulated": True},
        "reply": "Thanks for your question. A member of our team will get back to you.",
    }


# --- ticket triage ---

_TRIAGE_CATEGORIES = {
    "billing": ("invoice", "charge", "charged", "payment", "refund", "bill", "billing", "price"),
    "account": ("login", "password", "account", "sign", "locked", "2fa"),
    "shipping": ("delivery", "shipping", "parcel", "package", "tracking", "courier"),
    "technical": ("error", "bug", "crash", "down", "outage", "broken", "500", "api", "slow"),
}
_URGENT = ("outage", "down", "urgent", "asap", "data loss", "security", "breach", "cannot work")
_SLA_HOURS = {"urgent": 1, "high": 4, "normal": 24}


def tpl_triage_classify(state: dict[str, Any]) -> HandlerOutput:
    """Category from keywords; urgent tickets page the on-call engineer first."""
    text = f"{state.get('subject', '')} {state.get('body', '')}".lower()
    scores = {cat: sum(k in text for k in keys) for cat, keys in _TRIAGE_CATEGORIES.items()}
    category = max(scores, key=lambda c: scores[c]) if any(scores.values()) else "other"
    if any(k in text for k in _URGENT):
        priority = "urgent"
    elif str(state.get("plan", "")).lower() == "enterprise":
        priority = "high"
    else:
        priority = "normal"
    branch = "urgent" if priority == "urgent" else "normal"
    return HandlerOutput(
        state_update={"triage": {"category": category, "priority": priority}}, branch=branch
    )


def tpl_triage_page(state: dict[str, Any]) -> dict[str, Any]:
    """Record the on-call page; no pager is called."""
    return {"page": {"to": "on-call", "category": state["triage"]["category"], "simulated": True}}


def tpl_triage_assign(state: dict[str, Any]) -> dict[str, Any]:
    triage = dict(state["triage"])
    triage["queue"] = f"{triage['category']}-queue"
    triage["sla_hours"] = _SLA_HOURS[triage["priority"]]
    return {"triage": triage}


# --- sentiment escalation ---


def tpl_sentiment_score(state: dict[str, Any]) -> HandlerOutput:
    score = _sentiment(str(state.get("message", "")))
    label = "negative" if score < 0 else "positive" if score > 0 else "neutral"
    return HandlerOutput(
        state_update={"sentiment": {"score": score, "label": label}},
        branch="negative" if label == "negative" else "other",
    )


def tpl_sentiment_offer_credit(state: dict[str, Any]) -> dict[str, Any]:
    """Record a goodwill credit (10% of the order, capped at 50). Gated by approval."""
    credit = round(min(50.0, float(state.get("order_total", 0)) * 0.10), 2)
    return {"credit": {"amount": credit, "status": "granted", "simulated": True}}


async def tpl_sentiment_reply(state: dict[str, Any]) -> dict[str, Any]:
    name = str(state.get("customer", "there"))
    credit = state.get("credit")
    if credit:
        reply = (
            f"Hi {name}, we are sorry about your experience. We have added a credit of "
            f"{credit['amount']:g} to your account."
        )
    elif state["sentiment"]["label"] == "positive":
        reply = f"Hi {name}, thank you for the kind words. We are glad it went well."
    else:
        reply = f"Hi {name}, thanks for your message. We will follow up shortly."
    text = await _agent_says(
        "CareAgent", "Reply with empathy, briefly.", str(state.get("message", "")), reply
    )
    return {"reply": text}


# --- lead qualification ---

_SENIOR_TITLES = ("chief", "cto", "ceo", "cfo", "vp", "vice president", "head", "director")


def tpl_lead_score(state: dict[str, Any]) -> HandlerOutput:
    """Score 0-100 from company size, budget, title seniority and a stated timeline."""
    lead = state.get("lead", {})
    score = 0
    employees = int(lead.get("employees", 0))
    score += 30 if employees >= 500 else 20 if employees >= 50 else 5
    budget = float(lead.get("budget", 0))
    score += 30 if budget >= 50_000 else 15 if budget >= 10_000 else 0
    title = str(lead.get("title", "")).lower()
    score += 25 if any(t in title for t in _SENIOR_TITLES) else 10 if "manager" in title else 0
    score += 15 if str(lead.get("timeline", "")).lower() in ("now", "this quarter") else 0
    grade = "hot" if score >= 70 else "warm" if score >= 40 else "cold"
    return HandlerOutput(
        state_update={"qualification": {"score": score, "grade": grade}},
        branch="hot" if grade == "hot" else "nurture",
    )


def tpl_lead_create_deal(state: dict[str, Any]) -> dict[str, Any]:
    """Record the CRM deal; no CRM is called. Gated by approval."""
    lead = state.get("lead", {})
    return {
        "deal": {
            "company": lead.get("company", ""),
            "stage": "discovery",
            "owner": "sales",
            "simulated": True,
        }
    }


def tpl_lead_nurture(state: dict[str, Any]) -> dict[str, Any]:
    grade = state["qualification"]["grade"]
    sequence = "monthly-newsletter" if grade == "cold" else "product-education"
    return {"nurture": {"sequence": sequence, "simulated": True}}


# --- sales outreach email ---

_VALUE_PROPS = {
    "retail": "cut stock-outs with demand forecasts",
    "finance": "close the books faster with automated reconciliation",
    "healthcare": "reduce scheduling no-shows with automated reminders",
    "software": "ship faster with fewer on-call pages",
}


def tpl_outreach_research(state: dict[str, Any]) -> dict[str, Any]:
    prospect = state.get("prospect", {})
    industry = str(prospect.get("industry", "")).lower()
    value = _VALUE_PROPS.get(industry, "save your team time on repetitive work")
    return {"angle": {"industry": industry or "general", "value_prop": value}}


async def tpl_outreach_draft(state: dict[str, Any]) -> dict[str, Any]:
    prospect = state.get("prospect", {})
    first = str(prospect.get("name", "there")).split()[0]
    company = prospect.get("company", "your team")
    subject = f"An idea for {company}"
    body = (
        f"Hi {first},\n\nTeams like {company} use us to {state['angle']['value_prop']}. "
        "Would a 20-minute call next week be useful?\n\nBest regards"
    )
    text = await _agent_says("OutreachAgent", "Write a short, specific email.", subject, body)
    return {"email": {"to": prospect.get("email", ""), "subject": subject, "body": text}}


def tpl_outreach_send(state: dict[str, Any]) -> dict[str, Any]:
    """Queue the email in a dry-run outbox. Gated by approval."""
    return {"outbox": {"to": state["email"]["to"], "status": "queued", "mode": "dry_run"}}


# --- social posts ---

_LIMITS = {"x": 280, "linkedin": 1300, "instagram": 2200}


def tpl_social_brief(state: dict[str, Any]) -> dict[str, Any]:
    text = str(state.get("announcement", ""))
    sentences = _sentences(text)
    counts = Counter(w for w in _words(text) if len(w) > 3)
    hashtags = ["#" + w for w, _ in counts.most_common(3)]
    return {"brief": {"headline": sentences[0] if sentences else "", "hashtags": hashtags}}


async def tpl_social_write(state: dict[str, Any]) -> dict[str, Any]:
    brief = state["brief"]
    link = str(state.get("link", ""))
    tags = " ".join(brief["hashtags"])
    drafts = {
        "x": f"{brief['headline']} {link} {tags}".strip(),
        "linkedin": f"{state.get('announcement', '')}\n\n{link}\n\n{tags}".strip(),
        "instagram": f"{brief['headline']}\n\n{tags}".strip(),
    }
    posts = {}
    for network, draft in drafts.items():
        if len(draft) > _LIMITS[network]:
            draft = draft[: _LIMITS[network] - 1].rstrip() + "…"
        posts[network] = await _agent_says(
            "SocialAgent", f"Write a {network} post.", brief["headline"], draft
        )
    return {"posts": posts, "lengths": {k: len(v) for k, v in posts.items()}}


def tpl_social_schedule(state: dict[str, Any]) -> dict[str, Any]:
    """Record the publishing plan; nothing is posted. Gated by approval."""
    return {
        "schedule": {"networks": sorted(state["posts"]), "status": "scheduled", "simulated": True}
    }


# --- SEO metadata ---


def tpl_seo_keywords(state: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(w for w in _words(str(state.get("article", ""))) if len(w) > 3)
    return {"keywords": [w for w, _ in counts.most_common(5)]}


def tpl_seo_meta(state: dict[str, Any]) -> dict[str, Any]:
    article = str(state.get("article", ""))
    title = str(state.get("title") or (_sentences(article) or ["Untitled"])[0])
    full_title = title
    if len(title) > 60:
        title = title[:59].rstrip() + "…"
    sentences = _sentences(article)
    description = " ".join(sentences[:2])
    if len(description) > 155:
        description = description[:154].rstrip() + "…"
    slug = ""
    for word in re.findall(r"[a-z0-9]+", full_title.lower()):  # whole words, at most 60 chars
        if len(slug) + len(word) + 1 > 60:
            break
        slug = f"{slug}-{word}" if slug else word
    return {"meta": {"title": title, "description": description, "slug": slug}}


# --- resume screening ---

_SKILLS = [
    "python",
    "sql",
    "java",
    "javascript",
    "typescript",
    "react",
    "go",
    "rust",
    "aws",
    "gcp",
    "azure",
    "docker",
    "kubernetes",
    "terraform",
    "excel",
    "tableau",
    "figma",
    "spark",
    "kafka",
    "pandas",
    "django",
    "fastapi",
]
_YEARS = re.compile(r"(\d{1,2})\+?\s*(?:years|yrs)")


def tpl_resume_parse(state: dict[str, Any]) -> dict[str, Any]:
    """Extract only job-relevant facts (skills, years); no name, age or other personal data."""
    text = str(state.get("resume", ""))
    words = set(_WORD.findall(text.lower()))
    years = max((int(y) for y in _YEARS.findall(text.lower())), default=0)
    return {"candidate": {"skills": sorted(s for s in _SKILLS if s in words), "years": years}}


def tpl_resume_match(state: dict[str, Any]) -> HandlerOutput:
    job = state.get("job", {})
    required = [s.lower() for s in job.get("required_skills", [])]
    have = set(state["candidate"]["skills"])
    matched = [s for s in required if s in have]
    coverage = len(matched) / len(required) if required else 1.0
    enough_years = state["candidate"]["years"] >= int(job.get("min_years", 0))
    fit = round(coverage * 100)
    decision = "shortlist" if coverage >= 0.6 and enough_years else "decline"
    missing = [s for s in required if s not in have]
    return HandlerOutput(
        state_update={"match": {"fit": fit, "matched": matched, "missing": missing}},
        branch=decision,
    )


def tpl_resume_shortlist(state: dict[str, Any]) -> dict[str, Any]:
    return {"decision": "shortlist", "next_step": "schedule interview (human)"}


def tpl_resume_decline(state: dict[str, Any]) -> dict[str, Any]:
    """Draft only; a person reviews before anything is sent to a candidate."""
    return {"decision": "decline", "next_step": "recruiter reviews the draft decline note"}


# --- employee onboarding ---

_DEPARTMENT_TASKS = {
    "engineering": ["Laptop with developer image", "GitHub access", "On-call shadowing"],
    "sales": ["CRM seat", "Product demo training", "Territory handover"],
    "finance": ["ERP access", "Expense policy training"],
}


def tpl_onboard_plan(state: dict[str, Any]) -> dict[str, Any]:
    start = date.fromisoformat(str(state.get("start_date", "2026-11-02")))
    department = str(state.get("department", "")).lower()
    tasks = ["Sign contract", "Payroll and tax forms", "Security training"]
    tasks += _DEPARTMENT_TASKS.get(department, [])
    plan = [
        {"task": task, "due": (start + timedelta(days=min(i, 9))).isoformat()}
        for i, task in enumerate(tasks)
    ]
    return {"plan": plan, "task_count": len(plan)}


def tpl_onboard_accounts(state: dict[str, Any]) -> dict[str, Any]:
    """Record the accounts to create; no directory is touched. Gated by approval."""
    department = str(state.get("department", "")).lower()
    systems = ["email", "chat"] + (["github"] if department == "engineering" else [])
    return {"accounts": {"systems": systems, "status": "created", "simulated": True}}


async def tpl_onboard_welcome(state: dict[str, Any]) -> dict[str, Any]:
    name = str(state.get("name", "there")).split()[0]
    reply = (
        f"Welcome aboard, {name}! You start on {state.get('start_date', 'your first day')}. "
        f"You have {state['task_count']} onboarding tasks in your checklist."
    )
    text = await _agent_says("OnboardingAgent", "Welcome a new hire warmly.", name, reply)
    return {"welcome": text}


# --- leave request ---


def _weekdays(start: date, end: date) -> int:
    days = 0
    current = start
    while current <= end:
        days += current.weekday() < 5
        current += timedelta(days=1)
    return days


def tpl_leave_check(state: dict[str, Any]) -> HandlerOutput:
    start = date.fromisoformat(str(state["start"]))
    end = date.fromisoformat(str(state["end"]))
    days = _weekdays(start, end) if end >= start else 0
    balance = float(state.get("balance", 0))
    if days == 0 or days > balance:
        branch = "deny"
    elif days <= 2:
        branch = "auto"
    else:
        branch = "manager"
    return HandlerOutput(state_update={"leave": {"days": days, "balance": balance}}, branch=branch)


def tpl_leave_approve(state: dict[str, Any]) -> dict[str, Any]:
    leave = dict(state["leave"])
    leave.update(status="approved", remaining=leave["balance"] - leave["days"])
    return {"leave": leave}


def tpl_leave_deny(state: dict[str, Any]) -> dict[str, Any]:
    leave = dict(state["leave"])
    leave["status"] = "denied"
    leave["reason"] = "no working days" if leave["days"] == 0 else "not enough balance"
    return {"leave": leave}


# --- expense approval ---

_EXPENSE_LIMITS = {"meals": 75.0, "travel": 1500.0, "software": 300.0, "office": 200.0}


def tpl_expense_policy(state: dict[str, Any]) -> HandlerOutput:
    expense = state.get("expense", {})
    amount = float(expense.get("amount", 0))
    category = str(expense.get("category", "")).lower()
    issues = []
    if category not in _EXPENSE_LIMITS:
        issues.append(f"unknown category '{category}'")
    if amount > 25 and not expense.get("receipt"):
        issues.append("receipt required above 25")
    if issues:
        return HandlerOutput(state_update={"policy": {"issues": issues}}, branch="reject")
    over = amount > _EXPENSE_LIMITS[category]
    return HandlerOutput(
        state_update={"policy": {"issues": [], "over_limit": over}},
        branch="review" if over else "auto",
    )


def tpl_expense_reimburse(state: dict[str, Any]) -> dict[str, Any]:
    """Record the reimbursement; no payment is made."""
    amount = float(state["expense"]["amount"])
    return {"reimbursement": {"amount": amount, "status": "scheduled", "simulated": True}}


def tpl_expense_manager_review(state: dict[str, Any]) -> dict[str, Any]:
    """The gate itself is the approval; this step records who must sign."""
    return {"review": {"approver": "manager", "reason": "over category limit"}}


def tpl_expense_reject(state: dict[str, Any]) -> dict[str, Any]:
    return {"reimbursement": {"status": "rejected", "issues": state["policy"]["issues"]}}


# --- invoice and payment reconciliation ---


def tpl_recon_match(state: dict[str, Any]) -> HandlerOutput:
    """Match on reference first, then on an exact amount; everything else is an exception."""
    invoices = list(state.get("invoices", []))
    payments = list(state.get("payments", []))
    matched, open_payments = [], list(payments)
    unmatched_invoices = []
    for inv in invoices:
        hit = next((p for p in open_payments if p.get("reference") == inv.get("id")), None)
        if hit is None:
            hit = next(
                (p for p in open_payments if float(p["amount"]) == float(inv["amount"])), None
            )
        if hit is None:
            unmatched_invoices.append(inv["id"])
            continue
        open_payments.remove(hit)
        matched.append({"invoice": inv["id"], "payment": hit["id"]})
    result = {
        "matched": matched,
        "unmatched_invoices": unmatched_invoices,
        "unmatched_payments": [p["id"] for p in open_payments],
    }
    clean = not unmatched_invoices and not open_payments
    return HandlerOutput(
        state_update={"reconciliation": result}, branch="clean" if clean else "exceptions"
    )


def tpl_recon_flag(state: dict[str, Any]) -> dict[str, Any]:
    r = state["reconciliation"]
    return {"exceptions": len(r["unmatched_invoices"]) + len(r["unmatched_payments"])}


def tpl_recon_report(state: dict[str, Any]) -> dict[str, Any]:
    r = state["reconciliation"]
    status = "balanced" if not state.get("exceptions") else "needs_review"
    return {"report": {"matched": len(r["matched"]), "status": status}}


# --- incident log summary ---

_LOG_LINE = re.compile(
    r"^\S*\s*(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\s+([\w.-]+)\s*[:-]?\s*(.*)$"
)


def tpl_incident_parse(state: dict[str, Any]) -> dict[str, Any]:
    levels: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    services: Counter[str] = Counter()
    for raw in str(state.get("logs", "")).splitlines():
        match = _LOG_LINE.match(raw.strip())
        if not match:
            continue
        level = "WARN" if match.group(1) == "WARNING" else match.group(1)
        levels[level] += 1
        if level in ("ERROR", "CRITICAL"):
            services[match.group(2)] += 1
            errors[match.group(3)[:80]] += 1
    return {
        "logs_summary": {
            "levels": dict(levels),
            "error_services": dict(services),
            "top_errors": [e for e, _ in errors.most_common(3)],
        }
    }


def tpl_incident_assess(state: dict[str, Any]) -> HandlerOutput:
    levels = state["logs_summary"]["levels"]
    critical, errors = levels.get("CRITICAL", 0), levels.get("ERROR", 0)
    severity = "sev1" if critical else "sev2" if errors >= 5 else "sev3"
    return HandlerOutput(
        state_update={"severity": severity}, branch="page" if severity == "sev1" else "summary"
    )


def tpl_incident_page(state: dict[str, Any]) -> dict[str, Any]:
    """Record the page; no pager is called. Gated by approval."""
    return {"page": {"to": "on-call", "severity": state["severity"], "simulated": True}}


async def tpl_incident_summary(state: dict[str, Any]) -> dict[str, Any]:
    summary = state["logs_summary"]
    services = ", ".join(sorted(summary["error_services"])) or "none"
    reply = (
        f"Severity {state['severity']}. Errors by service: {services}. "
        f"Top error: {(summary['top_errors'] or ['none'])[0]}."
    )
    text = await _agent_says("IncidentAgent", "Summarise the incident.", "logs", reply)
    return {"incident_summary": text}


# --- code review checklist ---

_SECRET = re.compile(r"(AKIA[0-9A-Z]{16}|(?:password|secret|api_key)\s*=\s*['\"][^'\"]{6,})", re.I)


def tpl_review_scan(state: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    files: list[str] = []
    added = 0
    current = ""
    for line in str(state.get("diff", "")).splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            files.append(current)
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added += 1
        body = line[1:]
        if "TODO" in body or "FIXME" in body:
            findings.append({"file": current, "kind": "todo", "line": body.strip()[:80]})
        if re.search(r"\b(print\(|console\.log\()", body):
            findings.append({"file": current, "kind": "debug-output", "line": body.strip()[:80]})
        if _SECRET.search(body):
            findings.append({"file": current, "kind": "possible-secret", "line": "[redacted]"})
    if added > 400:
        findings.append({"file": "*", "kind": "large-change", "line": f"{added} added lines"})
    return {"review": {"files": files, "added_lines": added, "findings": findings}}


async def tpl_review_comment(state: dict[str, Any]) -> dict[str, Any]:
    review = state["review"]
    blocking = [f for f in review["findings"] if f["kind"] in ("possible-secret", "large-change")]
    verdict = "request_changes" if blocking else "comment" if review["findings"] else "approve"
    lines = [f"- {f['file']}: {f['kind']}" for f in review["findings"]] or ["- No findings."]
    reply = f"Verdict: {verdict}\n" + "\n".join(lines)
    text = await _agent_says("ReviewAgent", "Write a concise review.", "diff", reply)
    return {"verdict": verdict, "comment": text}


# --- release notes ---

_CONVENTIONAL = re.compile(r"^(\w+)(\([^)]*\))?(!)?:\s*(.+)$")
_SECTIONS = {"feat": "Features", "fix": "Fixes", "docs": "Documentation", "perf": "Performance"}


def tpl_release_group(state: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, list[str]] = {}
    breaking: list[str] = []
    for commit in state.get("commits", []):
        match = _CONVENTIONAL.match(str(commit).strip())
        kind, bang, subject = (
            (match.group(1), match.group(3), match.group(4)) if match else ("other", None, commit)
        )
        section = _SECTIONS.get(kind, "Other changes")
        groups.setdefault(section, []).append(subject)
        if bang or "BREAKING CHANGE" in str(commit):
            breaking.append(subject)
    return {"changes": groups, "breaking": breaking}


async def tpl_release_write(state: dict[str, Any]) -> dict[str, Any]:
    version = str(state.get("version", "unreleased"))
    parts = [f"## {version}"]
    if state["breaking"]:
        parts.append("### Breaking changes\n" + "\n".join(f"- {b}" for b in state["breaking"]))
    for section in ("Features", "Fixes", "Performance", "Documentation", "Other changes"):
        items = state["changes"].get(section)
        if items:
            parts.append(f"### {section}\n" + "\n".join(f"- {i}" for i in items))
    notes = "\n\n".join(parts)
    text = await _agent_says("ReleaseAgent", "Write release notes.", version, notes)
    return {"notes": text}


def tpl_release_publish(state: dict[str, Any]) -> dict[str, Any]:
    """Record the publication; nothing is posted. Gated by approval."""
    return {"published": {"version": state.get("version", ""), "simulated": True}}


# --- document summarizer ---


def tpl_summary_rank(state: dict[str, Any]) -> dict[str, Any]:
    """Score sentences by the frequency of their content words (extractive)."""
    sentences = _sentences(str(state.get("document", "")))
    freq = Counter(_words(" ".join(sentences)))
    scored = [
        {
            "index": i,
            "sentence": s,
            "score": sum(freq[w] for w in _words(s)) / (len(_words(s)) or 1),
        }
        for i, s in enumerate(sentences)
    ]
    return {"ranked": sorted(scored, key=lambda r: (-r["score"], r["index"]))}


async def tpl_summary_write(state: dict[str, Any]) -> dict[str, Any]:
    keep = int(state.get("max_sentences", 3))
    chosen = sorted(state["ranked"][:keep], key=lambda r: r["index"])
    bullets = "\n".join(f"- {r['sentence']}" for r in chosen)
    text = await _agent_says("SummaryAgent", "Summarise faithfully.", "document", bullets)
    return {"summary": text, "sentences_kept": len(chosen)}


# --- meeting notes ---

_ACTION = re.compile(r"^(?:action|todo)\s*[:-]\s*(.+)$|^(\w+) will (.+)$", re.I)


def tpl_meeting_extract(state: dict[str, Any]) -> dict[str, Any]:
    actions, decisions, attendees = [], [], []
    for raw in str(state.get("transcript", "")).splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if line.lower().startswith("attendees:"):
            attendees = [a.strip() for a in line.split(":", 1)[1].split(",") if a.strip()]
            continue
        if line.lower().startswith(("decision:", "decided:")):
            decisions.append(line.split(":", 1)[1].strip())
            continue
        match = _ACTION.match(line)
        if match:
            if match.group(1):
                actions.append({"owner": "unassigned", "task": match.group(1).strip()})
            else:
                actions.append({"owner": match.group(2), "task": match.group(3).rstrip(".")})
    return {"minutes": {"attendees": attendees, "decisions": decisions, "actions": actions}}


async def tpl_meeting_write(state: dict[str, Any]) -> dict[str, Any]:
    m = state["minutes"]
    parts = ["Attendees: " + (", ".join(m["attendees"]) or "not recorded")]
    parts += ["Decisions:"] + [f"- {d}" for d in m["decisions"] or ["none"]]
    parts += ["Actions:"] + [f"- {a['owner']}: {a['task']}" for a in m["actions"] or []]
    text = await _agent_says("MinutesAgent", "Write meeting minutes.", "notes", "\n".join(parts))
    return {"minutes_text": text}


def tpl_meeting_share(state: dict[str, Any]) -> dict[str, Any]:
    """Record sharing with the attendees; no mail is sent. Gated by approval."""
    return {"shared": {"recipients": state["minutes"]["attendees"], "simulated": True}}


# --- contract review ---

_RISKS = {
    "auto_renewal": (re.compile(r"automatic(ally)? renew", re.I), 2),
    "unlimited_liability": (
        re.compile(r"unlimited liability|liability shall not be limited", re.I),
        3,
    ),
    "exclusivity": (re.compile(r"\bexclusive(ly)?\b", re.I), 2),
    "long_payment_terms": (re.compile(r"\b(9\d|1[0-9]{2}) days\b", re.I), 1),
    "unilateral_changes": (re.compile(r"may (amend|change|modify) .* at any time", re.I), 2),
}


def tpl_contract_scan(state: dict[str, Any]) -> HandlerOutput:
    text = str(state.get("contract", ""))
    flags = [name for name, (pattern, _) in _RISKS.items() if pattern.search(text)]
    if not re.search(r"terminat\w+ for convenience", text, re.I):
        flags.append("no_termination_for_convenience")
    score = sum(_RISKS[f][1] if f in _RISKS else 1 for f in flags)
    level = "high" if score >= 4 else "low"
    return HandlerOutput(
        state_update={"risk": {"flags": flags, "score": score, "level": level}},
        branch="legal" if level == "high" else "report",
    )


def tpl_contract_legal_review(state: dict[str, Any]) -> dict[str, Any]:
    """The approval gate is the legal sign-off; this records it."""
    return {"legal": {"status": "signed_off", "flags_reviewed": len(state["risk"]["flags"])}}


def tpl_contract_report(state: dict[str, Any]) -> dict[str, Any]:
    risk = state["risk"]
    return {"contract_report": {"level": risk["level"], "flags": risk["flags"]}}


# --- customer feedback analysis ---

_THEMES = {
    "pricing": ("price", "pricing", "expensive", "cost", "cheap", "plan"),
    "bugs": ("bug", "crash", "error", "broken", "fails"),
    "performance": ("slow", "fast", "speed", "lag", "loading"),
    "usability": ("confusing", "easy", "intuitive", "ui", "design", "navigate"),
    "support": ("support", "agent", "help", "response"),
}


def tpl_feedback_classify(state: dict[str, Any]) -> dict[str, Any]:
    themes: Counter[str] = Counter()
    sentiment: Counter[str] = Counter()
    items = []
    for text in state.get("feedback", []):
        lower = str(text).lower()
        found = [t for t, keys in _THEMES.items() if any(k in lower for k in keys)] or ["other"]
        score = _sentiment(str(text))
        label = "negative" if score < 0 else "positive" if score > 0 else "neutral"
        themes.update(found)
        sentiment[label] += 1
        items.append({"themes": found, "sentiment": label})
    return {
        "analysis": {"themes": dict(themes), "sentiment": dict(sentiment), "count": len(items)},
        "items": items,
    }


async def tpl_feedback_report(state: dict[str, Any]) -> dict[str, Any]:
    a = state["analysis"]
    top = sorted(a["themes"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    reply = f"{a['count']} responses. Top themes: " + ", ".join(f"{t} ({n})" for t, n in top)
    text = await _agent_says("InsightsAgent", "Summarise feedback themes.", "feedback", reply)
    top_theme = top[0][0] if top else "none"
    return {"insights": text, "top_theme": top_theme}


# --- data quality check ---


def tpl_dq_validate(state: dict[str, Any]) -> HandlerOutput:
    rows = list(state.get("rows", []))
    required = list(state.get("required_fields", ["id", "email"]))
    issues: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for i, row in enumerate(rows):
        for field in required:
            if row.get(field) in (None, ""):
                issues.append({"row": i, "field": field, "problem": "missing"})
        email = row.get("email")
        if email and not _EMAIL.fullmatch(str(email)):
            issues.append({"row": i, "field": "email", "problem": "invalid"})
        if row.get("id") in seen:
            issues.append({"row": i, "field": "id", "problem": "duplicate"})
        seen.add(row.get("id"))
    report = {"rows": len(rows), "issues": issues, "passed": not issues}
    return HandlerOutput(state_update={"quality": report}, branch="pass" if not issues else "fail")


def tpl_dq_publish(state: dict[str, Any]) -> dict[str, Any]:
    return {"dataset": {"status": "published", "rows": state["quality"]["rows"]}}


def tpl_dq_quarantine(state: dict[str, Any]) -> dict[str, Any]:
    return {"dataset": {"status": "quarantined", "issue_count": len(state["quality"]["issues"])}}


# --- content moderation ---

_DEFAULT_BLOCKED = ("kill yourself", "buy followers", "free crypto")


def tpl_moderation_check(state: dict[str, Any]) -> HandlerOutput:
    text = str(state.get("post", ""))
    lower = text.lower()
    blocked = [t for t in state.get("blocked_terms", _DEFAULT_BLOCKED) if t.lower() in lower]
    pii = bool(_EMAIL.search(text) or _cards(text))
    links = len(re.findall(r"https?://", lower))
    letters = [c for c in text if c.isalpha()]
    shouting = len(letters) > 20 and sum(c.isupper() for c in letters) / len(letters) > 0.7
    reasons = [f"blocked term: {t}" for t in blocked]
    reasons += ["personal data"] * pii + ["many links"] * (links > 2) + ["shouting"] * shouting
    decision = "block" if blocked else "review" if reasons else "allow"
    return HandlerOutput(
        state_update={"moderation": {"decision": decision, "reasons": reasons}}, branch=decision
    )


def tpl_moderation_publish(state: dict[str, Any]) -> dict[str, Any]:
    return {"published": True}


def tpl_moderation_reject(state: dict[str, Any]) -> dict[str, Any]:
    return {"published": False}


# --- PII redaction ---

_PII = (("[EMAIL]", _EMAIL), ("[IP]", _IPV4))


def tpl_pii_detect(state: dict[str, Any]) -> dict[str, Any]:
    text = str(state.get("text", ""))
    counts = {
        "email": len(_EMAIL.findall(text)),
        "card": len(_cards(text)),
        "phone": len(_PHONE.findall(_CARD.sub("", text))) if not _cards(text) else 0,
        "ip": len(_IPV4.findall(text)),
    }
    return {"pii_found": {k: v for k, v in counts.items() if v}}


def tpl_pii_redact(state: dict[str, Any]) -> dict[str, Any]:
    text = str(state.get("text", ""))
    for card in _cards(text):
        text = text.replace(card, "[CARD]")
    for label, pattern in _PII:
        text = pattern.sub(label, text)
    text = _PHONE.sub("[PHONE]", text)
    return {"redacted": text}


# --- trip planner (agents) ---

_ACTIVITY_COST = {"museum": 25.0, "food tour": 60.0, "hiking": 0.0, "concert": 80.0, "beach": 0.0}


def tpl_trip_plan(state: dict[str, Any]) -> dict[str, Any]:
    days = int(state.get("days", 3))
    interests = [i for i in state.get("interests", ["museum", "hiking"]) if i in _ACTIVITY_COST]
    plan = [
        {"day": d + 1, "activity": interests[d % len(interests)] if interests else "free day"}
        for d in range(days)
    ]
    return {"plan": plan}


def tpl_trip_budget(state: dict[str, Any]) -> HandlerOutput:
    nightly = float(state.get("hotel_per_night", 100))
    activities = sum(_ACTIVITY_COST.get(p["activity"], 0.0) for p in state["plan"])
    total = nightly * len(state["plan"]) + activities
    budget = float(state.get("budget", 1000))
    return HandlerOutput(
        state_update={"cost": {"total": total, "budget": budget}},
        branch="within" if total <= budget else "over",
    )


def tpl_trip_trim(state: dict[str, Any]) -> dict[str, Any]:
    """Swap paid activities for free ones until the trip fits the budget."""
    plan = [dict(p) for p in state["plan"]]
    total, budget = state["cost"]["total"], state["cost"]["budget"]
    for item in sorted(plan, key=lambda p: -_ACTIVITY_COST.get(p["activity"], 0.0)):
        if total <= budget:
            break
        total -= _ACTIVITY_COST.get(item["activity"], 0.0)
        item["activity"] = "free walking tour"
    return {"plan": plan, "cost": {"total": total, "budget": budget}, "trimmed": True}


async def tpl_trip_itinerary(state: dict[str, Any]) -> dict[str, Any]:
    city = str(state.get("destination", "your destination"))
    lines = [f"Day {p['day']}: {p['activity']}" for p in state["plan"]]
    reply = f"Itinerary for {city} (total {state['cost']['total']:g}):\n" + "\n".join(lines)
    text = await _agent_says("TripAgent", "Write a day-by-day itinerary.", city, reply)
    return {"itinerary": text}


# --- order alerts ---


def tpl_alert_check(state: dict[str, Any]) -> HandlerOutput:
    order = state.get("order", {})
    threshold = float(state.get("threshold", 1000))
    big = float(order.get("total", 0)) >= threshold
    return HandlerOutput(state_update={"large_order": big}, branch="alert" if big else "skip")


def tpl_alert_notify(state: dict[str, Any]) -> dict[str, Any]:
    """Record the channel message; no chat service is called."""
    order = state.get("order", {})
    who = order.get("customer", "?")
    text = f"Large order {order.get('id', '?')} from {who}: {order.get('total')}"
    return {"alert": {"channel": "#sales", "text": text, "simulated": True}}


def tpl_alert_log(state: dict[str, Any]) -> dict[str, Any]:
    return {"logged": True}


LIBRARY_HANDLERS: dict[str, Any] = {
    name: fn for name, fn in sorted(globals().items()) if name.startswith("tpl_") and callable(fn)
}
