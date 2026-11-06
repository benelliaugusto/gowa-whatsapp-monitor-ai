"""Avisos ao gestor pelo WhatsApp, usando o próprio GOWA."""

from __future__ import annotations

import logging
import re

from . import db
from .config import settings
from .gowa import GowaError, get_gowa_client
from .timeutil import fmt_date_br, resolve_date


def normalizar_telefone(valor: str | None) -> str:
    """Deixa só dígitos: '+55 (11) 99999-9999' -> '5511999999999'."""
    return re.sub(r"\D", "", valor or "")


def validar_telefone(valor: str | None) -> str:
    """Valida o número do gestor e devolve a mensagem de erro que o operador precisa ver."""
    digitos = normalizar_telefone(valor)
    if len(digitos) < 10 or len(digitos) > 15:
        raise ValueError(
            f"telefone invalido ({valor or 'vazio'}): use o numero real com DDI e apenas digitos, "
            "ex.: 5511999999999 (variavel MANAGER_WHATSAPP)"
        )
    return digitos


def mascarar_telefone(valor: str | None) -> str:
    digitos = normalizar_telefone(valor)
    if len(digitos) < 6:
        return ""
    return f"{digitos[:4]}****{digitos[-4:]}"

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
    bruto = (phone or settings.manager_whatsapp or "").strip()
    if not bruto:
        raise ValueError("MANAGER_WHATSAPP nao configurado e nenhum telefone foi informado")
    telefone = validar_telefone(bruto)
    corpo = texto or resumo_do_dia(date_str)
    try:
        resultado = await get_gowa_client().send_text(telefone, corpo)
    except GowaError as exc:
        if getattr(exc, "status_code", None) == 400:
            raise ValueError(
                f"o GOWA nao reconhece o numero {mascarar_telefone(telefone)} no WhatsApp. "
                "Confira MANAGER_WHATSAPP: precisa ser o numero real com DDI (ex.: 5511999999999)."
            ) from exc
        raise
    log.info("resumo enviado ao gestor (%s)", mascarar_telefone(telefone))
    return {
        "ok": True,
        "phone": telefone,
        "message_id": resultado.get("message_id"),
        "text": corpo,
    }