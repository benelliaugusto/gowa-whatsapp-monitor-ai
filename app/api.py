"""API pública /api/v1 — contrato de leitura/escrita para os agentes de IA.

Fluxo esperado (pull):
  1. o agente lê GET /api/v1/digest?date=YYYY-MM-DD  (conversas do dia em texto);
  2. analisa com o próprio LLM;
  3. grava POST /api/v1/analyses (por conversa) e POST /api/v1/reports (relatório do gestor);
  4. o painel HTML mostra o resultado — o monitor nunca chama IA por conta própria.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from . import db, digest, notify, transcription
from .config import settings
from .gowa import get_gowa_client, get_sync_service, gowa_status, is_group, normalize_jid, phone_from_jid
from .schemas import (
    AnalysisIn,
    AnalysisOut,
    ConversationOut,
    DigestConversation,
    DigestOut,
    HealthOut,
    MessageOut,
    ReportIn,
    ReportListOut,
    ReportOut,
    ReportPatch,
    SyncIn,
    SyncOut,
    SyncRunOut,
)
from .schemas import (
    AgentRunIn,
    AgentRunOut,
    FunnelOut,
    LeadEventOut,
    NotifyIn,
    NotifyOut,
    SeriesPoint,
    StageIn,
    StageOut,
    TaskIn,
    TaskOut,
    TaskPatch,
    TranscriptionRunOut,
)
from .security import ApiKey, require_api_key, require_write_key
from .timeutil import day_bounds, iso, local_date_of, parse_ts, resolve_date, today_local, utc_now_iso

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["API para agentes de IA"])

BOT_LABEL = "Clinica"

CONTRACT_FALLBACK = """# Contrato rápido (fallback)

Leia as conversas do dia:
    GET /api/v1/digest?date=YYYY-MM-DD&format=text

Grave a análise de cada conversa:
    POST /api/v1/analyses  {"chat_jid": "...", "summary": "...", "classification": "lead_quente", ...}

Grave o relatório do gestor:
    POST /api/v1/reports   {"kind": "daily", "period_start": "YYYY-MM-DD", "title": "...", "body_md": "..."}

Autenticação: header X-API-Key ou Authorization: Bearer <chave>.
"""


def _manager_valido() -> bool:
    try:
        notify.validar_telefone(settings.manager_whatsapp)
        return True
    except ValueError:
        return False


def _contract_text() -> str:
    candidates = [
        Path(__file__).resolve().parent.parent / "docs" / "AGENT_PROMPT.md",
        Path("/app/docs/AGENT_PROMPT.md"),
    ]
    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover
            continue
    return CONTRACT_FALLBACK


# ---------------------------------------------------------------------------
# saúde (pública, usada pelo healthcheck do Easypanel)
# ---------------------------------------------------------------------------
@router.get("/health", response_model=HealthOut, summary="Saúde do monitor (público)")
async def health() -> HealthOut:
    gowa = await gowa_status()
    return HealthOut(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        timezone=settings.timezone,
        now=utc_now_iso(),
        database=db.database_summary(),
        gowa=gowa,
        sync={
            "enabled": settings.sync_enabled,
            "interval_minutes": settings.sync_interval_minutes,
            "window_days": settings.sync_window_days,
            "last_run": db.last_sync_run(),
        },
        notify={
            "manager_configured": bool(settings.manager_whatsapp),
            "manager_valid": _manager_valido(),
            "manager_masked": notify.mascarar_telefone(settings.manager_whatsapp),
        },
    )


# ---------------------------------------------------------------------------
# conversas (leitura)
# ---------------------------------------------------------------------------
def _conversation_summary(chat_jid: str, messages: list[dict]) -> dict:
    contato = db.get_contact(chat_jid) or {}
    inbound = sum(1 for m in messages if m.get("direction") == "in")
    return {
        "chat_jid": chat_jid,
        "name": contato.get("name") or "",
        "phone": contato.get("phone") or phone_from_jid(chat_jid),
        "is_group": is_group(chat_jid),
        "message_count": len(messages),
        "inbound": inbound,
        "outbound": len(messages) - inbound,
        "first_message_at": messages[0]["sent_at"] if messages else None,
        "last_message_at": messages[-1]["sent_at"] if messages else None,
    }


@router.get(
    "/conversations",
    response_model=list[ConversationOut],
    dependencies=[Depends(require_api_key)],
    summary="Conversas com movimento no período",
)
def list_conversations(
    date: str | None = Query(default=None, description="Dia YYYY-MM-DD (fuso da clínica). Padrão: hoje."),
    since: str | None = Query(default=None, description="ISO-8601 inicial (substitui date)"),
    until: str | None = Query(default=None, description="ISO-8601 final"),
    include_groups: bool = Query(default=False),
    limit: int = Query(default=300, ge=1, le=2000),
) -> list[ConversationOut]:
    if since or until:
        start = parse_ts(since) or day_bounds(date or today_local())[0]
        end = parse_ts(until) or (start + timedelta(days=1))
        rows = db.conversations_in_range(
            iso(start),
            iso(end),
            include_groups=include_groups,
            limit=limit,
            analysis_date=local_date_of(start),
        )
    else:
        rows = db.conversations_for_date(resolve_date(date), include_groups=include_groups, limit=limit)
    return [ConversationOut(**row) for row in rows]


@router.get(
    "/conversations/{chat_jid}",
    response_model=ConversationOut,
    dependencies=[Depends(require_api_key)],
    summary="Metadados de uma conversa",
)
def get_conversation(chat_jid: str, date: str | None = Query(default=None)) -> ConversationOut:
    jid = normalize_jid(chat_jid)
    if not db.has_messages(jid):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"Conversa {jid} nao encontrada"})
    dia = resolve_date(date)
    messages = db.messages_for_chat(jid, date_str=dia, limit=5000)
    if not messages:
        messages = db.messages_for_chat(jid, limit=5000)
    resumo = _conversation_summary(jid, messages)
    analise = db.get_analysis(jid, dia) or {}
    resumo.update(
        {
            "classification": analise.get("classification"),
            "sentiment": analise.get("sentiment"),
            "score": analise.get("score"),
            "analysis_summary": analise.get("summary"),
        }
    )
    return ConversationOut(**resumo)


@router.get(
    "/conversations/{chat_jid}/messages",
    response_model=list[MessageOut],
    dependencies=[Depends(require_api_key)],
    summary="Mensagens de uma conversa",
)
def conversation_messages(
    chat_jid: str,
    date: str | None = Query(default=None, description="Dia YYYY-MM-DD. Vazio = respeita since/until."),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    order: Literal["asc", "desc"] = Query(default="asc"),
) -> list[MessageOut]:
    jid = normalize_jid(chat_jid)
    if not db.has_messages(jid):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"Conversa {jid} nao encontrada"})
    if date:
        rows = db.messages_for_chat(jid, date_str=resolve_date(date), limit=limit, newest_first=order == "desc")
    else:
        rows = db.messages_for_chat(
            jid,
            start_iso=iso(parse_ts(since)) if since else None,
            end_iso=iso(parse_ts(until)) if until else None,
            limit=limit,
            newest_first=order == "desc",
        )
    return [MessageOut(**row) for row in rows]


@router.get(
    "/conversations/{chat_jid}/transcript",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_api_key)],
    summary="Transcrição em texto puro, pronta para o LLM",
)
def conversation_transcript(
    chat_jid: str,
    date: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    max_chars: int = Query(default=20000, ge=500, le=200000),
) -> PlainTextResponse:
    jid = normalize_jid(chat_jid)
    if not db.has_messages(jid):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"Conversa {jid} nao encontrada"})
    if date:
        messages = db.messages_for_chat(jid, date_str=resolve_date(date), limit=5000)
    else:
        messages = db.messages_for_chat(
            jid,
            start_iso=iso(parse_ts(since)) if since else None,
            end_iso=iso(parse_ts(until)) if until else None,
            limit=5000,
        )
    resumo = _conversation_summary(jid, messages)
    analise = db.get_analysis(jid, resolve_date(date)) or {}
    resumo["classification"] = analise.get("classification")
    resumo["analysis_summary"] = analise.get("summary")
    texto, _truncado = digest.conversation_block(resumo, messages, max_chars=max_chars, bot_label=BOT_LABEL)
    return PlainTextResponse(texto)


# ---------------------------------------------------------------------------
# digest: a entrada principal do agente
# ---------------------------------------------------------------------------
@router.get(
    "/digest",
    response_model=DigestOut,
    dependencies=[Depends(require_api_key)],
    summary="Conversas do dia (texto compacto) para a IA analisar",
    responses={200: {"description": "JSON, ou texto puro quando format=text"}},
)
def get_digest(
    date: str | None = Query(default=None, description="Dia YYYY-MM-DD. Padrão: hoje."),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    include_groups: bool = Query(default=False),
    limit: int = Query(default=300, ge=1, le=1000),
    max_chars: int | None = Query(default=None, description="Orçamento total de caracteres das transcrições"),
    format: Literal["json", "text"] = Query(default="json"),
):
    dia = resolve_date(date)
    usando_intervalo = bool(since or until)
    orcamento = max_chars or settings.digest_max_chars

    if usando_intervalo:
        start = parse_ts(since) or day_bounds(dia)[0]
        end = parse_ts(until) or (start + timedelta(days=1))
        rows = db.conversations_in_range(
            iso(start),
            iso(end),
            include_groups=include_groups,
            limit=limit,
            analysis_date=local_date_of(start),
        )
    else:
        rows = db.conversations_for_date(dia, include_groups=include_groups, limit=limit)

    por_conversa = max(1500, orcamento // max(1, len(rows))) if rows else orcamento
    conversas: list[DigestConversation] = []
    blocos: list[str] = []
    truncado_geral = False

    for row in rows:
        if usando_intervalo:
            mensagens = db.messages_for_chat(
                row["chat_jid"],
                start_iso=iso(parse_ts(since)) if since else None,
                end_iso=iso(parse_ts(until)) if until else None,
                limit=3000,
            )
        else:
            mensagens = db.messages_for_chat(row["chat_jid"], date_str=dia, limit=3000)
        texto, truncado = digest.conversation_block(row, mensagens, max_chars=por_conversa, bot_label=BOT_LABEL)
        truncado_geral = truncado_geral or truncado
        blocos.append(texto)
        conversas.append(
            DigestConversation(
                **row,
                transcript=texto,
                messages_included=len(mensagens),
                truncated=truncado,
            )
        )

    if usando_intervalo:
        totais = {
            "conversations": len(conversas),
            "messages": sum(c.message_count for c in conversas),
            "inbound": sum(c.inbound for c in conversas),
            "outbound": sum(c.outbound for c in conversas),
            "new_conversations": 0,
        }
    else:
        stats = db.daily_stats(dia)
        totais = {
            "conversations": stats["conversations"],
            "messages": stats["messages"],
            "inbound": stats["inbound"],
            "outbound": stats["outbound"],
            "new_conversations": stats["new_conversations"],
        }

    notas = []
    if not rows:
        notas.append("Nenhuma conversa com mensagens no periodo. Verifique se o webhook do GOWA esta configurado.")
    if truncado_geral:
        notas.append("Algumas transcrições foram reduzidas por limite de tamanho; use os endpoints por conversa para a íntegra.")
    if not settings.sync_enabled:
        notas.append("Sincronização automática desligada.")

    payload = DigestOut(
        date=dia,
        timezone=settings.timezone,
        generated_at=utc_now_iso(),
        totals=totais,
        conversations=conversas,
        truncated=truncado_geral,
        notes=notas,
        attendance=None if usando_intervalo else db.attendance_metrics(dia),
    )
    if format == "text":
        resumo_linhas = [
            f"Data: {dia} | Fuso: {settings.timezone}",
            f"Totais: {totais['conversations']} conversas, {totais['messages']} mensagens "
            f"({totais['inbound']} recebidas / {totais['outbound']} enviadas), "
            f"{totais['new_conversations']} conversas novas",
        ]
        texto = digest.digest_markdown(f"Digest de conversas - {dia}", resumo_linhas, blocos)
        return PlainTextResponse(texto)  # type: ignore[return-value]
    return payload


# ---------------------------------------------------------------------------
# análises (escrita pelo agente)
# ---------------------------------------------------------------------------
@router.post(
    "/analyses",
    response_model=AnalysisOut,
    dependencies=[Depends(require_write_key)],
    summary="Cria/atualiza a análise de uma conversa no dia (upsert)",
)
def save_analysis(payload: AnalysisIn) -> AnalysisOut:
    jid = normalize_jid(payload.chat_jid)
    if not jid:
        raise HTTPException(status_code=422, detail={"error": "validation", "message": "chat_jid inválido"})
    if not db.has_messages(jid):
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Nao ha mensagens para {jid}; sincronize antes de analisar"},
        )
    dia = payload.analysis_date.isoformat() if payload.analysis_date else today_local()
    data = payload.model_dump()
    data["chat_jid"] = jid
    data["analysis_date"] = dia
    row = db.upsert_analysis(data)
    if payload.stage:
        try:
            db.set_lead_stage(jid, payload.stage, source=payload.agent or "agent")
        except ValueError:
            pass
    return AnalysisOut(**row)


@router.get(
    "/analyses",
    response_model=list[AnalysisOut],
    dependencies=[Depends(require_api_key)],
    summary="Análises já gravadas",
)
def list_analyses(
    date: str | None = Query(default=None),
    chat_jid: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[AnalysisOut]:
    rows = db.list_analyses(
        date_str=resolve_date(date) if date else None,
        chat_jid=normalize_jid(chat_jid) if chat_jid else None,
        limit=limit,
    )
    return [AnalysisOut(**row) for row in rows]


# ---------------------------------------------------------------------------
# relatórios (escrita pelo agente)
# ---------------------------------------------------------------------------
@router.post(
    "/reports",
    response_model=ReportOut,
    status_code=201,
    dependencies=[Depends(require_write_key)],
    summary="Publica o relatório do gestor (idempotente por idempotency_key)",
)
def create_report(payload: ReportIn, response: Response) -> ReportOut:
    data = payload.model_dump()
    data["period_start"] = payload.period_start.isoformat()
    data["period_end"] = (payload.period_end or payload.period_start).isoformat()
    if data["period_end"] < data["period_start"]:
        raise HTTPException(status_code=422, detail={"error": "validation", "message": "period_end anterior a period_start"})
    row, created = db.create_report(data)
    if not created:
        response.status_code = 200
    return ReportOut(**row)


@router.get(
    "/reports",
    response_model=ReportListOut,
    dependencies=[Depends(require_api_key)],
    summary="Lista relatórios",
)
def list_reports(
    kind: Literal["daily", "weekly", "custom"] | None = Query(default=None),
    status: Literal["draft", "published"] | None = Query(default=None),
    start: str | None = Query(default=None, description="Data inicial (YYYY-MM-DD)"),
    end: str | None = Query(default=None, description="Data final (YYYY-MM-DD)"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ReportListOut:
    rows, total = db.list_reports(limit=limit, offset=offset, kind=kind, status=status, start=start, end=end)
    return ReportListOut(total=total, items=[ReportOut(**row) for row in rows])


@router.get(
    "/reports/{report_id}",
    response_model=ReportOut,
    dependencies=[Depends(require_api_key)],
    summary="Lê um relatório",
)
def get_report(report_id: int) -> ReportOut:
    row = db.get_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Relatorio nao encontrado"})
    return ReportOut(**row)


@router.patch(
    "/reports/{report_id}",
    response_model=ReportOut,
    dependencies=[Depends(require_write_key)],
    summary="Atualiza/publica um relatório",
)
def patch_report(report_id: int, payload: ReportPatch) -> ReportOut:
    fields = payload.model_dump(exclude_unset=True)
    for campo in ("period_start", "period_end"):
        if campos := fields.get(campo):
            fields[campo] = campos.isoformat()
    row = db.update_report(report_id, fields)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Relatorio nao encontrado"})
    return ReportOut(**row)


# ---------------------------------------------------------------------------
# estatísticas, sincronização e contrato
# ---------------------------------------------------------------------------
@router.get(
    "/stats/daily",
    dependencies=[Depends(require_api_key)],
    summary="KPIs do dia (mensagens, conversas, novas, top conversas)",
)
def stats_daily(date: str | None = Query(default=None)) -> dict[str, Any]:
    dia = resolve_date(date)
    dados = db.daily_stats(dia)
    dados["atendimento"] = db.attendance_metrics(dia)
    return dados


@router.post(
    "/sync",
    response_model=SyncOut,
    dependencies=[Depends(require_write_key)],
    summary="Força uma sincronização com o GOWA",
)
async def trigger_sync(payload: SyncIn | None = None) -> SyncOut:
    body = payload or SyncIn()
    result = await get_sync_service().sync(
        days=body.days,
        chat_jid=body.chat_jid,
        device_id=body.device_id,
        trigger="api",
    )
    return SyncOut(**result)


@router.get(
    "/sync/runs",
    response_model=list[SyncRunOut],
    dependencies=[Depends(require_api_key)],
    summary="Histórico de sincronizações",
)
def sync_runs(limit: int = Query(default=10, ge=1, le=100)) -> list[SyncRunOut]:
    return [SyncRunOut(**row) for row in db.recent_sync_runs(limit)]


@router.get(
    "/contract",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_api_key)],
    summary="Contrato/prompt completo para o agente (markdown)",
)
def contract() -> PlainTextResponse:
    return PlainTextResponse(_contract_text())


@router.get(
    "/gowa",
    dependencies=[Depends(require_api_key)],
    summary="Diagnóstico do GOWA (versão, devices, alcance)",
)
async def gowa_diagnostics() -> dict[str, Any]:
    client = get_gowa_client()
    return {
        "client": {"base_url": client.base_url, "basic_auth_configured": bool(settings.gowa_basic_auth)},
        "status": await gowa_status(),
    }

# ---------------------------------------------------------------------------
# tarefas (pendências da recepção/gestor)
# ---------------------------------------------------------------------------
@router.get(
    "/tasks",
    response_model=list[TaskOut],
    dependencies=[Depends(require_api_key)],
    summary="Lista tarefas (pendências)",
)
def list_tasks(
    status: str | None = Query(default=None, description="pending | done | cancelled"),
    owner: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[TaskOut]:
    return [TaskOut(**row) for row in db.list_tasks(status=status, owner=owner, limit=limit)]


@router.post(
    "/tasks",
    response_model=TaskOut,
    status_code=201,
    dependencies=[Depends(require_write_key)],
    summary="Cria uma tarefa (o agente transforma action_items em tarefas)",
)
def create_task(payload: TaskIn) -> TaskOut:
    dados = payload.model_dump()
    if payload.due_at:
        dados["due_at"] = payload.due_at.isoformat()
    return TaskOut(**db.create_task(dados))


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskOut,
    dependencies=[Depends(require_write_key)],
    summary="Atualiza ou conclui uma tarefa",
)
def patch_task(task_id: int, payload: TaskPatch) -> TaskOut:
    campos = payload.model_dump(exclude_unset=True)
    if campos.get("due_at") is not None:
        campos["due_at"] = campos["due_at"].isoformat()
    row = db.update_task(task_id, campos)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Tarefa nao encontrada"})
    return TaskOut(**row)


# ---------------------------------------------------------------------------
# transcrição de áudios
# ---------------------------------------------------------------------------
@router.post(
    "/transcriptions/run",
    response_model=TranscriptionRunOut,
    dependencies=[Depends(require_write_key)],
    summary="Transcreve áudios pendentes (backfill manual)",
)
async def run_transcriptions(limit: int = Query(default=10, ge=1, le=200)) -> TranscriptionRunOut:
    resultado = await transcription.processar_pendentes(limit)
    return TranscriptionRunOut(**resultado, stats=db.transcription_stats())


@router.get(
    "/transcriptions/stats",
    dependencies=[Depends(require_api_key)],
    summary="Status da fila de transcrição",
)
def transcription_stats() -> dict[str, Any]:
    return {
        "enabled": settings.transcribe_enabled,
        "model": settings.whisper_model,
        "stats": db.transcription_stats(),
    }


# ---------------------------------------------------------------------------
# avisos ao gestor
# ---------------------------------------------------------------------------
@router.post(
    "/notify/manager",
    response_model=NotifyOut,
    dependencies=[Depends(require_write_key)],
    summary="Envia o resumo do dia ao gestor pelo WhatsApp (GOWA)",
)
async def notify_manager(payload: NotifyIn | None = None) -> NotifyOut:
    body = payload or NotifyIn()
    try:
        resultado = await notify.enviar_resumo(
            body.day.isoformat() if body.day else None,
            body.phone,
            body.text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "validation", "message": str(exc)}) from exc
    return NotifyOut(**resultado)


# ---------------------------------------------------------------------------
# execuções do agente (observabilidade do cron)
# ---------------------------------------------------------------------------
@router.post(
    "/agent-runs",
    response_model=AgentRunOut,
    status_code=201,
    dependencies=[Depends(require_write_key)],
    summary="Registra uma execução do agente (cron)",
)
def create_agent_run(payload: AgentRunIn) -> AgentRunOut:
    run_id = db.start_agent_run(
        payload.day.isoformat() if payload.day else today_local(),
        payload.provider or "",
        payload.model or "",
    )
    db.finish_agent_run(
        run_id,
        ok=payload.ok,
        analyses=payload.analyses,
        report_id=payload.report_id,
        detail=payload.detail or "",
    )
    row = db.recent_agent_runs(1)[0]
    return AgentRunOut(**row)


@router.get(
    "/agent-runs",
    response_model=list[AgentRunOut],
    dependencies=[Depends(require_api_key)],
    summary="Últimas execuções do agente",
)
def list_agent_runs(limit: int = Query(default=10, ge=1, le=100)) -> list[AgentRunOut]:
    return [AgentRunOut(**row) for row in db.recent_agent_runs(limit)]

# ---------------------------------------------------------------------------
# funil de leads
# ---------------------------------------------------------------------------
@router.patch(
    "/conversations/{chat_jid}/stage",
    response_model=StageOut,
    dependencies=[Depends(require_write_key)],
    summary="Move o lead no funil (novo, qualificado, agendado, atendido, fechado, perdido)",
)
def set_stage(chat_jid: str, payload: StageIn) -> StageOut:
    jid = normalize_jid(chat_jid)
    if not db.has_messages(jid):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Conversa nao encontrada"})
    return StageOut(**db.set_lead_stage(jid, payload.stage, payload.source or "manual"))


@router.get(
    "/conversations/{chat_jid}/stage",
    dependencies=[Depends(require_api_key)],
    summary="Estágio atual do lead e histórico de mudanças",
)
def get_stage(chat_jid: str) -> dict[str, Any]:
    jid = normalize_jid(chat_jid)
    return {
        "chat_jid": jid,
        "current": db.get_lead_stage(jid),
        "history": [LeadEventOut(**row).model_dump() for row in db.lead_events(jid, 50)],
    }


@router.get(
    "/funnel",
    response_model=FunnelOut,
    dependencies=[Depends(require_api_key)],
    summary="Funil de leads com conversão",
)
def funnel(date: str | None = Query(default=None, description="Dia (opcional): só conversas com mensagens nele")) -> FunnelOut:
    return FunnelOut(**db.funnel_metrics(resolve_date(date) if date else None))


# ---------------------------------------------------------------------------
# tendências
# ---------------------------------------------------------------------------
@router.get(
    "/stats/series",
    response_model=list[SeriesPoint],
    dependencies=[Depends(require_api_key)],
    summary="Série diária (mensagens, conversas e novas conversas)",
)
def stats_series(days: int = Query(default=30, ge=1, le=180)) -> list[SeriesPoint]:
    return [SeriesPoint(**row) for row in db.daily_series(days)]