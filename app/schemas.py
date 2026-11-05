"""Contrato da API pública /api/v1 — é o que os agentes de IA leem e escrevem."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    msg_uid: str
    chat_jid: str
    sender_jid: str | None = None
    sender_name: str | None = None
    direction: Literal["in", "out"]
    msg_type: str | None = None
    body: str | None = None
    media_type: str | None = None
    sent_at: str
    local_date: str
    local_hour: int | None = None


class ConversationOut(BaseModel):
    chat_jid: str
    name: str = ""
    phone: str = ""
    is_group: bool = False
    message_count: int = 0
    inbound: int = 0
    outbound: int = 0
    first_message_at: str | None = None
    last_message_at: str | None = None
    last_preview: str | None = None
    classification: str | None = None
    sentiment: str | None = None
    score: int | None = None
    analysis_summary: str | None = None


class DigestConversation(ConversationOut):
    transcript: str = Field(..., description="Transcrição compacta, no formato [HH:MM] Autor: mensagem")
    messages_included: int = 0
    truncated: bool = Field(default=False, description="True quando a transcrição foi cortada por tamanho")


class DigestTotals(BaseModel):
    conversations: int = 0
    messages: int = 0
    inbound: int = 0
    outbound: int = 0
    new_conversations: int = 0


class DigestOut(BaseModel):
    date: str = Field(..., description="Dia analisado (fuso da clínica)")
    timezone: str
    generated_at: str
    totals: DigestTotals
    conversations: list[DigestConversation]
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


class AnalysisIn(BaseModel):
    chat_jid: str = Field(..., description="JID do WhatsApp, ex.: 5511999999999@s.whatsapp.net")
    analysis_date: date | None = Field(
        default=None, description="Dia da análise (YYYY-MM-DD no fuso da clínica). Vazio = hoje."
    )
    classification: str | None = Field(
        default=None,
        description="Ex.: lead_quente, lead_morno, lead_frio, agendado, paciente_em_retorno, suporte, spam",
        examples=["lead_quente"],
    )
    sentiment: Literal["positivo", "neutro", "negativo"] | None = None
    intent: str | None = Field(default=None, examples=["agendamento", "duvida_preco", "remarcacao"])
    summary: str = Field(..., min_length=3, max_length=4000, description="Resumo da conversa em 1-3 frases")
    next_step: str | None = Field(default=None, max_length=2000, description="Próxima ação recomendada")
    risk_flags: list[str] = Field(default_factory=list, description="Ex.: reclamacao, risco_abandono, urgencia")
    score: int | None = Field(default=None, ge=0, le=100, description="Temperatura do lead (0 = frio, 100 = quente)")
    extra: dict[str, Any] | None = Field(default=None, description="Campo livre para dados adicionais")
    agent: str | None = Field(default=None, description="Agente que gerou a análise: claude-code, grok, n8n...")
    model: str | None = Field(default=None, description="Modelo usado: claude-sonnet-4.5, grok-4...")


class AnalysisOut(AnalysisIn):
    id: int
    analysis_date: date
    created_at: str
    updated_at: str


class ReportIn(BaseModel):
    kind: Literal["daily", "weekly", "custom"] = "daily"
    period_start: date
    period_end: date | None = None
    title: str = Field(..., min_length=3, max_length=200)
    summary: str | None = Field(default=None, max_length=4000, description="Resumo executivo de 2-4 linhas")
    body_md: str = Field(..., min_length=10, description="Relatório completo em Markdown (é o que o gestor vê)")
    metrics: dict[str, Any] | None = Field(default=None, description="Números do dia: conversas, leads, agendamentos...")
    highlights: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    status: Literal["draft", "published"] = "published"
    agent: str | None = None
    model: str | None = None
    idempotency_key: str | None = Field(
        default=None,
        description="Chave única. Se repetir, a API devolve o relatório já existente em vez de duplicar.",
    )


class ReportPatch(BaseModel):
    kind: Literal["daily", "weekly", "custom"] | None = None
    period_start: date | None = None
    period_end: date | None = None
    title: str | None = Field(default=None, min_length=3, max_length=200)
    summary: str | None = None
    body_md: str | None = Field(default=None, min_length=10)
    metrics: dict[str, Any] | None = None
    highlights: list[str] | None = None
    action_items: list[str] | None = None
    status: Literal["draft", "published"] | None = None
    agent: str | None = None
    model: str | None = None


class ReportOut(BaseModel):
    id: int
    idempotency_key: str | None = None
    kind: str
    period_start: str
    period_end: str
    title: str
    summary: str | None = None
    body_md: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    highlights: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    status: str
    agent: str | None = None
    model: str | None = None
    created_at: str
    updated_at: str


class ReportListOut(BaseModel):
    total: int
    items: list[ReportOut]


class SyncIn(BaseModel):
    days: int | None = Field(default=None, ge=1, le=30, description="Quantos dias para trás buscar (padrão: SYNC_WINDOW_DAYS)")
    chat_jid: str | None = Field(default=None, description="Sincronizar apenas uma conversa")
    device_id: str | None = Field(default=None, description="Slot do GOWA (multi-device)")


class SyncChatResult(BaseModel):
    chat_jid: str
    fetched: int = 0
    inserted: int = 0


class SyncOut(BaseModel):
    ok: bool
    skipped: bool = False
    trigger: str | None = None
    days: int | None = None
    since: str | None = None
    devices: int = 0
    chats_scanned: int = 0
    chats_skipped: int = 0
    messages_fetched: int = 0
    messages_inserted: int = 0
    duration_ms: int = 0
    detail: str = ""
    chats: list[SyncChatResult] = Field(default_factory=list)


class SyncRunOut(BaseModel):
    id: int
    trigger: str | None = None
    scope: str | None = None
    started_at: str
    finished_at: str | None = None
    chats_scanned: int = 0
    messages_fetched: int = 0
    messages_inserted: int = 0
    ok: int = 1
    detail: str | None = None


class HealthOut(BaseModel):
    status: str
    app: str
    version: str
    timezone: str
    now: str
    database: dict[str, Any]
    gowa: dict[str, Any]
    sync: dict[str, Any]