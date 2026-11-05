"""Autenticação: API keys dos agentes de IA, basic auth do painel e HMAC do webhook."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Mapping

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import settings

log = logging.getLogger(__name__)
_basic = HTTPBasic(auto_error=False)

# Cabeçalhos que o GOWA pode usar para assinar o webhook (documentado: X-Hub-Signature-256).
SIGNATURE_HEADERS = ("X-Hub-Signature-256", "X-Hub-Signature", "X-Signature")


@dataclass(frozen=True)
class ApiKey:
    name: str
    scope: str  # "rw" | "ro"

    @property
    def can_write(self) -> bool:
        return self.scope == "rw"


def _match(candidate: str) -> dict | None:
    if not candidate:
        return None
    for entry in settings.api_key_entries():
        if hmac.compare_digest(entry["key"], candidate):
            return entry
    return None


def _extract_token(x_api_key: str | None, authorization: str | None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() in {"bearer", "token"} and token.strip():
            return token.strip()
    return ""


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> ApiKey:
    """Dependência de leitura: exige uma API key válida (X-API-Key ou Bearer)."""
    if not settings.require_api_key:
        request.state.api_key_name = "desativado"
        return ApiKey(name="desativado", scope="rw")

    token = _extract_token(x_api_key, authorization)
    entry = _match(token)
    if entry is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthorized",
                "message": "Informe a API key em X-API-Key ou Authorization: Bearer <chave>.",
            },
        )
    request.state.api_key_name = entry["name"]
    return ApiKey(name=entry["name"], scope=entry["scope"])


async def require_write_key(key: ApiKey = Depends(require_api_key)) -> ApiKey:
    """Dependência de escrita: exige API key com escopo rw."""
    if not key.can_write:
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "Esta API key é somente leitura (ro)."},
        )
    return key


async def dashboard_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """Protege o painel HTML quando DASHBOARD_PASSWORD está definido."""
    if not settings.dashboard_password:
        return
    if credentials is None or not (
        hmac.compare_digest(credentials.username, settings.dashboard_user)
        and hmac.compare_digest(credentials.password, settings.dashboard_password)
    ):
        raise HTTPException(
            status_code=401,
            detail="Credenciais inválidas",
            headers={"WWW-Authenticate": 'Basic realm="monitor-ia"'},
        )


def verify_webhook_signature(raw_body: bytes, headers: Mapping[str, str]) -> None:
    """Valida o HMAC-SHA256 do corpo cru, no formato "sha256=<hex>" (X-Hub-Signature-256)."""
    secret = settings.gowa_webhook_secret
    if not secret:
        log.warning("GOWA_WEBHOOK_SECRET vazio: webhook aceito sem verificação de assinatura")
        return

    provided = ""
    for name in SIGNATURE_HEADERS:
        value = headers.get(name) or headers.get(name.lower())
        if value:
            provided = value.strip()
            break
    if not provided:
        raise HTTPException(status_code=401, detail="Assinatura do webhook ausente")

    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided.lower()):
        raise HTTPException(status_code=401, detail="Assinatura do webhook inválida")