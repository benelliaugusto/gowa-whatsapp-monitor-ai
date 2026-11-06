"""Agente de IA em cron: lê o dia no monitor, analisa com um LLM e publica de volta.

Execução (serviço Cron do Easypanel):

    python -m app.agent --date $(date +%F) --notify

Variáveis de ambiente:
    MONITOR_API_URL   (padrão http://127.0.0.1:8000)
    MONITOR_API_KEY   (chave com escopo rw)
    LLM_PROVIDER      anthropic | openai | opencode (padrão anthropic)
    LLM_API_KEY       chave do provedor (no OpenCode: chave do Console/Go)
    LLM_MODEL         ex.: claude-sonnet-4-5 / grok-4 / deepseek-v4.1-flash
    LLM_BASE_URL      sobrescreve a URL (padrão https://api.openai.com/v1;
                      opencode usa https://opencode.ai/inference/go)

OpenCode Go (assinatura) também é suportado: use LLM_PROVIDER=opencode com a chave
do Console. O agente envia User-Agent próprio e o header x-opencode-session, como o
gateway exige, e registra o custo reportado em usage.cost.

Fluxo:
    GET  /api/v1/digest          -> conversas do dia em texto
    POST /api/v1/analyses        -> uma análise por conversa (upsert)
    POST /api/v1/tasks           -> pendências a partir de action_items
    POST /api/v1/reports         -> relatório do gestor (idempotente)
    POST /api/v1/agent-runs      -> telemetria da execução (aparece no /status)
    POST /api/v1/notify/manager  -> (opcional) resumo por WhatsApp
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date as date_cls
from datetime import datetime
from typing import Any

import httpx

MONITOR_URL = os.getenv("MONITOR_API_URL", "http://127.0.0.1:8000").rstrip("/")
MONITOR_KEY = os.getenv("MONITOR_API_KEY", "")
PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
_MODELOS_PADRAO = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "grok-4",
    "opencode": "deepseek-v4.1-flash",
    "opencode-go": "deepseek-v4.1-flash",
}
MODEL = os.getenv("LLM_MODEL", "").strip() or _MODELOS_PADRAO.get(PROVIDER, "claude-sonnet-4-5")
LLM_KEY = os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/") or "https://api.openai.com/v1"

# OpenCode Go/Console: gateway compatível com OpenAI em /inference/go (assinatura Go)
# ou /inference (pay-as-you-go). Requer User-Agent próprio e x-opencode-session estável.
OPENCODE_BASE = os.getenv("LLM_BASE_URL", "").rstrip("/") or "https://opencode.ai/inference/go"
USER_AGENT = os.getenv(
    "LLM_USER_AGENT",
    "monitor-ia-agent/2.1 (+https://github.com/benelliaugusto/gowa-whatsapp-monitor-ai)",
)
SESSION_ID = os.getenv("OPENCODE_SESSION_ID", "").strip() or f"monitor-ia-{date_cls.today().isoformat()}"
LLM_MAX_TENTATIVAS = max(1, int(os.getenv("LLM_MAX_RETRIES", "3")))
_fallbacks = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]
if not _fallbacks and PROVIDER in {"opencode", "opencode-go", "opencode_go"}:
    _fallbacks = ["glm-5.3-flash"]
FALLBACK_MODELS = _fallbacks
SISTEMA = "Voce e um analista de conversas de clinica. Responda SEMPRE apenas JSON valido, sem markdown."

_CUSTO_TOTAL = 0.0


def custo_total() -> float:
    return _CUSTO_TOTAL


def _registrar_custo(dados: dict) -> None:
    """Acumula o custo reportado pelo gateway (usage.cost ou opencode.list_cost)."""
    global _CUSTO_TOTAL
    try:
        uso = dados.get("usage") or {}
        valor = uso.get("cost")
        if valor is None:
            valor = (uso.get("opencode") or {}).get("list_cost")
        if valor:
            _CUSTO_TOTAL += float(valor)
    except (TypeError, ValueError, AttributeError):
        pass
LOTE_CHARS = int(os.getenv("AGENT_BATCH_CHARS", "24000"))
MAX_TAREFAS = int(os.getenv("AGENT_MAX_TASKS", "10"))

SCHEMA_ANALISE = """{
  "chat_jid": "string (copie exatamente do digest)",
  "classification": "lead_quente | lead_morno | lead_frio | agendado | paciente_em_retorno | suporte | spam",
  "sentiment": "positivo | neutro | negativo",
  "intent": "agendamento | duvida_preco | remarcacao | cancelamento | informacao | pos_consulta | outro",
  "summary": "1 a 3 frases objetivas, sem inventar",
  "next_step": "acao concreta com quem/quando",
  "risk_flags": ["reclamacao", "urgencia", "risco_abandono", "no_show"],
  "score": 0,
  "stage": "novo | qualificado | agendado | atendido | fechado | perdido"
}"""


def log(msg: str) -> None:
    print(f"[agent] {msg}", flush=True)


def headers() -> dict[str, str]:
    return {"X-API-Key": MONITOR_KEY, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# monitor-ia
# ---------------------------------------------------------------------------
def _com_retry(acao, descricao: str, tentativas: int = 3):
    """Executa uma chamada HTTP ao monitor com retry em falhas transitórias."""
    ultimo: Exception | None = None
    for tentativa in range(1, tentativas + 1):
        try:
            return acao()
        except (httpx.HTTPError, RuntimeError) as exc:
            ultimo = exc
            if tentativa < tentativas:
                espera = 2 ** (tentativa - 1)
                log(f"monitor indisponivel em {descricao} ({exc}); tentativa {tentativa}/{tentativas}, aguardando {espera}s")
                time.sleep(espera)
    raise RuntimeError(f"falha em {descricao}: {ultimo}")


def obter_digest(cliente: httpx.Client, dia: str, max_chars: int) -> dict:
    def buscar():
        resposta = cliente.get(
            f"{MONITOR_URL}/api/v1/digest",
            params={"date": dia, "max_chars": max_chars},
            headers=headers(),
            timeout=120,
        )
        resposta.raise_for_status()
        return resposta.json()

    return _com_retry(buscar, "GET /api/v1/digest")


def postar(cliente: httpx.Client, caminho: str, payload: dict, timeout: int = 60) -> httpx.Response:
    def enviar():
        resposta = cliente.post(f"{MONITOR_URL}{caminho}", json=payload, headers=headers(), timeout=timeout)
        if resposta.status_code >= 500:
            raise RuntimeError(f"HTTP {resposta.status_code} em {caminho}")
        return resposta

    return _com_retry(enviar, caminho)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class ErroTransitorio(RuntimeError):
    """Falha temporária do provedor (429/5xx/rede): vale tentar novamente."""


def _chamar_provedor(modelo: str, prompt: str, max_tokens: int) -> str:
    if PROVIDER in {"opencode", "opencode-go", "opencode_go"}:
        resposta = httpx.post(
            f"{OPENCODE_BASE}/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_KEY}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "x-opencode-session": SESSION_ID,
            },
            json={
                "model": modelo,
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SISTEMA},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=300,
        )
    elif PROVIDER == "anthropic":
        resposta = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": LLM_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": modelo, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
            timeout=240,
        )
    else:
        resposta = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json={
                "model": modelo,
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SISTEMA},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=240,
        )

    if resposta.status_code >= 400:
        trecho = resposta.text[:500]
        if resposta.status_code in {408, 409, 425, 429} or resposta.status_code >= 500:
            raise ErroTransitorio(f"{resposta.status_code}: {trecho}")
        raise RuntimeError(f"{resposta.status_code}: {trecho}")

    dados = resposta.json()
    _registrar_custo(dados)
    if PROVIDER == "anthropic":
        blocos = dados.get("content") or []
        return "".join(b.get("text", "") for b in blocos if isinstance(b, dict))
    return (dados.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def chamar_llm(prompt: str, max_tokens: int = 8000) -> str:
    """Chama o LLM com retry exponencial e fallback de modelos.

    Gateways como o OpenCode podem devolver 429/5xx quando o upstream está
    saturado. Tentamos de novo com backoff e, se persistir, trocamos para o
    próximo modelo de LLM_FALLBACK_MODELS (padrão no OpenCode: glm-5.3-flash).
    """
    if not LLM_KEY:
        raise SystemExit("defina LLM_API_KEY (ou ANTHROPIC_API_KEY / OPENAI_API_KEY)")

    modelos = [MODEL] + [m for m in FALLBACK_MODELS if m and m != MODEL]
    ultimo_erro: Exception | None = None

    for indice, modelo in enumerate(modelos):
        for tentativa in range(1, LLM_MAX_TENTATIVAS + 1):
            try:
                return _chamar_provedor(modelo, prompt, max_tokens)
            except ErroTransitorio as exc:
                ultimo_erro = exc
                espera = min(60.0, (2 ** (tentativa - 1)) * 2.0 + random.uniform(0, 1.5))
                log(
                    f"LLM indisponivel ({exc}); tentativa {tentativa}/{LLM_MAX_TENTATIVAS} "
                    f"em {modelo}, aguardando {espera:.0f}s"
                )
                if tentativa < LLM_MAX_TENTATIVAS:
                    time.sleep(espera)
        if indice < len(modelos) - 1:
            log(f"trocando de modelo: {modelo} -> {modelos[indice + 1]}")
    raise RuntimeError(f"LLM falhou em todos os modelos/tentativas: {ultimo_erro}")


def extrair_json(texto: str) -> Any:
    texto = (texto or "").strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\n?", "", texto)
        texto = re.sub(r"\n?```$", "", texto)
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim == -1:
        raise ValueError(f"LLM nao devolveu JSON: {texto[:300]}")
    return json.loads(texto[inicio : fim + 1])


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
def prompt_analises(dia: str, trecho: str) -> str:
    return f"""Voce e analista de conversas de WhatsApp de uma clinica. Analise APENAS as conversas
abaixo, do dia {dia}. Nao invente fatos: se algo nao estiver na transcricao, nao afirme.

Para CADA conversa, produza um objeto exatamente com este schema:
{SCHEMA_ANALISE}

Regras:
- "chat_jid" deve ser copiado EXATAMENTE do cabecalho ("chat_jid: ...").
- "Lead" e quem escreveu; "Clinica" e o atendimento.
- score 0-100 = temperatura comercial.
- summary: no maximo 3 frases. next_step: acao concreta e verificavel.
- Se for spam/sem interesse: classification="spam" e score baixo.
- stage = estagio atual do lead no funil: "novo" (acabou de chegar), "qualificado"
  (demonstrou interesse real), "agendado" (horario marcado), "atendido" (ja passou
  pela consulta), "fechado" (tratamento fechado), "perdido" (desistiu/nao respondeu).

Responda SOMENTE JSON no formato: {{"analyses": [ ... ]}}

CONVERSAS:
{trecho}
"""


def prompt_relatorio(dia: str, analises: list[dict], totais: dict, atendimento: dict | None = None) -> str:
    return f"""Voce e analista de dados de uma clinica. Com base nas analises do dia {dia},
escreva o relatorio que o gestor vai ler no painel.

Dados: {json.dumps(totais, ensure_ascii=False)}
Atendimento: {json.dumps(atendimento or {}, ensure_ascii=False)}
Analises: {json.dumps(analises, ensure_ascii=False)}

Responda SOMENTE este JSON:
{{
  "title": "Relatorio diario - DD/MM/AAAA",
  "summary": "2 a 4 linhas de resumo executivo",
  "body_md": "markdown com as secoes: ## Resumo, ## Numeros do dia, ## Oportunidades, ## Atencao / riscos, ## Recomendacoes",
  "metrics": {{"conversas": 0, "leads_quentes": 0, "agendamentos": 0, "sem_resposta": 0}},
  "highlights": ["fatos curtos e concretos"],
  "action_items": ["acao, responsavel e prazo"]
}}

Regras: portugues do Brasil, tom profissional; cite numeros reais; aponte o que precisa
de acao HOJE; nao inclua dados sensiveis (CPF, exames); nao invente metricas.
"""


def fatiar(conversas: list[dict], limite: int = LOTE_CHARS) -> list[str]:
    lotes: list[str] = []
    atual: list[str] = []
    tamanho = 0
    for conversa in conversas:
        bloco = conversa.get("transcript") or ""
        if atual and tamanho + len(bloco) > limite:
            lotes.append("\n\n".join(atual))
            atual, tamanho = [], 0
        atual.append(bloco)
        tamanho += len(bloco)
    if atual:
        lotes.append("\n\n".join(atual))
    return lotes


# ---------------------------------------------------------------------------
# execução
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Agente de análise do monitor-ia (cron)")
    parser.add_argument("--date", default=date_cls.today().isoformat(), help="dia YYYY-MM-DD")
    parser.add_argument("--max-chars", type=int, default=60000, help="orçamento de caracteres do digest")
    parser.add_argument("--dry-run", action="store_true", help="não grava nada no monitor")
    parser.add_argument("--notify", action="store_true", help="envia resumo ao gestor pelo WhatsApp")
    parser.add_argument("--skip-tasks", action="store_true", help="não cria tarefas")
    args = parser.parse_args()
    dia = args.date

    if not MONITOR_KEY:
        raise SystemExit("defina MONITOR_API_KEY (chave rw do monitor)")

    log(f"monitor={MONITOR_URL} dia={dia} modelo={MODEL} ({PROVIDER})")
    inicio = datetime.now().isoformat(timespec="seconds")
    analises: list[dict] = []
    relatorio_id = None
    ok = False
    detalhe = ""

    try:
        with httpx.Client() as cliente:
            digest = obter_digest(cliente, dia, args.max_chars)
            conversas = digest.get("conversations") or []
            totais = digest.get("totals") or {}
            log(f"digest: {len(conversas)} conversa(s), {totais.get('messages', 0)} mensagem(ns)")

            if not conversas:
                detalhe = "sem conversas no dia"
                log("sem conversas no dia — nada a publicar")
                ok = True
                return 0

            for indice, trecho in enumerate(fatiar(conversas), start=1):
                log(f"analisando lote {indice} ({len(trecho)} chars)...")
                dados = extrair_json(chamar_llm(prompt_analises(dia, trecho)))
                lote = dados.get("analyses") or []
                analises.extend(lote)
                log(f"  {len(lote)} analise(s)")

            if not analises:
                detalhe = "LLM nao devolveu analises"
                log("nenhuma analise gerada; abortando publicacao")
                return 1

            log("gerando relatorio...")
            relatorio = extrair_json(
                chamar_llm(
                    prompt_relatorio(dia, analises, totais, digest.get("attendance")),
                    max_tokens=6000,
                )
            )
            relatorio.setdefault("kind", "daily")
            relatorio["period_start"] = dia
            relatorio["period_end"] = dia
            relatorio.setdefault("title", f"Relatorio diario - {dia}")
            relatorio["agent"] = "app.agent"
            relatorio["model"] = MODEL
            relatorio["idempotency_key"] = f"daily-{dia}"

            if args.dry_run:
                print(json.dumps({"analyses": analises, "report": relatorio}, ensure_ascii=False, indent=2))
                ok = True
                return 0

            for analise in analises:
                analise["analysis_date"] = dia
                analise["agent"] = "app.agent"
                analise["model"] = MODEL
                resposta = postar(cliente, "/api/v1/analyses", analise)
                if resposta.status_code >= 400:
                    log(f"  ! analise recusada ({resposta.status_code}): {resposta.text[:200]}")
                else:
                    log(f"  + analise: {analise.get('chat_jid')} [{analise.get('classification')}]")

            resposta = postar(cliente, "/api/v1/reports", relatorio, timeout=120)
            criado_agora = resposta.status_code == 201
            if resposta.status_code >= 400:
                raise RuntimeError(f"relatorio recusado ({resposta.status_code}): {resposta.text[:300]}")
            corpo = resposta.json()
            relatorio_id = corpo.get("id")
            log(f"  + relatorio id={relatorio_id} ({'criado' if criado_agora else 'ja existia'})")

            if not args.skip_tasks and criado_agora:
                for item in (relatorio.get("action_items") or [])[:MAX_TAREFAS]:
                    tarefa = {
                        "title": str(item)[:200],
                        "details": "Gerada a partir do relatório do dia.",
                        "owner": "recepcao",
                        "due_at": dia,
                        "priority": 2,
                        "source": "report",
                        "report_id": relatorio_id,
                    }
                    resp = postar(cliente, "/api/v1/tasks", tarefa)
                    if resp.status_code >= 400:
                        log(f"  ! tarefa recusada ({resp.status_code}): {resp.text[:120]}")
                log(f"  + tarefas criadas a partir de action_items")

            if args.notify:
                resp = postar(cliente, "/api/v1/notify/manager", {"day": dia}, timeout=60)
                if resp.status_code >= 400:
                    dica = ""
                    if resp.status_code == 422:
                        dica = " (ajuste MANAGER_WHATSAPP: numero real, so digitos com DDI, ex.: 5511999999999)"
                    log(f"  ! aviso ao gestor falhou ({resp.status_code}){dica}: {resp.text[:200]}")
                else:
                    log("  + resumo enviado ao gestor pelo WhatsApp")

            ok = True
            detalhe = f"{len(analises)} analises"
            if custo_total():
                detalhe += f" | custo US$ {custo_total():.4f}"
    except Exception as exc:  # noqa: BLE001 - sempre registra a execução
        detalhe = f"{type(exc).__name__}: {exc}"[:500]
        log(f"ERRO: {detalhe}")
        ok = False
    finally:
        if not args.dry_run:
            try:
                payload = {
                    "day": dia,
                    "provider": PROVIDER,
                    "model": MODEL,
                    "ok": ok,
                    "analyses": len(analises),
                    "report_id": relatorio_id,
                    "detail": detalhe,
                }
                with httpx.Client() as cliente:
                    postar(cliente, "/api/v1/agent-runs", payload, timeout=30)
            except Exception:  # noqa: BLE001
                log("aviso: nao foi possivel registrar a execucao em /agent-runs")
        log(f"fim: ok={ok} inicio={inicio}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())