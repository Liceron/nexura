# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Large JSON payloads stored as blobs, with a small reference left in the database column.

A payload whose UTF-8 size is above the threshold (``NEXURA_BLOB_OFFLOAD_BYTES``, 8 KiB by
default in Postgres mode) is written to the blob store; the column then holds the *marker*::

    {"$nexura_blob": {"key": ..., "sha256": ..., "size": ..., "content_type": ...},
     "preview": "<first characters>", "store": "bs_<store id>"}

``store`` ties the row to the blob store that holds the bytes (``db blob-gc`` checks it). The
column must keep this exact text (TEXT, not JSONB, which would reorder the keys).

Rows written before offloading existed, and payloads below the threshold, stay inline and read
exactly as before. A marker is honoured only when its key is in the row's own workspace, and a
payload that merely *looks* like a marker is always offloaded, so a stored value can never point
a reader at another workspace's blob.

Reads are cached: keys are random and written once, so verified bytes never change. A payload
that cannot be read (deleted by retention, failing its integrity check, or a store error) reads
as ``payload_unavailable`` with a ``reason``; it never fails the whole run read.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any

from nexura_runtime.blobs.base import (
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRef,
    BlobStore,
    BlobStoreError,
    store_has_blobs,
    validate_store_id,
    workspace_segment,
)
from nexura_runtime.blobs.binding import (
    BlobOwnerError,
    binding_ident,
    claim_refused,
    owner_mismatch,
    owner_without_database,
)

logger = logging.getLogger(__name__)

MARKER_KEY = "$nexura_blob"
#: ``json.dumps`` output of a dict whose first key is the marker key starts with this.
MARKER_PREFIX = '{"' + MARKER_KEY + '"'
JSON_CONTENT_TYPE = "application/json"
DEFAULT_OFFLOAD_BYTES = 8192
PREVIEW_CHARS = 256
#: Decoded payloads kept in memory per codec (keys are write-once, so entries never go stale).
DEFAULT_CACHE_BYTES = 32 * 1024 * 1024

#: Studio run events (the Runs and Traces pages).
RUN_EVENT_NAMESPACE = "run-events"
#: Recorded traces imported for offline evaluation (``POST /evaluations/traces``).
EVAL_TRACE_NAMESPACE = "eval-traces"
#: Namespaces whose blobs age-based retention may delete while rows still point at them.
PAYLOAD_NAMESPACES = frozenset({RUN_EVENT_NAMESPACE, EVAL_TRACE_NAMESPACE})
#: In strict mode a payload that cannot go to the blob store is stored inline only up to this
#: many bytes (R3-3); above it only a preview is kept (``omitted_payload``).
DEFAULT_MAX_INLINE_BYTES = 1024 * 1024
#: Namespaces whose readers accept an omitted payload (run events read it as data; eval traces
#: must parse as a trace, so they keep the inline fallback).
OMITTABLE_NAMESPACES = frozenset({RUN_EVENT_NAMESPACE})

_MARKER_FIELDS = (frozenset({MARKER_KEY, "preview"}), frozenset({MARKER_KEY, "preview", "store"}))


def parse_marker_with_store(stored: Any) -> tuple[BlobRef, str, str | None] | None:
    """``(ref, preview, store id or None)`` when ``stored`` is a well-formed marker."""
    if not isinstance(stored, str) or not stored.startswith(MARKER_PREFIX):
        return None
    try:
        value = json.loads(stored)
    except ValueError:
        return None
    if not isinstance(value, dict) or frozenset(value) not in _MARKER_FIELDS:
        return None
    preview = value["preview"]
    if not isinstance(preview, str):
        return None
    try:
        ref = BlobRef.from_dict(value[MARKER_KEY])
        store = validate_store_id(value["store"]) if "store" in value else None
    except BlobStoreError:
        return None
    return ref, preview, store


def parse_marker(stored: Any) -> tuple[BlobRef, str] | None:
    """``(ref, preview)`` when ``stored`` is a well-formed marker, else ``None``."""
    parsed = parse_marker_with_store(stored)
    return None if parsed is None else (parsed[0], parsed[1])


def marker_text(ref: BlobRef, preview: str, store: str | None = None) -> str:
    value: dict[str, Any] = {MARKER_KEY: ref.to_dict(), "preview": preview}
    if store is not None:
        value["store"] = store
    return json.dumps(value, ensure_ascii=False)


class BlobPayloads:
    """Offload large JSON column values to a ``BlobStore``; read them back transparently.

    ``threshold`` is in bytes; ``0`` disables offloading new payloads (existing markers are still
    read). It is never applied to a payload that looks like a marker: that one is always stored
    as a blob, so it reads back as itself. ``cache_bytes`` bounds the in-memory LRU of verified,
    decoded payloads by their UTF-8 size (``0`` disables it); entries are keyed by the blob's
    key, sha256 and size, so a reference that records other bytes never hits.

    Bindings (``add_binding``): the databases whose rows reference this codec's blobs. Each one
    records the store's identity before its first blob reference is written (see
    ``nexura_runtime.blobs.binding``). ``strict`` (Postgres mode or a ``gs://`` store): the store
    must have passed ``check_ready`` at startup, and a later write that still fails is logged at
    ERROR; otherwise (a plain local SQLite service) the fallback to an inline payload is quiet.

    ``max_inline_bytes`` (R3-3): when a write fails, a run-event payload larger than this is not
    stored inline; the column keeps only a preview (``omitted_payload``). ``None`` means no cap
    (default unless ``strict``, where it is ``DEFAULT_MAX_INLINE_BYTES``). ``stats()`` returns
    the counters (``inline_fallbacks``, ``omitted_payloads``) that ``/readyz`` shows operators.
    """

    def __init__(
        self,
        store: BlobStore,
        threshold: int = DEFAULT_OFFLOAD_BYTES,
        *,
        preview_chars: int = PREVIEW_CHARS,
        cache_bytes: int = DEFAULT_CACHE_BYTES,
        strict: bool = False,
        max_inline_bytes: int | None = None,
    ) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            raise ValueError("blob offload threshold must be a non-negative integer of bytes")
        if max_inline_bytes is None and strict:
            max_inline_bytes = DEFAULT_MAX_INLINE_BYTES
        if max_inline_bytes is not None and (
            isinstance(max_inline_bytes, bool)
            or not isinstance(max_inline_bytes, int)
            or max_inline_bytes < 0
        ):
            raise ValueError("max_inline_bytes must be a non-negative integer of bytes")
        self.max_inline_bytes = max_inline_bytes
        self.omitted_payloads = 0
        self.store = store
        self.threshold = threshold
        self.preview_chars = preview_chars
        self.cache_bytes = max(0, int(cache_bytes))
        self.strict = strict
        self.inline_fallbacks = 0
        self._cache: OrderedDict[tuple[str, str, int], tuple[str, int]] = OrderedDict()
        self._cached_bytes = 0
        self._lock = threading.Lock()
        self._bindings: dict[tuple[str, str], Any] = {}
        self._bound: set[tuple[str, str]] = set()
        self._bind_lock = threading.Lock()

    # -- bindings ---------------------------------------------------------------------

    def add_binding(self, binding: Any) -> None:
        """Register a database that will hold references to this codec's blobs."""
        with self._bind_lock:
            self._bindings.setdefault(binding_ident(binding), binding)

    def _ensure_bound(self, store_id: str) -> None:
        with self._bind_lock:
            pending = [(i, b) for i, b in self._bindings.items() if i not in self._bound]
            if not pending:
                return
            # Every owner decision comes before any binding row is written (review L3).
            self._check_owner(store_id)
            for ident, binding in pending:
                binding.ensure(store_id, self.store.describe())
                self._bound.add(ident)

    def _check_owner(self, store_id: str) -> None:
        """Two-way binding (R3-1; review H1 and L4).

        - The store has an owner: one of this codec's databases must be it
          (``BlobOwnerError`` otherwise, also for a service whose databases cannot own one).
        - The store has none and a database here can own it (Postgres): it claims the store,
          but only when the store is empty or that database already uses it (bound to it, or
          a reference names it). A non-empty store is never claimed silently
          (``BlobClaimRefused``; the operator claims it with ``db blob-claim``).
        - Otherwise (SQLite only): nothing to record; the binding stays one-way.
        """
        read_owner = getattr(self.store, "owner", None)
        claim = getattr(self.store, "claim_owner", None)
        if not callable(read_owner) or not callable(claim):
            return
        claimants = [
            b for b in self._bindings.values() if callable(getattr(b, "database_id", None))
        ]
        describe = self.store.describe()
        owner = read_owner()
        if owner is not None:
            ids = [str(b.database_id()) for b in claimants]
            if not ids:
                raise owner_without_database(str(owner), describe)
            if owner not in ids:
                raise owner_mismatch(str(owner), ids[0], describe)
            return
        if not claimants:
            return
        binding = claimants[0]
        mine = str(binding.database_id())
        if store_has_blobs(self.store) and not uses_store(binding, store_id):
            raise claim_refused(describe, mine)
        recorded = claim(mine)
        if recorded != mine:
            raise owner_mismatch(str(recorded), mine, describe)

    def bind_now(self) -> bool:
        """Bind every registered database now, when offloading is on (MVP WP3, R3-2).

        The service calls this right after attaching the codec to its stores, so the first
        offload never binds inside a database write. Returns whether it bound. A plain local
        SQLite service is lenient: a failure is logged and the write path retries (its payload
        then stays inline); in strict mode ``check_ready`` at startup raises instead.
        """
        if self.threshold <= 0:
            return False
        try:
            self.check_ready()
        except BlobOwnerError:
            raise  # another deployment's store: never write into it (review L4)
        except Exception:
            if self.strict:
                raise
            logger.warning(
                "Blob store %s could not be bound to its database yet; the first offload retries",
                self.store.describe(),
                exc_info=True,
            )
            return False
        return True

    def stats(self) -> dict[str, Any]:
        """Counters for operators (``/readyz``): payloads stored inline because the blob store
        failed, and payloads above ``max_inline_bytes`` kept only as a preview."""
        with self._lock:
            return {
                "store": self.store.describe(),
                "offload_bytes": self.threshold,
                "max_inline_bytes": self.max_inline_bytes,
                "inline_fallbacks": self.inline_fallbacks,
                "omitted_payloads": self.omitted_payloads,
            }

    def check_ready(self) -> str:
        """Read or create the store identity (local: also the root permissions) and bind every
        registered database to it. Raises on any problem; returns the store id."""
        store_id = self.store.identity()
        if store_id is None:  # identity(create=True) never returns None for the built-in stores
            raise BlobStoreError(f"Blob store {self.store.describe()} has no identity")
        self._ensure_bound(store_id)
        return store_id

    # -- cache ------------------------------------------------------------------------

    def _cache_get(self, ref: BlobRef) -> str | None:
        key = (ref.key, ref.sha256, ref.size)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            self._cache.move_to_end(key)
            return entry[0]

    def _cache_put(self, ref: BlobRef, text: str) -> None:
        size = ref.size  # the UTF-8 bytes of ``text``: verified, or just written
        if size > self.cache_bytes:
            return
        key = (ref.key, ref.sha256, ref.size)
        with self._lock:
            if key in self._cache:
                return
            self._cache[key] = (text, size)
            self._cached_bytes += size
            while self._cached_bytes > self.cache_bytes:
                _, (_, dropped) = self._cache.popitem(last=False)
                self._cached_bytes -= dropped

    def clear_cache(self) -> None:
        """Forget every cached payload (tests; or after deleting blobs in this process)."""
        with self._lock:
            self._cache.clear()
            self._cached_bytes = 0

    # -- encode / decode --------------------------------------------------------------

    def needs_offload(self, text: str) -> bool:
        if text.startswith(MARKER_PREFIX):
            return True
        return self.threshold > 0 and len(text.encode("utf-8")) > self.threshold

    def encode(self, workspace_id: str, namespace: str, text: str) -> str:
        """The value to store in the column for the JSON ``text`` of ``workspace_id``."""
        if not self.needs_offload(text):
            return text
        data = text.encode("utf-8")
        try:
            store_id = self.store.identity()
            if store_id is None:
                raise BlobStoreError(f"Blob store {self.store.describe()} has no identity")
            self._ensure_bound(store_id)  # before the first reference reaches a database
            ref = self.store.put(workspace_id, namespace, data, JSON_CONTENT_TYPE)
        except Exception:
            if text.startswith(MARKER_PREFIX):
                raise  # it cannot be stored inline: it would read back as a reference
            # Keep the run going: an unavailable blob store must not lose the payload. The
            # row is larger than intended. In Postgres or gs:// mode the store passed its startup
            # check, so this is an outage and is logged as an error.
            cap = self.max_inline_bytes
            if cap is not None and len(data) > cap and namespace in OMITTABLE_NAMESPACES:
                # R3-3: never megabytes inline in the shared database; keep the preview.
                with self._lock:
                    self.omitted_payloads += 1
                logger.error(
                    "Blob store %s could not take a %s payload of %d bytes, which is above the "
                    "inline limit of %d bytes; only its preview is kept",
                    self.store.describe(),
                    namespace,
                    len(data),
                    cap,
                    exc_info=True,
                )
                return omitted_payload(text, data, self.preview_chars)
            with self._lock:
                self.inline_fallbacks += 1
            log = logger.error if self.strict else logger.warning
            log(
                "Blob store %s could not take a %s payload of %d bytes; it is stored inline",
                self.store.describe(),
                namespace,
                len(data),
                exc_info=True,
            )
            return text
        self._cache_put(ref, text)  # what was just written is known to be correct
        return marker_text(ref, text[: self.preview_chars], store_id)

    def reference(self, stored: Any, workspace_id: str) -> tuple[BlobRef, str] | None:
        """The marker in ``stored`` when it belongs to ``workspace_id``, else ``None``."""
        parsed = parse_marker(stored)
        if parsed is None or parsed[0].workspace != workspace_segment(workspace_id):
            return None
        return parsed

    def decode(self, stored: str, workspace_id: str) -> str:
        """The original JSON text. Raises ``BlobNotFoundError`` (deleted by retention),
        ``BlobIntegrityError`` (the bytes do not match the recorded sha256) or a store error."""
        parsed = self.reference(stored, workspace_id)
        if parsed is None:
            return stored
        ref = parsed[0]
        cached = self._cache_get(ref)
        if cached is not None:
            return cached
        text = self.store.get(ref).decode("utf-8")  # verified against sha256 and size
        self._cache_put(ref, text)
        return text


def uses_store(binding: Any, store_id: str) -> bool:
    """True when the binding's database is already bound to ``store_id`` or one of its blob
    references names it (``binding.references``)."""
    if binding.read() == store_id:
        return True
    references = getattr(binding, "references", None)
    return bool(callable(references) and references(store_id))


def omitted_payload(text: str, data: bytes, preview_chars: int = PREVIEW_CHARS) -> str:
    """The column value for a payload that neither the blob store nor the row could take: plain
    JSON (never a marker) that reads as ``payload_unavailable`` with reason ``not_stored``."""
    import hashlib

    return json.dumps(
        {
            "payload_unavailable": True,
            "reason": "not_stored",
            "message": "The payload was too large to keep in the database while the blob store "
            "was unavailable; only its preview was stored.",
            "preview": text[:preview_chars],
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        ensure_ascii=False,
    )


def unavailable_payload(stored: str, reason: str = "missing") -> dict[str, Any]:
    """What a reader gets for a payload it cannot read: the preview, the reference, a reason
    (``missing``, ``integrity`` or ``store_error``)."""
    parsed = parse_marker(stored)
    if parsed is None:
        raise ValueError("not a blob reference")
    ref, preview = parsed
    messages = {
        "missing": "The payload was stored outside the database and has been deleted "
        "(retention) or is missing.",
        "integrity": "The stored payload failed its integrity check (sha256) and is not shown.",
        "store_error": "The blob store could not be read; try again later.",
    }
    return {
        "payload_unavailable": True,
        "reason": reason,
        "message": messages[reason],
        "preview": preview,
        "size": ref.size,
        "sha256": ref.sha256,
    }


def decode_json(codec: BlobPayloads | None, stored: str, workspace_id: str) -> Any:
    """``json.loads`` of a column value, following a blob reference when there is one.

    Without a codec the value is read as before. A payload that cannot be read becomes
    ``unavailable_payload`` with its reason; integrity failures are logged at ERROR.
    """
    if codec is None:
        return json.loads(stored)
    try:
        return json.loads(codec.decode(stored, workspace_id))
    except BlobNotFoundError:
        logger.warning("A %d-byte payload blob is missing", len(stored))
        return unavailable_payload(stored, "missing")
    except BlobIntegrityError:
        logger.error("A payload blob failed its integrity check", exc_info=True)
        return unavailable_payload(stored, "integrity")
    except Exception:
        logger.warning("A payload blob could not be read", exc_info=True)
        return unavailable_payload(stored, "store_error")


class DeferredPayload(dict[str, Any]):
    """An event payload not yet read from the blob store (``get_run(..., payloads=False)``).

    It is a plain placeholder dict (``payload_deferred``, preview, size), so returning it never
    leaks or downloads anything; ``StudioStore.load_payloads`` replaces it with the payload.
    """

    def __init__(self, stored: str, workspace_id: str) -> None:
        parsed = parse_marker(stored)
        preview, size = (parsed[1], parsed[0].size) if parsed else ("", 0)
        super().__init__(payload_deferred=True, preview=preview, size=size)
        self.stored = stored
        self.workspace_id = workspace_id
