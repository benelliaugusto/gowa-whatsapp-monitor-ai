"""Ações operacionais disparadas pelo painel do gestor.

Cada ação registra uma linha em `action_runs` (aparece em /acoes e /status), mesmo
quando roda em background — é o feedback que o operador precisa ter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from . import db, notify, transcription
from .config import settings
from .gowa import get_sync_service
from .timeutil import resolve_date

log = logging.getLogger(__name__)
RAIZ = Path(__file__).resolve().parent.parent


def _resumo(params: dict | None = None) -> str:
    return ", ".join(f"{k}={v}" for k, v in (params or {}).items()) or "-"


def _executar_cli(run_id: int, comando: list[str]) -> None:
    """Roda um CLI do projeto em background e grava a saída em action_runs."""

    async def acompanhar() -> None:
        try:
            processo = await asyncio.create_subprocess_exec(
                *comando,
                cwd=str(RAIZ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            saida, _ = await processo.communicate()
            texto = (saida or b"").decode("utf-8", errors="replace")[-3000:]
            db.finish_action_run(run_id, "ok" if processo.returncode == 0 else "error", texto)
        except Exception as exc:  # noqa: BLE001
            db.finish_action_run(run_id, "error", f"{type(exc).__name__}: {exc}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(acompanhar())
    else:
        import threading

        threading.Thread(target=lambda: asyncio.run(acompanhar()), daemon=True).start()


def iniciar_agente(date_str: str | None = None, notify_manager: bool = False) -> dict:
    """Dispara `python -m app.agent` em background (não bloqueia a requisição)."""
    dia = resolve_date(date_str)
    if db.running_action("agente") is not None:
        return {"ok": False, "detail": "ja existe uma execucao do agente em andamento"}
    run_id = db.start_action_run("agente", {"date": dia, "notify": notify_manager})
    comando = [sys.executable, "-m", "app.agent", "--date", dia]
    if notify_manager:
        comando.append("--notify")
    _executar_cli(run_id, comando)
    log.info("agente disparado pelo painel: %s %s", _resumo({"date": dia, "notify": notify_manager}), comando[1:])
    return {"ok": True, "run_id": run_id, "detail": "agente iniciado em background"}


async def rodar_sync(days: int | None = None) -> dict:
    run_id = db.start_action_run("sync", {"days": days})
    try:
        resultado = await get_sync_service().sync(days=days, trigger="painel")
        texto = (
            f"{resultado.get('messages_inserted', 0)} novas de "
            f"{resultado.get('messages_fetched', 0)} lidas em "
            f"{resultado.get('chats_scanned', 0)} conversa(s)"
        )
        db.finish_action_run(run_id, "ok" if resultado.get("ok") else "error", texto)
        return resultado
    except Exception as exc:  # noqa: BLE001
        db.finish_action_run(run_id, "error", f"{type(exc).__name__}: {exc}")
        raise


async def rodar_transcricao(limit: int = 10) -> dict:
    run_id = db.start_action_run("transcricao", {"limit": limit})
    try:
        resultado = await transcription.processar_pendentes(limit, incluir_ignorados=True)
        texto = f"{resultado.get('done', 0)} ok / {resultado.get('failed', 0)} falha(s)"
        db.finish_action_run(run_id, "ok", texto)
        return resultado
    except Exception as exc:  # noqa: BLE001
        db.finish_action_run(run_id, "error", f"{type(exc).__name__}: {exc}")
        raise


async def rodar_backup() -> dict:
    run_id = db.start_action_run("backup", {"keep": settings.backup_keep})
    comando = [sys.executable, "-m", "app.backup", "--keep", str(settings.backup_keep)]
    try:
        processo = await asyncio.create_subprocess_exec(
            *comando,
            cwd=str(RAIZ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        saida, _ = await processo.communicate()
        texto = (saida or b"").decode("utf-8", errors="replace")[-3000:]
        db.finish_action_run(run_id, "ok" if processo.returncode == 0 else "error", texto)
        return {"ok": processo.returncode == 0, "output": texto}
    except Exception as exc:  # noqa: BLE001
        db.finish_action_run(run_id, "error", f"{type(exc).__name__}: {exc}")
        raise


async def rodar_aviso(date_str: str | None = None) -> dict:
    run_id = db.start_action_run("aviso", {"date": date_str})
    try:
        resultado = await notify.enviar_resumo(date_str)
        db.finish_action_run(run_id, "ok", f"enviado para {resultado.get('phone')}")
        return resultado
    except Exception as exc:  # noqa: BLE001
        db.finish_action_run(run_id, "error", f"{type(exc).__name__}: {exc}")
        raise