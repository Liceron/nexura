# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Durable session storage, transaction-buffered turns, and long-term memory store."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from nexura_agents.contracts.canonical import canonical_json
from nexura_agents.session import deserialize_session, serialize_session
from nexura_agents.types import Message

from nexura_runtime.memory.errors import (
    DuplicateSystemPromptError,
    FactNotFoundError,
    SessionAccessDeniedError,
    SessionNotFoundError,
    SessionRevisionConflictError,
)
from nexura_runtime.memory.models import (
    ContextTruncationPolicy,
    ConversationSession,
    LongTermFact,
)
from nexura_runtime.storage.migrations import run_sqlite_migrations


class SessionTransaction:
    """Explicit staged turn buffer ensuring aborted runs do not mutate session history."""

    def __init__(
        self,
        store: MemoryStore,
        workspace_id: str,
        session_id: str,
        principal_id: str,
        base_revision: int,
    ) -> None:
        self.store = store
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.principal_id = principal_id
        self.base_revision = base_revision
        self.staged_messages: list[Message] = []
        self._committed = False
        self._aborted = False

    def stage_turn(self, user_msg: Message, assistant_msg: Message) -> None:
        """Stage a user/assistant turn in memory without persisting to storage."""
        if self._committed or self._aborted:
            raise RuntimeError("Transaction is already finalized.")
        if user_msg.role == "system" or assistant_msg.role == "system":
            raise DuplicateSystemPromptError(self.session_id)
        self.staged_messages.extend([user_msg, assistant_msg])

    def abort(self) -> None:
        """Explicitly abort transaction and discard all staged turns."""
        self.staged_messages.clear()
        self._aborted = True

    def commit(self) -> ConversationSession:
        """Atomically commit all staged messages to storage with revision check."""
        if self._aborted:
            raise RuntimeError("Cannot commit an aborted transaction.")
        if self._committed:
            raise RuntimeError("Transaction has already been committed.")

        sess = self.store.get_session(
            self.workspace_id, self.session_id, principal_id=self.principal_id
        )
        if not sess:
            raise SessionNotFoundError(self.workspace_id, self.session_id)

        all_messages = list(sess.messages) + self.staged_messages
        new_sess = self.store.save_session(
            self.workspace_id,
            self.session_id,
            all_messages,
            expected_revision=self.base_revision,
            principal_id=self.principal_id,
            metadata=sess.metadata,
        )
        self._committed = True
        return new_sess


class MemoryStore:
    """Manages workspace-scoped sessions, ownership checks, context bounds, and facts."""

    def __init__(self, db_path_or_conn: str | Path | sqlite3.Connection = ":memory:") -> None:
        if isinstance(db_path_or_conn, (str, Path)):
            self.db_path = str(db_path_or_conn)
            self._shared_conn: sqlite3.Connection | None = None
            conn = self._get_connection()
            try:
                run_sqlite_migrations(conn)
            finally:
                conn.close()
        else:
            self.db_path = ":memory:"
            self._shared_conn = db_path_or_conn
            run_sqlite_migrations(self._shared_conn)

    def _get_connection(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            autocommit=True,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # -----------------------------------------------------------------------
    # Session Management
    # -----------------------------------------------------------------------
    def create_session(
        self,
        workspace_id: str,
        principal_id: str,
        session_id: str,
        initial_messages: list[Message] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationSession:
        """Create a new conversation session owned by a principal."""
        messages = list(initial_messages or [])
        # Ensure at most one system message
        system_count = sum(1 for m in messages if m.role == "system")
        if system_count > 1:
            raise DuplicateSystemPromptError(session_id)

        now = time.time()
        meta = dict(metadata or {})
        meta["principal_id"] = principal_id
        meta["created_at"] = now

        payload_str = serialize_session(session_id, revision=1, messages=messages)
        # Store metadata inside payload
        payload_data = json.loads(payload_str)
        payload_data["metadata"] = meta
        raw_json = json.dumps(payload_data, ensure_ascii=False)

        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO unified_sessions (workspace_id, session_id, revision, data, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (workspace_id, session_id, 1, raw_json, now),
            )
            conn.execute("COMMIT")
            return ConversationSession(
                session_id=session_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                revision=1,
                messages=messages,
                metadata=meta,
                created_at=now,
                updated_at=now,
            )
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def get_session(
        self,
        workspace_id: str,
        session_id: str,
        principal_id: str | None = None,
    ) -> ConversationSession | None:
        """Retrieve a session by workspace and ID, enforcing principal ownership."""
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """
                SELECT revision, data, updated_at FROM unified_sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (workspace_id, session_id),
            )
            row = cur.fetchone()
            if not row:
                return None

            raw_json = row["data"]
            _, rev, messages = deserialize_session(raw_json)

            data_obj = json.loads(raw_json)
            meta = data_obj.get("metadata", {})
            owner = meta.get("principal_id", "default")
            created_at = meta.get("created_at", row["updated_at"])

            # Enforce user boundary
            if principal_id is not None and principal_id != owner:
                raise SessionAccessDeniedError(
                    workspace_id=workspace_id,
                    session_id=session_id,
                    principal_id=principal_id,
                    owner_principal_id=owner,
                )

            return ConversationSession(
                session_id=session_id,
                workspace_id=workspace_id,
                principal_id=owner,
                revision=row["revision"],
                messages=messages,
                metadata=meta,
                created_at=created_at,
                updated_at=row["updated_at"],
            )
        finally:
            if self._shared_conn is None:
                conn.close()

    def save_session(
        self,
        workspace_id: str,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
        principal_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationSession:
        """Atomically update session messages and advance revision using CAS."""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                SELECT revision, data, updated_at FROM unified_sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (workspace_id, session_id),
            )
            row = cur.fetchone()
            if not row:
                raise SessionNotFoundError(workspace_id, session_id)

            actual_rev = row["revision"]
            if actual_rev != expected_revision:
                raise SessionRevisionConflictError(
                    session_id=session_id,
                    expected_revision=expected_revision,
                    actual_revision=actual_rev,
                )

            data_obj = json.loads(row["data"])
            meta = dict(metadata or data_obj.get("metadata", {}))
            owner = meta.get("principal_id", "default")

            if principal_id is not None and principal_id != owner:
                raise SessionAccessDeniedError(
                    workspace_id=workspace_id,
                    session_id=session_id,
                    principal_id=principal_id,
                    owner_principal_id=owner,
                )

            # Prevent duplicate system prompts
            system_prompts = [m for m in messages if m.role == "system"]
            if len(system_prompts) > 1:
                raise DuplicateSystemPromptError(session_id)

            new_rev = expected_revision + 1
            now = time.time()
            meta["updated_at"] = now

            payload_str = serialize_session(session_id, revision=new_rev, messages=messages)
            new_payload_obj = json.loads(payload_str)
            new_payload_obj["metadata"] = meta
            new_raw_json = json.dumps(new_payload_obj, ensure_ascii=False)

            conn.execute(
                """
                UPDATE unified_sessions
                SET revision = ?, data = ?, updated_at = ?
                WHERE workspace_id = ? AND session_id = ? AND revision = ?
                """,
                (new_rev, new_raw_json, now, workspace_id, session_id, expected_revision),
            )
            conn.execute("COMMIT")

            return ConversationSession(
                session_id=session_id,
                workspace_id=workspace_id,
                principal_id=owner,
                revision=new_rev,
                messages=messages,
                metadata=meta,
                created_at=meta.get("created_at", now),
                updated_at=now,
            )
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def append_turn(
        self,
        workspace_id: str,
        session_id: str,
        user_message: Message,
        assistant_message: Message,
        expected_revision: int,
        principal_id: str | None = None,
    ) -> ConversationSession:
        """Append a completed user-assistant turn to the session with CAS."""
        if user_message.role == "system" or assistant_message.role == "system":
            raise DuplicateSystemPromptError(session_id)

        sess = self.get_session(workspace_id, session_id, principal_id=principal_id)
        if not sess:
            raise SessionNotFoundError(workspace_id, session_id)

        all_msgs = list(sess.messages) + [user_message, assistant_message]
        return self.save_session(
            workspace_id,
            session_id,
            all_msgs,
            expected_revision=expected_revision,
            principal_id=principal_id,
            metadata=sess.metadata,
        )

    def delete_session(
        self,
        workspace_id: str,
        session_id: str,
        principal_id: str | None = None,
    ) -> bool:
        """Delete a session from storage after verifying ownership."""
        sess = self.get_session(workspace_id, session_id, principal_id=principal_id)
        if not sess:
            return False

        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM unified_sessions WHERE workspace_id = ? AND session_id = ?",
                (workspace_id, session_id),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def begin_transaction(
        self,
        workspace_id: str,
        session_id: str,
        principal_id: str,
    ) -> SessionTransaction:
        """Begin an explicit turn transaction for buffered execution."""
        sess = self.get_session(workspace_id, session_id, principal_id=principal_id)
        if not sess:
            raise SessionNotFoundError(workspace_id, session_id)
        return SessionTransaction(
            self,
            workspace_id=workspace_id,
            session_id=session_id,
            principal_id=principal_id,
            base_revision=sess.revision,
        )

    # -----------------------------------------------------------------------
    # Long-Term Memory Facts
    # -----------------------------------------------------------------------
    def store_fact(
        self,
        workspace_id: str,
        principal_id: str,
        key: str,
        value: Any,
        category: str = "general",
        source_session_id: str | None = None,
        source_message_index: int | None = None,
        confidence: float = 1.0,
    ) -> LongTermFact:
        """Store or update a long-term memory fact."""
        fact_id = f"fact_{uuid.uuid4().hex[:12]}"
        now = time.time()
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO long_term_facts (
                    workspace_id, principal_id, fact_id, fact_key, value_json,
                    category, source_session_id, source_message_index, confidence,
                    is_deleted, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    workspace_id,
                    principal_id,
                    fact_id,
                    key,
                    canonical_json(value),
                    category,
                    source_session_id,
                    source_message_index,
                    confidence,
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
            return LongTermFact(
                fact_id=fact_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                fact_key=key,
                value=value,
                category=category,
                source_session_id=source_session_id,
                source_message_index=source_message_index,
                confidence=confidence,
                is_deleted=False,
                created_at=now,
                updated_at=now,
            )
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def get_fact(
        self,
        workspace_id: str,
        principal_id: str,
        fact_id: str,
        include_deleted: bool = False,
    ) -> LongTermFact | None:
        """Retrieve a specific fact by ID."""
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """
                SELECT * FROM long_term_facts
                WHERE workspace_id = ? AND principal_id = ? AND fact_id = ?
                """,
                (workspace_id, principal_id, fact_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            if not include_deleted and row["is_deleted"] != 0:
                return None
            val = json.loads(row["value_json"])
            return LongTermFact(
                fact_id=row["fact_id"],
                workspace_id=row["workspace_id"],
                principal_id=row["principal_id"],
                fact_key=row["fact_key"],
                value=val,
                category=row["category"],
                source_session_id=row["source_session_id"],
                source_message_index=row["source_message_index"],
                confidence=row["confidence"],
                is_deleted=bool(row["is_deleted"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        finally:
            if self._shared_conn is None:
                conn.close()

    def find_facts(
        self,
        workspace_id: str,
        principal_id: str,
        query_key: str | None = None,
        category: str | None = None,
        include_deleted: bool = False,
    ) -> list[LongTermFact]:
        """Query facts, guaranteeing deleted memory is excluded from retrieval."""
        conn = self._get_connection()
        try:
            query = """
                SELECT * FROM long_term_facts
                WHERE workspace_id = ? AND principal_id = ?
            """
            params: list[Any] = [workspace_id, principal_id]
            if not include_deleted:
                query += " AND is_deleted = 0"
            if query_key is not None:
                query += " AND fact_key = ?"
                params.append(query_key)
            if category is not None:
                query += " AND category = ?"
                params.append(category)

            query += " ORDER BY created_at ASC"
            cur = conn.execute(query, params)
            rows = cur.fetchall()
            results: list[LongTermFact] = []
            for row in rows:
                val = json.loads(row["value_json"])
                results.append(
                    LongTermFact(
                        fact_id=row["fact_id"],
                        workspace_id=row["workspace_id"],
                        principal_id=row["principal_id"],
                        fact_key=row["fact_key"],
                        value=val,
                        category=row["category"],
                        source_session_id=row["source_session_id"],
                        source_message_index=row["source_message_index"],
                        confidence=row["confidence"],
                        is_deleted=bool(row["is_deleted"]),
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return results
        finally:
            if self._shared_conn is None:
                conn.close()

    def delete_fact(
        self,
        workspace_id: str,
        principal_id: str,
        fact_id: str,
        hard_delete: bool = False,
    ) -> bool:
        """Delete a long-term memory fact (soft-delete by default, excluded from retrieval)."""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                SELECT is_deleted FROM long_term_facts
                WHERE workspace_id = ? AND principal_id = ? AND fact_id = ?
                """,
                (workspace_id, principal_id, fact_id),
            )
            row = cur.fetchone()
            if not row:
                raise FactNotFoundError(workspace_id, principal_id, fact_id)

            now = time.time()
            if hard_delete:
                conn.execute(
                    """
                    DELETE FROM long_term_facts
                    WHERE workspace_id = ? AND principal_id = ? AND fact_id = ?
                    """,
                    (workspace_id, principal_id, fact_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE long_term_facts
                    SET is_deleted = 1, updated_at = ?
                    WHERE workspace_id = ? AND principal_id = ? AND fact_id = ?
                    """,
                    (now, workspace_id, principal_id, fact_id),
                )
            conn.execute("COMMIT")
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    # -----------------------------------------------------------------------
    # Bounded Context Construction
    # -----------------------------------------------------------------------
    def build_bounded_context(
        self,
        session: ConversationSession,
        policy: ContextTruncationPolicy,
        facts: list[LongTermFact] | None = None,
    ) -> list[Message]:
        """Construct bounded message list for LLM context, preventing duplicate system prompts."""
        raw_msgs = list(session.messages)

        # 1. Identify existing system message if any
        system_msg: Message | None = None
        non_system_msgs: list[Message] = []
        for m in raw_msgs:
            if m.role == "system":
                if system_msg is None:
                    system_msg = m
            else:
                non_system_msgs.append(m)

        # 2. Format long-term facts block
        facts_text = ""
        active_facts = [f for f in (facts or []) if not f.is_deleted]
        if active_facts:
            facts_lines = [f"- {f.fact_key}: {f.value}" for f in active_facts]
            facts_text = "Retrieved User Facts:\n" + "\n".join(facts_lines)

        # 3. Assemble system prompt without duplication
        final_system_content: str | None = None
        if system_msg and facts_text:
            final_system_content = f"{system_msg.content}\n\n{facts_text}"
        elif system_msg:
            final_system_content = system_msg.content
        elif facts_text:
            final_system_content = facts_text

        # 4. Truncate non-system messages according to policy
        max_allowed = policy.max_messages
        if final_system_content:
            max_allowed = max(1, max_allowed - 1)

        truncated_history = non_system_msgs[-max_allowed:]

        bounded_list: list[Message] = []
        if final_system_content:
            bounded_list.append(Message(role="system", content=final_system_content))
        bounded_list.extend(truncated_history)

        return bounded_list
