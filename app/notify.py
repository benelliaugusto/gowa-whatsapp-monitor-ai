"""Avisos ao gestor pelo WhatsApp, usando o próprio GOWA."""

from __future__ import annotations

import logging

from . import db
from .config import settings
from .gowa import get_gowa_client
from .timeutil import fmt_date_br, resolve_date

log = logging.getLogger(__name__)


def resumo_do_dia(date_str: str | None = None) -> str:
    """Resumo curto (cabe numa mensagem de WhatsApp) para o gestor."""
    dia = resolve_date(date_str)
    stats = db.daily_stats(dia)
    atendimento = db.attendance_metrics(dia)
    pendencias = db.pending_tasks_count()

    linhas = [
        f"*Resumo da clinica - {fmt_date_br(dia)}*",
        f"Conversas: {stats['conversations']} ({stats['new_conversations']} novas)",
        f"Mensagens: {stats['messages']} ({stats['inbound']} recebidas)",
    ]
    if atendimento.get("primeira_resposta_media_min") is not None:
        linhas.append(f"1a resposta (media): {atendimento['primeira_resposta_media_min']} min")
    if atendimento.get("sem_resposta_24h"):
        linhas.append(f"ATENCAO: {atendimento['sem_resposta_24h']} conversa(s) sem resposta ha +24h")
    if pendencias:
        linhas.append(f"Pendencias abertas: {pendencias}")
    return "\n".join(linhas)


async def enviar_resumo(
    date_str: str | None = None,
    phone: str | None = None,
    texto: str | None = None,
) -> dict:
    """Envia o resumo para MANAGER_WHATSAPP (ou para o telefone informado)."""
    telefone = (phone or settings.manager_whatsapp or "").strip()
    if not telefone:
        raise ValueError("MANAGER_WHATSAPP nao configurado e nenhum telefone foi informado")
    corpo = texto or resumo_do_dia(date_str)
    resultado = await get_gowa_client().send_text(telefone, corpo)
    log.info("resumo enviado ao gestor (%s)", telefone)
    return {
        "ok": True,
        "phone": telefone,
        "message_id": resultado.get("message_id"),
        "text": corpo,
    }