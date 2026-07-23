"""Private SQLite persistence for the Hours Recon remediation workflow."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .remediation import PRIORITY_RANK, build_candidates

SCHEMA_VERSION = 1
ACTIVE_GAP_STATUSES = {"open", "acknowledged", "in_progress", "pending_validation", "snoozed"}


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
            if version not in {0, SCHEMA_VERSION}:
                raise QueueError(f"Unsupported remediation database schema version {version}.")
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE queue_runs (
                        retrieval_id TEXT NOT NULL,
                        scope_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        report_as_of TEXT NOT NULL,
                        coverage_complete INTEGER NOT NULL,
                        report_digest TEXT,
                        candidate_count INTEGER NOT NULL,
                        PRIMARY KEY(scope_id, retrieval_id)
                    );

                    CREATE TABLE cases (
                        fingerprint TEXT PRIMARY KEY,
                        scope_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        account_name TEXT,
                        overall_tier TEXT,
                        status TEXT NOT NULL,
                        priority TEXT,
                        primary_route TEXT,
                        due_on TEXT,
                        active_gap_count INTEGER NOT NULL DEFAULT 0,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(scope_id, account_id)
                    );

                    CREATE TABLE gaps (
                        fingerprint TEXT PRIMARY KEY,
                        case_fingerprint TEXT NOT NULL REFERENCES cases(fingerprint) ON DELETE CASCADE,
                        dimension TEXT NOT NULL,
                        tier TEXT NOT NULL,
                        reason_code TEXT,
                        summary TEXT,
                        recommended_action TEXT,
                        priority TEXT NOT NULL,
                        route TEXT NOT NULL,
                        primary_owner TEXT,
                        required_partner TEXT,
                        assignee TEXT,
                        due_on TEXT,
                        status TEXT NOT NULL,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        last_retrieval_id TEXT NOT NULL,
                        evidence_hash TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        regression_count INTEGER NOT NULL DEFAULT 0,
                        waiver_reason TEXT,
                        waiver_expires_on TEXT,
                        waiver_approved_by TEXT,
                        snoozed_until TEXT,
                        version INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(case_fingerprint, dimension)
                    );

                    CREATE TABLE observations (
                        scope_id TEXT NOT NULL,
                        retrieval_id TEXT NOT NULL,
                        gap_fingerprint TEXT NOT NULL REFERENCES gaps(fingerprint) ON DELETE CASCADE,
                        observed_at TEXT NOT NULL,
                        evidence_hash TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        PRIMARY KEY(scope_id, retrieval_id, gap_fingerprint),
                        FOREIGN KEY(scope_id, retrieval_id) REFERENCES queue_runs(scope_id, retrieval_id) ON DELETE CASCADE
                    );

                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        gap_fingerprint TEXT REFERENCES gaps(fingerprint) ON DELETE CASCADE,
                        case_fingerprint TEXT NOT NULL REFERENCES cases(fingerprint) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );

                    CREATE INDEX idx_cases_scope_status ON cases(scope_id, status);
                    CREATE INDEX idx_gaps_case_status ON gaps(case_fingerprint, status);
                    CREATE INDEX idx_gaps_route_priority ON gaps(route, priority, status);
                    PRAGMA user_version = 1;
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
        case_id: str,
        gap_id: Optional[str],
        event_type: str,
        actor: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(gap_fingerprint, case_fingerprint, event_type, actor, created_at, payload_json) VALUES(?,?,?,?,?,?)",
            (gap_id, case_id, event_type, actor, _utc_now(), _json(dict(payload or {}))),
        )

    def observe(
        self,
        report: Mapping[str, Any],
        *,
        retrieval_id: str,
        scope_id: str,
        coverage_complete: bool,
        report_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not retrieval_id:
            raise QueueValidationError("A source retrieval ID is required.")
        coverage_complete = coverage_complete is True
        candidates = build_candidates(report, scope_id=scope_id)
        account_ids = {str(item.get("id")) for item in report.get("accounts", []) if item.get("id")}
        observed_at = _utc_now()
        report_as_of = str(report.get("meta", {}).get("as_of") or "")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_run = connection.execute(
                "SELECT retrieval_id FROM queue_runs WHERE scope_id = ? AND retrieval_id = ?", (scope_id, retrieval_id)
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
                "INSERT INTO queue_runs(retrieval_id, scope_id, observed_at, report_as_of, coverage_complete, report_digest, candidate_count) VALUES(?,?,?,?,?,?,?)",
                (retrieval_id, scope_id, observed_at, report_as_of, int(bool(coverage_complete)), report_digest, len(candidates)),
            )
            seen_gap_ids = set()
            touched_case_ids = set()

            for candidate in candidates:
                case_id = str(candidate["fingerprint"])
                touched_case_ids.add(case_id)
                existing_case = connection.execute(
                    "SELECT fingerprint FROM cases WHERE fingerprint = ?", (case_id,)
                ).fetchone()
                if existing_case:
                    connection.execute(
                        "UPDATE cases SET account_name=?, overall_tier=?, last_seen=?, version=version+1 WHERE fingerprint=?",
                        (candidate.get("account_name"), candidate.get("overall_tier"), observed_at, case_id),
                    )
                else:
                    connection.execute(
                        "INSERT INTO cases(fingerprint, scope_id, account_id, account_name, overall_tier, status, first_seen, last_seen) VALUES(?,?,?,?,?,'open',?,?)",
                        (case_id, scope_id, candidate["account_id"], candidate.get("account_name"), candidate.get("overall_tier"), observed_at, observed_at),
                    )
                    self._event(connection, case_id=case_id, gap_id=None, event_type="case_created", actor="system")

                for gap in candidate.get("gaps", []):
                    gap_id = str(gap["fingerprint"])
                    seen_gap_ids.add(gap_id)
                    existing_gap = connection.execute("SELECT * FROM gaps WHERE fingerprint = ?", (gap_id,)).fetchone()
                    status = "open"
                    regression_count = 0
                    waiver_reason = None
                    waiver_expires_on = None
                    snoozed_until = None
                    assignee = None
                    event_type = "gap_detected"
                    if existing_gap:
                        status = str(existing_gap["status"])
                        regression_count = int(existing_gap["regression_count"])
                        waiver_reason = existing_gap["waiver_reason"]
                        waiver_expires_on = existing_gap["waiver_expires_on"]
                        snoozed_until = existing_gap["snoozed_until"]
                        assignee = existing_gap["assignee"]
                        reopened = False
                        if status == "resolved":
                            if coverage_complete:
                                status = "open"
                                regression_count += 1
                                reopened = True
                                event_type = "gap_reopened"
                            else:
                                event_type = "incomplete_retrieval_preserved_resolution"
                        elif status == "pending_validation":
                            if coverage_complete:
                                status = "in_progress"
                                event_type = "validation_failed"
                            else:
                                event_type = "incomplete_retrieval_preserved_validation"
                        elif status == "waived":
                            event_type = "waiver_observed"
                        elif status == "snoozed":
                            event_type = "snooze_observed"
                        else:
                            event_type = "gap_updated"

                        existing_due = str(existing_gap["due_on"] or "")
                        candidate_due = str(gap.get("due_on") or "")
                        if reopened or not existing_due:
                            due_on = candidate_due or None
                        elif candidate_due:
                            due_on = min(existing_due, candidate_due)
                        else:
                            due_on = existing_due
                        connection.execute(
                            """UPDATE gaps SET tier=?, reason_code=?, summary=?, recommended_action=?, priority=?, route=?,
                               primary_owner=?, required_partner=?, assignee=?, due_on=?, status=?, last_seen=?,
                               last_retrieval_id=?, evidence_hash=?, evidence_json=?, regression_count=?, waiver_reason=?,
                               waiver_expires_on=?, snoozed_until=?, version=version+1 WHERE fingerprint=?""",
                            (
                                gap.get("tier"), gap.get("reason_code"), gap.get("summary"), gap.get("recommended_action"),
                                gap.get("priority"), gap.get("route"), gap.get("primary_owner"), gap.get("required_partner"),
                                assignee, due_on, status, observed_at, retrieval_id, gap.get("evidence_hash"),
                                _json(gap.get("evidence") or {}), regression_count, waiver_reason, waiver_expires_on,
                                snoozed_until, gap_id,
                            ),
                        )
                    else:
                        connection.execute(
                            """INSERT INTO gaps(fingerprint, case_fingerprint, dimension, tier, reason_code, summary,
                               recommended_action, priority, route, primary_owner, required_partner, due_on, status,
                               first_seen, last_seen, last_retrieval_id, evidence_hash, evidence_json)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                gap_id, case_id, gap.get("dimension"), gap.get("tier"), gap.get("reason_code"),
                                gap.get("summary"), gap.get("recommended_action"), gap.get("priority"), gap.get("route"),
                                gap.get("primary_owner"), gap.get("required_partner"), gap.get("due_on"), status,
                                observed_at, observed_at, retrieval_id, gap.get("evidence_hash"), _json(gap.get("evidence") or {}),
                            ),
                        )
                    connection.execute(
                        "INSERT INTO observations(scope_id, retrieval_id, gap_fingerprint, observed_at, evidence_hash, evidence_json) VALUES(?,?,?,?,?,?)",
                        (scope_id, retrieval_id, gap_id, observed_at, gap.get("evidence_hash"), _json(gap.get("evidence") or {})),
                    )
                    self._event(
                        connection,
                        case_id=case_id,
                        gap_id=gap_id,
                        event_type=event_type,
                        actor="system",
                        payload={"retrieval_id": retrieval_id, "tier": gap.get("tier"), "reason_code": gap.get("reason_code")},
                    )

            revalidation_performed = False
            if coverage_complete:
                revalidation_performed = True
                if account_ids:
                    placeholders = ",".join("?" for _ in account_ids)
                    rows = connection.execute(
                        f"""SELECT g.fingerprint, g.case_fingerprint, g.status FROM gaps g
                            JOIN cases c ON c.fingerprint = g.case_fingerprint
                            WHERE c.scope_id = ? AND c.account_id IN ({placeholders})""",
                        (scope_id, *sorted(account_ids)),
                    ).fetchall()
                    for row in rows:
                        gap_id = str(row["fingerprint"])
                        if gap_id in seen_gap_ids or row["status"] == "resolved":
                            continue
                        connection.execute(
                            "UPDATE gaps SET status='resolved', last_seen=?, version=version+1 WHERE fingerprint=?",
                            (observed_at, gap_id),
                        )
                        touched_case_ids.add(str(row["case_fingerprint"]))
                        self._event(
                            connection,
                            case_id=str(row["case_fingerprint"]),
                            gap_id=gap_id,
                            event_type="gap_resolved_by_revalidation",
                            actor="system",
                            payload={"retrieval_id": retrieval_id},
                        )

            for case_id in touched_case_ids:
                self._recompute_case(connection, case_id)
            connection.commit()
            os.chmod(self.path, 0o600)
            return {
                "new_source_observation": True,
                "revalidation_performed": revalidation_performed,
                "reason": "new_complete_retrieval" if coverage_complete else "new_incomplete_retrieval",
                "retrieval_id": retrieval_id,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recompute_case(self, connection: sqlite3.Connection, case_id: str) -> None:
        rows = connection.execute(
            "SELECT status, priority, route, due_on FROM gaps WHERE case_fingerprint = ?", (case_id,)
        ).fetchall()
        active = [row for row in rows if row["status"] in ACTIVE_GAP_STATUSES]
        waived = [row for row in rows if row["status"] == "waived"]
        if active:
            status_order = {"open": 0, "in_progress": 1, "acknowledged": 2, "pending_validation": 3, "snoozed": 4}
            status = min((str(row["status"]) for row in active), key=lambda value: status_order.get(value, 99))
            strongest = min(active, key=lambda row: (PRIORITY_RANK.get(str(row["priority"]), 99), str(row["route"])))
            due_values = sorted(str(row["due_on"]) for row in active if row["due_on"])
            priority = strongest["priority"]
            primary_route = strongest["route"]
            due_on = due_values[0] if due_values else None
        elif waived:
            status = "waived"
            priority = None
            primary_route = None
            due_on = None
        else:
            status = "resolved"
            priority = None
            primary_route = None
            due_on = None
        connection.execute(
            """UPDATE cases SET status=?, priority=?, primary_route=?, due_on=?, active_gap_count=?,
               version=version+1 WHERE fingerprint=?""",
            (status, priority, primary_route, due_on, len(active), case_id),
        )

    def _expire_temporary_states(self, connection: sqlite3.Connection, *, scope_id: str) -> None:
        today = date.today().isoformat()
        rows = connection.execute(
            """SELECT g.fingerprint, g.case_fingerprint, g.status FROM gaps g
               JOIN cases c ON c.fingerprint=g.case_fingerprint
               WHERE c.scope_id=? AND (
                    (g.status='waived' AND g.waiver_expires_on IS NOT NULL AND g.waiver_expires_on < ?)
                 OR (g.status='snoozed' AND g.snoozed_until IS NOT NULL AND g.snoozed_until < ?)
               )""",
            (scope_id, today, today),
        ).fetchall()
        touched = set()
        for row in rows:
            event_type = "waiver_expired" if row["status"] == "waived" else "snooze_expired"
            connection.execute(
                """UPDATE gaps SET status='open', waiver_reason=NULL, waiver_expires_on=NULL,
                   waiver_approved_by=NULL, snoozed_until=NULL, version=version+1 WHERE fingerprint=?""",
                (row["fingerprint"],),
            )
            touched.add(str(row["case_fingerprint"]))
            self._event(
                connection, case_id=str(row["case_fingerprint"]), gap_id=str(row["fingerprint"]),
                event_type=event_type, actor="system",
            )
        for case_id in touched:
            self._recompute_case(connection, case_id)

    @staticmethod
    def _row_to_gap(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json") or "{}")
        return result

    def list_cases(
        self,
        *,
        scope_id: str,
        status: Optional[str] = None,
        route: Optional[str] = None,
        priority: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["c.scope_id = ?"]
        values: List[Any] = [scope_id]
        if status:
            clauses.append("c.status = ?")
            values.append(status)
        if priority:
            clauses.append("c.priority = ?")
            values.append(priority)
        if account_id:
            clauses.append("c.account_id = ?")
            values.append(account_id)
        if route:
            clauses.append("EXISTS (SELECT 1 FROM gaps rg WHERE rg.case_fingerprint=c.fingerprint AND rg.route=? AND rg.status != 'resolved')")
            values.append(route)
        where = " WHERE " + " AND ".join(clauses)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_temporary_states(connection, scope_id=scope_id)
            connection.commit()
            case_rows = connection.execute(
                "SELECT c.* FROM cases c" + where +
                " ORDER BY CASE c.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 9 END, c.due_on, c.account_name",
                values,
            ).fetchall()
            result = []
            for case_row in case_rows:
                case = dict(case_row)
                gap_rows = connection.execute(
                    """SELECT * FROM gaps WHERE case_fingerprint=?
                       ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 9 END, dimension""",
                    (case["fingerprint"],),
                ).fetchall()
                case["gaps"] = [self._row_to_gap(row) for row in gap_rows]
                result.append(case)
            return result
        finally:
            connection.close()

    def get_case(self, fingerprint: str, *, scope_id: str) -> Optional[Dict[str, Any]]:
        case = next((item for item in self.list_cases(scope_id=scope_id) if item["fingerprint"] == fingerprint), None)
        if not case:
            return None
        connection = self._connect()
        try:
            event_rows = connection.execute(
                "SELECT event_type, actor, created_at, payload_json, gap_fingerprint FROM events WHERE case_fingerprint=? ORDER BY id DESC LIMIT 200",
                (fingerprint,),
            ).fetchall()
            case["events"] = [{**dict(row), "payload": json.loads(row["payload_json"] or "{}")} for row in event_rows]
            for event in case["events"]:
                event.pop("payload_json", None)
            return case
        finally:
            connection.close()

    def summary(self, *, scope_id: str) -> Dict[str, Any]:
        cases = self.list_cases(scope_id=scope_id)
        active_cases = [item for item in cases if item["status"] not in {"resolved", "waived"}]
        active_gaps = [gap for case in active_cases for gap in case["gaps"] if gap["status"] in ACTIVE_GAP_STATUSES]
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "observe_only",
            "scope_id": scope_id,
            "case_count": len(cases),
            "active_case_count": len(active_cases),
            "active_gap_count": len(active_gaps),
            "p0_gap_count": sum(1 for item in active_gaps if item["priority"] == "P0"),
            "p1_gap_count": sum(1 for item in active_gaps if item["priority"] == "P1"),
            "p2_gap_count": sum(1 for item in active_gaps if item["priority"] == "P2"),
            "cases": cases,
        }

    def latest_run(self, *, scope_id: str) -> Optional[Dict[str, Any]]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM queue_runs WHERE scope_id=? ORDER BY observed_at DESC LIMIT 1", (scope_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def health(self, *, scope_id: str) -> Dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("SELECT 1").fetchone()
            return {
                "available": True,
                "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "latest_run": self.latest_run(scope_id=scope_id),
            }
        finally:
            connection.close()

    def action(
        self,
        gap_id: str,
        *,
        scope_id: str,
        action: str,
        expected_version: int,
        actor: str = "local_dashboard",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        data = dict(payload or {})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_temporary_states(connection, scope_id=scope_id)
            row = connection.execute(
                """SELECT g.* FROM gaps g JOIN cases c ON c.fingerprint=g.case_fingerprint
                   WHERE g.fingerprint=? AND c.scope_id=?""",
                (gap_id, scope_id),
            ).fetchone()
            if not row:
                raise QueueValidationError("Unknown remediation gap.")
            if int(row["version"]) != int(expected_version):
                raise QueueConflict("The remediation gap changed; reload before applying this action.")
            current = str(row["status"])
            new_status = current
            updates: Dict[str, Any] = {}
            event_payload: Dict[str, Any] = {}
            if action == "acknowledge":
                if current not in {"open", "snoozed"}:
                    raise QueueValidationError(f"Cannot acknowledge a {current} gap.")
                new_status = "acknowledged"
            elif action == "start":
                if current not in {"open", "acknowledged", "snoozed", "pending_validation"}:
                    raise QueueValidationError(f"Cannot start a {current} gap.")
                new_status = "in_progress"
            elif action == "ready_for_validation":
                if current not in {"acknowledged", "in_progress"}:
                    raise QueueValidationError(f"Cannot validate a {current} gap.")
                new_status = "pending_validation"
            elif action == "assign":
                if current not in ACTIVE_GAP_STATUSES:
                    raise QueueValidationError(f"Cannot assign a {current} gap.")
                assignee = str(data.get("assignee") or "").strip()
                if not assignee or len(assignee) > 200:
                    raise QueueValidationError("A valid assignee is required.")
                updates["assignee"] = assignee
                event_payload = {"assignee": assignee}
            elif action == "snooze":
                if current not in ACTIVE_GAP_STATUSES:
                    raise QueueValidationError(f"Cannot snooze a {current} gap.")
                until = str(data.get("until") or "")
                try:
                    parsed = date.fromisoformat(until)
                except ValueError as exc:
                    raise QueueValidationError("A valid snooze date is required.") from exc
                if parsed <= date.today():
                    raise QueueValidationError("Snooze date must be in the future.")
                new_status = "snoozed"
                updates["snoozed_until"] = until
                event_payload = {"until": until}
            elif action == "waive":
                if current not in ACTIVE_GAP_STATUSES:
                    raise QueueValidationError(f"Cannot waive a {current} gap.")
                reason = str(data.get("reason") or "").strip()
                approved_by = str(data.get("approved_by") or "").strip()
                expires_on = str(data.get("expires_on") or "")
                try:
                    parsed = date.fromisoformat(expires_on)
                except ValueError as exc:
                    raise QueueValidationError("A valid waiver expiration date is required.") from exc
                if not reason or len(reason) > 1000 or not approved_by or len(approved_by) > 200 or parsed <= date.today():
                    raise QueueValidationError("Waivers require a reason, approver, and future expiration date.")
                new_status = "waived"
                updates["waiver_reason"] = reason
                updates["waiver_expires_on"] = expires_on
                updates["waiver_approved_by"] = approved_by
                event_payload = {"reason": reason, "expires_on": expires_on, "approved_by": approved_by}
            else:
                raise QueueValidationError("Unsupported remediation action.")

            assignments = ["status=?", "version=version+1"]
            values: List[Any] = [new_status]
            for column, value in updates.items():
                assignments.append(f"{column}=?")
                values.append(value)
            values.append(gap_id)
            connection.execute(f"UPDATE gaps SET {', '.join(assignments)} WHERE fingerprint=?", values)
            self._event(
                connection,
                case_id=str(row["case_fingerprint"]),
                gap_id=gap_id,
                event_type=f"action_{action}",
                actor=actor,
                payload=event_payload,
            )
            self._recompute_case(connection, str(row["case_fingerprint"]))
            connection.commit()
            updated = connection.execute("SELECT * FROM gaps WHERE fingerprint=?", (gap_id,)).fetchone()
            return self._row_to_gap(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
