"""Cliente HTTP do GOWA (WhatsApp gateway) e serviço de sincronização de histórico."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import timedelta
from typing import Any

import httpx

from . import db
from .config import settings
from .timeutil import iso, parse_ts, utc_now

log = logging.getLogger(__name__)

DIRECT_SUFFIX = "@s.whatsapp.net"
SKIP_JIDS = {"status@broadcast"}


class GowaError(RuntimeError):
    """Falha ao conversar com o GOWA."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# helpers de JID / payload
# ---------------------------------------------------------------------------
def normalize_jid(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    if "@" in text:
        return text
    digits = re.sub(r"\D", "", text)
    return f"{digits}{DIRECT_SUFFIX}" if digits else ""


def phone_from_jid(jid: Any) -> str:
    if not jid:
        return ""
    return str(jid).split("@")[0].split(":")[0]


def is_group(jid: Any) -> bool:
    return bool(jid) and str(jid).endswith("@g.us")


def _auth_headers() -> dict[str, str]:
    if not settings.gowa_basic_auth:
        return {}
    token = base64.b64encode(settings.gowa_basic_auth.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _list_from(payload: Any) -> list[dict]:
    """Extrai itens do envelope do GOWA: {"results": {"data": [...]}} ou lista pura."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    results = payload.get("results", payload)
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    if isinstance(results, dict):
        for key in ("data", "items", "messages", "chats", "devices"):
            value = results.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _nested(payload: Any, key: str) -> dict:
    if isinstance(payload, dict):
        results = payload.get("results", payload)
        if isinstance(results, dict) and isinstance(results.get(key), dict):
            return results[key]
    return {}


# ---------------------------------------------------------------------------
# cliente
# ---------------------------------------------------------------------------
class GowaClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or settings.gowa_url).rstrip("/")
        self.timeout = timeout or settings.gowa_timeout
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=_auth_headers(),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get(self, path: str, *, params: dict | None = None, device_id: str | None = None) -> Any:
        client = await self.client()
        headers: dict[str, str] = {}
        device = device_id or settings.gowa_device_id
        if device:
            headers["X-Device-Id"] = device
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            response = await client.get(path, params=clean, headers=headers)
        except httpx.HTTPError as exc:
            raise GowaError(f"nao foi possivel conectar ao GOWA em {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise GowaError(
                f"GOWA respondeu HTTP {response.status_code} em {path}: {response.text[:300]}",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            return response.text
        if isinstance(payload, dict) and payload.get("code") not in (None, "SUCCESS"):
            raise GowaError(f"GOWA retornou {payload.get('code')}: {payload.get('message')}", response.status_code)
        return payload

    async def _post(self, path: str, *, json: dict | None = None, device_id: str | None = None) -> Any:
        client = await self.client()
        headers: dict[str, str] = {}
        device = device_id or settings.gowa_device_id
        if device:
            headers["X-Device-Id"] = device
        try:
            response = await client.post(path, json=json or {}, headers=headers)
        except httpx.HTTPError as exc:
            raise GowaError(f"nao foi possivel conectar ao GOWA em {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise GowaError(
                f"GOWA respondeu HTTP {response.status_code} em {path}: {response.text[:300]}",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            return response.text
        if isinstance(payload, dict) and payload.get("code") not in (None, "SUCCESS"):
            raise GowaError(f"GOWA retornou {payload.get('code')}: {payload.get('message')}", response.status_code)
        return payload

    async def send_text(self, phone: str, message: str, *, device_id: str | None = None) -> dict:
        """Envia mensagem de texto pelo GOWA (avisos ao gestor)."""
        payload = await self._post(
            "/send/message",
            json={"phone": phone, "message": message},
            device_id=device_id,
        )
        if isinstance(payload, dict) and isinstance(payload.get("results"), dict):
            return payload["results"]
        return payload if isinstance(payload, dict) else {}

    async def download_media_bytes(
        self, message_id: str, chat_jid: str, *, device_id: str | None = None
    ) -> bytes:
        """Baixa a mídia de uma mensagem para transcrição local."""
        payload = await self._get(
            f"/message/{message_id}/download",
            params={"phone": chat_jid},
            device_id=device_id,
        )
        resultados = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(resultados, dict):
            raise GowaError("GOWA nao retornou metadados da midia")
        url = resultados.get("file_url") or resultados.get("url")
        caminho = resultados.get("file_path") or resultados.get("path")
        if not url and caminho:
            url = f"{self.base_url}/{str(caminho).lstrip('/')}"
        if not url:
            raise GowaError("GOWA nao informou file_url/file_path para a midia")
        client = await self.client()
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise GowaError(f"falha ao baixar midia: {exc}") from exc
        if response.status_code >= 400:
            raise GowaError(f"falha ao baixar midia: HTTP {response.status_code}", response.status_code)
        return response.content

    async def health(self) -> bool:
        try:
            client = await self.client()
            response = await client.get("/health", timeout=5.0)
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    async def app_info(self) -> dict:
        payload = await self._get("/app/info")
        if isinstance(payload, dict) and isinstance(payload.get("results"), dict):
            return payload["results"]
        return payload if isinstance(payload, dict) else {}

    async def devices(self) -> list[dict]:
        return _list_from(await self._get("/devices"))

    async def chats(self, *, device_id: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        payload = await self._get("/chats", params={"limit": limit, "offset": offset}, device_id=device_id)
        return _list_from(payload)

    async def chat_messages(
        self,
        chat_jid: str,
        *,
        device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        start_time: str | None = None,
    ) -> tuple[list[dict], dict]:
        payload = await self._get(
            f"/chat/{chat_jid}/messages",
            params={"limit": limit, "offset": offset, "start_time": start_time},
            device_id=device_id,
        )
        return _list_from(payload), _nested(payload, "chat_info")


# ---------------------------------------------------------------------------
# conversão de payloads em registros internos
# ---------------------------------------------------------------------------
def record_from_storage(item: dict, chat_jid: str, device_id: str = "") -> dict | None:
    """Converte um item de /chat/{jid}/messages em registro da tabela messages."""
    jid = normalize_jid(item.get("chat_jid") or chat_jid)
    sent = parse_ts(item.get("timestamp") or item.get("created_at"))
    if not jid or sent is None:
        return None

    media_type = (item.get("media_type") or "").strip()
    body = (item.get("content") or item.get("body") or "").strip()
    if not body and media_type:
        body = f"[{media_type}]"
    if not body:
        return None

    direction = "out" if item.get("is_from_me") else "in"
    msg_id = item.get("id") or item.get("message_id") or ""
    return {
        "msg_uid": f"gowa:{msg_id}" if msg_id else "",
        "chat_jid": jid,
        "sender_jid": normalize_jid(item.get("sender_jid") or item.get("from")),
        "sender_name": item.get("sender_display_name") or "",
        "direction": direction,
        "msg_type": media_type or "text",
        "body": body,
        "media_type": media_type,
        "source": "gowa",
        "device_id": device_id or "",
        "sent_at": iso(sent),
    }


def record_from_webhook(payload: dict, device_id: str = "") -> dict | None:
    """Converte o payload de webhook 'message' em registro interno."""
    jid = normalize_jid(payload.get("chat_id") or payload.get("from"))
    sent = parse_ts(payload.get("timestamp"))
    if not jid or sent is None:
        return None

    body = (payload.get("body") or "").strip()
    media_type = ""
    for key in ("image", "video", "audio", "document", "sticker"):
        value = payload.get(key)
        if isinstance(value, dict):
            media_type = key
            if not body:
                body = (value.get("caption") or "").strip()
        elif isinstance(value, str) and value:
            media_type = key
    if not body and media_type:
        body = f"[{media_type}]"
    if not body:
        return None

    direction = "out" if payload.get("is_from_me") else "in"
    msg_id = payload.get("id") or ""
    return {
        "msg_uid": f"gowa:{msg_id}" if msg_id else "",
        "chat_jid": jid,
        "sender_jid": normalize_jid(payload.get("participant") or payload.get("from")),
        "sender_name": payload.get("from_name") or payload.get("sender_display_name") or "",
        "direction": direction,
        "msg_type": media_type or "text",
        "body": body,
        "media_type": media_type,
        "source": "webhook",
        "device_id": device_id or "",
        "sent_at": iso(sent),
    }


# ---------------------------------------------------------------------------
# sincronização
# ---------------------------------------------------------------------------
class SyncService:
    """Puxa o histórico recente do GOWA para o banco do monitor."""

    def __init__(self, client: GowaClient | None = None):
        self.client = client or GowaClient()
        self._lock = asyncio.Lock()

    async def sync(
        self,
        *,
        days: int | None = None,
        device_id: str | None = None,
        chat_jid: str | None = None,
        trigger: str = "manual",
    ) -> dict:
        if self._lock.locked():
            return {"ok": False, "skipped": True, "detail": "sincronizacao ja em andamento"}
        async with self._lock:
            return await self._run(
                days=days or settings.sync_window_days,
                device_id=device_id,
                chat_jid=chat_jid,
                trigger=trigger,
            )

    async def _run(
        self,
        *,
        days: int,
        device_id: str | None,
        chat_jid: str | None,
        trigger: str,
    ) -> dict:
        days = max(1, int(days))
        started_at = utc_now()
        since_dt = started_at - timedelta(days=days)
        since_iso = iso(since_dt)
        scope = normalize_jid(chat_jid) if chat_jid else (device_id or "todos")
        run_id = db.start_sync_run(trigger, scope)

        stats: dict = {
            "ok": True,
            "skipped": False,
            "trigger": trigger,
            "days": days,
            "since": since_iso,
            "devices": 0,
            "chats_scanned": 0,
            "chats_skipped": 0,
            "messages_fetched": 0,
            "messages_inserted": 0,
            "duration_ms": 0,
            "detail": "",
            "chats": [],
        }

        try:
            devices = await self._resolve_devices(device_id)
            stats["devices"] = len(devices)
            alvo = normalize_jid(chat_jid) if chat_jid else ""

            targets: list[dict] = []
            for device in devices:
                for jid, name, last_time in await self._collect_chats(device):
                    if alvo and jid != alvo:
                        continue
                    if not settings.sync_include_groups and is_group(jid):
                        stats["chats_skipped"] += 1
                        continue
                    last_dt = parse_ts(last_time)
                    if last_dt is not None and last_dt < since_dt and not alvo:
                        stats["chats_skipped"] += 1
                        continue
                    db.upsert_contact(
                        jid,
                        name=name or None,
                        phone=phone_from_jid(jid),
                        is_group=is_group(jid),
                        device_id=device or "",
                    )
                    targets.append({"jid": jid, "device": device, "name": name})
                    stats["chats_scanned"] += 1

            if targets:
                semaphore = asyncio.Semaphore(4)

                async def tarefa(target: dict) -> dict:
                    async with semaphore:
                        return await self._sync_chat(target["jid"], target["device"], since_iso)

                results = await asyncio.gather(*(tarefa(t) for t in targets), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        stats["detail"] = (stats["detail"] + f" | {result}")[:500]
                        log.warning("falha ao sincronizar conversa: %s", result)
                        continue
                    stats["messages_fetched"] += result["fetched"]
                    stats["messages_inserted"] += result["inserted"]
                    if len(stats["chats"]) < 200:
                        stats["chats"].append(result)

            log.info(
                "sync[%s]: %s chats, %s mensagens novas (de %s lidas) em %s ms",
                trigger,
                stats["chats_scanned"],
                stats["messages_inserted"],
                stats["messages_fetched"],
                stats["duration_ms"],
            )
        except GowaError as exc:
            stats["ok"] = False
            stats["detail"] = str(exc)[:500]
            log.warning("sincronizacao falhou (GOWA): %s", exc)
        except Exception as exc:  # noqa: BLE001 - queremos registrar qualquer falha
            stats["ok"] = False
            stats["detail"] = f"erro inesperado: {exc}"[:500]
            log.exception("sincronizacao falhou")

        stats["duration_ms"] = int((utc_now() - started_at).total_seconds() * 1000)
        db.finish_sync_run(run_id, stats, ok=stats["ok"], detail=stats["detail"])
        return stats

    async def _resolve_devices(self, device_id: str | None) -> list[str | None]:
        if device_id:
            return [device_id]
        if settings.gowa_device_id:
            return [settings.gowa_device_id]
        try:
            devices = await self.client.devices()
        except GowaError as exc:
            log.warning("nao foi possivel listar devices do GOWA: %s", exc)
            return [None]
        ids: list[str] = []
        for device in devices:
            state = (device.get("state") or "").lower()
            ident = device.get("id") or device.get("device_id") or device.get("device")
            if ident and state in {"", "logged_in", "connected"}:
                ids.append(str(ident))
        return ids or [None]

    async def _collect_chats(self, device_id: str | None) -> list[tuple[str, str, str]]:
        chats: dict[str, tuple[str, str, str]] = {}
        offset = 0
        while len(chats) < settings.sync_max_chats:
            try:
                items = await self.client.chats(device_id=device_id, limit=100, offset=offset)
            except GowaError as exc:
                log.warning("falha ao listar chats do GOWA: %s", exc)
                break
            if not items:
                break
            for item in items:
                jid = normalize_jid(item.get("jid") or item.get("chat_jid"))
                if not jid or jid in SKIP_JIDS or jid.endswith("@broadcast"):
                    continue
                name = (item.get("name") or "").strip()
                last_time = item.get("last_message_time") or ""
                chats.setdefault(jid, (jid, name, last_time))
            if len(items) < 100:
                break
            offset += len(items)
        return list(chats.values())

    async def _sync_chat(self, chat_jid: str, device_id: str | None, since_iso: str) -> dict:
        fetched = 0
        inserted = 0
        offset = 0
        for _page in range(max(1, settings.sync_max_pages)):
            items, info = await self.client.chat_messages(
                chat_jid,
                device_id=device_id,
                limit=settings.sync_page_size,
                offset=offset,
                start_time=since_iso,
            )
            if not items:
                break
            fetched += len(items)
            records = [
                record
                for record in (record_from_storage(item, chat_jid, device_id or "") for item in items)
                if record
            ]
            inserted += db.insert_messages(records)
            name = (info.get("name") or "").strip()
            if name:
                db.upsert_contact(
                    chat_jid,
                    name=name,
                    phone=phone_from_jid(chat_jid),
                    is_group=is_group(chat_jid),
                    device_id=device_id or "",
                )
            if len(items) < settings.sync_page_size:
                break
            offset += len(items)
        return {"chat_jid": chat_jid, "fetched": fetched, "inserted": inserted}


# ---------------------------------------------------------------------------
# singletons
# ---------------------------------------------------------------------------
_client: GowaClient | None = None
_service: SyncService | None = None


def get_gowa_client() -> GowaClient:
    global _client
    if _client is None:
        _client = GowaClient()
    return _client


def get_sync_service() -> SyncService:
    global _service
    if _service is None:
        _service = SyncService(get_gowa_client())
    return _service


async def gowa_status() -> dict:
    """Resumo de saúde do GOWA para o /health e para o painel."""
    client = get_gowa_client()
    status: dict = {"url": client.base_url, "reachable": False, "version": None, "devices": 0}
    if await client.health():
        status["reachable"] = True
        try:
            status["version"] = (await client.app_info()).get("version")
        except GowaError:
            pass
        try:
            status["devices"] = len(await client.devices())
        except GowaError:
            pass
    return status