"""Transcrição local de áudios do WhatsApp com faster-whisper (CPU).

O modelo é carregado sob demanda e fica em WHISPER_MODEL_DIR (volume), para não
baixar a cada deploy. Se a biblioteca não estiver instalada, as mensagens são
marcadas como "failed" com log claro e o resto do monitor continua funcionando.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
from typing import Any

from . import db
from .config import settings
from .gowa import GowaError, get_gowa_client

log = logging.getLogger(__name__)

_modelo: Any = None
_indisponivel = False
_trava = threading.Lock()


def _carregar_modelo():
    global _modelo, _indisponivel
    if _modelo is not None or _indisponivel:
        return _modelo
    with _trava:
        if _modelo is not None or _indisponivel:
            return _modelo
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001 - import opcional
            _indisponivel = True
            log.warning("faster-whisper indisponivel (%s): transcricao desativada", exc)
            return None
        try:
            os.makedirs(settings.whisper_model_dir, exist_ok=True)
            _modelo = WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                download_root=settings.whisper_model_dir,
            )
            log.info(
                "modelo whisper '%s' carregado (%s/%s)",
                settings.whisper_model,
                settings.whisper_device,
                settings.whisper_compute_type,
            )
        except Exception as exc:  # noqa: BLE001
            _indisponivel = True
            log.warning("falha ao carregar o whisper (%s): transcricao desativada", exc)
            return None
    return _modelo


def _transcrever_sync(audio: bytes) -> str:
    modelo = _carregar_modelo()
    if modelo is None:
        raise RuntimeError("faster-whisper indisponivel")
    segmentos, _info = modelo.transcribe(
        io.BytesIO(audio),
        language=settings.whisper_language or None,
        vad_filter=True,
        beam_size=1,
    )
    texto = " ".join(seg.text.strip() for seg in segmentos if seg.text and seg.text.strip())
    return texto.strip()


async def transcrever_mensagem(msg_uid: str, chat_jid: str, media_type: str = "audio") -> bool:
    """Baixa e transcreve UMA mensagem. Devolve True se gravou o texto."""
    message_id = msg_uid.split(":", 1)[1] if ":" in msg_uid else ""
    if not message_id:
        db.set_transcription(msg_uid, None, "failed", "sem id do GOWA")
        return False
    try:
        audio = await get_gowa_client().download_media_bytes(message_id, chat_jid)
    except GowaError as exc:
        log.warning("transcricao: falha ao baixar midia de %s: %s", chat_jid, exc)
        db.set_transcription(msg_uid, None, "failed", str(exc)[:120])
        return False
    if not audio:
        db.set_transcription(msg_uid, None, "failed", "audio vazio")
        return False
    try:
        texto = await asyncio.to_thread(_transcrever_sync, audio)
    except Exception as exc:  # noqa: BLE001
        log.warning("transcricao: falha em %s: %s", chat_jid, exc)
        db.set_transcription(msg_uid, None, "failed", f"{type(exc).__name__}: {exc}"[:120])
        return False
    if not texto:
        db.set_transcription(msg_uid, None, "failed", "sem fala detectada")
        return False
    db.set_transcription(msg_uid, texto, "done", settings.whisper_model)
    log.info("transcricao ok: %s (%s caracteres)", chat_jid, len(texto))
    return True


async def processar_pendentes(limit: int = 3, incluir_ignorados: bool = False) -> dict:
    """Processa áudios pendentes (worker periódico e endpoint manual)."""
    if not settings.transcribe_enabled:
        return {"enabled": False, "processed": 0, "done": 0, "failed": 0}
    pendentes = db.pending_transcriptions(limit, incluir_ignorados=incluir_ignorados)
    feitos = falhas = 0
    for item in pendentes:
        ok = await transcrever_mensagem(
            item["msg_uid"], item["chat_jid"], item.get("media_type") or "audio"
        )
        feitos += 1 if ok else 0
        falhas += 0 if ok else 1
    return {"enabled": True, "processed": len(pendentes), "done": feitos, "failed": falhas}


class TranscriptionWorker:
    """Loop em background que consome a fila de áudios pendentes."""

    def __init__(self, interval_seconds: int = 60, batch: int = 2):
        self.interval = max(15, int(interval_seconds))
        self.batch = max(1, int(batch))
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="transcription-worker")

    async def _loop(self) -> None:
        while True:
            try:
                resultado = await processar_pendentes(self.batch)
                if resultado["processed"]:
                    log.info(
                        "transcricao: %s processados (%s ok / %s falhas)",
                        resultado["processed"],
                        resultado["done"],
                        resultado["failed"],
                    )
            except Exception:  # noqa: BLE001 - o worker nunca deve morrer
                log.exception("transcricao: erro no worker")
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass