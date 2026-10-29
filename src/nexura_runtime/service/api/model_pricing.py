# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Operator-supplied model prices for the service's budgets, traces and diagnostics.

A run under a monetary cap refuses a model call whose price is unknown (fail closed). The
built-in table (``create_operator_standard_pricing_table``) only knows a few older models,
so operators add their own with ``NEXURA_MODEL_PRICING`` (or ``create_api_app(
model_pricing=...)``), a JSON object keyed by model id::

    NEXURA_MODEL_PRICING='{"gemini-3.5-flash-lite": {"input_per_million": "0.10",
      "output_per_million": "0.40", "source": "Google AI price list",
      "effective_date": "2026-10-01"}}'

Each entry needs ``input_per_million`` and ``output_per_million`` (USD per million tokens,
decimal strings or JSON numbers, 0 to 100000) and ``effective_date`` (``YYYY-MM-DD``);
``source`` (default ``NEXURA_MODEL_PRICING``) and ``cached_input_per_million`` are optional.
Any other key and a model listed twice are refused. The entries are merged over the built-in
table (an entry with the same id replaces it). A key may be the provider's model id
(``gemini-3.5-flash-lite``) or a ``NEXURA_CHAT_MODELS`` name (``gemini-flash-lite``): the price
is registered under both, so it is found whichever id a run or trace records. A built-in price
never crosses a chat model name.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from nexura_agents.usage.models import ModelPricing
from nexura_agents.usage.pricing import PricingTable, create_operator_standard_pricing_table

MODEL_PRICING_ENV = "NEXURA_MODEL_PRICING"
DEFAULT_PRICING_SOURCE = MODEL_PRICING_ENV

_REQUIRED = ("input_per_million", "output_per_million", "effective_date")
_OPTIONAL = ("source", "cached_input_per_million")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_MODELS = 500
_MAX_ID = 200
_MAX_SOURCE = 200
#: Highest accepted price (USD per million tokens); anything above is a typo, and a huge
#: exponent would overflow every capped admission instead of being refused at start.
MAX_PRICE_PER_MILLION = Decimal("100000")


class ModelPricingError(ValueError):
    """``NEXURA_MODEL_PRICING`` (or ``model_pricing``) is not a valid price list."""


def _price(model_id: str, field: str, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ModelPricingError(
            f"Model price '{model_id}': '{field}' must be a decimal number such as \"0.10\"."
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ModelPricingError(f"Model price '{model_id}': '{field}' must be finite.")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise ModelPricingError(
            f"Model price '{model_id}': '{field}' must be a decimal number such as \"0.10\"."
        ) from None
    if not amount.is_finite() or amount < 0:
        raise ModelPricingError(
            f"Model price '{model_id}': '{field}' must be a finite number of at least 0."
        )
    if amount > MAX_PRICE_PER_MILLION:
        raise ModelPricingError(
            f"Model price '{model_id}': '{field}' is above {MAX_PRICE_PER_MILLION} USD per "
            "million tokens."
        )
    return amount


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook``: JSON keeps the last of two equal keys silently; refuse them."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ModelPricingError(f"{MODEL_PRICING_ENV} lists '{key}' twice.")
        out[key] = value
    return out


def _entry(model_id: Any, raw: Any) -> ModelPricing:
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > _MAX_ID:
        raise ModelPricingError(f"Model price keys must be model ids of 1 to {_MAX_ID} characters.")
    model_id = model_id.strip()
    if not isinstance(raw, Mapping):
        raise ModelPricingError(f"Model price '{model_id}' must be an object.")
    unknown = sorted(str(k) for k in raw if k not in _REQUIRED + _OPTIONAL)
    if unknown:
        raise ModelPricingError(
            f"Model price '{model_id}' has unknown keys: {', '.join(unknown)} (allowed: "
            f"{', '.join(_REQUIRED + _OPTIONAL)})."
        )
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise ModelPricingError(f"Model price '{model_id}' needs {', '.join(missing)}.")
    effective = raw["effective_date"]
    if not isinstance(effective, str) or not _DATE.match(effective):
        raise ModelPricingError(
            f"Model price '{model_id}': 'effective_date' must be a date as YYYY-MM-DD."
        )
    try:
        date.fromisoformat(effective)
    except ValueError:
        raise ModelPricingError(
            f"Model price '{model_id}': 'effective_date' is not a valid date."
        ) from None
    source = raw.get("source", DEFAULT_PRICING_SOURCE)
    if not isinstance(source, str) or not source.strip() or len(source) > _MAX_SOURCE:
        raise ModelPricingError(
            f"Model price '{model_id}': 'source' must be text of 1 to {_MAX_SOURCE} characters."
        )
    cached = raw.get("cached_input_per_million")
    return ModelPricing(
        model_id=model_id,
        input_cost_per_million=_price(model_id, "input_per_million", raw["input_per_million"]),
        output_cost_per_million=_price(model_id, "output_per_million", raw["output_per_million"]),
        cached_prompt_cost_per_million=(
            None if cached is None else _price(model_id, "cached_input_per_million", cached)
        ),
        effective_date=effective,
        source=source.strip(),
    )


def parse_model_pricing(value: str | Mapping[str, Any] | None) -> dict[str, ModelPricing]:
    """Validate a price list (JSON text or an already parsed mapping); ``{}`` when empty."""
    if value is None:
        return {}
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            value = json.loads(value, parse_float=Decimal, object_pairs_hook=_no_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ModelPricingError(f"{MODEL_PRICING_ENV} is not valid JSON ({exc.msg}).") from None
    if not isinstance(value, Mapping):
        raise ModelPricingError(f"{MODEL_PRICING_ENV} must be a JSON object keyed by model id.")
    if len(value) > _MAX_MODELS:
        raise ModelPricingError(f"{MODEL_PRICING_ENV} lists more than {_MAX_MODELS} models.")
    parsed: dict[str, ModelPricing] = {}
    for model_id, raw in value.items():
        entry = _entry(model_id, raw)
        if entry.model_id in parsed:
            raise ModelPricingError(f"Model price '{entry.model_id}' is listed twice.")
        parsed[entry.model_id] = entry
    return parsed


def model_pricing_from_environment(environ: Mapping[str, str] | None = None) -> str | None:
    """The raw ``NEXURA_MODEL_PRICING`` value (validated by :func:`build_pricing_table`)."""
    env = environ if environ is not None else os.environ
    return env.get(MODEL_PRICING_ENV)


def build_pricing_table(
    model_pricing: str | Mapping[str, Any] | None = None,
    *,
    aliases: Mapping[str, str] | None = None,
) -> PricingTable:
    """The built-in table with the operator's prices merged over it.

    ``aliases`` maps a configured chat model name to the provider's model id. Only a price the
    operator gave (in ``model_pricing``) crosses an alias: an operator price for the name also
    prices the provider id, and one for the provider id also prices the name. A built-in price
    never does, so a chat model named like a built-in model (``gemini-1.5-flash=gemini:...``)
    does not give its unpriced provider model that price (a capped run of it is refused).
    Refused (``ModelPricingError``): two names of one provider id with different operator
    prices when the id itself has none, and an operator price for a name that is both a chat
    model name and another chat model's provider id, or for that name's provider id (either
    would be ambiguous).
    """
    table = create_operator_standard_pricing_table()
    overrides = parse_model_pricing(model_pricing)
    if overrides:
        digest = hashlib.sha256(
            json.dumps(
                {k: v.model_dump(mode="json") for k, v in sorted(overrides.items())},
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        # Each entry keeps its own source; only the merged version changes.
        table.version = f"{table.version}+operator-{digest}"
        for entry in overrides.values():
            table.add_model_pricing(entry)
    pairs = [
        (alias, model_id)
        for alias, model_id in (aliases or {}).items()
        if alias and model_id and alias != model_id
    ]
    provider_ids = {model_id for _, model_id in pairs}
    for alias, model_id in pairs:
        # A name that is also another chat model's provider id would carry two prices (its
        # own model's and the provider model's), whichever crossed the alias last.
        if alias in provider_ids and (alias in overrides or model_id in overrides):
            raise ModelPricingError(
                f"'{alias}' is a chat model name and another chat model's provider model id; "
                f"its price in {MODEL_PRICING_ENV} is ambiguous. Rename the chat model."
            )
    # An operator price for a name prices its provider id (unless the operator priced the id).
    derived: dict[str, ModelPricing] = {}
    for alias, model_id in pairs:
        if alias not in overrides or model_id in overrides:
            continue
        copy = overrides[alias].model_copy(update={"model_id": model_id})
        earlier = derived.get(model_id)
        if earlier is not None and earlier.model_dump(
            exclude={"model_id", "source", "effective_date"}
        ) != copy.model_dump(exclude={"model_id", "source", "effective_date"}):
            raise ModelPricingError(
                f"Chat models naming '{model_id}' have different prices; price "
                f"'{model_id}' itself in {MODEL_PRICING_ENV}."
            )
        derived[model_id] = copy
    for entry in derived.values():
        table.add_model_pricing(entry)
    # An operator price for a provider id (given or derived) prices its other names.
    operator_ids = set(overrides) | set(derived)
    for alias, model_id in pairs:
        if alias not in overrides and model_id in operator_ids:
            if alias in provider_ids:  # the same ambiguity, with a derived price
                raise ModelPricingError(
                    f"'{alias}' is a chat model name and another chat model's provider model "
                    f"id; its price in {MODEL_PRICING_ENV} is ambiguous. Rename the chat model."
                )
            price = table.get_pricing(model_id)
            assert price is not None
            table.add_model_pricing(price.model_copy(update={"model_id": alias}))
    return table


def check_model_pricing(
    model_pricing: str | Mapping[str, Any] | None, chat_models: Mapping[str, Any] | None
) -> PricingTable:
    """What ``create_api_app`` will build, for launchers to check before they start the app
    (every ``ModelPricingError``, alias conflicts included, becomes their clean start error)."""
    from nexura_runtime.service.api.chat_models import ChatModelRegistry

    return build_pricing_table(model_pricing, aliases=ChatModelRegistry(chat_models).model_ids())
