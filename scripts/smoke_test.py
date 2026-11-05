"""Teste de fumaça do monitor-ia (roda sem Docker, com SQLite temporário).

Uso:
    python scripts/smoke_test.py

Ele configura variáveis de ambiente isoladas, cria o banco em ./data/smoke.db,
insere conversas de exemplo e exercita a API /api/v1, o webhook e o painel HTML.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

BANCO = RAIZ / "data" / "smoke.db"
if BANCO.exists():
    BANCO.unlink()
shutil.rmtree(RAIZ / "data", ignore_errors=True)
BANCO.parent.mkdir(parents=True, exist_ok=True)

os.environ.update(
    {
        "DATABASE_PATH": str(BANCO),
        "TIMEZONE": "America/Sao_Paulo",
        "API_KEYS": "teste-rw:rw:chave-rw-de-teste,teste-ro:ro:chave-ro-de-teste",
        "REQUIRE_API_KEY": "true",
        "DASHBOARD_PASSWORD": "",
        "DASHBOARD_USER": "gestor",
        "SYNC_ENABLED": "false",
        "SYNC_ON_STARTUP": "false",
        "SYNC_INTERVAL_MINUTES": "0",
        "GOWA_URL": "http://127.0.0.1:9",
        "GOWA_BASIC_AUTH": "admin:senha",
        "GOWA_WEBHOOK_SECRET": "segredo-de-teste",
        "LOG_LEVEL": "WARNING",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

RW = {"X-API-Key": "chave-rw-de-teste"}
RO = {"X-API-Key": "chave-ro-de-teste"}

FALHAS: list[str] = []
PASSOS = 0


def check(nome: str, condicao: bool, detalhe: str = "") -> None:
    global PASSOS
    PASSOS += 1
    if condicao:
        print(f"  ok   {nome}")
    else:
        FALHAS.append(nome)
        print(f"  FALHA {nome} {detalhe}")


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def popular_banco() -> None:
    agora = agora_utc()
    registros = []
    conversas = {
        "5511988887777@s.whatsapp.net": [
            ("in", "Oi! Queria saber o valor da limpeza de pele", 90),
            ("out", "Ola Maria! A limpeza de pele custa R$ 180. Quer agendar?", 80),
            ("in", "Pode ser sexta as 15h?", 70),
            ("out", "Fechado para sexta 15h! Vou confirmar por aqui.", 60),
        ],
        "5511977776666@s.whatsapp.net": [
            ("in", "Boa tarde, preciso remarcar minha consulta", 40),
            ("out", "Claro! Qual seria a melhor data?", 30),
        ],
    }
    for jid, mensagens in conversas.items():
        for direcao, corpo, minutos in mensagens:
            registros.append(
                {
                    "chat_jid": jid,
                    "sender_jid": jid if direcao == "in" else "5511900000000@s.whatsapp.net",
                    "sender_name": "Maria" if direcao == "in" else "Clinica",
                    "direction": direcao,
                    "body": corpo,
                    "source": "teste",
                    "sent_at": (agora - timedelta(minutes=minutos)).isoformat(),
                }
            )
    db.insert_messages(registros)
    db.upsert_contact("5511988887777@s.whatsapp.net", name="Maria Silva", phone="5511988887777")
    db.upsert_contact("5511977776666@s.whatsapp.net", name="Joao Souza", phone="5511977776666")


def main() -> int:
    with TestClient(app) as cliente:
        popular_banco()
        dia = datetime.now().astimezone().strftime("%Y-%m-%d")
        dia_br = datetime.strptime(dia, "%Y-%m-%d").strftime("%d/%m/%Y")

        print("\n[1] infraestrutura")
        check("GET /healthz", cliente.get("/healthz").status_code == 200)
        check("GET /docs", cliente.get("/docs").status_code == 200)
        check("GET /openapi.json", cliente.get("/openapi.json").status_code == 200)

        resposta = cliente.get("/api/v1/health")
        check("GET /api/v1/health (público)", resposta.status_code == 200, resposta.text[:200])
        corpo = resposta.json()
        check("health.gowa.reachable == False", corpo["gowa"]["reachable"] is False)
        check("health.database.messages >= 6", corpo["database"]["messages"] >= 6)

        print("\n[2] autenticação")
        check("digest sem chave -> 401", cliente.get(f"/api/v1/digest?date={dia}").status_code == 401)
        check("digest chave inválida -> 401", cliente.get(f"/api/v1/digest?date={dia}", headers={"X-API-Key": "x"}).status_code == 401)
        check("digest chave ro -> 200", cliente.get(f"/api/v1/digest?date={dia}", headers=RO).status_code == 200)
        resposta = cliente.post("/api/v1/reports", headers=RO, json={"period_start": dia, "title": "t", "body_md": "corpo do relatorio"})
        check("escrita com chave ro -> 403", resposta.status_code == 403, resposta.text[:200])

        print("\n[3] leitura para a IA")
        resposta = cliente.get(f"/api/v1/digest?date={dia}", headers=RW)
        check("GET /api/v1/digest", resposta.status_code == 200, resposta.text[:300])
        digest = resposta.json()
        check("digest com 2 conversas", len(digest["conversations"]) == 2, str(len(digest["conversations"])))
        check("digest com totais", digest["totals"]["messages"] >= 6)
        check("transcrição com direção", "Lead:" in digest["conversations"][0]["transcript"])
        check("transcrição com resposta da clínica", "Clinica:" in digest["conversations"][0]["transcript"])

        resposta = cliente.get(f"/api/v1/digest?date={dia}&format=text", headers=RO)
        check("digest texto puro", resposta.status_code == 200 and "Digest de conversas" in resposta.text, resposta.text[:200])

        resposta = cliente.get(f"/api/v1/conversations?date={dia}", headers=RO)
        check("GET /api/v1/conversations", resposta.status_code == 200 and len(resposta.json()) == 2, resposta.text[:200])

        resposta = cliente.get(f"/api/v1/conversations/5511988887777%40s.whatsapp.net/messages?date={dia}", headers=RO)
        check("mensagens da conversa", resposta.status_code == 200 and len(resposta.json()) == 4, resposta.text[:200])

        resposta = cliente.get(f"/api/v1/conversations/5511988887777%40s.whatsapp.net/transcript?date={dia}", headers=RO)
        check("transcrição individual", resposta.status_code == 200 and "limpeza de pele" in resposta.text)

        resposta = cliente.get(f"/api/v1/stats/daily?date={dia}", headers=RO)
        check("GET /api/v1/stats/daily", resposta.status_code == 200 and resposta.json()["messages"] >= 6)

        check("conversa inexistente -> 404", cliente.get("/api/v1/conversations/5511900000000%40s.whatsapp.net/messages", headers=RO).status_code == 404)

        print("\n[4] escrita de análises")
        analise = {
            "chat_jid": "5511988887777@s.whatsapp.net",
            "analysis_date": dia,
            "classification": "lead_quente",
            "sentiment": "positivo",
            "intent": "agendamento",
            "summary": "Lead pediu preço e fechou horário para sexta 15h.",
            "next_step": "Confirmar presença na véspera.",
            "risk_flags": ["no_show"],
            "score": 92,
            "agent": "teste",
            "model": "fake",
        }
        resposta = cliente.post("/api/v1/analyses", headers=RW, json=analise)
        check("POST /api/v1/analyses", resposta.status_code == 200, resposta.text[:300])
        criada = resposta.json()
        check("análise persistida", criada["classification"] == "lead_quente" and criada["score"] == 92)
        check("risk_flags decodificadas", criada["risk_flags"] == ["no_show"])

        resposta = cliente.post("/api/v1/analyses", headers=RW, json={**analise, "summary": "Resumo atualizado", "score": 88})
        check("upsert de análise mantém 1 registro", resposta.status_code == 200 and resposta.json()["score"] == 88)
        resposta = cliente.get(f"/api/v1/analyses?date={dia}", headers=RO)
        check("GET /api/v1/analyses", resposta.status_code == 200 and len(resposta.json()) == 1)

        print("\n[5] relatórios")
        relatorio = {
            "kind": "daily",
            "period_start": dia,
            "title": "Relatório diário de testes",
            "summary": "Dois leads atendidos, um agendamento fechado.",
            "body_md": "## Resumo\n\n- 2 conversas\n- 1 agendamento\n\n**Prioridade:** retorno do Joao.",
            "metrics": {"conversas": 2, "agendamentos": 1},
            "highlights": ["Maria fechou para sexta"],
            "action_items": ["Confirmar horário da Maria"],
            "status": "published",
            "agent": "teste",
            "idempotency_key": "teste-diario-1",
        }
        resposta = cliente.post("/api/v1/reports", headers=RW, json=relatorio)
        check("POST /api/v1/reports -> 201", resposta.status_code == 201, resposta.text[:300])
        primeiro_id = resposta.json()["id"] if resposta.status_code == 201 else 0

        resposta = cliente.post("/api/v1/reports", headers=RW, json=relatorio)
        check("idempotência devolve 200 e mesmo id", resposta.status_code == 200 and resposta.json()["id"] == primeiro_id, resposta.text[:200])

        resposta = cliente.get("/api/v1/reports", headers=RO)
        check("GET /api/v1/reports", resposta.status_code == 200 and resposta.json()["total"] == 1, resposta.text[:200])
        check("GET /api/v1/reports/{id}", cliente.get(f"/api/v1/reports/{primeiro_id}", headers=RO).status_code == 200)
        check(
            "PATCH relatório",
            cliente.patch(f"/api/v1/reports/{primeiro_id}", headers=RW, json={"status": "draft"}).json()["status"] == "draft",
        )

        print("\n[6] painel do gestor")
        resposta = cliente.get("/")
        check("GET / painel", resposta.status_code == 200 and "Painel do gestor" in resposta.text, resposta.text[:200])
        check("painel lista conversas", "Maria Silva" in resposta.text)
        check("painel mostra o dia selecionado", dia_br in resposta.text, dia_br)
        resposta = cliente.get("/relatorios")
        check("GET /relatorios", resposta.status_code == 200 and "Relatório diário de testes" in resposta.text)
        resposta = cliente.get(f"/relatorios/{primeiro_id}")
        check("GET /relatorios/{id} renderiza markdown", resposta.status_code == 200 and "<h2>" in resposta.text)
        check("período exibido como dd/mm/aaaa", dia_br in resposta.text, dia_br)
        resposta = cliente.get("/conversas/5511988887777%40s.whatsapp.net")
        check("GET /conversas/{jid}", resposta.status_code == 200 and "limpeza de pele" in resposta.text)
        resposta = cliente.get("/conversas/5511988887777%40s.whatsapp.net/transcript.txt")
        check("GET transcript.txt", resposta.status_code == 200 and "Clinica:" in resposta.text)
        check("GET /status", cliente.get("/status").status_code == 200)

        print("\n[7] webhook GOWA")
        payload = {
            "event": "message",
            "device_id": "5511900000000@s.whatsapp.net",
            "session_id": "teste",
            "payload": {
                "id": "MSG-TESTE-1",
                "chat_id": "5511966665555@s.whatsapp.net",
                "from": "5511966665555@s.whatsapp.net",
                "from_name": "Ana",
                "timestamp": agora_utc().isoformat().replace("+00:00", "Z"),
                "is_from_me": False,
                "body": "Oi, vocês atendem sábado?",
            },
        }
        cru = json.dumps(payload).encode()
        assinatura = hmac.new(b"segredo-de-teste", cru, hashlib.sha256).hexdigest()
        resposta = cliente.post("/wa-hook", content=cru, headers={"X-Hub-Signature-256": f"sha256={assinatura}"})
        check("webhook assinado -> ok", resposta.status_code == 200 and resposta.json().get("inserted") == 1, resposta.text[:200])
        resposta = cliente.post("/wa-hook", content=cru, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        check("webhook assinatura inválida -> 401", resposta.status_code == 401)
        resposta = cliente.post("/wa-hook", content=cru, headers={"X-Hub-Signature-256": f"sha256={assinatura}"})
        check("webhook duplicado não duplica", resposta.json().get("inserted") == 0)
        resposta = cliente.get(f"/api/v1/conversations?date={dia}", headers=RO)
        check("nova conversa do webhook aparece", any(c["chat_jid"] == "5511966665555@s.whatsapp.net" for c in resposta.json()))

        print("\n[8] contrato para o agente")
        resposta = cliente.get("/api/v1/contract", headers=RO)
        check("GET /api/v1/contract", resposta.status_code == 200 and len(resposta.text) > 200)

    print(f"\n{PASSOS - len(FALHAS)}/{PASSOS} verificações passaram")
    if FALHAS:
        print("FALHAS:", ", ".join(FALHAS))
        return 1
    print("TUDO OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
