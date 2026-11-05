"""Montagem de digests/transcrições em texto compacto, otimizados para LLM."""

from __future__ import annotations

from .timeutil import fmt_hhmm, parse_ts

MAX_MESSAGE_CHARS = 1500


def message_line(msg: dict, bot_label: str = "Clinica", mostrar_nome: bool = False) -> str:
    sent = parse_ts(msg.get("sent_at"))
    hora = fmt_hhmm(sent) if sent else "--:--"
    if (msg.get("direction") or "in") == "out":
        autor = bot_label
    else:
        nome = (msg.get("sender_name") or "").strip() if mostrar_nome else ""
        autor = f"{nome} (lead)" if nome else "Lead"
    body = " ".join((msg.get("body") or "").split())
    if len(body) > MAX_MESSAGE_CHARS:
        body = body[:MAX_MESSAGE_CHARS] + " [truncado]"
    return f"[{hora}] {autor}: {body}"


def conversation_block(
    convo: dict,
    messages: list[dict],
    *,
    max_chars: int = 12000,
    bot_label: str = "Clinica",
) -> tuple[str, bool]:
    """Monta o bloco de UMA conversa. Devolve (texto, truncado)."""
    name = (convo.get("name") or "").strip()
    phone = convo.get("phone") or ""
    jid = convo.get("chat_jid") or ""
    titulo = f"### {name or phone or jid}"

    meta = [f"chat_jid: {jid}"]
    if phone:
        meta.append(f"telefone: {phone}")
    if convo.get("is_group"):
        meta.append("grupo")
    meta.append(
        f"{convo.get('message_count', len(messages))} msgs "
        f"({convo.get('inbound', 0)} recebidas / {convo.get('outbound', 0)} enviadas)"
    )
    first, last = parse_ts(convo.get("first_message_at")), parse_ts(convo.get("last_message_at"))
    if first and last:
        meta.append(f"{fmt_hhmm(first)}-{fmt_hhmm(last)}")

    linhas = [titulo, " | ".join(meta)]
    if convo.get("classification"):
        resumo = convo.get("analysis_summary") or ""
        linhas.append(f"Analise anterior: {convo['classification']}" + (f" - {resumo}" if resumo else ""))
    linhas.append("Transcricao:")

    corpo = [message_line(m, bot_label, bool(convo.get("is_group"))) for m in messages]
    truncated = False
    if max_chars and corpo:
        mantidas: list[str] = []
        tamanho = 0
        for linha in reversed(corpo):
            if tamanho + len(linha) + 1 > max_chars and mantidas:
                omitidas = len(corpo) - len(mantidas)
                corpo = [f"... ({omitidas} mensagens mais antigas omitidas)"] + list(reversed(mantidas))
                truncated = True
                break
            mantidas.append(linha)
            tamanho += len(linha) + 1
        else:
            corpo = list(reversed(mantidas))
    linhas.extend(corpo)
    return "\n".join(linhas), truncated


def digest_markdown(titulo: str, resumo_linhas: list[str], blocos: list[str]) -> str:
    partes = [f"# {titulo}", "", *resumo_linhas, "", "---", ""]
    for bloco in blocos:
        partes.append(bloco)
        partes.append("")
    return "\n".join(partes).strip() + "\n"