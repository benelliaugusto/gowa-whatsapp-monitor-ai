"""Webhook do GOWA: recebe mensagens em tempo real e grava no banco."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request

from . import db
from .config import settings
from .gowa import is_group, phone_from_jid, record_from_webhook
from .security import verify_webhook_signature

log = logging.getLogger(__name__)
router = APIRouter(tags=["Webhook GOWA"])

# Eventos que não viram mensagem (recibos, presença, reações...).
IGNORED_EVENTS = {
    "message.ack",
    "message.reaction",
    "message.revoked",
    "message.edited",
    "message.deleted",
    "message.received",
    "presence",
    "chat_presence",
    "group.participants",
    "group.info",
    "group.joined",
    "call.offer",
    "label.edit",
    "label.association",
}


async def _handle(request: Request, token: str | None) -> dict:
    if settings.gowa_webhook_token and token != settings.gowa_webhook_token:
        raise HTTPException(status_code=401, detail="token do webhook inválido")

    raw = await request.body()
    verify_webhook_signature(raw, request.headers)

    try:
        data = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="payload inesperado")

    event = str(data.get("event") or "").lower()
    if event in IGNORED_EVENTS:
        return {"status": "ignored", "event": event}
    if event and event != "message":
        return {"status": "ignored", "event": event}

    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        return {"status": "ignored", "event": event or "message"}

    device_id = str(data.get("device_id") or data.get("session_id") or "")
    record = record_from_webhook(payload, device_id)
    if record is None:
        return {"status": "ignored", "reason": "sem texto (mídia sem legenda ou formato desconhecido)"}

    chat_jid = record["chat_jid"]
    inserted = db.insert_messages([record])
    if record["direction"] == "in":
        db.upsert_contact(
            chat_jid,
            name=record.get("sender_name") or None,
            phone=phone_from_jid(chat_jid),
            is_group=is_group(chat_jid),
            device_id=device_id,
        )
    else:
        # mensagem enviada pela clínica: preserva o nome que já existir do lead
        db.upsert_contact(
            chat_jid,
            phone=phone_from_jid(chat_jid),
            is_group=is_group(chat_jid),
            device_id=device_id,
        )
    if inserted:
        log.info("webhook: +1 mensagem em %s (%s)", chat_jid, record["direction"])
    return {"status": "ok", "inserted": inserted, "event": event or "message"}


@router.post("/wa-hook", summary="Webhook do GOWA (configure WHATSAPP_WEBHOOK apontando para cá)")
async def gowa_webhook(request: Request, token: str | None = Query(default=None)) -> dict:
    return await _handle(request, token)


@router.post("/webhook", include_in_schema=False)
async def gowa_webhook_alias(request: Request, token: str | None = Query(default=None)) -> dict:
    return await _handle(request, token)