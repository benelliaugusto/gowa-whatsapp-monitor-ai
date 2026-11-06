"""Persistência em SQLite (WAL).

O monitor é a fonte de verdade do histórico do WhatsApp que o GOWA enxergou.
SQLite em arquivo no volume /data evita mais um serviço no Easypanel e aguenta
com folga o volume de uma clínica. Todas as consultas ficam isoladas aqui.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator

from .config import settings
from .timeutil import iso, local_date_of, parse_ts, today_local, utc_now, utc_now_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    chat_jid    TEXT PRIMARY KEY,
    phone       TEXT,
    name        TEXT,
    is_group    INTEGER NOT NULL DEFAULT 0,
    device_id   TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uid     TEXT NOT NULL UNIQUE,
    chat_jid    TEXT NOT NULL,
    sender_jid  TEXT,
    sender_name TEXT,
    direction   TEXT NOT NULL,
    msg_type    TEXT,
    body        TEXT,
    media_type  TEXT,
    source      TEXT,
    device_id   TEXT,
    sent_at     TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    local_hour  INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages (chat_jid, local_date);
CREATE INDEX IF NOT EXISTS idx_messages_chat_sent ON messages (chat_jid, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages (local_date);

CREATE TABLE IF NOT EXISTS analyses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_jid       TEXT NOT NULL,
    analysis_date  TEXT NOT NULL,
    classification TEXT,
    sentiment      TEXT,
    intent         TEXT,
    summary        TEXT,
    next_step      TEXT,
    risk_flags     TEXT,
    score          INTEGER,
    extra          TEXT,
    agent          TEXT,
    model          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (chat_jid, analysis_date)
);
CREATE INDEX IF NOT EXISTS idx_analyses_date ON analyses (analysis_date);

CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'daily',
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT,
    body_md         TEXT NOT NULL,
    metrics         TEXT,
    highlights      TEXT,
    action_items    TEXT,
    status          TEXT NOT NULL DEFAULT 'published',
    agent           TEXT,
    model           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_period ON reports (period_start, period_end);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger           TEXT,
    scope             TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    chats_scanned     INTEGER NOT NULL DEFAULT 0,
    messages_fetched  INTEGER NOT NULL DEFAULT 0,
    messages_inserted INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 1,
    detail            TEXT
);

CREATE TABLE IF NOT EXISTS api_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    method    TEXT,
    path      TEXT,
    key_name  TEXT,
    status    INTEGER,
    client_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls (ts);
"""


# ---------------------------------------------------------------------------
# conexão
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(settings.database_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(EXTRA_SCHEMA)
        _ensure_columns(conn)
    log.info("banco inicializado em %s", settings.database_path)


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# contatos
# ---------------------------------------------------------------------------
def upsert_contact(
    chat_jid: str,
    *,
    name: str | None = None,
    phone: str | None = None,
    is_group: bool | None = None,
    device_id: str | None = None,
) -> None:
    if not chat_jid:
        return
    group = int(bool(is_group)) if is_group is not None else int(chat_jid.endswith("@g.us"))
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO contacts (chat_jid, phone, name, is_group, device_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_jid) DO UPDATE SET
                phone     = COALESCE(excluded.phone, contacts.phone),
                name      = COALESCE(NULLIF(excluded.name, ''), contacts.name),
                is_group  = excluded.is_group,
                device_id = COALESCE(excluded.device_id, contacts.device_id),
                updated_at = excluded.updated_at
            """,
            (chat_jid, phone, (name or "").strip() or None, group, device_id, utc_now_iso()),
        )


def get_contact(chat_jid: str) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM contacts WHERE chat_jid = ?", (chat_jid,)).fetchone())


# ---------------------------------------------------------------------------
# mensagens
# ---------------------------------------------------------------------------
def make_msg_uid(chat_jid: str, sender_jid: str, sent_at: str, body: str) -> str:
    raw = "|".join([chat_jid or "", sender_jid or "", sent_at or "", body or ""])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def insert_messages(records: Iterable[dict]) -> int:
    """Insere em lote ignorando duplicatas. Devolve quantas mensagens entraram."""
    rows: list[tuple] = []
    now = utc_now_iso()

    for rec in records:
        chat_jid = rec.get("chat_jid")
        if not chat_jid:
            continue
        sent = parse_ts(rec.get("sent_at")) or utc_now()
        sent_iso = iso(sent)
        local = sent.astimezone(settings.tz)
        body = rec.get("body") or ""
        uid = rec.get("msg_uid") or make_msg_uid(chat_jid, rec.get("sender_jid") or "", sent_iso, body)
        midia = (rec.get("media_type") or "").lower()
        if midia in {"audio", "ptt"}:
            status_transcricao = "pending" if settings.transcribe_enabled else "skipped"
        else:
            status_transcricao = "none"
        rows.append(
            (
                uid,
                chat_jid,
                rec.get("sender_jid") or "",
                rec.get("sender_name") or "",
                "out" if rec.get("direction") == "out" else "in",
                rec.get("msg_type") or "text",
                body,
                rec.get("media_type") or "",
                rec.get("source") or "webhook",
                rec.get("device_id") or "",
                sent_iso,
                local.strftime("%Y-%m-%d"),
                local.hour,
                status_transcricao,
                now,
            )
        )

    if not rows:
        return 0

    with transaction() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO messages
                (msg_uid, chat_jid, sender_jid, sender_name, direction, msg_type,
                 body, media_type, source, device_id, sent_at, local_date, local_hour,
                 transcription_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return conn.total_changes - before


def has_messages(chat_jid: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM messages WHERE chat_jid = ? LIMIT 1", (chat_jid,)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# consultas de conversas
# ---------------------------------------------------------------------------
_CONVERSATION_SELECT = """
SELECT m.chat_jid                         AS chat_jid,
       COALESCE(c.name, '')               AS name,
       COALESCE(c.phone, '')              AS phone,
       COALESCE(c.is_group, CASE WHEN m.chat_jid LIKE '%@g.us' THEN 1 ELSE 0 END) AS is_group,
       COUNT(*)                           AS message_count,
       SUM(CASE WHEN m.direction = 'in'  THEN 1 ELSE 0 END) AS inbound,
       SUM(CASE WHEN m.direction = 'out' THEN 1 ELSE 0 END) AS outbound,
       MIN(m.sent_at)                     AS first_message_at,
       MAX(m.sent_at)                     AS last_message_at,
       (SELECT x.body FROM messages x
         WHERE x.chat_jid = m.chat_jid AND x.local_date = :dia
         ORDER BY x.sent_at DESC LIMIT 1)  AS last_preview,
       a.classification                   AS classification,
       a.sentiment                        AS sentiment,
       a.score                            AS score,
       a.summary                          AS analysis_summary
  FROM messages m
  LEFT JOIN contacts c ON c.chat_jid = m.chat_jid
  LEFT JOIN analyses a ON a.chat_jid = m.chat_jid AND a.analysis_date = :dia
 WHERE m.local_date = :dia {group_filter}
 GROUP BY m.chat_jid
 ORDER BY last_message_at DESC
 LIMIT :limit
"""


def conversations_for_date(date_str: str, *, include_groups: bool = False, limit: int = 500) -> list[dict]:
    sql = _CONVERSATION_SELECT.format(group_filter="" if include_groups else "AND m.chat_jid NOT LIKE '%@g.us'")
    with connect() as conn:
        return _rows(conn.execute(sql, {"dia": date_str, "limit": limit}).fetchall())


def conversations_in_range(
    start_iso: str,
    end_iso: str,
    *,
    include_groups: bool = False,
    limit: int = 500,
    analysis_date: str | None = None,
) -> list[dict]:
    sql = """
    SELECT m.chat_jid AS chat_jid,
           COALESCE(c.name, '')  AS name,
           COALESCE(c.phone, '') AS phone,
           COALESCE(c.is_group, CASE WHEN m.chat_jid LIKE '%@g.us' THEN 1 ELSE 0 END) AS is_group,
           COUNT(*) AS message_count,
           SUM(CASE WHEN m.direction = 'in'  THEN 1 ELSE 0 END) AS inbound,
           SUM(CASE WHEN m.direction = 'out' THEN 1 ELSE 0 END) AS outbound,
           MIN(m.sent_at) AS first_message_at,
           MAX(m.sent_at) AS last_message_at,
           (SELECT x.body FROM messages x WHERE x.chat_jid = m.chat_jid
             AND x.sent_at >= :start AND x.sent_at < :end
             ORDER BY x.sent_at DESC LIMIT 1) AS last_preview,
           a.classification AS classification,
           a.sentiment AS sentiment,
           a.score AS score,
           a.summary AS analysis_summary
      FROM messages m
      LEFT JOIN contacts c ON c.chat_jid = m.chat_jid
      LEFT JOIN analyses a ON a.chat_jid = m.chat_jid AND a.analysis_date = :dia
     WHERE m.sent_at >= :start AND m.sent_at < :end {group_filter}
     GROUP BY m.chat_jid
     ORDER BY last_message_at DESC
     LIMIT :limit
    """
    params = {
        "start": start_iso,
        "end": end_iso,
        "dia": analysis_date or "",
        "limit": limit,
    }
    sql = sql.format(group_filter="" if include_groups else "AND m.chat_jid NOT LIKE '%@g.us'")
    with connect() as conn:
        return _rows(conn.execute(sql, params).fetchall())


def messages_for_chat(
    chat_jid: str,
    *,
    date_str: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
    limit: int = 1000,
    newest_first: bool = False,
) -> list[dict]:
    if date_str:
        where, params = "chat_jid = :jid AND local_date = :dia", {"jid": chat_jid, "dia": date_str}
    else:
        where = "chat_jid = :jid"
        params = {"jid": chat_jid}
        if start_iso:
            where += " AND sent_at >= :start"
            params["start"] = start_iso
        if end_iso:
            where += " AND sent_at < :end"
            params["end"] = end_iso
    order = "DESC" if newest_first else "ASC"
    params["limit"] = limit
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY sent_at {order} LIMIT :limit", params
        ).fetchall()
    data = _rows(rows)
    if newest_first:
        data.reverse()
    return data


def daily_stats(date_str: str) -> dict:
    with connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS messages,
                   COALESCE(SUM(direction = 'in'), 0)  AS inbound,
                   COALESCE(SUM(direction = 'out'), 0) AS outbound,
                   COUNT(DISTINCT chat_jid) AS conversations
              FROM messages WHERE local_date = ?
            """,
            (date_str,),
        ).fetchone()
        new_conversations = conn.execute(
            "SELECT COUNT(*) FROM (SELECT chat_jid, MIN(local_date) AS primeira FROM messages GROUP BY chat_jid) WHERE primeira = ?",
            (date_str,),
        ).fetchone()[0]
        hourly = [0] * 24
        for row in conn.execute(
            "SELECT local_hour AS h, COUNT(*) AS c FROM messages WHERE local_date = ? GROUP BY local_hour",
            (date_str,),
        ).fetchall():
            if row["h"] is not None and 0 <= row["h"] < 24:
                hourly[row["h"]] = row["c"]
        top = _rows(
            conn.execute(
                """SELECT m.chat_jid AS chat_jid, COALESCE(c.name, '') AS name, COUNT(*) AS messages
                     FROM messages m LEFT JOIN contacts c ON c.chat_jid = m.chat_jid
                    WHERE m.local_date = ?
                    GROUP BY m.chat_jid ORDER BY messages DESC LIMIT 5""",
                (date_str,),
            ).fetchall()
        )
        classifications = _rows(
            conn.execute(
                """SELECT COALESCE(NULLIF(classification, ''), 'sem_classificacao') AS classification,
                          COUNT(*) AS total
                     FROM analyses WHERE analysis_date = ?
                    GROUP BY classification ORDER BY total DESC""",
                (date_str,),
            ).fetchall()
        )
        reports_today = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE period_start <= ? AND period_end >= ?",
            (date_str, date_str),
        ).fetchone()[0]

    return {
        "date": date_str,
        "messages": totals["messages"] if totals else 0,
        "inbound": totals["inbound"] if totals else 0,
        "outbound": totals["outbound"] if totals else 0,
        "conversations": totals["conversations"] if totals else 0,
        "new_conversations": new_conversations,
        "reports": reports_today,
        "hourly": hourly,
        "top_conversations": top,
        "classifications": classifications,
    }


# ---------------------------------------------------------------------------
# análises de conversa (escritas pelos agentes de IA)
# ---------------------------------------------------------------------------
def upsert_analysis(data: dict) -> dict:
    now = utc_now_iso()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO analyses
                (chat_jid, analysis_date, classification, sentiment, intent, summary,
                 next_step, risk_flags, score, extra, agent, model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_jid, analysis_date) DO UPDATE SET
                classification = excluded.classification,
                sentiment      = excluded.sentiment,
                intent         = excluded.intent,
                summary        = excluded.summary,
                next_step      = excluded.next_step,
                risk_flags     = excluded.risk_flags,
                score          = excluded.score,
                extra          = excluded.extra,
                agent          = excluded.agent,
                model          = excluded.model,
                updated_at     = excluded.updated_at
            """,
            (
                data["chat_jid"],
                data["analysis_date"],
                data.get("classification"),
                data.get("sentiment"),
                data.get("intent"),
                data.get("summary"),
                data.get("next_step"),
                json.dumps(data.get("risk_flags") or [], ensure_ascii=False),
                data.get("score"),
                json.dumps(data.get("extra") or {}, ensure_ascii=False),
                data.get("agent"),
                data.get("model"),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM analyses WHERE chat_jid = ? AND analysis_date = ?",
            (data["chat_jid"], data["analysis_date"]),
        ).fetchone()
    return _decode_analysis(_row(row)) or {}


def _decode_analysis(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    try:
        out["risk_flags"] = json.loads(out.get("risk_flags") or "[]")
    except (TypeError, ValueError):
        out["risk_flags"] = []
    try:
        out["extra"] = json.loads(out.get("extra") or "{}")
    except (TypeError, ValueError):
        out["extra"] = {}
    return out


def get_analysis(chat_jid: str, date_str: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE chat_jid = ? AND analysis_date = ?", (chat_jid, date_str)
        ).fetchone()
    return _decode_analysis(_row(row))


def list_analyses(
    *, date_str: str | None = None, chat_jid: str | None = None, limit: int = 200
) -> list[dict]:
    where, params = ["1 = 1"], {}
    if date_str:
        where.append("analysis_date = :dia")
        params["dia"] = date_str
    if chat_jid:
        where.append("chat_jid = :jid")
        params["jid"] = chat_jid
    params["limit"] = limit
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM analyses WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT :limit",
            params,
        ).fetchall()
    return [_decode_analysis(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# relatórios (escritos pelos agentes de IA)
# ---------------------------------------------------------------------------
def _decode_report(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for field in ("metrics", "highlights", "action_items"):
        default = {} if field == "metrics" else []
        try:
            out[field] = json.loads(out.get(field) or json.dumps(default))
        except (TypeError, ValueError):
            out[field] = default
    return out


def create_report(data: dict) -> tuple[dict, bool]:
    """Cria relatório. Se idempotency_key já existir, devolve o existente (criado=False)."""
    now = utc_now_iso()
    with transaction() as conn:
        key = data.get("idempotency_key")
        if key:
            existing = conn.execute("SELECT * FROM reports WHERE idempotency_key = ?", (key,)).fetchone()
            if existing is not None:
                return _decode_report(dict(existing)) or {}, False
        cursor = conn.execute(
            """
            INSERT INTO reports
                (idempotency_key, kind, period_start, period_end, title, summary, body_md,
                 metrics, highlights, action_items, status, agent, model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                data.get("kind") or "daily",
                data["period_start"],
                data.get("period_end") or data["period_start"],
                data["title"],
                data.get("summary"),
                data["body_md"],
                json.dumps(data.get("metrics") or {}, ensure_ascii=False),
                json.dumps(data.get("highlights") or [], ensure_ascii=False),
                json.dumps(data.get("action_items") or [], ensure_ascii=False),
                data.get("status") or "published",
                data.get("agent"),
                data.get("model"),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _decode_report(dict(row)) or {}, True


def update_report(report_id: int, fields: dict) -> dict | None:
    allowed = {"kind", "period_start", "period_end", "title", "summary", "body_md", "status", "agent", "model",
               "metrics", "highlights", "action_items", "idempotency_key"}
    sets, params = [], {}
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key in {"metrics", "highlights", "action_items"}:
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key} = :{key}")
        params[key] = value
    if not sets:
        return get_report(report_id)
    sets.append("updated_at = :updated_at")
    params["updated_at"] = utc_now_iso()
    params["id"] = report_id
    with transaction() as conn:
        conn.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id = :id", params)
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _decode_report(_row(row))


def get_report(report_id: int) -> dict | None:
    with connect() as conn:
        return _decode_report(_row(conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()))


def latest_report(kind: str | None = None) -> dict | None:
    sql, params = "SELECT * FROM reports", {}
    if kind:
        sql += " WHERE kind = :kind"
        params["kind"] = kind
    sql += " ORDER BY period_end DESC, id DESC LIMIT 1"
    with connect() as conn:
        return _decode_report(_row(conn.execute(sql, params).fetchone()))


def list_reports(
    *,
    limit: int = 50,
    offset: int = 0,
    kind: str | None = None,
    status: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[list[dict], int]:
    where, params = ["1 = 1"], {}
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind
    if status:
        where.append("status = :status")
        params["status"] = status
    if start:
        where.append("period_end >= :start")
        params["start"] = start
    if end:
        where.append("period_start <= :end")
        params["end"] = end
    clause = " AND ".join(where)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM reports WHERE {clause}", params).fetchone()[0]
        params.update({"limit": limit, "offset": offset})
        rows = conn.execute(
            f"SELECT * FROM reports WHERE {clause} ORDER BY period_end DESC, id DESC LIMIT :limit OFFSET :offset",
            params,
        ).fetchall()
    return [_decode_report(dict(r)) or {} for r in rows], total


# ---------------------------------------------------------------------------
# sincronizações
# ---------------------------------------------------------------------------
def start_sync_run(trigger: str, scope: str) -> int:
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO sync_runs (trigger, scope, started_at) VALUES (?, ?, ?)",
            (trigger, scope, utc_now_iso()),
        )
    return int(cursor.lastrowid)


def finish_sync_run(run_id: int, stats: dict, ok: bool = True, detail: str = "") -> None:
    with transaction() as conn:
        conn.execute(
            """UPDATE sync_runs SET finished_at = ?, chats_scanned = ?, messages_fetched = ?,
                      messages_inserted = ?, ok = ?, detail = ? WHERE id = ?""",
            (
                utc_now_iso(),
                int(stats.get("chats_scanned") or 0),
                int(stats.get("messages_fetched") or 0),
                int(stats.get("messages_inserted") or 0),
                1 if ok else 0,
                (detail or "")[:2000],
                run_id,
            ),
        )


def recent_sync_runs(limit: int = 10) -> list[dict]:
    with connect() as conn:
        return _rows(conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall())


def last_sync_run() -> dict | None:
    runs = recent_sync_runs(1)
    return runs[0] if runs else None


# ---------------------------------------------------------------------------
# auditoria e resumo
# ---------------------------------------------------------------------------
def log_api_call(method: str, path: str, key_name: str, status_code: int, client_ip: str = "") -> None:
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO api_calls (ts, method, path, key_name, status, client_ip) VALUES (?, ?, ?, ?, ?, ?)",
                (utc_now_iso(), method, path, key_name or "", status_code, client_ip or ""),
            )
    except sqlite3.Error as exc:  # nunca derruba a requisição por causa de auditoria
        log.warning("falha ao auditar chamada de API: %s", exc)


def database_summary() -> dict:
    with connect() as conn:
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conversations = conn.execute("SELECT COUNT(DISTINCT chat_jid) FROM messages").fetchone()[0]
        analyses = conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
        reports = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        first = conn.execute("SELECT MIN(sent_at) FROM messages").fetchone()[0]
        last = conn.execute("SELECT MAX(sent_at) FROM messages").fetchone()[0]
    return {
        "messages": messages,
        "conversations": conversations,
        "analyses": analyses,
        "reports": reports,
        "first_message_at": first,
        "last_message_at": last,
        "database_path": settings.database_path,
    }

# ---------------------------------------------------------------------------
# tarefas (ações geradas pelos agentes/relatórios)
# ---------------------------------------------------------------------------
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_jid    TEXT,
    title       TEXT NOT NULL,
    details     TEXT,
    owner       TEXT,
    due_at      TEXT,
    priority    INTEGER NOT NULL DEFAULT 2,
    status      TEXT NOT NULL DEFAULT 'pending',
    source      TEXT,
    report_id   INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status, due_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    day         TEXT,
    ok          INTEGER NOT NULL DEFAULT 1,
    provider    TEXT,
    model       TEXT,
    analyses    INTEGER NOT NULL DEFAULT 0,
    report_id   INTEGER,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS lead_stages (
    chat_jid   TEXT PRIMARY KEY,
    stage      TEXT NOT NULL,
    source     TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lead_stages_stage ON lead_stages (stage);

CREATE TABLE IF NOT EXISTS lead_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_jid       TEXT NOT NULL,
    stage          TEXT NOT NULL,
    previous_stage TEXT,
    source         TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lead_events_chat ON lead_events (chat_jid, created_at);

CREATE TABLE IF NOT EXISTS action_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    params      TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    output      TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_runs_started ON action_runs (started_at);
"""

_MESSAGE_COLUMNS = {
    "transcription_status": "TEXT NOT NULL DEFAULT 'none'",
    "transcription_model": "TEXT",
    "transcribed_at": "TEXT",
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Migração idempotente: adiciona colunas novas em bancos já existentes."""
    existentes = {linha[1] for linha in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for coluna, definicao in _MESSAGE_COLUMNS.items():
        if coluna not in existentes:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {coluna} {definicao}")
            log.info("migracao: coluna messages.%s adicionada", coluna)


def create_task(data: dict) -> dict:
    now = utc_now_iso()
    with transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO tasks (chat_jid, title, details, owner, due_at, priority,
                                  status, source, report_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("chat_jid"),
                (data.get("title") or "").strip(),
                data.get("details"),
                data.get("owner"),
                data.get("due_at"),
                int(data.get("priority") or 2),
                data.get("status") or "pending",
                data.get("source") or "agent",
                data.get("report_id"),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) if row else {}


def get_task(task_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def update_task(task_id: int, fields: dict) -> dict | None:
    permitidos = {"title", "details", "owner", "due_at", "priority", "status", "chat_jid"}
    sets, params = [], {}
    for chave, valor in fields.items():
        if chave not in permitidos:
            continue
        sets.append(f"{chave} = :{chave}")
        params[chave] = valor
    if not sets:
        return get_task(task_id)
    sets.append("updated_at = :updated_at")
    params["updated_at"] = utc_now_iso()
    if fields.get("status") == "done":
        sets.append("done_at = :done_at")
        params["done_at"] = params["updated_at"]
    params["id"] = task_id
    with transaction() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = :id", params)
    return get_task(task_id)


def list_tasks(*, status: str | None = None, owner: str | None = None, limit: int = 200) -> list[dict]:
    where, params = ["1 = 1"], {}
    if status:
        where.append("status = :status")
        params["status"] = status
    if owner:
        where.append("owner = :owner")
        params["owner"] = owner
    params["limit"] = limit
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM tasks WHERE {' AND '.join(where)}
                 ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'done' THEN 1 ELSE 2 END,
                          priority ASC, COALESCE(due_at, '9999-12-31') ASC, id DESC
                 LIMIT :limit""",
            params,
        ).fetchall()
    return _rows(rows)


def pending_tasks_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'pending'").fetchone()[0]


# ---------------------------------------------------------------------------
# transcrição de áudio
# ---------------------------------------------------------------------------
def pending_transcriptions(limit: int = 5, incluir_ignorados: bool = False) -> list[dict]:
    """Áudios pendentes.

    Com incluir_ignorados=True também pega os marcados como 'skipped' (áudios que
    chegaram com a transcrição desligada) — é o que o backfill manual usa.
    """
    status = ("pending", "skipped") if incluir_ignorados else ("pending",)
    marcadores = ", ".join("?" for _ in status)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT msg_uid, chat_jid, media_type, body, sent_at
                  FROM messages
                 WHERE transcription_status IN ({marcadores})
                   AND (media_type IN ('audio', 'ptt') OR msg_type IN ('audio', 'ptt'))
                 ORDER BY sent_at ASC
                 LIMIT ?""",
            (*status, limit),
        ).fetchall()
    return _rows(rows)


def set_transcription(msg_uid: str, texto: str | None, status: str, model: str | None = None) -> None:
    agora = utc_now_iso()
    with transaction() as conn:
        if texto:
            conn.execute(
                """UPDATE messages
                      SET body = :texto, transcription_status = :status,
                          transcription_model = :model, transcribed_at = :agora,
                          msg_type = 'audio_transcrito'
                    WHERE msg_uid = :uid""",
                {"texto": texto, "status": status, "model": model, "agora": agora, "uid": msg_uid},
            )
        else:
            conn.execute(
                """UPDATE messages
                      SET transcription_status = :status, transcription_model = :model,
                          transcribed_at = :agora
                    WHERE msg_uid = :uid""",
                {"status": status, "model": model, "agora": agora, "uid": msg_uid},
            )


def transcription_stats() -> dict:
    with connect() as conn:
        rows = conn.execute(
            "SELECT transcription_status AS status, COUNT(*) AS total FROM messages GROUP BY transcription_status"
        ).fetchall()
    return {row["status"] or "none": row["total"] for row in rows}


# ---------------------------------------------------------------------------
# métricas de atendimento
# ---------------------------------------------------------------------------
def _janela_comercial() -> tuple[int, int, set[int]]:
    try:
        h_ini = int(str(settings.business_hours_start).split(":")[0])
        h_fim = int(str(settings.business_hours_end).split(":")[0])
    except (ValueError, AttributeError):
        h_ini, h_fim = 8, 18
    dias: set[int] = set()
    for parte in str(settings.business_days).split(","):
        parte = parte.strip()
        if parte.isdigit():
            dias.add(int(parte) % 7)
    return h_ini, h_fim, dias or {1, 2, 3, 4, 5}


def attendance_metrics(date_str: str) -> dict:
    """Tempo de primeira resposta, conversas respondidas e contato fora do horário."""
    h_ini, h_fim, dias = _janela_comercial()
    with connect() as conn:
        mensagens = _rows(
            conn.execute(
                """SELECT chat_jid, direction, sent_at, local_hour, local_date
                     FROM messages
                    WHERE local_date = ?
                    ORDER BY chat_jid, sent_at ASC""",
                (date_str,),
            ).fetchall()
        )
        limite = iso(utc_now() - timedelta(hours=24))
        sem_resposta = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT chat_jid,
                          MAX(CASE WHEN direction = 'in'  THEN sent_at END) AS ult_in,
                          MAX(CASE WHEN direction = 'out' THEN sent_at END) AS ult_out
                     FROM messages GROUP BY chat_jid
               )
               WHERE ult_in IS NOT NULL
                 AND (ult_out IS NULL OR ult_in > ult_out)
                 AND ult_in < ?""",
            (limite,),
        ).fetchone()[0]

    por_chat: dict[str, list[dict]] = {}
    fora_horario = 0
    for m in mensagens:
        por_chat.setdefault(m["chat_jid"], []).append(m)
        if m["direction"] != "in":
            continue
        try:
            hora = int(m["local_hour"])
            data = datetime.strptime(str(m["local_date"]), "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        # dias usa 0=domingo ... 6=sabado; weekday()+1 da 1=segunda ... 7=domingo
        dia_semana = (data.weekday() + 1) % 7
        if dia_semana not in dias or not (h_ini <= hora < h_fim):
            fora_horario += 1

    respondidas = 0
    tempos: list[float] = []
    for lista in por_chat.values():
        primeira_in = next((m for m in lista if m["direction"] == "in"), None)
        if primeira_in is None:
            continue
        resposta = next(
            (m for m in lista if m["direction"] == "out" and m["sent_at"] > primeira_in["sent_at"]),
            None,
        )
        if resposta:
            respondidas += 1
            p, r = parse_ts(primeira_in["sent_at"]), parse_ts(resposta["sent_at"])
            if p and r:
                tempos.append((r - p).total_seconds() / 60.0)

    tempos.sort()
    return {
        "conversas_com_lead": len(por_chat),
        "conversas_respondidas": respondidas,
        "pct_respondidas": round(100 * respondidas / len(por_chat), 1) if por_chat else 0.0,
        "primeira_resposta_media_min": round(sum(tempos) / len(tempos), 1) if tempos else None,
        "primeira_resposta_mediana_min": round(tempos[len(tempos) // 2], 1) if tempos else None,
        "sem_resposta_24h": sem_resposta,
        "mensagens_fora_horario": fora_horario,
        "horario_comercial": f"{h_ini:02d}h-{h_fim:02d}h",
    }


# ---------------------------------------------------------------------------
# execuções do agente (cron)
# ---------------------------------------------------------------------------
def start_agent_run(day: str, provider: str, model: str) -> int:
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO agent_runs (started_at, day, provider, model) VALUES (?, ?, ?, ?)",
            (utc_now_iso(), day, provider, model),
        )
    return int(cursor.lastrowid)


def finish_agent_run(
    run_id: int, *, ok: bool, analyses: int, report_id: int | None, detail: str = ""
) -> None:
    with transaction() as conn:
        conn.execute(
            """UPDATE agent_runs
                  SET finished_at = ?, ok = ?, analyses = ?, report_id = ?, detail = ?
                WHERE id = ?""",
            (utc_now_iso(), 1 if ok else 0, analyses, report_id, (detail or "")[:1000], run_id),
        )


def recent_agent_runs(limit: int = 10) -> list[dict]:
    with connect() as conn:
        return _rows(
            conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        )


def recent_sync_failures(limit: int = 2) -> int:
    with connect() as conn:
        rows = conn.execute("SELECT ok FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return sum(1 for r in rows if not r["ok"])

# ---------------------------------------------------------------------------
# funil de leads
# ---------------------------------------------------------------------------
LEAD_STAGES = ("novo", "qualificado", "agendado", "atendido", "fechado", "perdido")


def set_lead_stage(chat_jid: str, stage: str, source: str = "agent") -> dict:
    """Move o lead de estágio (grava histórico quando muda)."""
    stage = (stage or "").strip().lower()
    if stage not in LEAD_STAGES:
        raise ValueError(f"estagio invalido: {stage}")
    agora = utc_now_iso()
    with transaction() as conn:
        atual = conn.execute("SELECT stage FROM lead_stages WHERE chat_jid = ?", (chat_jid,)).fetchone()
        anterior = atual["stage"] if atual else None
        conn.execute(
            """INSERT INTO lead_stages (chat_jid, stage, source, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_jid) DO UPDATE SET
                   stage = excluded.stage, source = excluded.source, updated_at = excluded.updated_at""",
            (chat_jid, stage, source, agora),
        )
        if anterior != stage:
            conn.execute(
                """INSERT INTO lead_events (chat_jid, stage, previous_stage, source, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_jid, stage, anterior, source, agora),
            )
    return {"chat_jid": chat_jid, "stage": stage, "source": source, "updated_at": agora, "previous_stage": anterior}


def get_lead_stage(chat_jid: str) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM lead_stages WHERE chat_jid = ?", (chat_jid,)).fetchone())


def lead_events(chat_jid: str, limit: int = 50) -> list[dict]:
    with connect() as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM lead_events WHERE chat_jid = ? ORDER BY id DESC LIMIT ?",
                (chat_jid, limit),
            ).fetchall()
        )


def funnel_metrics(date_str: str | None = None) -> dict:
    """Contagem por estágio (opcionalmente só conversas com mensagens no dia)."""
    with connect() as conn:
        if date_str:
            linhas = conn.execute(
                """SELECT s.stage AS stage, COUNT(*) AS total
                     FROM lead_stages s
                    WHERE s.chat_jid IN (SELECT DISTINCT chat_jid FROM messages WHERE local_date = ?)
                    GROUP BY s.stage""",
                (date_str,),
            ).fetchall()
        else:
            linhas = conn.execute(
                "SELECT stage, COUNT(*) AS total FROM lead_stages GROUP BY stage"
            ).fetchall()
        total_com_estagio = sum(row["total"] for row in linhas)
        sem_estagio = conn.execute(
            """SELECT COUNT(DISTINCT chat_jid) FROM messages
                WHERE (? IS NULL OR local_date = ?)
                  AND chat_jid NOT IN (SELECT chat_jid FROM lead_stages)""",
            (date_str, date_str),
        ).fetchone()[0]

    contagem = {stage: 0 for stage in LEAD_STAGES}
    for row in linhas:
        if row["stage"] in contagem:
            contagem[row["stage"]] = row["total"]
    base = total_com_estagio + sem_estagio
    convertidos = contagem["agendado"] + contagem["atendido"] + contagem["fechado"]
    return {
        "stages": contagem,
        "sem_estagio": sem_estagio,
        "total": base,
        "conversion": round(100 * convertidos / base, 1) if base else 0.0,
    }


# ---------------------------------------------------------------------------
# séries temporais
# ---------------------------------------------------------------------------
def daily_series(days: int = 30) -> list[dict]:
    """Mensagens/conversas por dia (para o gráfico de tendência)."""
    days = max(1, min(int(days), 180))
    desde = (datetime.strptime(today_local(), "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with connect() as conn:
        linhas = conn.execute(
            """SELECT local_date AS dia,
                      COUNT(*) AS mensagens,
                      COUNT(DISTINCT chat_jid) AS conversas
                 FROM messages
                WHERE local_date >= :desde
                GROUP BY local_date
                ORDER BY dia""",
            {"desde": desde},
        ).fetchall()
        novas = conn.execute(
            """SELECT dia, COUNT(*) AS total FROM (
                   SELECT chat_jid, MIN(local_date) AS dia FROM messages GROUP BY chat_jid
               ) WHERE dia >= :desde GROUP BY dia""",
            {"desde": desde},
        ).fetchall()

    por_dia = {row["dia"]: dict(row) for row in linhas}
    for row in novas:
        por_dia.setdefault(row["dia"], {"dia": row["dia"], "mensagens": 0, "conversas": 0})
        por_dia[row["dia"]]["novas_conversas"] = row["total"]
    return [
        {
            "date": valor["dia"],
            "messages": valor.get("mensagens", 0),
            "conversations": valor.get("conversas", 0),
            "new_conversations": valor.get("novas_conversas", 0),
        }
        for _, valor in sorted(por_dia.items())
    ]


# ---------------------------------------------------------------------------
# busca/filtros no painel
# ---------------------------------------------------------------------------
def search_conversations(
    *,
    date_str: str | None = None,
    query: str | None = None,
    stage: str | None = None,
    only_unanswered: bool = False,
    include_groups: bool = True,
    limit: int = 200,
) -> list[dict]:
    filtros, params = ["1 = 1"], {"limit": limit, "dia": date_str or ""}
    if date_str:
        filtros.append("m.local_date = :dia")
    if query:
        filtros.append("(c.name LIKE :busca OR c.phone LIKE :busca OR m.chat_jid LIKE :busca)")
        params["busca"] = f"%{query.strip()}%"
    if stage:
        filtros.append("s.stage = :stage")
        params["stage"] = stage
    if not include_groups:
        filtros.append("m.chat_jid NOT LIKE '%@g.us'")

    tendo = ""
    if only_unanswered:
        tendo = """
        HAVING SUM(CASE WHEN m.direction = 'in' THEN 1 ELSE 0 END) > 0
           AND MAX(CASE WHEN m.direction = 'in' THEN m.sent_at END)
               > COALESCE(MAX(CASE WHEN m.direction = 'out' THEN m.sent_at END), '')"""

    sql = f"""
    SELECT m.chat_jid AS chat_jid,
           COALESCE(c.name, '')  AS name,
           COALESCE(c.phone, '') AS phone,
           COALESCE(c.is_group, CASE WHEN m.chat_jid LIKE '%@g.us' THEN 1 ELSE 0 END) AS is_group,
           COUNT(*) AS message_count,
           SUM(CASE WHEN m.direction = 'in' THEN 1 ELSE 0 END) AS inbound,
           SUM(CASE WHEN m.direction = 'out' THEN 1 ELSE 0 END) AS outbound,
           MIN(m.sent_at) AS first_message_at,
           MAX(m.sent_at) AS last_message_at,
           (SELECT x.body FROM messages x WHERE x.chat_jid = m.chat_jid
             AND (:dia = '' OR x.local_date = :dia) ORDER BY x.sent_at DESC LIMIT 1) AS last_preview,
           a.classification AS classification,
           a.sentiment AS sentiment,
           a.score AS score,
           a.summary AS analysis_summary,
           s.stage AS stage
      FROM messages m
      LEFT JOIN contacts c ON c.chat_jid = m.chat_jid
      LEFT JOIN analyses a ON a.chat_jid = m.chat_jid AND a.analysis_date = :dia
      LEFT JOIN lead_stages s ON s.chat_jid = m.chat_jid
     WHERE {' AND '.join(filtros)}
     GROUP BY m.chat_jid
     {tendo}
     ORDER BY last_message_at DESC
     LIMIT :limit
    """
    with connect() as conn:
        return _rows(conn.execute(sql, params).fetchall())

# ---------------------------------------------------------------------------
# execuções de ações do painel (sync, transcrição, agente, backup, aviso)
# ---------------------------------------------------------------------------
def start_action_run(kind: str, params: dict | None = None) -> int:
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO action_runs (kind, params, status, started_at) VALUES (?, ?, 'running', ?)",
            (kind, json.dumps(params or {}, ensure_ascii=False), utc_now_iso()),
        )
    return int(cursor.lastrowid)


def finish_action_run(run_id: int, status: str, output: str = "") -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE action_runs SET status = ?, finished_at = ?, output = ? WHERE id = ?",
            (status, utc_now_iso(), (output or "")[:4000], run_id),
        )


def recent_action_runs(limit: int = 15) -> list[dict]:
    with connect() as conn:
        return _rows(
            conn.execute("SELECT * FROM action_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        )


def running_action(kind: str) -> dict | None:
    with connect() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM action_runs WHERE kind = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        )