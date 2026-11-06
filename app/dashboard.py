"""Painel HTML do gestor (server-side; sem build de front-end)."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import db, digest
from .config import settings
from .gowa import get_sync_service, gowa_status, is_group, normalize_jid, phone_from_jid
from .security import dashboard_auth
from .templating import templates
from .timeutil import resolve_date, today_local

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(dashboard_auth)])

BOT_LABEL = "Clinica"


def _ctx(request: Request, **extra) -> dict:
    contexto = {
        "request": request,
        "app_name": settings.app_name,
        "version": settings.app_version,
        "fuso": settings.timezone,
        "hoje": today_local(),
        "tem_analise": False,
    }
    contexto.update(extra)
    return contexto


@router.get("/", response_class=HTMLResponse)
def painel(
    request: Request,
    date: str | None = Query(default=None),
    q: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    sem_resposta: int | None = Query(default=None),
    ok: int | None = Query(default=None),
    ins: int | None = Query(default=None),
    erro: str | None = Query(default=None),
) -> HTMLResponse:
    dia = resolve_date(date)
    filtro_estagio = stage if stage in db.LEAD_STAGES else None
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=_ctx(
            request,
            dia=dia,
            stats=db.daily_stats(dia),
            conversas=db.search_conversations(
                date_str=dia,
                query=q,
                stage=filtro_estagio,
                only_unanswered=bool(sem_resposta),
                limit=500,
            ),
            funil=db.funnel_metrics(dia),
            serie=db.daily_series(14),
            estagios=db.LEAD_STAGES,
            filtros={"q": q or "", "stage": filtro_estagio or "", "sem_resposta": bool(sem_resposta)},
            relatorio=db.latest_report(),
            sync=db.last_sync_run(),
            atendimento=db.attendance_metrics(dia),
            tarefas=db.list_tasks(status="pending", limit=8),
            transcricoes=db.transcription_stats(),
            ok=ok,
            ins=ins,
            erro=erro,
        ),
    )


@router.get("/conversas/{chat_jid}", response_class=HTMLResponse)
def conversa(request: Request, chat_jid: str, date: str | None = Query(default=None)) -> HTMLResponse:
    jid = normalize_jid(chat_jid)
    dia = resolve_date(date)
    mensagens = db.messages_for_chat(jid, date_str=dia, limit=1000)
    if not mensagens:
        mensagens = db.messages_for_chat(jid, limit=300)
    contato = db.get_contact(jid) or {}
    return templates.TemplateResponse(
        request=request,
        name="conversation.html",
        context=_ctx(
            request,
            dia=dia,
            jid=jid,
            contato=contato,
            mensagens=mensagens,
            analise=db.get_analysis(jid, dia),
            estagio=db.get_lead_stage(jid),
            historico=db.lead_events(jid, 20),
            estagios=db.LEAD_STAGES,
            telefone=contato.get("phone") or phone_from_jid(jid),
            total=len(mensagens),
        ),
    )


@router.get("/conversas/{chat_jid}/transcript.txt", response_class=PlainTextResponse)
def conversa_transcript(chat_jid: str, date: str | None = Query(default=None)) -> PlainTextResponse:
    jid = normalize_jid(chat_jid)
    dia = resolve_date(date)
    mensagens = db.messages_for_chat(jid, date_str=dia, limit=3000) or db.messages_for_chat(jid, limit=1000)
    contato = db.get_contact(jid) or {}
    inbound = sum(1 for m in mensagens if m.get("direction") == "in")
    resumo = {
        "chat_jid": jid,
        "name": contato.get("name") or "",
        "phone": contato.get("phone") or phone_from_jid(jid),
        "is_group": is_group(jid),
        "message_count": len(mensagens),
        "inbound": inbound,
        "outbound": len(mensagens) - inbound,
        "first_message_at": mensagens[0]["sent_at"] if mensagens else None,
        "last_message_at": mensagens[-1]["sent_at"] if mensagens else None,
    }
    texto, _ = digest.conversation_block(resumo, mensagens, max_chars=100000, bot_label=BOT_LABEL)
    return PlainTextResponse(texto, headers={"Content-Disposition": f'inline; filename="{jid}.txt"'})


@router.get("/relatorios", response_class=HTMLResponse)
def relatorios(request: Request, kind: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=200)) -> HTMLResponse:
    rows, total = db.list_reports(limit=limit, offset=0, kind=kind)
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context=_ctx(request, relatorios=rows, total=total, kind=kind or ""),
    )


@router.get("/relatorios/{report_id}", response_class=HTMLResponse)
def relatorio(request: Request, report_id: int) -> HTMLResponse:
    row = db.get_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    return templates.TemplateResponse(
        request=request,
        name="report.html",
        context=_ctx(request, relatorio=row),
    )


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="status.html",
        context=_ctx(
            request,
            gowa=await gowa_status(),
            resumo=db.database_summary(),
            syncs=db.recent_sync_runs(10),
            agent_runs=db.recent_agent_runs(10),
            transcricoes=db.transcription_stats(),
            tarefas_pendentes=db.pending_tasks_count(),
            chaves=[
                {"nome": entry["name"], "escopo": entry["scope"]}
                for entry in settings.api_key_entries()
            ],
            sync_enabled=settings.sync_enabled,
            sync_minutos=settings.sync_interval_minutes,
            sync_dias=settings.sync_window_days,
            painel_protegido=bool(settings.dashboard_password),
            webhook_protegido=bool(settings.gowa_webhook_secret),
            require_api_key=settings.require_api_key,
        ),
    )


@router.post("/admin/sincronizar")
async def sincronizar(days: int = Form(default=0)) -> RedirectResponse:
    resultado = await get_sync_service().sync(days=days or None, trigger="painel")
    parametros = f"ok={1 if resultado.get('ok') else 0}&ins={resultado.get('messages_inserted', 0)}"
    if not resultado.get("ok"):
        parametros += f"&erro={quote(str(resultado.get('detail') or '')[:200])}"
    return RedirectResponse(url=f"/?{parametros}", status_code=303)


@router.post("/admin/relatorio/{report_id}/publicar")
def publicar(report_id: int) -> RedirectResponse:
    db.update_report(report_id, {"status": "published"})
    return RedirectResponse(url=f"/relatorios/{report_id}", status_code=303)

@router.post("/admin/tarefa/{task_id}/concluir")
def concluir_tarefa(task_id: int, date: str | None = Form(default=None)) -> RedirectResponse:
    db.update_task(task_id, {"status": "done"})
    return RedirectResponse(url=f"/?date={date}" if date else "/", status_code=303)

@router.post("/admin/conversa/{chat_jid}/estagio")
def definir_estagio(
    chat_jid: str,
    stage: str = Form(...),
    date: str | None = Form(default=None),
) -> RedirectResponse:
    db.set_lead_stage(normalize_jid(chat_jid), stage, source="painel")
    destino = f"/conversas/{quote(chat_jid, safe='')}"
    if date:
        destino += f"?date={date}"
    return RedirectResponse(url=destino, status_code=303)