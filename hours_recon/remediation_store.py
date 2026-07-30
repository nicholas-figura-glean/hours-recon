"""Private SQLite persistence for remediation workstreams and account instances."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from .evidence import TIER_RANK
from .remediation import METRIC_FIELDS, PRIORITY_RANK, build_workstreams, format_slack_followup

SCHEMA_VERSION = 2
ACTIVE_WORKSTREAM_STATUSES = {"open", "acknowledged", "in_progress", "pending_validation", "snoozed"}


class QueueError(RuntimeError):
    pass


class QueueConflict(QueueError):
    pass


class QueueValidationError(QueueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _tier_meets(current: Any, target: Any) -> bool:
    return TIER_RANK.get(str(current), 99) <= TIER_RANK.get(str(target), 0)


class RemediationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, SCHEMA_VERSION}:
                raise QueueError(f"Unsupported remediation database schema version {version}.")
            if version == 1:
                # The v2 planner intentionally starts clean: v1 cases had no
                # workstream grouping or path semantics, so pretending to
                # migrate their workflow state would create misleading data.
                connection.executescript(
                    """
                    DROP TABLE IF EXISTS observations;
                    DROP TABLE IF EXISTS events;
                    DROP TABLE IF EXISTS gaps;
                    DROP TABLE IF EXISTS cases;
                    DROP TABLE IF EXISTS queue_runs;
                    PRAGMA user_version = 0;
                    """
                )
                connection.commit()
                version = 0
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE planner_runs (
                        scope_id TEXT NOT NULL,
                        portfolio_id TEXT NOT NULL,
                        retrieval_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        report_as_of TEXT NOT NULL,
                        coverage_complete INTEGER NOT NULL,
                        report_digest TEXT,
                        workstream_count INTEGER NOT NULL,
                        instance_count INTEGER NOT NULL,
                        PRIMARY KEY(scope_id, portfolio_id, retrieval_id)
                    );

                    CREATE TABLE workstreams (
                        fingerprint TEXT PRIMARY KEY,
                        scope_id TEXT NOT NULL,
                        portfolio_id TEXT NOT NULL,
                        policy_version TEXT NOT NULL,
                        family TEXT NOT NULL,
                        group_key TEXT NOT NULL,
                        title TEXT NOT NULL,
                        dimensions_json TEXT NOT NULL,
                        reason_codes_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        priority TEXT,
                        route TEXT NOT NULL,
                        primary_owner TEXT,
                        required_partners_json TEXT NOT NULL,
                        minimum_target_tier TEXT NOT NULL,
                        due_on TEXT,
                        paths_json TEXT NOT NULL,
                        recommended_path_id TEXT NOT NULL,
                        recommendation_reason TEXT NOT NULL,
                        selected_path_id TEXT NOT NULL,
                        selected_target_tier TEXT NOT NULL,
                        selected_path_json TEXT NOT NULL,
                        assignee TEXT,
                        slack_recipient TEXT,
                        slack_prepared_at TEXT,
                        slack_copied_at TEXT,
                        slack_recipient_id TEXT,
                        slack_channel_id TEXT,
                        slack_message_ts TEXT,
                        slack_permalink TEXT,
                        slack_sent_at TEXT,
                        slack_sent_path_id TEXT,
                        slack_outbox_id TEXT,
                        slack_client_msg_id TEXT,
                        slack_message_sha256 TEXT,
                        execution_plan_json TEXT,
                        execution_path_id TEXT,
                        execution_prepared_at TEXT,
                        mcp_request_copied_at TEXT,
                        impact_json TEXT NOT NULL,
                        affected_instance_count INTEGER NOT NULL DEFAULT 0,
                        active_instance_count INTEGER NOT NULL DEFAULT 0,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        last_retrieval_id TEXT NOT NULL,
                        regression_count INTEGER NOT NULL DEFAULT 0,
                        waiver_reason TEXT,
                        waiver_expires_on TEXT,
                        waiver_approved_by TEXT,
                        snoozed_until TEXT,
                        version INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(scope_id, portfolio_id, family, group_key)
                    );

                    CREATE TABLE instances (
                        fingerprint TEXT PRIMARY KEY,
                        workstream_fingerprint TEXT NOT NULL REFERENCES workstreams(fingerprint) ON DELETE CASCADE,
                        scope_id TEXT NOT NULL,
                        portfolio_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        account_name TEXT,
                        dimension TEXT NOT NULL,
                        current_tier TEXT NOT NULL,
                        last_governed_tier TEXT,
                        reason_code TEXT,
                        summary TEXT,
                        governance_status TEXT NOT NULL,
                        priority TEXT NOT NULL,
                        minimum_target_tier TEXT NOT NULL,
                        due_on TEXT,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        last_retrieval_id TEXT NOT NULL,
                        evidence_hash TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        regression_count INTEGER NOT NULL DEFAULT 0,
                        version INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(scope_id, portfolio_id, account_id, dimension)
                    );

                    CREATE TABLE slack_outbox (
                        id TEXT PRIMARY KEY,
                        workstream_fingerprint TEXT NOT NULL REFERENCES workstreams(fingerprint) ON DELETE CASCADE,
                        scope_id TEXT NOT NULL,
                        portfolio_id TEXT NOT NULL,
                        execution_id TEXT NOT NULL,
                        path_id TEXT NOT NULL,
                        recipient_query TEXT NOT NULL,
                        recipient_id TEXT,
                        message_text TEXT NOT NULL,
                        message_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL,
                        queued_at TEXT NOT NULL,
                        claimed_at TEXT,
                        sent_at TEXT,
                        permalink TEXT,
                        error TEXT,
                        version INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE TABLE observations (
                        scope_id TEXT NOT NULL,
                        portfolio_id TEXT NOT NULL,
                        retrieval_id TEXT NOT NULL,
                        instance_fingerprint TEXT NOT NULL REFERENCES instances(fingerprint) ON DELETE CASCADE,
                        workstream_fingerprint TEXT NOT NULL REFERENCES workstreams(fingerprint) ON DELETE CASCADE,
                        observed_at TEXT NOT NULL,
                        evidence_hash TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        PRIMARY KEY(scope_id, portfolio_id, retrieval_id, instance_fingerprint),
                        FOREIGN KEY(scope_id, portfolio_id, retrieval_id)
                            REFERENCES planner_runs(scope_id, portfolio_id, retrieval_id) ON DELETE CASCADE
                    );

                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        workstream_fingerprint TEXT NOT NULL REFERENCES workstreams(fingerprint) ON DELETE CASCADE,
                        instance_fingerprint TEXT REFERENCES instances(fingerprint) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );

                    CREATE INDEX idx_workstreams_scope_portfolio_status
                        ON workstreams(scope_id, portfolio_id, status);
                    CREATE INDEX idx_workstreams_route_priority
                        ON workstreams(scope_id, portfolio_id, route, priority, status);
                    CREATE INDEX idx_instances_workstream_status
                        ON instances(workstream_fingerprint, governance_status);
                    CREATE INDEX idx_instances_owner
                        ON instances(scope_id, portfolio_id, account_id);
                    CREATE INDEX idx_events_workstream
                        ON events(workstream_fingerprint, id);
                    CREATE INDEX idx_slack_outbox_scope_status
                        ON slack_outbox(scope_id, portfolio_id, status, queued_at);
                    CREATE UNIQUE INDEX idx_slack_outbox_one_active
                        ON slack_outbox(workstream_fingerprint) WHERE status IN ('pending', 'sending', 'needs_review');
                    PRAGMA user_version = 2;
                    """
                )
                connection.commit()
            workstream_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(workstreams)").fetchall()}
            for column in (
                "slack_copied_at", "slack_recipient_id", "slack_channel_id", "slack_message_ts",
                "slack_permalink", "slack_sent_at", "slack_sent_path_id", "slack_outbox_id", "slack_client_msg_id",
                "slack_message_sha256", "execution_plan_json", "execution_path_id",
                "execution_prepared_at", "mcp_request_copied_at",
            ):
                if version == SCHEMA_VERSION and column not in workstream_columns:
                    connection.execute(f"ALTER TABLE workstreams ADD COLUMN {column} TEXT")
            instance_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(instances)").fetchall()}
            if version == SCHEMA_VERSION and "last_governed_tier" not in instance_columns:
                connection.execute("ALTER TABLE instances ADD COLUMN last_governed_tier TEXT")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS slack_outbox (
                    id TEXT PRIMARY KEY,
                    workstream_fingerprint TEXT NOT NULL REFERENCES workstreams(fingerprint) ON DELETE CASCADE,
                    scope_id TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    path_id TEXT NOT NULL,
                    recipient_query TEXT NOT NULL,
                    recipient_id TEXT,
                    message_text TEXT NOT NULL,
                    message_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    claimed_at TEXT,
                    sent_at TEXT,
                    permalink TEXT,
                    error TEXT,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_slack_outbox_scope_status
                    ON slack_outbox(scope_id, portfolio_id, status, queued_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_slack_outbox_one_active
                    ON slack_outbox(workstream_fingerprint) WHERE status IN ('pending', 'sending', 'needs_review');
                """
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        workstream_id: str,
        instance_id: Optional[str],
        event_type: str,
        actor: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        connection.execute(
            """INSERT INTO events(workstream_fingerprint, instance_fingerprint, event_type, actor, created_at, payload_json)
               VALUES(?,?,?,?,?,?)""",
            (workstream_id, instance_id, event_type, actor, _utc_now(), _json(dict(payload or {}))),
        )

    @staticmethod
    def _current_tiers(report: Mapping[str, Any]) -> Dict[Tuple[str, str], str]:
        result: Dict[Tuple[str, str], str] = {}
        for account in report.get("accounts", []):
            account_id = str(account.get("id") or "")
            dimensions = (account.get("governance") or {}).get("dimensions") or {}
            for dimension, evidence in dimensions.items():
                if account_id and isinstance(evidence, Mapping) and evidence.get("tier"):
                    result[(account_id, str(dimension))] = str(evidence["tier"])
        return result

    def observe(
        self,
        report: Mapping[str, Any],
        *,
        retrieval_id: str,
        scope_id: str,
        coverage_complete: bool,
        report_digest: Optional[str] = None,
        portfolio_id: str = "local-default",
    ) -> Dict[str, Any]:
        if not retrieval_id:
            raise QueueValidationError("A source retrieval ID is required.")
        if not scope_id or not portfolio_id:
            raise QueueValidationError("Scope and portfolio identities are required.")
        coverage_complete = coverage_complete is True
        workstreams = build_workstreams(report, scope_id=scope_id, portfolio_id=portfolio_id)
        instances = [item for workstream in workstreams for item in workstream.get("instances", [])]
        account_ids = {str(item.get("id")) for item in report.get("accounts", []) if item.get("id")}
        current_tiers = self._current_tiers(report)
        observed_at = _utc_now()
        report_as_of = str(report.get("meta", {}).get("as_of") or "")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_run = connection.execute(
                """SELECT retrieval_id FROM planner_runs
                   WHERE scope_id=? AND portfolio_id=? AND retrieval_id=?""",
                (scope_id, portfolio_id, retrieval_id),
            ).fetchone()
            if existing_run:
                connection.rollback()
                return {
                    "new_source_observation": False,
                    "revalidation_performed": False,
                    "reason": "same_retrieval_id",
                    "retrieval_id": retrieval_id,
                }

            connection.execute(
                """INSERT INTO planner_runs(
                       scope_id, portfolio_id, retrieval_id, observed_at, report_as_of,
                       coverage_complete, report_digest, workstream_count, instance_count
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    scope_id, portfolio_id, retrieval_id, observed_at, report_as_of,
                    int(coverage_complete), report_digest, len(workstreams), len(instances),
                ),
            )
            seen_instance_ids: Set[str] = set()
            touched_workstream_ids: Set[str] = set()

            for candidate in workstreams:
                workstream_id = str(candidate["fingerprint"])
                touched_workstream_ids.add(workstream_id)
                existing = connection.execute(
                    "SELECT * FROM workstreams WHERE fingerprint=?", (workstream_id,)
                ).fetchone()
                candidate_paths = list(candidate.get("paths") or [])
                recommended_id = str(candidate["recommended_path_id"])
                recommended_path = next(item for item in candidate_paths if str(item.get("id")) == recommended_id)
                if existing:
                    selected_id = str(existing["selected_path_id"] or "")
                    valid_selected = next((item for item in candidate_paths if str(item.get("id")) == selected_id), None)
                    if valid_selected is None:
                        selected_id = recommended_id
                        selected_path = recommended_path
                        selected_target = str(recommended_path["target_tier"])
                        self._event(
                            connection, workstream_id=workstream_id, instance_id=None,
                            event_type="selected_path_replaced_by_policy", actor="system",
                            payload={"retrieval_id": retrieval_id, "selected_path_id": selected_id},
                        )
                    else:
                        # Keep the original selected snapshot for auditability;
                        # current alternatives remain in paths_json.
                        selected_path = _loads(existing["selected_path_json"], valid_selected)
                        selected_target = str(existing["selected_target_tier"] or valid_selected["target_tier"])
                    old_due = str(existing["due_on"] or "")
                    candidate_due = str(candidate.get("due_on") or "")
                    due_on = min(old_due, candidate_due) if old_due and candidate_due else old_due or candidate_due or None
                    status = str(existing["status"])
                    event_type = "workstream_updated"
                    if status == "pending_validation" and coverage_complete:
                        status = "in_progress"
                        event_type = "validation_failed"
                    elif status == "pending_validation":
                        event_type = "incomplete_retrieval_preserved_validation"
                    connection.execute(
                        """UPDATE slack_outbox SET status='cancelled', message_text='',
                           error='Superseded by a new source observation', version=version+1
                           WHERE workstream_fingerprint=? AND status='pending'""",
                        (workstream_id,),
                    )
                    connection.execute(
                        """UPDATE workstreams SET policy_version=?, title=?, dimensions_json=?, reason_codes_json=?,
                           status=?, priority=?, route=?, primary_owner=?, required_partners_json=?, minimum_target_tier=?,
                           due_on=?, paths_json=?, recommended_path_id=?, recommendation_reason=?, selected_path_id=?,
                           selected_target_tier=?, selected_path_json=?, impact_json=?, affected_instance_count=?,
                           last_seen=?, last_retrieval_id=?, execution_plan_json=NULL, execution_path_id=NULL,
                           execution_prepared_at=NULL, mcp_request_copied_at=NULL, slack_prepared_at=NULL,
                           slack_copied_at=NULL, slack_recipient_id=NULL, slack_channel_id=NULL,
                           slack_message_ts=NULL, slack_permalink=NULL, slack_sent_at=NULL,
                           slack_sent_path_id=NULL, slack_outbox_id=NULL, slack_client_msg_id=NULL, slack_message_sha256=NULL,
                           version=version+1 WHERE fingerprint=?""",
                        (
                            candidate["policy_version"], candidate["title"], _json(candidate["dimensions"]),
                            _json(candidate["reason_codes"]), status, candidate["priority"], candidate["route"],
                            candidate["primary_owner"], _json(candidate["required_partners"]),
                            candidate["minimum_target_tier"], due_on, _json(candidate_paths), recommended_id,
                            candidate["recommendation_reason"], selected_id, selected_target, _json(selected_path),
                            _json(candidate["impact"]), len(candidate.get("instances", [])), observed_at,
                            retrieval_id, workstream_id,
                        ),
                    )
                    self._event(
                        connection, workstream_id=workstream_id, instance_id=None,
                        event_type=event_type, actor="system", payload={"retrieval_id": retrieval_id},
                    )
                else:
                    connection.execute(
                        """INSERT INTO workstreams(
                               fingerprint, scope_id, portfolio_id, policy_version, family, group_key, title,
                               dimensions_json, reason_codes_json, status, priority, route, primary_owner,
                               required_partners_json, minimum_target_tier, due_on, paths_json,
                               recommended_path_id, recommendation_reason, selected_path_id,
                               selected_target_tier, selected_path_json, impact_json, affected_instance_count,
                               active_instance_count, first_seen, last_seen, last_retrieval_id
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            workstream_id, scope_id, portfolio_id, candidate["policy_version"], candidate["family"],
                            candidate["group_key"], candidate["title"], _json(candidate["dimensions"]),
                            _json(candidate["reason_codes"]), "open", candidate["priority"], candidate["route"],
                            candidate["primary_owner"], _json(candidate["required_partners"]),
                            candidate["minimum_target_tier"], candidate["due_on"], _json(candidate_paths),
                            recommended_id, candidate["recommendation_reason"], recommended_id,
                            recommended_path["target_tier"], _json(recommended_path), _json(candidate["impact"]),
                            len(candidate.get("instances", [])), len(candidate.get("instances", [])),
                            observed_at, observed_at, retrieval_id,
                        ),
                    )
                    self._event(
                        connection, workstream_id=workstream_id, instance_id=None,
                        event_type="workstream_created", actor="system",
                        payload={"retrieval_id": retrieval_id, "recommended_path_id": recommended_id},
                    )

                for item in candidate.get("instances", []):
                    instance_id = str(item["fingerprint"])
                    seen_instance_ids.add(instance_id)
                    previous = connection.execute(
                        "SELECT * FROM instances WHERE fingerprint=?", (instance_id,)
                    ).fetchone()
                    governance_status = "open"
                    regression_count = 0
                    event_type = "instance_detected"
                    if previous:
                        old_workstream_id = str(previous["workstream_fingerprint"])
                        touched_workstream_ids.add(old_workstream_id)
                        governance_status = str(previous["governance_status"])
                        regression_count = int(previous["regression_count"])
                        if governance_status == "governed":
                            if coverage_complete:
                                governance_status = "open"
                                regression_count += 1
                                event_type = "instance_regressed"
                            else:
                                event_type = "incomplete_retrieval_preserved_governed"
                        else:
                            event_type = "instance_updated"
                        old_due = str(previous["due_on"] or "")
                        candidate_due = str(item.get("due_on") or "")
                        due_on = min(old_due, candidate_due) if old_due and candidate_due else old_due or candidate_due or None
                        connection.execute(
                            """UPDATE instances SET workstream_fingerprint=?, account_name=?, current_tier=?, reason_code=?,
                               summary=?, governance_status=?, priority=?, minimum_target_tier=?, due_on=?, last_seen=?,
                               last_retrieval_id=?, evidence_hash=?, evidence_json=?, regression_count=?, version=version+1
                               WHERE fingerprint=?""",
                            (
                                workstream_id, item.get("account_name"), item["current_tier"], item["reason_code"],
                                item.get("summary"), governance_status, item["priority"], item["minimum_target_tier"],
                                due_on, observed_at, retrieval_id, item["evidence_hash"], _json(item["evidence"]),
                                regression_count, instance_id,
                            ),
                        )
                        if old_workstream_id != workstream_id:
                            event_type = "instance_regrouped"
                    else:
                        connection.execute(
                            """INSERT INTO instances(
                                   fingerprint, workstream_fingerprint, scope_id, portfolio_id, account_id, account_name,
                                   dimension, current_tier, reason_code, summary, governance_status, priority,
                                   minimum_target_tier, due_on, first_seen, last_seen, last_retrieval_id,
                                   evidence_hash, evidence_json
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?)""",
                            (
                                instance_id, workstream_id, scope_id, portfolio_id, item["account_id"],
                                item.get("account_name"), item["dimension"], item["current_tier"], item["reason_code"],
                                item.get("summary"), item["priority"], item["minimum_target_tier"], item["due_on"],
                                observed_at, observed_at, retrieval_id, item["evidence_hash"], _json(item["evidence"]),
                            ),
                        )
                    connection.execute(
                        """INSERT INTO observations(
                               scope_id, portfolio_id, retrieval_id, instance_fingerprint, workstream_fingerprint,
                               observed_at, evidence_hash, evidence_json
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            scope_id, portfolio_id, retrieval_id, instance_id, workstream_id, observed_at,
                            item["evidence_hash"], _json(item["evidence"]),
                        ),
                    )
                    self._event(
                        connection, workstream_id=workstream_id, instance_id=instance_id,
                        event_type=event_type, actor="system",
                        payload={"retrieval_id": retrieval_id, "tier": item["current_tier"], "reason_code": item["reason_code"]},
                    )

            if coverage_complete and account_ids:
                placeholders = ",".join("?" for _ in account_ids)
                existing_instances = connection.execute(
                    f"""SELECT * FROM instances WHERE scope_id=? AND portfolio_id=?
                        AND account_id IN ({placeholders})""",
                    (scope_id, portfolio_id, *sorted(account_ids)),
                ).fetchall()
                for row in existing_instances:
                    instance_id = str(row["fingerprint"])
                    if instance_id in seen_instance_ids:
                        continue
                    current_tier = current_tiers.get((str(row["account_id"]), str(row["dimension"])))
                    if not current_tier or not _tier_meets(current_tier, row["minimum_target_tier"]):
                        continue
                    touched_workstream_ids.add(str(row["workstream_fingerprint"]))
                    if row["governance_status"] != "governed" or str(row["current_tier"]) != current_tier:
                        connection.execute(
                            """UPDATE instances SET governance_status='governed', current_tier=?, last_governed_tier=?,
                               last_seen=?, last_retrieval_id=?, version=version+1 WHERE fingerprint=?""",
                            (current_tier, current_tier, observed_at, retrieval_id, instance_id),
                        )
                        self._event(
                            connection, workstream_id=str(row["workstream_fingerprint"]), instance_id=instance_id,
                            event_type="instance_governed_by_revalidation", actor="system",
                            payload={"retrieval_id": retrieval_id, "tier": current_tier},
                        )

            for workstream_id in touched_workstream_ids:
                self._recompute_workstream(connection, workstream_id, coverage_complete=coverage_complete)
            connection.commit()
            os.chmod(self.path, 0o600)
            return {
                "new_source_observation": True,
                "revalidation_performed": coverage_complete,
                "reason": "new_complete_retrieval" if coverage_complete else "new_incomplete_retrieval",
                "retrieval_id": retrieval_id,
                "workstream_count": len(workstreams),
                "instance_count": len(instances),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recompute_workstream(
        self,
        connection: sqlite3.Connection,
        workstream_id: str,
        *,
        coverage_complete: bool = False,
    ) -> None:
        workstream = connection.execute(
            "SELECT * FROM workstreams WHERE fingerprint=?", (workstream_id,)
        ).fetchone()
        if not workstream:
            return
        rows = connection.execute(
            "SELECT * FROM instances WHERE workstream_fingerprint=?", (workstream_id,)
        ).fetchall()
        active = [row for row in rows if row["governance_status"] == "open"]
        current_status = str(workstream["status"])
        if not active:
            status = "governed"
            priority = None
            due_on = None
            connection.execute(
                """UPDATE slack_outbox SET status='cancelled', message_text='',
                   error='Workstream governed by source revalidation', version=version+1
                   WHERE workstream_fingerprint=? AND status='pending'""",
                (workstream_id,),
            )
        else:
            if current_status == "governed" and coverage_complete:
                status = "open"
                connection.execute(
                    "UPDATE workstreams SET regression_count=regression_count+1 WHERE fingerprint=?",
                    (workstream_id,),
                )
                self._event(
                    connection, workstream_id=workstream_id, instance_id=None,
                    event_type="workstream_reopened", actor="system",
                )
            elif current_status in ACTIVE_WORKSTREAM_STATUSES | {"waived"}:
                status = current_status
            else:
                status = "open"
            strongest = min(active, key=lambda row: (PRIORITY_RANK.get(str(row["priority"]), 99), str(row["dimension"])))
            priority = strongest["priority"]
            due_values = sorted(str(row["due_on"]) for row in active if row["due_on"])
            due_on = due_values[0] if due_values else None
        connection.execute(
            """UPDATE workstreams SET status=?, priority=?, due_on=?, affected_instance_count=?,
               active_instance_count=?, version=version+1 WHERE fingerprint=?""",
            (status, priority, due_on, len(rows), len(active), workstream_id),
        )

    def _expire_temporary_states(
        self,
        connection: sqlite3.Connection,
        *,
        scope_id: str,
        portfolio_id: str,
    ) -> None:
        today = date.today().isoformat()
        rows = connection.execute(
            """SELECT fingerprint, status FROM workstreams
               WHERE scope_id=? AND portfolio_id=? AND (
                    (status='waived' AND waiver_expires_on IS NOT NULL AND waiver_expires_on < ?)
                 OR (status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until < ?)
               )""",
            (scope_id, portfolio_id, today, today),
        ).fetchall()
        for row in rows:
            event_type = "waiver_expired" if row["status"] == "waived" else "snooze_expired"
            connection.execute(
                """UPDATE workstreams SET status='open', waiver_reason=NULL, waiver_expires_on=NULL,
                   waiver_approved_by=NULL, snoozed_until=NULL, version=version+1 WHERE fingerprint=?""",
                (row["fingerprint"],),
            )
            self._event(
                connection, workstream_id=str(row["fingerprint"]), instance_id=None,
                event_type=event_type, actor="system",
            )
            self._recompute_workstream(connection, str(row["fingerprint"]))

    @staticmethod
    def _row_to_instance(row: sqlite3.Row, selected_target_tier: str) -> Dict[str, Any]:
        result = dict(row)
        result["evidence"] = _loads(result.pop("evidence_json", "{}"), {})
        last_governed = result.get("last_governed_tier")
        validation_tier = last_governed if result.get("governance_status") == "governed" and last_governed else result.get("current_tier")
        result["validation_tier"] = validation_tier
        result["unverified_observed_tier"] = (
            result.get("current_tier")
            if result.get("governance_status") == "governed" and last_governed and result.get("current_tier") != last_governed
            else None
        )
        result["minimum_target_met"] = _tier_meets(validation_tier, result.get("minimum_target_tier"))
        result["selected_target_tier"] = selected_target_tier
        result["selected_target_met"] = _tier_meets(validation_tier, selected_target_tier)
        return result

    @staticmethod
    def _visible_impact(instances: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
        by_account: Dict[str, Mapping[str, Any]] = {}
        for item in instances:
            evidence = item.get("evidence") or {}
            by_account.setdefault(str(item.get("account_id")), evidence.get("metric_impact") or {})
        return {
            field: round(sum(float(metrics.get(field, 0) or 0) for metrics in by_account.values()), 2)
            for field in METRIC_FIELDS
        }

    def _serialize_workstream(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        account_ids: Optional[Iterable[str]] = None,
        account_id: Optional[str] = None,
        include_events: bool = False,
    ) -> Optional[Dict[str, Any]]:
        clauses = ["workstream_fingerprint=?"]
        values: List[Any] = [row["fingerprint"]]
        if account_ids is not None:
            owned = sorted({str(value) for value in account_ids})
            if not owned:
                return None
            clauses.append("account_id IN (" + ",".join("?" for _ in owned) + ")")
            values.extend(owned)
        if account_id:
            clauses.append("account_id=?")
            values.append(account_id)
        instance_rows = connection.execute(
            "SELECT * FROM instances WHERE " + " AND ".join(clauses) +
            " ORDER BY account_name, dimension",
            values,
        ).fetchall()
        if not instance_rows:
            return None
        result = dict(row)
        for field, fallback in (
            ("dimensions_json", []), ("reason_codes_json", []), ("required_partners_json", []),
            ("paths_json", []), ("selected_path_json", {}), ("impact_json", {}),
            ("execution_plan_json", None),
        ):
            result[field.removesuffix("_json")] = _loads(result.pop(field, None), fallback)
        target = str(result.get("selected_target_tier") or "T2")
        instances = [self._row_to_instance(item, target) for item in instance_rows]
        result["instances"] = instances
        result["affected_account_count"] = len({str(item["account_id"]) for item in instances})
        result["affected_instance_count"] = len(instances)
        result["active_instance_count"] = sum(1 for item in instances if item["governance_status"] == "open")
        result["impact"] = self._visible_impact(instances)
        result["dimensions"] = sorted({str(item["dimension"]) for item in instances})
        result["reason_codes"] = sorted({str(item.get("reason_code") or "") for item in instances})
        result["minimum_target_met"] = all(bool(item["minimum_target_met"]) for item in instances)
        result["selected_target_met"] = all(bool(item["selected_target_met"]) for item in instances)
        outbox_row = connection.execute(
            """SELECT id, recipient_query, status, queued_at, claimed_at, sent_at, permalink, error, version
               FROM slack_outbox WHERE workstream_fingerprint=? ORDER BY queued_at DESC LIMIT 1""",
            (row["fingerprint"],),
        ).fetchone()
        result["slack_outbox"] = dict(outbox_row) if outbox_row else None
        if include_events:
            event_rows = connection.execute(
                """SELECT event_type, actor, created_at, payload_json, instance_fingerprint
                   FROM events WHERE workstream_fingerprint=? ORDER BY id DESC LIMIT 200""",
                (row["fingerprint"],),
            ).fetchall()
            result["events"] = [
                {
                    "event_type": event["event_type"],
                    "actor": event["actor"],
                    "created_at": event["created_at"],
                    "instance_fingerprint": event["instance_fingerprint"],
                    "payload": _loads(event["payload_json"], {}),
                }
                for event in event_rows
            ]
        return result

    def list_workstreams(
        self,
        *,
        scope_id: str,
        portfolio_id: str = "local-default",
        status: Optional[str] = None,
        route: Optional[str] = None,
        priority: Optional[str] = None,
        account_id: Optional[str] = None,
        account_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["scope_id=?", "portfolio_id=?"]
        values: List[Any] = [scope_id, portfolio_id]
        if status:
            clauses.append("status=?")
            values.append(status)
        if route:
            clauses.append("route=?")
            values.append(route)
        if priority:
            clauses.append("priority=?")
            values.append(priority)
        visible_accounts: Optional[List[str]] = None
        if account_ids is not None:
            visible_accounts = sorted({str(value) for value in account_ids})
            if visible_accounts:
                clauses.append(
                    "EXISTS (SELECT 1 FROM instances vi WHERE vi.workstream_fingerprint=workstreams.fingerprint "
                    "AND vi.account_id IN (" + ",".join("?" for _ in visible_accounts) + "))"
                )
                values.extend(visible_accounts)
            else:
                clauses.append("1=0")
        if account_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM instances ai WHERE ai.workstream_fingerprint=workstreams.fingerprint AND ai.account_id=?)"
            )
            values.append(account_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_temporary_states(connection, scope_id=scope_id, portfolio_id=portfolio_id)
            connection.commit()
            rows = connection.execute(
                "SELECT * FROM workstreams WHERE " + " AND ".join(clauses) +
                " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 9 END, due_on, title",
                values,
            ).fetchall()
            result = []
            for row in rows:
                item = self._serialize_workstream(
                    connection, row, account_ids=visible_accounts, account_id=account_id,
                )
                if item:
                    result.append(item)
            return result
        finally:
            connection.close()

    def get_workstream(
        self,
        fingerprint: str,
        *,
        scope_id: str,
        portfolio_id: str = "local-default",
        account_ids: Optional[Iterable[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM workstreams WHERE fingerprint=? AND scope_id=? AND portfolio_id=?",
                (fingerprint, scope_id, portfolio_id),
            ).fetchone()
            if not row:
                return None
            return self._serialize_workstream(connection, row, account_ids=account_ids, include_events=True)
        finally:
            connection.close()

    def summary(
        self,
        *,
        scope_id: str,
        portfolio_id: str = "local-default",
        account_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        workstreams = self.list_workstreams(
            scope_id=scope_id, portfolio_id=portfolio_id, account_ids=account_ids,
        )
        active = [item for item in workstreams if item["status"] in ACTIVE_WORKSTREAM_STATUSES]
        active_instances = [
            instance for workstream in active for instance in workstream["instances"]
            if instance["governance_status"] == "open"
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "observe_only",
            "scope_id": scope_id,
            "portfolio_id": portfolio_id,
            "workstream_count": len(workstreams),
            "active_workstream_count": len(active),
            "active_instance_count": len(active_instances),
            "governed_instance_count": sum(
                1 for workstream in workstreams for instance in workstream["instances"]
                if instance["governance_status"] == "governed"
            ),
            "p0_workstream_count": sum(1 for item in active if item["priority"] == "P0"),
            "p1_workstream_count": sum(1 for item in active if item["priority"] == "P1"),
            "p2_workstream_count": sum(1 for item in active if item["priority"] == "P2"),
            "workstreams": workstreams,
        }

    def latest_run(self, *, scope_id: str, portfolio_id: str = "local-default") -> Optional[Dict[str, Any]]:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM planner_runs WHERE scope_id=? AND portfolio_id=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (scope_id, portfolio_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def health(self, *, scope_id: str, portfolio_id: str = "local-default") -> Dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("SELECT 1").fetchone()
            return {
                "available": True,
                "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "latest_run": self.latest_run(scope_id=scope_id, portfolio_id=portfolio_id),
            }
        finally:
            connection.close()

    @staticmethod
    def _validate_future_date(value: Any, label: str) -> str:
        raw = str(value or "")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise QueueValidationError(f"A valid {label} date is required.") from exc
        if parsed <= date.today():
            raise QueueValidationError(f"{label.capitalize()} date must be in the future.")
        return raw

    @staticmethod
    def _serialize_outbox(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _select_outbox(
        connection: sqlite3.Connection,
        outbox_id: str,
        *,
        scope_id: str,
        portfolio_id: str,
        account_ids: Optional[Iterable[str]],
    ) -> Optional[sqlite3.Row]:
        query = "SELECT * FROM slack_outbox WHERE id=? AND scope_id=? AND portfolio_id=?"
        values: List[Any] = [outbox_id, scope_id, portfolio_id]
        if account_ids is not None:
            owned = sorted({str(value) for value in account_ids})
            if not owned:
                query += " AND 1=0"
            else:
                query += (
                    " AND EXISTS (SELECT 1 FROM instances oi WHERE "
                    "oi.workstream_fingerprint=slack_outbox.workstream_fingerprint AND oi.account_id IN ("
                    + ",".join("?" for _ in owned) + "))"
                )
                values.extend(owned)
        return connection.execute(query, values).fetchone()

    def list_slack_outbox(
        self,
        *,
        scope_id: str,
        portfolio_id: str = "local-default",
        status: Optional[str] = "pending",
        account_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        allowed = {"pending", "sending", "needs_review", "sent", "cancelled"}
        if status and status not in allowed:
            raise QueueValidationError("Select a valid Slack outbox status.")
        connection = self._connect()
        try:
            query = "SELECT * FROM slack_outbox WHERE scope_id=? AND portfolio_id=?"
            values: List[Any] = [scope_id, portfolio_id]
            if status:
                query += " AND status=?"
                values.append(status)
            if account_ids is not None:
                owned = sorted({str(value) for value in account_ids})
                if not owned:
                    query += " AND 1=0"
                else:
                    query += (
                        " AND EXISTS (SELECT 1 FROM instances oi WHERE "
                        "oi.workstream_fingerprint=slack_outbox.workstream_fingerprint AND oi.account_id IN ("
                        + ",".join("?" for _ in owned) + "))"
                    )
                    values.extend(owned)
            query += " ORDER BY queued_at, id"
            return [self._serialize_outbox(row) for row in connection.execute(query, values).fetchall()]
        finally:
            connection.close()

    def queue_slack_message(
        self,
        workstream_id: str,
        *,
        scope_id: str,
        portfolio_id: str,
        account_ids: Optional[Iterable[str]],
        expected_version: int,
        execution_id: str,
        path_id: str,
        recipient_query: str,
        message: str,
    ) -> Dict[str, Any]:
        recipient = str(recipient_query or "").strip()
        reviewed_message = str(message or "").strip()
        if not recipient or len(recipient) > 200:
            raise QueueValidationError("Enter a Slack user, email, @handle, channel, or Slack ID.")
        if not reviewed_message or len(reviewed_message) > 4000:
            raise QueueValidationError("Review a Slack message between 1 and 4,000 characters before queueing.")
        message_digest = sha256(reviewed_message.encode("utf-8")).hexdigest()
        outbox_id = "hro1_" + sha256(
            f"{workstream_id}\n{execution_id}\n{path_id}\n{recipient.casefold()}\n{message_digest}".encode("utf-8")
        ).hexdigest()
        owned = sorted({str(value) for value in account_ids}) if account_ids is not None else None
        connection = self._connect()
        outbox: Optional[Dict[str, Any]] = None
        idempotent = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM workstreams WHERE fingerprint=? AND scope_id=? AND portfolio_id=?"
            params: List[Any] = [workstream_id, scope_id, portfolio_id]
            if owned is not None:
                if owned:
                    query += (
                        " AND EXISTS (SELECT 1 FROM instances oi WHERE oi.workstream_fingerprint=workstreams.fingerprint "
                        "AND oi.account_id IN (" + ",".join("?" for _ in owned) + "))"
                    )
                    params.extend(owned)
                else:
                    query += " AND 1=0"
            workstream = connection.execute(query, params).fetchone()
            if not workstream:
                raise QueueValidationError("Unknown remediation workstream.")
            existing_same = connection.execute("SELECT * FROM slack_outbox WHERE id=?", (outbox_id,)).fetchone()
            if existing_same and str(existing_same["status"]) in {"pending", "sending", "needs_review", "sent"}:
                outbox = self._serialize_outbox(existing_same)
                idempotent = True
                connection.commit()
            else:
                if int(workstream["version"]) != int(expected_version):
                    raise QueueConflict("The remediation workstream changed; reload before queueing the Slack message.")
                stored_plan = _loads(workstream["execution_plan_json"], {})
                if (
                    not stored_plan
                    or str(workstream["execution_path_id"] or "") != path_id
                    or str(stored_plan.get("execution_id") or "") != execution_id
                ):
                    raise QueueValidationError("Open the selected remediation path and review its current Slack handoff before queueing.")
                active = connection.execute(
                    """SELECT * FROM slack_outbox WHERE workstream_fingerprint=?
                       AND status IN ('pending','sending','needs_review') ORDER BY queued_at DESC LIMIT 1""",
                    (workstream_id,),
                ).fetchone()
                if active and str(active["status"]) in {"sending", "needs_review"}:
                    raise QueueConflict(
                        "The existing Slack MCP delivery must be reconciled before another message can be queued."
                    )
                if active:
                    connection.execute(
                        "UPDATE slack_outbox SET status='cancelled', message_text='', version=version+1 WHERE id=?",
                        (active["id"],),
                    )
                    self._event(
                        connection, workstream_id=workstream_id, instance_id=None,
                        event_type="slack_mcp_queue_superseded", actor="local_dashboard",
                        payload={"outbox_id": str(active["id"])},
                    )
                queued_at = _utc_now()
                if existing_same:
                    connection.execute(
                        """UPDATE slack_outbox SET execution_id=?, path_id=?, recipient_query=?, recipient_id=NULL,
                           message_text=?, message_sha256=?, status='pending', queued_at=?, claimed_at=NULL,
                           sent_at=NULL, permalink=NULL, error=NULL, version=version+1 WHERE id=?""",
                        (execution_id, path_id, recipient, reviewed_message, message_digest, queued_at, outbox_id),
                    )
                else:
                    connection.execute(
                        """INSERT INTO slack_outbox(
                               id, workstream_fingerprint, scope_id, portfolio_id, execution_id, path_id,
                               recipient_query, message_text, message_sha256, status, queued_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)""",
                        (
                            outbox_id, workstream_id, scope_id, portfolio_id, execution_id, path_id,
                            recipient, reviewed_message, message_digest, queued_at,
                        ),
                    )
                connection.execute(
                    """UPDATE workstreams SET assignee=?, slack_recipient=?, slack_prepared_at=?,
                       slack_copied_at=NULL, version=version+1 WHERE fingerprint=?""",
                    (recipient, recipient, queued_at, workstream_id),
                )
                self._event(
                    connection, workstream_id=workstream_id, instance_id=None,
                    event_type="slack_mcp_queued", actor="local_dashboard",
                    payload={
                        "outbox_id": outbox_id, "recipient": recipient, "path_id": path_id,
                        "message_sha256": message_digest, "delivery": "queued_not_sent",
                    },
                )
                connection.commit()
                outbox = self._serialize_outbox(
                    connection.execute("SELECT * FROM slack_outbox WHERE id=?", (outbox_id,)).fetchone()
                )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        workstream_result = self.get_workstream(
            workstream_id, scope_id=scope_id, portfolio_id=portfolio_id, account_ids=owned,
        )
        if not workstream_result or not outbox:
            raise QueueError("The queued Slack MCP handoff is unavailable.")
        return {
            "workstream": workstream_result,
            "outbox": outbox,
            "delivery": "already_" + str(outbox["status"]) if idempotent else "queued_not_sent",
        }

    def claim_slack_outbox(
        self,
        outbox_id: str,
        *,
        scope_id: str,
        portfolio_id: str,
        expected_version: int,
        account_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_outbox(
                connection, outbox_id, scope_id=scope_id, portfolio_id=portfolio_id, account_ids=account_ids,
            )
            if not row:
                raise QueueValidationError("Unknown Slack outbox item.")
            if int(row["version"]) != int(expected_version):
                raise QueueConflict("The Slack outbox item changed; list pending messages again.")
            if str(row["status"]) != "pending":
                raise QueueValidationError(f"Cannot claim a {row['status']} Slack outbox item.")
            claimed_at = _utc_now()
            connection.execute(
                "UPDATE slack_outbox SET status='sending', claimed_at=?, version=version+1 WHERE id=?",
                (claimed_at, outbox_id),
            )
            self._event(
                connection, workstream_id=str(row["workstream_fingerprint"]), instance_id=None,
                event_type="slack_mcp_claimed", actor="glean_pi",
                payload={"outbox_id": outbox_id, "delivery": "sending_not_confirmed"},
            )
            connection.commit()
            updated = connection.execute("SELECT * FROM slack_outbox WHERE id=?", (outbox_id,)).fetchone()
            return self._serialize_outbox(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_slack_outbox(
        self,
        outbox_id: str,
        *,
        scope_id: str,
        portfolio_id: str,
        expected_version: int,
        recipient_id: str,
        permalink: str,
        account_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        resolved_recipient = str(recipient_id or "").strip().upper()
        message_link = str(permalink or "").strip()
        parsed = urlsplit(message_link)
        if not re.fullmatch(r"[A-Z][A-Z0-9]{2,}", resolved_recipient):
            raise QueueValidationError("Slack MCP returned an invalid recipient ID.")
        if parsed.scheme != "https" or not (parsed.hostname or "").lower().endswith(".slack.com"):
            raise QueueValidationError("Slack MCP returned an invalid message permalink.")
        archive_match = re.search(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})", parsed.path)
        channel_id = archive_match.group(1) if archive_match else None
        message_ts = f"{archive_match.group(2)}.{archive_match.group(3)}" if archive_match else None
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_outbox(
                connection, outbox_id, scope_id=scope_id, portfolio_id=portfolio_id, account_ids=account_ids,
            )
            if not row:
                raise QueueValidationError("Unknown Slack outbox item.")
            if int(row["version"]) != int(expected_version):
                raise QueueConflict("The Slack outbox item changed; do not record delivery twice.")
            if str(row["status"]) not in {"sending", "needs_review"}:
                raise QueueValidationError(f"Cannot complete a {row['status']} Slack outbox item.")
            sent_at = _utc_now()
            connection.execute(
                """UPDATE slack_outbox SET status='sent', recipient_id=?, message_text='', sent_at=?, permalink=?,
                   error=NULL, version=version+1 WHERE id=?""",
                (resolved_recipient, sent_at, message_link, outbox_id),
            )
            connection.execute(
                """UPDATE workstreams SET slack_recipient_id=?, slack_channel_id=?, slack_message_ts=?,
                   slack_permalink=?, slack_sent_at=?, slack_sent_path_id=?, slack_outbox_id=?,
                   slack_message_sha256=?, version=version+1 WHERE fingerprint=?""",
                (
                    resolved_recipient, channel_id, message_ts, message_link, sent_at, row["path_id"],
                    outbox_id, row["message_sha256"], row["workstream_fingerprint"],
                ),
            )
            self._event(
                connection, workstream_id=str(row["workstream_fingerprint"]), instance_id=None,
                event_type="slack_mcp_sent", actor="glean_pi_slack_mcp",
                payload={
                    "outbox_id": outbox_id, "recipient": row["recipient_query"],
                    "recipient_id": resolved_recipient, "permalink": message_link,
                    "message_sha256": row["message_sha256"], "path_id": row["path_id"],
                    "delivery": "sent",
                },
            )
            connection.commit()
            updated = connection.execute("SELECT * FROM slack_outbox WHERE id=?", (outbox_id,)).fetchone()
            return self._serialize_outbox(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_slack_outbox_uncertain(
        self,
        outbox_id: str,
        *,
        scope_id: str,
        portfolio_id: str,
        expected_version: int,
        error: str,
        account_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        reason = str(error or "").strip()[:500]
        if not reason:
            raise QueueValidationError("Describe why Slack MCP delivery needs review.")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_outbox(
                connection, outbox_id, scope_id=scope_id, portfolio_id=portfolio_id, account_ids=account_ids,
            )
            if not row:
                raise QueueValidationError("Unknown Slack outbox item.")
            if int(row["version"]) != int(expected_version):
                raise QueueConflict("The Slack outbox item changed; list it again before updating.")
            if str(row["status"]) != "sending":
                raise QueueValidationError(f"Cannot flag a {row['status']} Slack outbox item for review.")
            connection.execute(
                "UPDATE slack_outbox SET status='needs_review', error=?, version=version+1 WHERE id=?",
                (reason, outbox_id),
            )
            connection.execute(
                "UPDATE workstreams SET version=version+1 WHERE fingerprint=?",
                (row["workstream_fingerprint"],),
            )
            self._event(
                connection, workstream_id=str(row["workstream_fingerprint"]), instance_id=None,
                event_type="slack_mcp_delivery_uncertain", actor="glean_pi_slack_mcp",
                payload={"outbox_id": outbox_id, "error": reason, "delivery": "needs_review"},
            )
            connection.commit()
            updated = connection.execute("SELECT * FROM slack_outbox WHERE id=?", (outbox_id,)).fetchone()
            return self._serialize_outbox(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def retry_slack_outbox(
        self,
        outbox_id: str,
        *,
        scope_id: str,
        portfolio_id: str,
        expected_version: int,
        confirmed_not_delivered: bool,
        account_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        if confirmed_not_delivered is not True:
            raise QueueValidationError("Confirm that Slack was checked and the message was not delivered before retrying.")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_outbox(
                connection, outbox_id, scope_id=scope_id, portfolio_id=portfolio_id, account_ids=account_ids,
            )
            if not row:
                raise QueueValidationError("Unknown Slack outbox item.")
            if int(row["version"]) != int(expected_version):
                raise QueueConflict("The Slack outbox item changed; list it again before retrying.")
            if str(row["status"]) != "needs_review":
                raise QueueValidationError(f"Cannot retry a {row['status']} Slack outbox item.")
            connection.execute(
                """UPDATE slack_outbox SET status='pending', claimed_at=NULL, error=NULL,
                   version=version+1 WHERE id=?""",
                (outbox_id,),
            )
            connection.execute(
                "UPDATE workstreams SET version=version+1 WHERE fingerprint=?",
                (row["workstream_fingerprint"],),
            )
            self._event(
                connection, workstream_id=str(row["workstream_fingerprint"]), instance_id=None,
                event_type="slack_mcp_retry_authorized", actor="glean_pi_slack_mcp",
                payload={"outbox_id": outbox_id, "delivery": "pending_retry"},
            )
            connection.commit()
            updated = connection.execute("SELECT * FROM slack_outbox WHERE id=?", (outbox_id,)).fetchone()
            return self._serialize_outbox(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def action(
        self,
        workstream_id: str,
        *,
        scope_id: str,
        action: str,
        expected_version: int,
        actor: str = "local_dashboard",
        payload: Optional[Mapping[str, Any]] = None,
        account_ids: Optional[Iterable[str]] = None,
        portfolio_id: str = "local-default",
    ) -> Dict[str, Any]:
        data = dict(payload or {})
        owned = sorted({str(value) for value in account_ids}) if account_ids is not None else None
        connection = self._connect()
        slack_recipient: Optional[str] = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_temporary_states(connection, scope_id=scope_id, portfolio_id=portfolio_id)
            query = "SELECT * FROM workstreams WHERE fingerprint=? AND scope_id=? AND portfolio_id=?"
            params: List[Any] = [workstream_id, scope_id, portfolio_id]
            if owned is not None:
                if owned:
                    query += (
                        " AND EXISTS (SELECT 1 FROM instances oi WHERE oi.workstream_fingerprint=workstreams.fingerprint "
                        "AND oi.account_id IN (" + ",".join("?" for _ in owned) + "))"
                    )
                    params.extend(owned)
                else:
                    query += " AND 1=0"
            row = connection.execute(query, params).fetchone()
            if not row:
                raise QueueValidationError("Unknown remediation workstream.")
            if int(row["version"]) != int(expected_version):
                raise QueueConflict("The remediation workstream changed; reload before applying this action.")

            current = str(row["status"])
            new_status = current
            updates: Dict[str, Any] = {}
            event_payload: Dict[str, Any] = {}
            if action == "acknowledge":
                if current not in {"open", "snoozed"}:
                    raise QueueValidationError(f"Cannot acknowledge a {current} workstream.")
                new_status = "acknowledged"
            elif action in {"start", "resume"}:
                if current not in {"open", "acknowledged", "snoozed", "pending_validation"}:
                    raise QueueValidationError(f"Cannot start a {current} workstream.")
                new_status = "in_progress"
                updates["snoozed_until"] = None
            elif action == "ready_for_validation":
                if current not in {"acknowledged", "in_progress"}:
                    raise QueueValidationError(f"Cannot validate a {current} workstream.")
                new_status = "pending_validation"
            elif action == "select_path":
                path_id = str(data.get("path_id") or "").strip()
                paths = _loads(row["paths_json"], [])
                selected = next((item for item in paths if str(item.get("id")) == path_id), None)
                if not selected:
                    raise QueueValidationError("Select a valid remediation path.")
                connection.execute(
                    """UPDATE slack_outbox SET status='cancelled', message_text='',
                       error='Superseded by a selected-path change', version=version+1
                       WHERE workstream_fingerprint=? AND status='pending'""",
                    (workstream_id,),
                )
                updates.update({
                    "selected_path_id": path_id,
                    "selected_target_tier": selected["target_tier"],
                    "selected_path_json": _json(selected),
                    "execution_plan_json": None,
                    "execution_path_id": None,
                    "execution_prepared_at": None,
                    "mcp_request_copied_at": None,
                    "slack_prepared_at": None,
                    "slack_copied_at": None,
                    "slack_recipient_id": None,
                    "slack_channel_id": None,
                    "slack_message_ts": None,
                    "slack_permalink": None,
                    "slack_sent_at": None,
                    "slack_sent_path_id": None,
                    "slack_outbox_id": None,
                    "slack_client_msg_id": None,
                    "slack_message_sha256": None,
                })
                event_payload = {"path_id": path_id, "target_tier": selected["target_tier"], "execution_plan_invalidated": True}
            elif action == "prepare_execution":
                plan = data.get("execution_plan")
                if not isinstance(plan, Mapping):
                    raise QueueValidationError("A server-generated execution plan is required.")
                selected_path = plan.get("selected_path")
                plan_path_id = str(selected_path.get("id") if isinstance(selected_path, Mapping) else "")
                if str(plan.get("workstream_id") or "") != workstream_id or plan_path_id != str(row["selected_path_id"]):
                    raise QueueValidationError("The execution plan does not match the selected workstream path.")
                if plan.get("source_write_performed") is not False:
                    raise QueueValidationError("Execution preparation cannot claim that a source write occurred.")
                serialized_plan = _json(plan)
                if len(serialized_plan.encode("utf-8")) > 500_000:
                    raise QueueValidationError("The execution plan is too large.")
                updates.update({
                    "execution_plan_json": serialized_plan,
                    "execution_path_id": plan_path_id,
                    "execution_prepared_at": _utc_now(),
                    "mcp_request_copied_at": None,
                    "slack_prepared_at": None,
                    "slack_copied_at": None,
                })
                event_payload = {
                    "execution_id": str(plan.get("execution_id") or ""),
                    "path_id": plan_path_id,
                    "execution_mode": str(plan.get("execution_mode") or ""),
                    "delivery": "prepared_not_executed",
                }
            elif action == "record_mcp_request_copy":
                if not row["execution_prepared_at"] or not row["execution_plan_json"]:
                    raise QueueValidationError("Prepare MCP next steps before recording a copy.")
                updates["mcp_request_copied_at"] = _utc_now()
                event_payload = {
                    "execution_path_id": row["execution_path_id"],
                    "delivery": "copied_not_executed",
                }
            elif action == "prepare_slack":
                slack_recipient = str(data.get("recipient") or "").strip()
                if not slack_recipient or len(slack_recipient) > 200:
                    raise QueueValidationError("A valid Slack recipient or team label is required.")
                updates.update({
                    "assignee": slack_recipient,
                    "slack_recipient": slack_recipient,
                    "slack_prepared_at": _utc_now(),
                    "slack_copied_at": None,
                })
                event_payload = {"recipient": slack_recipient, "delivery": "prepared_not_sent"}
            elif action == "record_slack_copy":
                if not row["slack_prepared_at"]:
                    raise QueueValidationError("Prepare a Slack follow-up before recording a copy.")
                updates["slack_copied_at"] = _utc_now()
                event_payload = {"recipient": row["slack_recipient"], "delivery": "copied_not_sent"}
            elif action == "snooze":
                if current not in ACTIVE_WORKSTREAM_STATUSES:
                    raise QueueValidationError(f"Cannot snooze a {current} workstream.")
                until = self._validate_future_date(data.get("until"), "snooze")
                new_status = "snoozed"
                updates["snoozed_until"] = until
                event_payload = {"until": until}
            elif action == "waive":
                if current not in ACTIVE_WORKSTREAM_STATUSES:
                    raise QueueValidationError(f"Cannot waive a {current} workstream.")
                reason = str(data.get("reason") or "").strip()
                approved_by = str(data.get("approved_by") or "").strip()
                expires_on = self._validate_future_date(data.get("expires_on"), "waiver expiration")
                if not reason or len(reason) > 1000 or not approved_by or len(approved_by) > 200:
                    raise QueueValidationError("Waivers require a reason, approver, and future expiration date.")
                new_status = "waived"
                updates.update({
                    "waiver_reason": reason,
                    "waiver_expires_on": expires_on,
                    "waiver_approved_by": approved_by,
                })
                event_payload = {"reason": reason, "expires_on": expires_on, "approved_by": approved_by}
            else:
                raise QueueValidationError("Unsupported remediation action.")

            assignments = ["status=?", "version=version+1"]
            values: List[Any] = [new_status]
            for column, value in updates.items():
                assignments.append(f"{column}=?")
                values.append(value)
            values.append(workstream_id)
            connection.execute(
                f"UPDATE workstreams SET {', '.join(assignments)} WHERE fingerprint=?", values,
            )
            self._event(
                connection, workstream_id=workstream_id, instance_id=None,
                event_type=f"action_{action}", actor=actor, payload=event_payload,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        workstream = self.get_workstream(
            workstream_id, scope_id=scope_id, portfolio_id=portfolio_id, account_ids=owned,
        )
        if not workstream:
            raise QueueError("The updated remediation workstream is unavailable.")
        result: Dict[str, Any] = {"workstream": workstream}
        if slack_recipient is not None:
            result["slack_message"] = format_slack_followup(workstream, slack_recipient)
            result["delivery"] = "prepared_not_sent"
        elif action == "record_slack_copy":
            result["delivery"] = "copied_not_sent"
        elif action == "prepare_execution":
            result["execution_workspace"] = workstream.get("execution_plan")
            result["execution"] = "prepared_not_executed"
        elif action == "record_mcp_request_copy":
            result["execution"] = "copied_not_executed"
        return result
