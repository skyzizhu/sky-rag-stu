"""Operational audit for employee uploads. Retrieval and the index ledger do not read it."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from src import auth_db

_LOG = logging.getLogger(__name__)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def source_url_for_log(url: str) -> str:
    """Keep the source address while omitting credentials, query strings and fragments."""
    try:
        address = urlsplit(url)
        if address.scheme not in ("http", "https") or not address.hostname:
            return ""
        host = address.hostname
        if ":" in host:
            host = f"[{host}]"
        if address.port is not None:
            host += f":{address.port}"
        return urlunsplit((address.scheme, host, address.path, "", ""))
    except ValueError:
        return ""


@dataclass
class IngestItem:
    file_name: str
    source_path: str = ""
    path: str = ""
    bytes: int | None = None
    outcome: str = "running"
    reason: str = ""
    attributes: dict = field(default_factory=dict)


class IngestAudit:
    """Keep one row per input file, grouped by a server-generated batch ID."""

    def __init__(self, entry: str, items: list[IngestItem], *, username: str,
                 actor_department: str, ip: str, knowledge_dir: Path, requested: dict,
                 user_agent: str = "", accept_language: str = "", peer_ip: str = ""):
        self.entry = entry
        self.items = items
        self.username = username
        self.actor_department = actor_department
        self.ip = ""
        for value in (ip, peer_ip):
            try:
                self.ip = str(ip_address(value))
                break
            except ValueError:
                continue
        self.user_agent = user_agent
        self.browser, self.os_name = auth_db.parse_user_agent(user_agent)
        self.fingerprint = auth_db.make_fingerprint(user_agent, accept_language, self.ip) if user_agent else ""
        self.knowledge_dir = knowledge_dir.resolve()
        self.requested = requested
        self.batch_id = uuid4().hex
        self.started_at = self.finished_at = None
        self.elapsed_ms = None
        self.stage = "validation"

    def __enter__(self):
        self.started_at = _utc(datetime.now(timezone.utc))
        self._started = time.monotonic()
        self._persist()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.fail(self.stage)
        self.finished_at = _utc(datetime.now(timezone.utc))
        self.elapsed_ms = round((time.monotonic() - self._started) * 1000)
        self._persist()

    def saved(self, index: int, target: Path) -> None:
        try:
            path = target.resolve().relative_to(self.knowledge_dir).as_posix()
        except (OSError, ValueError):
            return
        item = self.items[index]
        item.path = path
        if not item.file_name:
            item.file_name = target.name
        if item.bytes is None:
            try:
                item.bytes = target.stat().st_size
            except OSError:
                pass

    def skip(self, index: int, reason: str) -> None:
        item = self.items[index]
        item.outcome, item.reason = "skipped", reason

    def fail(self, reason: str) -> None:
        for item in self.items:
            if item.outcome == "running":
                item.outcome, item.reason = "failed", reason

    def finish(self, summary) -> None:
        rows = {row["path"]: row for row in summary.file_rows}
        unchanged = set(summary.skipped_files)
        updated = set(summary.updated_files)
        for item in self.items:
            if item.outcome != "running" or not item.path:
                continue
            if row := rows.get(item.path):
                item.attributes = row
                if row["chunks"]:
                    item.outcome = "ingested"
                    item.reason = "new_revision" if item.path in updated else "indexed"
                else:
                    item.outcome, item.reason = "skipped", "no_text"
            elif item.path in unchanged:
                item.outcome, item.reason = "skipped", "unchanged"
            elif reason := summary.file_errors.get(item.path):
                item.outcome, item.reason = "failed", reason

    def _persist(self) -> None:
        if not self.items:
            return
        try:
            with closing(auth_db._connect(timeout=1)) as conn, conn:
                # The Web process can be updated before the admin service has restarted.
                auth_db.ensure_ingest_log_schema(conn)
                snapshots: dict[int, dict] = {}

                def details(values):
                    project_id = values.get("project_id")
                    if type(project_id) is int and 0 < project_id < 2**63:
                        if project_id not in snapshots:
                            project = conn.execute(
                                "SELECT p.name AS project_name, d.name AS business_direction "
                                "FROM projects p LEFT JOIN business_directions d ON d.id=p.direction_id "
                                "WHERE p.id=?", (project_id,),
                            ).fetchone()
                            snapshots[project_id] = dict(project) if project else {}
                        values = {**values, **snapshots[project_id]}
                    return json.dumps(values, ensure_ascii=False, default=str)

                requested = details(self.requested)
                conn.executemany(
                    """INSERT INTO ingest_logs(batch_id,file_index,username,actor_department,ip,
                           user_agent,browser,os_name,fingerprint,entry,
                           file_name,source_path,path,bytes,started_at,finished_at,elapsed_ms,outcome,
                           reason,requested,attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(batch_id,file_index) DO UPDATE SET file_name=excluded.file_name,
                           path=excluded.path,bytes=excluded.bytes,finished_at=excluded.finished_at,
                           elapsed_ms=excluded.elapsed_ms,
                           outcome=excluded.outcome,reason=excluded.reason,attributes=excluded.attributes""",
                    ((self.batch_id, i, self.username, self.actor_department, self.ip,
                      self.user_agent, self.browser, self.os_name, self.fingerprint, self.entry,
                      item.file_name, item.source_path, item.path, item.bytes, self.started_at,
                      self.finished_at, self.elapsed_ms, item.outcome, item.reason, requested,
                      details(item.attributes)) for i, item in enumerate(self.items)),
                )
        except (OSError, sqlite3.Error):
            _LOG.exception("Unable to save ingestion audit batch %s", self.batch_id)


def list_logs(*, page: int, page_size: int, username: str = "", entry: str = "", outcome: str = "",
              keyword: str = "", batch_id: str = "", since: datetime | None = None,
              before: datetime | None = None) -> dict:
    conditions, params = [], []
    for column, value in (("username", username), ("entry", entry), ("outcome", outcome), ("batch_id", batch_id)):
        if value:
            conditions.append(f"{column} = ?")
            params.append(value)
    if keyword:
        conditions.append("(instr(lower(file_name), lower(?)) OR instr(lower(path), lower(?)) "
                          "OR instr(lower(source_path), lower(?)))")
        params.extend([keyword] * 3)
    if since:
        conditions.append("started_at >= ?")
        params.append(_utc(since))
    if before:
        conditions.append("started_at < ?")
        params.append(_utc(before))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with closing(auth_db._connect()) as conn, conn:
        conn.execute("BEGIN")
        total = conn.execute("SELECT count(*) FROM ingest_logs" + where, params).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = conn.execute("SELECT * FROM ingest_logs" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                            [*params, page_size, (page - 1) * page_size]).fetchall()
        employees = conn.execute("""
            SELECT accounts.username, COALESCE(users.real_name, '') AS real_name
            FROM (SELECT username FROM users
                  UNION SELECT username FROM ingest_logs WHERE username <> '') AS accounts
            LEFT JOIN users ON users.username = accounts.username
            ORDER BY accounts.username COLLATE NOCASE, accounts.username
        """).fetchall()
    logs = [{**row, "requested": json.loads(row["requested"]), "attributes": json.loads(row["attributes"])}
            for row in rows]
    return {"ok": True, "logs": logs, "employee_options": [dict(row) for row in employees], "total": total, "page": page,
            "page_size": page_size, "total_pages": total_pages}


def get_log_path(log_id: int) -> str | None:
    with closing(auth_db._connect()) as conn:
        row = conn.execute("SELECT path FROM ingest_logs WHERE id=?", (log_id,)).fetchone()
    return row["path"] if row else None
