# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Import OpenAI Evals data (a JSONL file of eval items) as a Studio eval dataset.

OpenAI Evals takes test data as JSONL, one ``{"item": {...}}`` object per line, and an eval
run can also carry ``{"item": {...}, "sample": {...}}`` rows inline
(``source.type == "file_content"``). The older open-source ``openai/evals`` registry uses bare
objects with ``"input"`` (text or chat messages) and ``"ideal"``. :func:`convert_openai_evals`
reads all three and returns the portable ``nexura.eval-dataset`` document that
``PUT /api/v1/evaluations/studio/datasets/{id}`` accepts.

The document goes through :func:`studio_engine.validate_dataset`, so it is size-capped and
secret-redacted exactly like a dataset saved from Studio. Rules:

* Nothing is guessed silently. The input and expected fields are taken from the options, from
  the ``{{ item.<field> }}`` reference of a string check, or from a short documented list of
  common names; the report says which was used.
* A malformed line fails the import with its line number, unless ``skip_invalid`` is set, in
  which case it is skipped and still listed.
* ``testing_criteria`` (from the eval definition) are mapped to Studio scorers only where a
  Nexura scorer corresponds; everything else is listed in ``unmapped_criteria`` with a reason.
* ``sample`` rows (outputs a model already produced) are not imported: a Studio eval runs its
  target again. The report counts them.

This module never calls a network and never runs code from the import.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from nexura_agents.evals import REGEX_MAX_SUBJECT_CHARS, check_regex_safety

from nexura_runtime.evaluation import studio_engine as engine
from nexura_runtime.evaluation.studio_engine import StudioEvalError
from nexura_runtime.observability.run_diagnostics import redact_secret_text

#: Raw import size accepted before parsing (the converted dataset has its own, smaller cap).
MAX_IMPORT_BYTES = 4 * 1024 * 1024
#: Line problems reported in one error; the count of the rest is given.
MAX_REPORTED_ERRORS = 50
#: Tag added to every imported case so imported cases can be found and filtered.
IMPORT_TAG = "openai-evals"
#: Deepest nesting of objects and lists accepted in one row (or in the criteria).
MAX_NESTING = 64

#: Item fields tried, in order, when no input field is given.
INPUT_CANDIDATES = ("input", "question", "prompt", "query", "text")
#: Item fields tried, in order, when no expected field is given and no criterion names one.
EXPECTED_CANDIDATES = (
    "expected_output",
    "expected",
    "ideal",
    "answer",
    "correct_label",
    "label",
    "reference",
    "ground_truth",
    "target",
)

_ITEM_REF = re.compile(r"^\{\{\s*item\.([A-Za-z0-9_.\-]+)\s*\}\}$")
_OUTPUT_TEXT = re.compile(r"^\{\{\s*sample\.output_text\s*\}\}$")
_TEMPLATE = re.compile(r"\{\{.*?\}\}")
_SURROGATE = re.compile("[\ud800-\udfff]")
_SURROGATE_ERROR = (
    "holds a lone UTF-16 surrogate (such as the escape \\ud800), which is not valid text"
)
_TOO_DEEP_ERROR = f"nested deeper than {MAX_NESTING} levels"


class OpenAIImportError(StudioEvalError):
    """The import was refused; ``field_errors`` name the lines (``"line 3"``) or fields."""


@dataclass
class OpenAIImportResult:
    """A converted dataset and a report of every choice the converter made."""

    dataset: dict[str, Any]
    input_field: str
    expected_field: str | None
    scorers: list[dict[str, Any]] = field(default_factory=list)
    mapped_criteria: list[dict[str, Any]] = field(default_factory=list)
    unmapped_criteria: list[dict[str, Any]] = field(default_factory=list)
    skipped_lines: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    samples_ignored: int = 0
    redactions: int = 0

    def report(self) -> dict[str, Any]:
        """The JSON report: fields used, scorers, unmapped criteria, skipped lines."""
        return {
            "source": "openai-evals",
            "dataset_id": self.dataset["dataset_id"],
            "cases": len(self.dataset["cases"]),
            "input_field": self.input_field,
            "expected_field": self.expected_field,
            "scorers": self.scorers,
            "mapped_criteria": self.mapped_criteria,
            "unmapped_criteria": self.unmapped_criteria,
            "skipped_lines": self.skipped_lines,
            "samples_ignored": self.samples_ignored,
            "redactions": self.redactions,
            "warnings": self.warnings,
        }


# ------------------------------------------------------------------ reading rows


def _strip_item(path: str) -> str:
    path = path.strip()
    match = _ITEM_REF.match(path)
    if match:
        return match.group(1)
    return path[5:] if path.startswith("item.") else path


def _lookup(item: dict[str, Any], path: str) -> tuple[bool, Any]:
    """``item[path]``, or a dotted path into nested objects. ``(found, value)``."""
    if path in item:
        return True, item[path]
    current: Any = item
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _value_problem(value: Any, depth: int = 0) -> str | None:
    """Why a parsed value cannot be imported (too deep, or a lone surrogate), or ``None``.

    Iterative, so a value nested just under the parser's own limit cannot exhaust the stack in
    the later steps (validation, redaction, the JSON response).
    """
    stack: list[tuple[Any, int]] = [(value, depth)]
    while stack:
        current, level = stack.pop()
        if isinstance(current, str):
            if _SURROGATE.search(current):
                return _SURROGATE_ERROR
        elif isinstance(current, (dict, list)):
            if level >= MAX_NESTING:
                return _TOO_DEEP_ERROR
            if isinstance(current, dict):
                for key, item in current.items():
                    stack.append((key, level))
                    stack.append((item, level + 1))
            else:
                stack.extend((item, level + 1) for item in current)
    return None


def _checked(row: Any) -> Any:
    problem = _value_problem(row)
    return row if problem is None else _BadLine(problem)


def _parse(text: str) -> Any:
    """``json.loads``; ``None`` when the text is not one JSON value (or nests past the stack)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return None


def _rows(text: str) -> list[tuple[int, Any]]:
    """``(line number, parsed value)``; a parse failure is ``(line, _BadLine(msg))``."""
    stripped = text.strip()
    # A whole-file JSON array, or an inline run source {"type": "file_content", "content": [...]}.
    if stripped.startswith("["):
        whole = _parse(stripped)
        if isinstance(whole, list):
            return [(i + 1, _checked(row)) for i, row in enumerate(whole)]
    if stripped.startswith("{"):
        whole = _parse(stripped)  # None for several lines: ordinary JSONL
        if isinstance(whole, dict):
            if whole.get("format") == engine.DATASET_FORMAT:
                raise OpenAIImportError(
                    422,
                    "ALREADY_NEXURA_DATASET",
                    "This is already a Nexura dataset export; import it as a dataset instead.",
                )
            content = whole.get("content")
            if whole.get("type") == "file_content" and isinstance(content, list):
                return [(i + 1, _checked(row)) for i, row in enumerate(content)]
            return [(1, _checked(whole))]  # one row, possibly pretty-printed
    out: list[tuple[int, Any]] = []
    # JSONL lines end at "\n" only: str.splitlines() would also split inside a JSON string at a
    # raw U+2028, U+0085 or form feed, which JSON allows there.
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.removesuffix("\r")
        if not line.strip():
            continue
        try:
            out.append((number, _checked(json.loads(line))))
        except json.JSONDecodeError as exc:
            out.append((number, _BadLine(f"not valid JSON: {exc.msg} (column {exc.colno})")))
        except RecursionError:
            out.append((number, _BadLine(_TOO_DEEP_ERROR)))
    return out


@dataclass
class _BadLine:
    error: str


def _item_of(row: Any) -> tuple[dict[str, Any] | None, bool, str | None]:
    """``(item, had_sample, error)`` for one row."""
    if isinstance(row, _BadLine):
        return None, False, row.error
    if not isinstance(row, dict):
        return None, False, f"expected a JSON object, got {type(row).__name__}"
    if "item" in row:
        item = row["item"]
        if not isinstance(item, dict):
            return None, False, "'item' must be an object"
        extra = sorted(set(row) - {"item", "sample"})
        if extra:
            return None, False, f"unexpected keys next to 'item': {', '.join(extra)}"
        return item, "sample" in row, None
    return row, False, None


def _message_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif (
                isinstance(part, dict)
                and part.get("type") in ("text", "input_text", "output_text")
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
            else:
                return None
        return "".join(parts)
    return None


def _input_value(value: Any) -> tuple[Any, list[dict[str, str]] | None, str | None]:
    """``(case input, original chat messages or None, error)``."""
    if isinstance(value, str):
        return value, None, None
    if isinstance(value, dict):
        return value, None, None
    if isinstance(value, list) and value and all(isinstance(m, dict) for m in value):
        messages: list[dict[str, str]] = []
        for m in value:
            role, text = m.get("role"), _message_text(m.get("content"))
            if not isinstance(role, str) or text is None:
                return (
                    None,
                    None,
                    "input messages need a role and text content (images and files are "
                    "not converted)",
                )
            messages.append({"role": role, "content": text})
        users = [m["content"] for m in messages if m["role"] == "user"]
        if not users:
            return None, None, "input messages have no user message"
        return users[-1], messages, None
    return None, None, "input must be text, an object or a list of chat messages"


# ------------------------------------------------------------------ criteria


def _criteria_list(criteria: Any) -> list[dict[str, Any]]:
    """Accept the eval object (``{"testing_criteria": [...]}``) or the list itself."""
    if criteria is None:
        return []
    if isinstance(criteria, dict):
        criteria = criteria.get("testing_criteria", [])
    if not isinstance(criteria, list):
        raise OpenAIImportError(
            422,
            "INVALID_CRITERIA",
            "testing_criteria must be a list or an eval object with 'testing_criteria'.",
        )
    problem = _value_problem(criteria)
    if problem is not None:
        raise OpenAIImportError(422, "INVALID_CRITERIA", f"testing_criteria {problem}.")
    return [c if isinstance(c, dict) else {"type": repr(c)} for c in criteria]


def _criterion_field(criterion: dict[str, Any]) -> str | None:
    """The item field a string check compares the output with, if it is a template."""
    if criterion.get("type") not in ("string_check", "text_similarity"):
        return None
    if not _OUTPUT_TEXT.match(str(criterion.get("input", "")).strip()):
        return None
    match = _ITEM_REF.match(str(criterion.get("reference", "")).strip())
    return match.group(1) if match else None


def _map_criteria(
    criteria: list[dict[str, Any]], expected_field: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """``(scorer specs, mapped, unmapped)``; Studio allows one scorer of each type."""
    scorers: list[dict[str, Any]] = []
    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    used: dict[str, dict[str, Any]] = {}

    # The report is printed, written to --report and returned by the route, so everything
    # copied from the criteria is redacted like the dataset is.
    def skip(c: dict[str, Any], reason: str) -> None:
        unmapped.append(
            {
                "name": redact_secret_text(str(c.get("name", ""))),
                "type": redact_secret_text(str(c.get("type"))),
                "reason": redact_secret_text(reason),
            }
        )

    for c in criteria:
        kind = c.get("type")
        name = redact_secret_text(str(c.get("name", "")))
        if kind == "string_check":
            op = c.get("operation")
            if not _OUTPUT_TEXT.match(str(c.get("input", "")).strip()):
                skip(c, "input is not {{ sample.output_text }}; Studio scorers read the final text")
                continue
            reference = str(c.get("reference", ""))
            ref_field = _criterion_field(c)
            if op in ("ne", "neq"):
                skip(c, "no Nexura scorer passes when the output differs from a value")
                continue
            if op not in ("eq", "like", "ilike"):
                skip(c, f"unknown operation {op!r}")
                continue
            if ref_field is not None:
                if ref_field != expected_field:
                    skip(
                        c,
                        f"compares with item.{ref_field}, but expected_output comes from "
                        f"{'item.' + expected_field if expected_field else 'no field'}",
                    )
                    continue
                if op == "eq":
                    spec = {"type": "exact_match"}
                    note = (
                        "Nexura exact_match ignores case and repeated whitespace; OpenAI 'eq' is "
                        "case-sensitive"
                    )
                else:
                    spec = {"type": "contains"}
                    note = (
                        "same check (output contains the expected value, ignoring case)"
                        if op == "ilike"
                        else "Nexura contains ignores case; OpenAI 'like' is case-sensitive"
                    )
            elif _TEMPLATE.search(reference):
                skip(c, "reference is a template other than {{ item.<field> }}")
                continue
            else:
                if redact_secret_text(reference) != reference:
                    # re.escape only adds backslashes, so a scorer would carry the value.
                    skip(c, "the reference looks like a secret; it is not copied into a scorer")
                    continue
                escaped = re.escape(reference)
                pattern = {
                    "eq": rf"\A{escaped}\Z",
                    "like": escaped,
                    "ilike": f"(?i){escaped}",
                }[str(op)]
                unusable = check_regex_safety(pattern)
                if unusable is not None:
                    skip(
                        c, f"the escaped reference does not fit a Studio regex scorer ({unusable})"
                    )
                    continue
                spec = {"type": "regex", "pattern": pattern}
                note = (
                    f"a regex on the final output; an output over {REGEX_MAX_SUBJECT_CHARS} "
                    "characters fails it ('output too long'), where OpenAI still checks it"
                )
            key = str(spec["type"])
            if key in used:
                if used[key] == spec:
                    mapped.append({"name": name, "type": kind, "scorer": spec, "note": note})
                else:
                    skip(
                        c, f"a Studio run takes one {key} scorer; another criterion already uses it"
                    )
                continue
            used[key] = spec
            scorers.append(spec)
            mapped.append({"name": name, "type": kind, "scorer": spec, "note": note})
        elif kind == "text_similarity":
            skip(
                c,
                f"no Nexura scorer for {c.get('evaluation_metric', 'similarity')} similarity",
            )
        elif kind in ("score_model", "label_model"):
            skip(
                c,
                "model-graded; Studio's llm_judge scorer needs a judge model and a rubric you "
                "write, so it is not created automatically",
            )
        elif kind == "python":
            skip(c, "Nexura never runs uploaded code; rewrite it as a scorer in your own code")
        elif kind == "multi":
            skip(c, "multi graders are not converted")
        else:
            skip(c, "unknown criterion type")
    return scorers, mapped, unmapped


# ------------------------------------------------------------------ conversion


def _pick(
    first: dict[str, Any], given: str | None, candidates: Sequence[str]
) -> tuple[str | None, bool]:
    if given:
        return _strip_item(given), True
    for name in candidates:
        if name in first:
            return name, False
    return None, False


def _lines_text(lines: list[int], limit: int = 10) -> str:
    """``line 2`` / ``lines 2, 5`` / ``lines 1, 2, ... and 7 more``."""
    if len(lines) == 1:
        return f"line {lines[0]}"
    head = ", ".join(str(n) for n in lines[:limit])
    more = f" and {len(lines) - limit} more" if len(lines) > limit else ""
    return f"lines {head}{more}"


def _line_errors(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    # Error text can quote item content (a key, a name); redact it like the dataset.
    shown = [{**e, "error": redact_secret_text(e["error"])} for e in errors[:MAX_REPORTED_ERRORS]]
    if len(errors) > MAX_REPORTED_ERRORS:
        shown.append(
            {"field": "lines", "error": f"{len(errors) - MAX_REPORTED_ERRORS} more lines failed"}
        )
    return shown


def convert_openai_evals(
    text: str,
    dataset_id: str,
    *,
    input_field: str | None = None,
    expected_field: str | None = None,
    name_field: str | None = None,
    testing_criteria: Any = None,
    name: str | None = None,
    description: str | None = None,
    skip_invalid: bool = False,
) -> OpenAIImportResult:
    """Convert OpenAI Evals items to a ``nexura.eval-dataset`` document.

    ``text`` is the JSONL file (``{"item": {...}}`` or bare objects per line), a JSON array of
    rows, or an inline run source ``{"type": "file_content", "content": [...]}``.
    ``input_field`` / ``expected_field`` / ``name_field`` name item fields (``ticket_text``,
    ``item.ticket_text`` or a dotted path). ``testing_criteria`` is the eval's criteria list
    (or the eval object) and is mapped to scorers where one corresponds. Raises
    :class:`OpenAIImportError` with line-numbered ``field_errors``.
    """
    if not engine.SAFE_ID.match(dataset_id):
        raise OpenAIImportError(
            400, "INVALID_DATASET_ID", "Dataset id must be 1-80 chars [A-Za-z0-9_.-]."
        )
    options = {
        "input_field": input_field,
        "expected_field": expected_field,
        "name_field": name_field,
        "name": name,
        "description": description,
    }
    bad_options = [k for k, v in options.items() if isinstance(v, str) and _SURROGATE.search(v)]
    if bad_options:
        raise OpenAIImportError(
            422,
            "OPENAI_IMPORT_INVALID",
            "An import option is not valid text.",
            [{"field": k, "error": _SURROGATE_ERROR} for k in bad_options],
        )
    # Windows editors and spreadsheet exports start UTF-8 files with a byte order mark.
    text = text.removeprefix("\ufeff")
    # "surrogatepass": a lone surrogate is reported on its line below, not raised here.
    size = len(text.encode("utf-8", "surrogatepass"))
    if size > MAX_IMPORT_BYTES:
        raise OpenAIImportError(
            413, "IMPORT_TOO_LARGE", f"The file is {size} bytes; the limit is {MAX_IMPORT_BYTES}."
        )
    criteria = _criteria_list(testing_criteria)
    rows = _rows(text)
    if not rows:
        raise OpenAIImportError(
            422,
            "EMPTY_DATASET",
            "The file has no eval items.",
            [{"field": "file", "error": "empty"}],
        )
    if len(rows) > engine.MAX_CASES:
        raise OpenAIImportError(
            413,
            "DATASET_TOO_LARGE",
            f"The file has {len(rows)} items; a dataset may hold at most {engine.MAX_CASES}. "
            "Split it into several datasets.",
        )

    items: list[tuple[int, dict[str, Any], bool]] = []
    errors: list[dict[str, str]] = []
    for line, row in rows:
        item, had_sample, error = _item_of(row)
        if error is not None or item is None:
            errors.append({"field": f"line {line}", "error": error or "not an item"})
            continue
        items.append((line, item, had_sample))
    if not items:
        raise OpenAIImportError(
            422, "OPENAI_IMPORT_INVALID", "No line holds an eval item.", _line_errors(errors)
        )

    first = items[0][1]
    in_field, in_given = _pick(first, input_field, INPUT_CANDIDATES)
    if in_field is None:
        raise OpenAIImportError(
            422,
            "INPUT_FIELD_REQUIRED",
            "Could not tell which item field is the input. Item fields: "
            + redact_secret_text(", ".join(sorted(first)) or "none")
            + ". Name it with input_field (--input-field).",
            [{"field": "input_field", "error": f"none of {list(INPUT_CANDIDATES)} found"}],
        )
    criterion_fields = [f for f in (_criterion_field(c) for c in criteria) if f]
    exp_field: str | None
    if expected_field:
        exp_field = _strip_item(expected_field)
    elif criterion_fields:
        exp_field = criterion_fields[0]
    else:
        exp_field, _ = _pick(first, None, EXPECTED_CANDIDATES)
    name_path = _strip_item(name_field) if name_field else None

    result_warnings: list[str] = []
    cases: list[dict[str, Any]] = []
    lines_of_cases: list[int] = []
    seen: set[str] = set()
    as_text: list[int] = []
    flattened = multi = samples = 0
    for line, item, had_sample in items:
        where = f"line {line}"
        found, raw_input = _lookup(item, in_field)
        if not found:
            errors.append({"field": where, "error": f"missing input field {in_field!r}"})
            continue
        value, messages, error = _input_value(raw_input)
        if error is not None:
            errors.append({"field": where, "error": error})
            continue
        case: dict[str, Any] = {"input": value, "tags": [IMPORT_TAG]}
        rendered = False
        metadata: dict[str, Any] = {"openai_line": line}
        if messages is not None and messages != [{"role": "user", "content": value}]:
            metadata["openai_input_messages"] = messages
            flattened += 1
        if exp_field is not None:
            has_expected, expected = _lookup(item, exp_field)
            if has_expected and expected is not None:
                if isinstance(expected, list) and all(isinstance(e, str) for e in expected):
                    if not expected:
                        errors.append({"field": where, "error": f"{exp_field!r} is an empty list"})
                        continue
                    if len(expected) > 1:
                        metadata["openai_acceptable_outputs"] = expected
                        multi += 1
                    expected = expected[0]
                if isinstance(expected, (bool, int, float)):
                    # OpenAI renders {{ item.<field> }} into the reference as text, so 4
                    # matches the output "4"; exact_match would compare the number itself.
                    expected = json.dumps(expected)
                    rendered = True
                case["expected_output"] = expected
        label: str
        if name_path is not None:
            has_name, raw_name = _lookup(item, name_path)
            if not has_name or not isinstance(raw_name, (str, int)) or not str(raw_name).strip():
                errors.append({"field": where, "error": f"missing name field {name_path!r}"})
                continue
            label = str(raw_name).strip()[: engine.MAX_NAME_CHARS]
        else:
            label = f"item-{line:04d}"
        # Names are stored redacted, so two names that differ only in a secret collide, and the
        # error must not repeat the secret.
        shown = redact_secret_text(label)
        if shown in seen:
            errors.append({"field": where, "error": f"duplicate case name {shown!r}"})
            continue
        seen.add(shown)
        case["name"] = label
        used = {in_field, exp_field, name_path}
        rest = {k: v for k, v in item.items() if k not in used}
        if rest:
            metadata["openai_item"] = rest
        case["metadata"] = metadata
        if had_sample:
            samples += 1
        if rendered:
            as_text.append(line)
        cases.append(case)
        lines_of_cases.append(line)

    skipped: list[dict[str, Any]] = []
    if errors:
        if not skip_invalid:
            raise OpenAIImportError(
                422,
                "OPENAI_IMPORT_INVALID",
                f"{len(errors)} line(s) could not be converted; fix them or skip invalid lines.",
                _line_errors(errors),
            )
        skipped = [
            {"line": int(e["field"].removeprefix("line ")), "error": redact_secret_text(e["error"])}
            for e in errors
        ]
        result_warnings.append(f"skipped {len(errors)} invalid line(s)")
    if not cases:
        raise OpenAIImportError(
            422, "OPENAI_IMPORT_INVALID", "No line could be converted.", _line_errors(errors)
        )

    without_expected: list[int] = []
    if exp_field is not None and any("expected_output" in c for c in cases):
        # OpenAI renders a missing {{ item.<field> }} as empty text, so the check fails; a case
        # without expected_output would score "n/a" and pass. Give it the empty text instead.
        for case, line in zip(cases, lines_of_cases, strict=True):
            if "expected_output" not in case:
                case["expected_output"] = ""
                without_expected.append(line)

    body = {
        "name": name if name is not None else dataset_id,
        "description": description
        if description is not None
        else f"Imported from OpenAI Evals ({len(cases)} items).",
        "cases": cases,
    }
    try:
        clean, meta, redactions = engine.validate_dataset(dataset_id, body)
    except StudioEvalError as exc:
        raise OpenAIImportError(
            exc.status_code,
            exc.code,
            exc.message,
            [_to_line(fe, lines_of_cases) for fe in exc.field_errors],
        ) from exc

    if exp_field is None:
        result_warnings.append(
            "no expected field found; cases have no expected_output (set expected_field)"
        )
    elif not any("expected_output" in c for c in cases):
        result_warnings.append(f"no item has {exp_field!r}; cases have no expected_output")
    if without_expected:
        result_warnings.append(
            f"{len(without_expected)} item(s) have no {exp_field!r} "
            f"({_lines_text(without_expected)}): expected_output is empty text, as OpenAI "
            "renders a missing field, so exact_match passes only an empty output"
        )
    if as_text:
        result_warnings.append(
            f"{len(as_text)} item(s) have a number or true/false as {exp_field!r} "
            f'({_lines_text(as_text)}): expected_output is its text (4 becomes "4"), as OpenAI '
            "compares the rendered reference as text"
        )
    if flattened:
        result_warnings.append(
            f"{flattened} input(s) were chat messages: the case input is the last user message "
            "and the full list is in metadata.openai_input_messages"
        )
    if multi:
        result_warnings.append(
            f"{multi} item(s) list several acceptable answers: expected_output is the first, "
            "all are in metadata.openai_acceptable_outputs"
        )
    if samples:
        result_warnings.append(
            f"{samples} row(s) carried a 'sample' (an earlier model output); it is not imported "
            "because a Studio eval runs its target again"
        )
    scorers, mapped, unmapped = _map_criteria(criteria, exp_field)
    dataset = {
        "format": engine.DATASET_FORMAT,
        "format_version": 1,
        "dataset_id": dataset_id,
        "name": meta["name"] or dataset_id,
        "description": meta["description"],
        "cases": clean,
    }
    return OpenAIImportResult(
        dataset=dataset,
        input_field=in_field,
        expected_field=exp_field,
        scorers=scorers,
        mapped_criteria=mapped,
        unmapped_criteria=unmapped,
        skipped_lines=skipped,
        warnings=result_warnings,
        samples_ignored=samples,
        redactions=redactions,
    )


def _to_line(error: dict[str, str], lines: list[int]) -> dict[str, str]:
    """``cases[3].input`` -> ``line 7 (input)`` using the case's source line."""
    match = re.match(r"^cases\[(\d+)\](?:\.(.+))?$", error.get("field", ""))
    if match and int(match.group(1)) < len(lines):
        line = lines[int(match.group(1))]
        part = f" ({match.group(2)})" if match.group(2) else ""
        return {"field": f"line {line}{part}", "error": redact_secret_text(error.get("error", ""))}
    return {**error, "error": redact_secret_text(error.get("error", ""))}


def dataset_to_cases(dataset: dict[str, Any]) -> list[Any]:
    """SDK ``Case`` objects for :func:`nexura_agents.evaluate` from a converted dataset.

    ``Case.expected`` is the case's ``expected_output``; the rest stays in ``metadata``.
    """
    from nexura_agents import Case

    out: list[Any] = []
    for c in dataset["cases"]:
        text = c["input"] if isinstance(c["input"], str) else json.dumps(c["input"], default=str)
        out.append(
            Case(
                name=c["name"],
                input=text,
                expected=c.get("expected_output"),
                metadata=dict(c.get("metadata") or {}),
            )
        )
    return out


__all__ = [
    "EXPECTED_CANDIDATES",
    "IMPORT_TAG",
    "INPUT_CANDIDATES",
    "MAX_IMPORT_BYTES",
    "OpenAIImportError",
    "OpenAIImportResult",
    "convert_openai_evals",
    "dataset_to_cases",
]
