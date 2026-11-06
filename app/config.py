"""Configuração do monitor-ia (12-factor: tudo por variável de ambiente).

As variáveis estão documentadas em .env.example e no README.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- aplicação ------------------------------------------------------
    app_name: str = "monitor-ia"
    app_version: str = "2.0.0"
    timezone: str = "America/Sao_Paulo"
    database_path: str = "/data/monitor.db"
    log_level: str = "INFO"

    # --- GOWA -----------------------------------------------------------
    gowa_url: str = "http://gowa-server:3000"
    gowa_basic_auth: str = ""          # "usuario:senha" (mesmo APP_BASIC_AUTH do GOWA)
    gowa_device_id: str = ""           # slot padrão; vazio = descobre em GET /devices
    gowa_timeout: float = 20.0
    gowa_webhook_secret: str = ""      # mesmo WHATSAPP_WEBHOOK_SECRET do GOWA
    gowa_webhook_token: str = ""       # alternativa ao HMAC: ?token=... na URL do webhook

    # --- sincronização automática do histórico ---------------------------
    sync_enabled: bool = True
    sync_on_startup: bool = True
    sync_interval_minutes: int = 15
    sync_window_days: int = 3
    sync_page_size: int = 100
    sync_max_pages: int = 6
    sync_max_chats: int = 300
    sync_include_groups: bool = False

    # --- API para os agentes de IA --------------------------------------
    api_keys: str = ""                 # "claude:rw:chave,grok:ro:chave"
    require_api_key: bool = True

    # --- painel do gestor ------------------------------------------------
    dashboard_user: str = "gestor"
    dashboard_password: str = ""       # vazio = painel sem senha (só use atrás de proxy)

    # --- transcrição local de áudios (faster-whisper) --------------------
    transcribe_enabled: bool = True
    whisper_model: str = "small"          # tiny | base | small | medium | large-v3
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_model_dir: str = "/data/models"
    whisper_language: str = "pt"
    whisper_max_seconds: int = 300
    whisper_interval_seconds: int = 60    # de quanto em quanto tempo busca áudios pendentes

    # --- horário comercial (métricas e alertas) --------------------------
    business_hours_start: str = "08:00"
    business_hours_end: str = "18:00"
    business_days: str = "1,2,3,4,5"      # 0=domingo ... 6=sábado

    # --- avisos ao gestor ------------------------------------------------
    manager_whatsapp: str = ""            # ex.: 5511999999999 (só dígitos com DDI)
    manager_name: str = ""

    # --- backup ----------------------------------------------------------
    backup_dir: str = "/data/backups"
    backup_keep: int = 14

    # --- agente de IA em cron (app/agent.py) -----------------------------
    llm_provider: str = "anthropic"       # anthropic | openai
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    monitor_api_url: str = "http://127.0.0.1:8000"
    monitor_api_key: str = ""

    # --- digest ----------------------------------------------------------
    digest_max_chars: int = 60000

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def api_key_entries(self) -> list[dict]:
        """Interpreta API_KEYS (itens separados por vírgula).

        Formatos aceitos:
          - "nome:rw:chave" -> leitura + escrita
          - "nome:ro:chave" -> somente leitura
          - "chave"         -> nome=default, rw
        """
        entries: list[dict] = []
        for item in (self.api_keys or "").split(","):
            item = item.strip()
            if not item:
                continue
            parts = item.split(":", 2)
            if len(parts) == 3:
                name, scope, key = parts
            elif len(parts) == 2:
                name, scope, key = parts[0], "rw", parts[1]
            else:
                name, scope, key = "default", "rw", item
            scope = scope.strip().lower()
            if scope not in {"rw", "ro"}:
                scope = "rw"
            key = key.strip()
            if key:
                entries.append({"name": name.strip() or "default", "scope": scope, "key": key})
        return entries


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()