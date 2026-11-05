"""Agente de exemplo do monitor-ia: lê o dia, analisa com um LLM e publica no monitor.

Suporta Anthropic (Claude) e qualquer endpoint compatível com OpenAI (xAI/Grok,
OpenRouter, Ollama, etc.). O monitor NÃO chama IA: este script é quem pensa e
quem grava as análises/relatórios via API.

Instalação:
    pip install httpx

Configuração (variáveis de ambiente):
    MONITOR_URL=https://monitor.sua-clinica.com.br
    MONITOR_API_KEY=sk_monitor_xxx            # chave com escopo rw

    # Opção A — Claude (Anthropic)
    LLM_PROVIDER=anthropic
    ANTHROPIC_API_KEY=sk-ant-...
    LLM_MODEL=claude-sonnet-4-5

    # Opção B — Grok / xAI (compatível com OpenAI)
    LLM_PROVIDER=openai
    OPENAI_API_KEY=xai-...
    OPENAI_BASE_URL=https://api.x.ai/v1
    LLM_MODEL=grok-4

Uso:
    python examples/agent_claude.py                    # hoje
    python examples/agent_claude.py --date 2026-11-05
    python examples/agent_claude.py --dry-run          # não grava no monitor
    python examples/agent_claude.py --max-chars 40000  # limita o contexto

Saída: 1 análise por conversa (POST /api/v1/analyses) + 1 relatório do dia
(POST /api/v1/reports, idempotente por idempotency_key).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date as date_cls
from datetime import datetime
from typing import Any

import httpx

MONITOR_URL = os.getenv("MONITOR_URL", "http://localhost:8000").rstrip("/")
MONITOR_API_KEY = os.getenv("MONITOR_API_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-5" if LLM_PROVIDER == "anthropic" else "grok-4")
BATCH_CHARS = 24000  # quantos caracteres de transcrição por chamada ao LLM

SCHEMA_ANALISE = """{
  "chat_jid": "string (copie exatamente do digest)",
  "classification": "lead_quente | lead_morno | lead_frio | agendado | paciente_em_retorno | suporte | spam",
  "sentiment": "positivo | neutro | negativo",
  "intent": "agendamento | duvida_preco | remarcacao | cancelamento | informacao | pos_consulta | outro",
  "summary": "1 a 3 frases objetivas, sem inventar",
  "next_step": "acao concreta com quem/quando",
  "risk_flags": ["reclamacao", "urgencia", "risco_abandono", "no_show"],
  "score": 0
}"""


def log(msg: str) -> None:
    print(f"[agente] {msg}", flush=True)


# ---------------------------------------------------------------------------
# monitor-ia
# ---------------------------------------------------------------------------
def headers() -> dict[str, str]:
    return {"X-API-Key": MONITOR_API_KEY, "Content-Type": "application/json"}


def obter_digest(cliente: httpx.Client, dia: str, max_chars: int) -> dict:
    resposta = cliente.get(
        f"{MONITOR_URL}/api/v1/digest",
        params={"date": dia, "max_chars": max_chars},
        headers=headers(),
        timeout=120,
    )
    resposta.raise_for_status()
    return resposta.json()


def enviar_analise(cliente: httpx.Client, analise: dict[str, Any], dia: str) -> None:
    payload = {"analysis_date": dia, "agent": "examples/agent_claude.py", "model": LLM_MODEL, **analise}
    resposta = cliente.post(f"{MONITOR_URL}/api/v1/analyses", json=payload, headers=headers(), timeout=60)
    if resposta.status_code >= 400:
        log(f"  ! análise recusada ({resposta.status_code}): {resposta.text[:200]}")
    else:
        log(f"  + análise salva: {payload['chat_jid']} [{payload.get('classification')}]")


def enviar_relatorio(cliente: httpx.Client, relatorio: dict[str, Any], dia: str) -> None:
    payload = {
        "kind": "daily",
        "period_start": dia,
        "period_end": dia,
        "status": "published",
        "agent": "examples/agent_claude.py",
        "model": LLM_MODEL,
        "idempotency_key": f"daily-{dia}",
        **relatorio,
    }
    resposta = cliente.post(f"{MONITOR_URL}/api/v1/reports", json=payload, headers=headers(), timeout=120)
    if resposta.status_code >= 400:
        log(f"  ! relatório recusado ({resposta.status_code}): {resposta.text[:300]}")
        return
    corpo = resposta.json()
    acao = "criado" if resposta.status_code == 201 else "já existia (idempotência)"
    log(f"  + relatório {acao}: id={corpo['id']} — {corpo['title']}")


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
def chamar_llm(prompt: str, max_tokens: int = 8000) -> str:
    if LLM_PROVIDER == "anthropic":
        chave = os.getenv("ANTHROPIC_API_KEY", "")
        if not chave:
            raise SystemExit("defina ANTHROPIC_API_KEY")
        resposta = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": chave,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=180,
        )
    else:
        chave = os.getenv("OPENAI_API_KEY", "")
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if not chave:
            raise SystemExit("defina OPENAI_API_KEY")
        resposta = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "Voce e um analista de conversas de clinica. Responda SEMPRE apenas JSON valido, sem markdown."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=180,
        )
    if resposta.status_code >= 400:
        raise RuntimeError(f"LLM respondeu {resposta.status_code}: {resposta.text[:500]}")
    dados = resposta.json()
    if LLM_PROVIDER == "anthropic":
        blocos = dados.get("content") or []
        return "".join(b.get("text", "") for b in blocos if isinstance(b, dict))
    return (dados.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def extrair_json(texto: str) -> Any:
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\n?", "", texto)
        texto = re.sub(r"\n?```$", "", texto)
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim == -1:
        raise ValueError(f"LLM não devolveu JSON: {texto[:300]}")
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
- "chat_jid" deve ser copiado EXATAMENTE do cabecalho da conversa ("chat_jid: ...").
- Use "Lead" como quem escreveu e "Clinica" como o atendimento.
- score 0-100 = temperatura comercial (0 frio, 100 pronto para fechar).
- summary: no maximo 3 frases. next_step: acao concreta e verificavel.
- Se a conversa for so spam/sem interesse, classification="spam" e score baixo.

Responda SOMENTE JSON no formato: {{"analyses": [ ... ]}}

CONVERSAS:
{trecho}
"""


def prompt_relatorio(dia: str, analises: list[dict], totais: dict) -> str:
    return f"""Voce e analista de dados de uma clinica. Com base nas analises do dia {dia},
escreva o relatorio que o gestor vai ler no painel.

Dados: {json.dumps(totais, ensure_ascii=False)}
Analises: {json.dumps(analises, ensure_ascii=False)}

Responda SOMENTE este JSON:
{{
  "title": "Relatorio diario - DD/MM/AAAA",
  "summary": "2 a 4 linhas de resumo executivo",
  "body_md": "markdown completo com as secoes: ## Resumo, ## Numeros do dia, ## Oportunidades, ## Atencao / riscos, ## Recomendacoes",
  "metrics": {{"conversas": 0, "leads_quentes": 0, "agendamentos": 0, "sem_resposta": 0}},
  "highlights": ["fatos curtos e concretos"],
  "action_items": ["acao, responsavel e prazo"]
}}

Regras: portugues do Brasil, tom profissional; cite numeros reais; aponte o que precisa
de acao HOJE; nao inclua dados sensiveis (CPF, exames); nao invente metricas.
"""


# ---------------------------------------------------------------------------
# orquestração
# ---------------------------------------------------------------------------
def fatiar(conversas: list[dict], limite: int = BATCH_CHARS) -> list[str]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Agente de análise do monitor-ia")
    parser.add_argument("--date", default=date_cls.today().isoformat(), help="dia YYYY-MM-DD")
    parser.add_argument("--max-chars", type=int, default=60000, help="orçamento de caracteres do digest")
    parser.add_argument("--dry-run", action="store_true", help="não grava no monitor")
    args = parser.parse_args()
    dia = args.date

    if not MONITOR_API_KEY:
        raise SystemExit("defina MONITOR_API_KEY (chave rw do monitor)")

    log(f"monitor: {MONITOR_URL} | dia: {dia} | modelo: {LLM_MODEL} ({LLM_PROVIDER})")
    with httpx.Client() as cliente:
        digest = obter_digest(cliente, dia, args.max_chars)
        conversas = digest.get("conversations") or []
        totais = digest.get("totals") or {}
        log(f"digest: {len(conversas)} conversa(s), {totais.get('messages', 0)} mensagem(ns)")

        if not conversas:
            log("sem conversas no dia — nada a fazer (o monitor não inventa relatório)")
            return 0

        analises: list[dict] = []
        for indice, trecho in enumerate(fatiar(conversas), start=1):
            log(f"analisando lote {indice} ({len(trecho)} chars)...")
            resposta = chamar_llm(prompt_analises(dia, trecho))
            dados = extrair_json(resposta)
            lote = dados.get("analyses") or []
            analises.extend(lote)
            log(f"  {len(lote)} análise(s) geradas")

        if not analises:
            log("LLM não devolveu análises; abortando")
            return 1

        log("gerando relatório do dia...")
        relatorio = extrair_json(chamar_llm(prompt_relatorio(dia, analises, totais), max_tokens=6000))

        if args.dry_run:
            print(json.dumps({"analyses": analises, "report": relatorio}, ensure_ascii=False, indent=2))
            return 0

        for analise in analises:
            enviar_analise(cliente, analise, dia)
        enviar_relatorio(cliente, relatorio, dia)

    log("concluído")
    return 0


if __name__ == "__main__":
    sys.exit(main())