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
from typing import Any, Iterable, Iterator

from .config import settings
from .timeutil import iso, local_date_of, parse_ts, utc_now, utc_now_iso

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
                 body, media_type, source, device_id, sent_at, local_date, local_hour, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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