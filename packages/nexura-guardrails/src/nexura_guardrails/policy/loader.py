# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Load a ``GuardrailPolicy`` from a JSON or YAML document (``version: 1``).

JSON needs nothing extra; YAML needs PyYAML (``pip install "nexura-guardrails[yaml]"``).
Every key is checked: unknown keys, duplicate keys, unknown check types, rule ids used twice
and rules that name unknown checks are refused with the path of the offending field. A
policy file is data: it never names Python code, and it holds no credentials (model
credentials come from the environment or, in the service, from the operator's model
registry). Relative paths (``policy_file`` of a judge, the JSONL audit ``path``) resolve
against the policy file's directory. The policy digest covers the document and the contents
of every ``policy_file`` it names.

Example::

    version: 1
    name: acme-default
    on_error: block
    default_tool_action: allow
    timeouts: {check_seconds: 5, stage_seconds: 8}
    checks:
      pii: {type: pii, kinds: [card, aadhaar, pan, secret]}
      inject_fast: {type: injection, threshold: 0.5}
    rules:
      - id: gate-writes
        when: {stage: tool_input, effect: [non_idempotent], origin: ["mcp:*", "openapi:*"]}
        action: require_approval
      - id: scan-tool-results
        when: {stage: tool_output}
        checks: [inject_fast]
        action: block
      - id: scrub-output
        when: {stage: [model_output, output]}
        checks: [pii]
        action: scrub
    audit: {sink: jsonl, path: audit/guardrails.jsonl}
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from nexura_guardrails.policy.audit import JsonlAuditSink
from nexura_guardrails.policy.core import GuardrailPolicy
from nexura_guardrails.policy.rules import PolicyError, Rule, When

__all__ = [
    "CHECK_TYPES",
    "MAX_POLICY_BYTES",
    "load_policy",
    "policy_from_dict",
    "policy_from_json",
    "policy_from_yaml",
    "rule_from_dict",
]

MAX_POLICY_BYTES = 1_000_000
#: Deepest nesting of objects and lists a policy document may have.
MAX_POLICY_DEPTH = 32
ModelResolver = Callable[[str], Any]

_TOP_KEYS = frozenset(
    {
        "version",
        "name",
        "on_error",
        "default_tool_action",
        "timeouts",
        "checks",
        "rules",
        "audit",
    }
)
_RULE_KEYS = frozenset({"id", "when", "action", "check", "checks", "mode", "on_error"})
_WHEN_KEYS = frozenset({"stage", "tools", "origin", "toolset", "effect", "agents", "args"})
_AUDIT_KEYS = frozenset({"sink", "path", "audit_allows", "required", "capture_content"})
_TIMEOUT_KEYS = frozenset({"check_seconds", "stage_seconds"})
_SAFETY_COMMON = frozenset(
    {
        "model",
        "base_url",
        "api",
        "timeout",
        "max_output_tokens",
        "context_tokens",
        "on_error",
        "keep_alive",
        "logprobs",
        "zero_price",
        "cache",
    }
)
#: Check types and the options each accepts (``None``: the adapter's own keyword set).
CHECK_TYPES: Mapping[str, frozenset[str]] = {
    "pii": frozenset({"kinds", "action", "mask"}),
    "injection": frozenset({"threshold", "extra_patterns"}),
    "length": frozenset({"chars", "tokens"}),
    "topics": frozenset({"keywords", "deny"}),
    "json_only": frozenset(),
    "judge": frozenset(
        {
            "model",
            "policy",
            "policy_file",
            "categories",
            "timeout",
            "max_output_tokens",
            "on_error",
            "context_tokens",
            "api",
            "base_url",
            "cache",
        }
    ),
    "llama_guard": _SAFETY_COMMON | {"categories"},
    "shieldgemma": _SAFETY_COMMON | {"harms", "threshold"},
    "granite_guardian": _SAFETY_COMMON | {"risks", "version", "threshold"},
    "nemotron": _SAFETY_COMMON | {"categories"},
}
_SAFETY_ADAPTERS = {
    "llama_guard": "LlamaGuard",
    "shieldgemma": "ShieldGemma",
    "granite_guardian": "GraniteGuardian",
    "nemotron": "NemotronGuard",
}


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyError("must be an object.", path)
    for key in value:
        if not isinstance(key, str):
            raise PolicyError("keys must be strings.", path)
    return value


def _only(value: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PolicyError(f"unknown keys {unknown}; allowed: {sorted(allowed)}.", path)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise PolicyError("must be a positive number.", path)
    return float(value)


def rule_from_dict(data: Any, index: int = 0) -> Rule:
    """A ``Rule`` from its policy-file form (``{"id", "when", "action", "checks"...}``)."""
    path = f"rules[{index}]"
    data = _mapping(data, path)
    _only(data, _RULE_KEYS, path)
    if "id" not in data:
        raise PolicyError("a rule needs an id.", path)
    when_data = _mapping(data.get("when", {}), f"{path}.when")
    _only(when_data, _WHEN_KEYS, f"{path}.when")
    try:
        when = When(**when_data)
        return Rule(
            data["id"],
            when,
            action=data.get("action", "block"),
            check=data.get("check"),
            checks=data.get("checks"),
            mode=data.get("mode", "tiered"),
            on_error=data.get("on_error"),
        )
    except PolicyError as exc:
        raise PolicyError(exc.message, f"{path}.{exc.path}" if exc.path else path) from None
    except TypeError as exc:
        raise PolicyError(str(exc), path) from None


def _strings(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError("must be a list of strings.", path)
    return list(value)


def _build_check(
    name: str,
    spec: Any,
    *,
    base_dir: Path | None,
    model_resolver: ModelResolver | None,
    extra_digests: dict[str, str],
) -> Any:
    path = f"checks.{name}"
    spec = _mapping(spec, path)
    kind = spec.get("type")
    if kind not in CHECK_TYPES:
        raise PolicyError(f"unknown check type {kind!r}; use {sorted(CHECK_TYPES)}.", path)
    options = {k: v for k, v in spec.items() if k != "type"}
    _only(options, CHECK_TYPES[kind], path)
    from nexura_guardrails import prefilters

    try:
        if kind == "pii":
            kinds = _strings(options["kinds"], f"{path}.kinds") if "kinds" in options else None
            return prefilters.pii(
                kinds,
                action=options.get("action", "block"),
                mask=options.get("mask", "[REDACTED:{kind}]"),
                name=name,
            )
        if kind == "injection":
            extra = options.get("extra_patterns", [])
            return prefilters.injection(
                threshold=options.get("threshold", 0.5),
                extra_patterns=_strings(extra, f"{path}.extra_patterns"),
                name=name,
            )
        if kind == "length":
            return prefilters.length(options.get("chars"), options.get("tokens"), name=name)
        if kind == "topics":
            return prefilters.topics(
                _strings(options.get("keywords", []), f"{path}.keywords"),
                _strings(options.get("deny", []), f"{path}.deny"),
                name=name,
            )
        if kind == "json_only":
            return prefilters.json_only(name=name)
        if kind == "judge":
            return _build_judge(name, options, path, base_dir, model_resolver, extra_digests)
        from nexura_guardrails import classifiers

        adapter = getattr(classifiers, _SAFETY_ADAPTERS[kind])
        return adapter(name=name, **options)
    except PolicyError:
        raise
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"invalid options: {exc}", path) from None


def _build_judge(
    name: str,
    options: dict[str, Any],
    path: str,
    base_dir: Path | None,
    model_resolver: ModelResolver | None,
    extra_digests: dict[str, str],
) -> Any:
    from nexura_guardrails.judge import JudgeCheck

    model = options.pop("model", None)
    if not isinstance(model, str) or not model.strip():
        raise PolicyError("a judge needs a model name.", f"{path}.model")
    policy_file = options.pop("policy_file", None)
    if policy_file is not None:
        if not isinstance(policy_file, str) or not policy_file:
            raise PolicyError("must be a file path.", f"{path}.policy_file")
        file_path = Path(policy_file)
        if not file_path.is_absolute() and base_dir is not None:
            file_path = base_dir / file_path
        try:
            raw, _ = _read_regular_file(file_path)  # the same guards as the policy file
        except PolicyError as exc:
            raise PolicyError(exc.message, f"{path}.policy_file") from None
        extra_digests[f"{name}:policy_file"] = hashlib.sha256(raw).hexdigest()
        try:
            options["policy"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PolicyError("the policy file is not UTF-8 text.", f"{path}.policy_file") from None
    if "categories" in options:
        options["categories"] = _strings(options["categories"], f"{path}.categories")
    resolved: Any = model_resolver(model) if model_resolver is not None else model
    return JudgeCheck(resolved, name=name, **options)


def policy_from_dict(
    data: Any,
    *,
    base_dir: str | Path | None = None,
    model_resolver: ModelResolver | None = None,
    source: str | None = None,
    raw_digest: str | None = None,
    resolved_source: str | None = None,
) -> GuardrailPolicy:
    """Build a policy from its parsed document (see the module docstring).

    ``model_resolver(name)`` maps a judge's ``model`` to a model object or another name
    (the service passes its chat-model registry); without it Nexura model strings are used
    (``ollama:qwen3:8b`` uses Ollama's native API). ``raw_digest`` (the file bytes' digest)
    is mixed into the policy digest.
    """
    _check_depth(data)
    data = _mapping(data, "policy")
    _only(data, _TOP_KEYS, "policy")
    if data.get("version") != 1:
        raise PolicyError("version must be 1.", "version")
    base = Path(base_dir) if base_dir is not None else None
    name = data.get("name", "policy")
    timeouts = _mapping(data.get("timeouts", {}), "timeouts")
    _only(timeouts, _TIMEOUT_KEYS, "timeouts")
    check_timeout = (
        _number(timeouts["check_seconds"], "timeouts.check_seconds")
        if "check_seconds" in timeouts
        else None
    )
    stage_timeout = (
        _number(timeouts["stage_seconds"], "timeouts.stage_seconds")
        if "stage_seconds" in timeouts
        else None
    )
    extra_digests: dict[str, str] = {}
    checks: dict[str, Any] = {}
    for check_name, spec in _mapping(data.get("checks", {}), "checks").items():
        if not check_name or len(check_name) > 64:
            raise PolicyError("check names must be 1-64 characters.", "checks")
        checks[check_name] = _build_check(
            check_name,
            spec,
            base_dir=base,
            model_resolver=model_resolver,
            extra_digests=extra_digests,
        )
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise PolicyError("must be a list.", "rules")
    rules = [rule_from_dict(item, i) for i, item in enumerate(raw_rules)]
    audit_data = _mapping(data.get("audit", {}), "audit")
    _only(audit_data, _AUDIT_KEYS, "audit")
    if audit_data.get("capture_content", False) is not False:
        raise PolicyError(
            "capture_content is not supported: audit records never hold content.",
            "audit.capture_content",
        )
    sink_kind = audit_data.get("sink", "none")
    sinks: list[Any] = []
    if sink_kind == "jsonl":
        target = audit_data.get("path")
        if not isinstance(target, str) or not target:
            raise PolicyError("a jsonl audit sink needs a path.", "audit.path")
        audit_path = Path(target)
        if not audit_path.is_absolute() and base is not None:
            audit_path = base / audit_path
        sinks.append(JsonlAuditSink(audit_path))
    elif sink_kind != "none":
        raise PolicyError("sink must be 'jsonl' or 'none'.", "audit.sink")
    elif "path" in audit_data:
        raise PolicyError("path needs sink: jsonl.", "audit.path")
    for flag in ("audit_allows", "required"):
        if not isinstance(audit_data.get(flag, False), bool):
            raise PolicyError("must be true or false.", f"audit.{flag}")
    document = json.dumps(
        {"document": data, "files": extra_digests, "raw": raw_digest},
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    try:
        return GuardrailPolicy(
            name=name,
            checks=checks,
            rules=rules,
            on_error=data.get("on_error", "block"),
            default_tool_action=data.get("default_tool_action", "allow"),
            check_timeout=check_timeout,
            stage_timeout=stage_timeout,
            audit=sinks,
            audit_allows=audit_data.get("audit_allows", False),
            audit_required=audit_data.get("required", False),
            digest=hashlib.sha256(document).hexdigest(),
            source=source,
            resolved_source=resolved_source,
        )
    except PolicyError:
        raise
    except (TypeError, ValueError) as exc:
        raise PolicyError(str(exc)) from None


def _check_depth(data: Any) -> None:
    """Refuse documents nested deeper than ``MAX_POLICY_DEPTH`` (iterative: no recursion)."""
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (dict, list)):
            if depth > MAX_POLICY_DEPTH:
                raise PolicyError(f"the document is nested deeper than {MAX_POLICY_DEPTH} levels.")
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, depth + 1) for child in children)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PolicyError(f"duplicate key {key!r}.")
        out[key] = value
    return out


def policy_from_json(text: str, **kwargs: Any) -> GuardrailPolicy:
    """Build a policy from JSON text (duplicate keys refused)."""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicates)
    except PolicyError:
        raise
    except ValueError as exc:
        raise PolicyError(f"not valid JSON: {exc}") from None
    except (RecursionError, MemoryError):
        raise PolicyError(
            f"the document is nested deeper than {MAX_POLICY_DEPTH} levels."
        ) from None
    return policy_from_dict(data, **kwargs)


def policy_from_yaml(text: str, **kwargs: Any) -> GuardrailPolicy:
    """Build a policy from YAML text (``safe_load``; duplicate keys refused). Needs PyYAML:
    ``pip install "nexura-guardrails[yaml]"``."""
    try:
        yaml: Any = importlib.import_module("yaml")
    except ImportError:  # pragma: no cover - the yaml extra is installed in development
        raise PolicyError(
            'YAML policies need PyYAML: pip install "nexura-guardrails[yaml]" (or use JSON).'
        ) from None

    # A SafeLoader of our own (never yaml.Loader): no Python tags, duplicate keys refused.
    _Loader: Any = type("_PolicyLoader", (yaml.SafeLoader,), {})

    def mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
        loader.flatten_mapping(node)
        pairs = [
            (loader.construct_object(k, deep=deep), loader.construct_object(v, deep=deep))
            for k, v in node.value
        ]
        return _reject_duplicates(pairs)

    _Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        # Events first: no anchors or aliases (a few hundred bytes of aliases expand to
        # millions of values) and a bounded depth, before anything is constructed.
        depth = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise PolicyError("YAML anchors and aliases (& and *) are not allowed.")
            if isinstance(event, yaml.CollectionStartEvent):
                depth += 1
                if depth > MAX_POLICY_DEPTH:
                    raise PolicyError(
                        f"the document is nested deeper than {MAX_POLICY_DEPTH} levels."
                    )
            elif isinstance(event, yaml.CollectionEndEvent):
                depth -= 1
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass
    except PolicyError:
        raise
    except yaml.YAMLError as exc:
        raise PolicyError(f"not valid YAML: {type(exc).__name__}") from None
    except (RecursionError, MemoryError):
        raise PolicyError(
            f"the document is nested deeper than {MAX_POLICY_DEPTH} levels."
        ) from None
    return policy_from_dict(data, **kwargs)


def _read_regular_file(file_path: Path) -> tuple[bytes, Path]:
    """``(bytes, resolved path)`` of a regular file, at most ``MAX_POLICY_BYTES``.

    The path is resolved once (``os.path.realpath``: symbolic links such as a Kubernetes
    ConfigMap mount, ``policy.yaml -> ..data/policy.yaml``, are followed), then the resolved
    target is opened with ``O_NOFOLLOW`` (where the platform has it; a link swapped in after
    resolving is refused) and ``O_NONBLOCK`` (a FIFO cannot hang the start), then ``fstat``:
    anything but a regular file (a device such as ``/dev/zero``, a FIFO, a directory) is
    refused. The size cap counts the bytes actually read.
    """
    name = repr(str(file_path))
    real = Path(os.path.realpath(file_path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(real, flags)
    except FileNotFoundError:
        raise PolicyError(f"policy file {name} does not exist.") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicyError(
                f"policy file {name} changed while it was opened (a symbolic link)."
            ) from None
        raise PolicyError(f"cannot read policy file {name} ({type(exc).__name__}).") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PolicyError(f"policy file {name} is not a regular file.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_POLICY_BYTES:
                raise PolicyError(f"{name} is larger than {MAX_POLICY_BYTES} bytes.")
            chunks.append(chunk)
        return b"".join(chunks), real
    except OSError as exc:
        raise PolicyError(f"cannot read policy file {name} ({type(exc).__name__}).") from None
    finally:
        os.close(fd)


def load_policy(
    path: str | Path, *, model_resolver: ModelResolver | None = None
) -> GuardrailPolicy:
    """Load a policy file: ``.json``, or ``.yaml``/``.yml`` (needs PyYAML).

    Raises ``PolicyError`` when the file is missing, larger than 1 MB, or invalid.
    """
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix not in (".json", ".yaml", ".yml"):
        raise PolicyError(f"{str(file_path)!r}: a policy file must end in .json, .yaml or .yml.")
    raw, real = _read_regular_file(file_path)
    if real.suffix.lower() not in (".json", ".yaml", ".yml"):
        raise PolicyError(
            f"{str(file_path)!r} resolves to {real.name!r}: a policy file must end in .json, "
            ".yaml or .yml."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PolicyError(f"{str(file_path)!r} is not UTF-8 text.") from None
    options: dict[str, Any] = {
        "base_dir": real.parent,  # relative paths resolve against the resolved directory
        "resolved_source": str(real),
        "model_resolver": model_resolver,
        "source": str(file_path),
        "raw_digest": hashlib.sha256(raw).hexdigest(),
    }
    if suffix == ".json":
        return policy_from_json(text, **options)
    return policy_from_yaml(text, **options)
