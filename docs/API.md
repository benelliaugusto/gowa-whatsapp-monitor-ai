# API do monitor-ia — referência rápida

Base: `https://SEU-DOMINIO` (Easypanel) ou `http://localhost:8000` (local).
Swagger interativo: `GET /docs` · OpenAPI: `GET /openapi.json`.
Contrato/prompt para o agente: `GET /api/v1/contract` (texto markdown).

## Autenticação

Rotas `/api/v1/*` (exceto `/health`) exigem API key:

```bash
curl -H "X-API-Key: sk_xxx" https://monitor.exemplo.com/api/v1/digest?date=2026-11-05
# ou
curl -H "Authorization: Bearer sk_xxx" ...
```

`API_KEYS` no ambiente define as chaves: `nome:escopo:chave,...` com escopo
`rw` (leitura + escrita) ou `ro` (somente leitura).

O painel HTML (`/`, `/relatorios`, `/status`) usa **HTTP Basic** quando
`DASHBOARD_PASSWORD` está definido.

O webhook (`POST /wa-hook`) valida `X-Hub-Signature-256` (HMAC-SHA256 do corpo
cru com `GOWA_WEBHOOK_SECRET`).

## Endpoints

### Leitura

| método | rota | descrição |
|---|---|---|
| GET | `/api/v1/health` | saúde do monitor, banco e GOWA (público) |
| GET | `/api/v1/conversations` | conversas com movimento (`date`, `since`, `until`, `include_groups`, `limit`) |
| GET | `/api/v1/conversations/{chat_jid}` | metadados de uma conversa |
| GET | `/api/v1/conversations/{chat_jid}/messages` | mensagens (`date`/`since`/`until`, `limit`, `order=asc|desc`) |
| GET | `/api/v1/conversations/{chat_jid}/transcript` | transcrição em `text/plain` |
| GET | `/api/v1/digest` | **entrada principal da IA**: conversas do dia + transcrições |
| GET | `/api/v1/analyses` | análises gravadas (`date`, `chat_jid`, `limit`) |
| GET | `/api/v1/reports` | relatórios (`kind`, `status`, `start`, `end`, `limit`, `offset`) |
| GET | `/api/v1/reports/{id}` | um relatório |
| GET | `/api/v1/stats/daily` | KPIs do dia |
| GET | `/api/v1/sync/runs` | histórico de sincronizações |
| GET | `/api/v1/gowa` | diagnóstico do GOWA |
| GET | `/api/v1/contract` | contrato completo para o agente |
| GET | `/api/v1/tasks` | tarefas (`status`, `owner`, `limit`) |
| GET | `/api/v1/transcriptions/stats` | fila de transcrição por status |
| GET | `/api/v1/agent-runs` | últimas execuções do agente |
| GET | `/api/v1/stats/series` | série diária (tendências) |
| GET | `/api/v1/funnel` | funil de leads + conversão |
| GET | `/api/v1/conversations/{jid}/stage` | estágio atual + histórico |

### Escrita

| método | rota | descrição |
|---|---|---|
| POST | `/api/v1/analyses` | upsert de análise por conversa/dia |
| POST | `/api/v1/reports` | cria relatório (idempotente) |
| PATCH | `/api/v1/reports/{id}` | atualiza/publica relatório |
| POST | `/api/v1/sync` | força sincronização com o GOWA |
| POST | `/api/v1/tasks` | cria tarefa (pendência) |
| PATCH | `/api/v1/tasks/{id}` | conclui/atualiza tarefa |
| POST | `/api/v1/transcriptions/run` | transcreve áudios pendentes |
| POST | `/api/v1/notify/manager` | envia resumo do dia ao gestor (WhatsApp) |
| POST | `/api/v1/agent-runs` | registra execução do agente (cron) |
| PATCH | `/api/v1/conversations/{jid}/stage` | move o lead no funil |

### Webhook

| método | rota | descrição |
|---|---|---|
| POST | `/wa-hook` (alias `/webhook`) | recebe eventos do GOWA e grava mensagens |

## Exemplos

### Digest do dia (JSON)

```bash
curl -s -H "X-API-Key: sk_xxx" \
  "https://monitor.exemplo.com/api/v1/digest?date=2026-11-05" | jq
```

### Digest do dia (texto para o LLM)

```bash
curl -s -H "X-API-Key: sk_xxx" \
  "https://monitor.exemplo.com/api/v1/digest?date=2026-11-05&format=text"
```

### Transcrição de uma conversa

```bash
curl -s -H "X-API-Key: sk_xxx" \
  "https://monitor.exemplo.com/api/v1/conversations/5511999999999%40s.whatsapp.net/transcript?date=2026-11-05"
```

> O `@` do JID deve ir como `%40` na URL.

### Gravar análise

```bash
curl -s -X POST "https://monitor.exemplo.com/api/v1/analyses" \
  -H "X-API-Key: sk_xxx" -H "Content-Type: application/json" \
  -d '{
    "chat_jid": "5511999999999@s.whatsapp.net",
    "analysis_date": "2026-11-05",
    "classification": "lead_quente",
    "sentiment": "positivo",
    "intent": "agendamento",
    "summary": "Pediu valor da limpeza de pele e fechou sexta 15h.",
    "next_step": "Confirmar presença na véspera.",
    "risk_flags": ["confirmacao_pendente"],
    "score": 92,
    "agent": "claude-code",
    "model": "claude-sonnet-4-5"
  }'
```

Resposta `200`:

```json
{
  "id": 12,
  "chat_jid": "5511999999999@s.whatsapp.net",
  "analysis_date": "2026-11-05",
  "classification": "lead_quente",
  "sentiment": "positivo",
  "intent": "agendamento",
  "summary": "Pediu valor da limpeza de pele e fechou sexta 15h.",
  "next_step": "Confirmar presença na véspera.",
  "risk_flags": ["confirmacao_pendente"],
  "score": 92,
  "extra": {},
  "agent": "claude-code",
  "model": "claude-sonnet-4-5",
  "created_at": "2026-11-05T13:20:00Z",
  "updated_at": "2026-11-05T13:20:00Z"
}
```

### Publicar relatório

```bash
curl -s -X POST "https://monitor.exemplo.com/api/v1/reports" \
  -H "X-API-Key: sk_xxx" -H "Content-Type: application/json" \
  -d '{
    "kind": "daily",
    "period_start": "2026-11-05",
    "title": "Relatório diário — 05/11/2026",
    "summary": "12 conversas, 4 leads quentes, 3 agendamentos.",
    "body_md": "## Resumo\n\n- 12 conversas\n- 4 leads quentes\n\n## Atenção\n\n- Joao sem resposta desde 14h",
    "metrics": {"conversas": 12, "leads_quentes": 4, "agendamentos": 3},
    "highlights": ["Maria fechou limpeza de pele"],
    "action_items": ["Ligar para Joao antes das 18h"],
    "agent": "claude-code",
    "idempotency_key": "daily-2026-11-05"
  }'
```

- `201` na primeira gravação; `200` quando a `idempotency_key` já existe (mesmo `id`).
- O painel renderiza `body_md` em `/relatorios/{id}`.

### Forçar sincronização

```bash
curl -s -X POST "https://monitor.exemplo.com/api/v1/sync" \
  -H "X-API-Key: sk_xxx" -H "Content-Type: application/json" \
  -d '{"days": 2}'
```

```json
{
  "ok": true, "skipped": false, "trigger": "api", "days": 2,
  "devices": 1, "chats_scanned": 34, "chats_skipped": 120,
  "messages_fetched": 210, "messages_inserted": 18,
  "duration_ms": 4120, "chats": [{"chat_jid": "...", "fetched": 40, "inserted": 3}]
}
```

### Health

```bash
curl -s https://monitor.exemplo.com/api/v1/health
```

```json
{
  "status": "ok", "app": "monitor-ia", "version": "2.0.0",
  "timezone": "America/Sao_Paulo", "now": "2026-11-05T13:00:00Z",
  "database": {"messages": 4210, "conversations": 320, "analyses": 12, "reports": 3, "last_message_at": "..."},
  "gowa": {"url": "http://clinica_gowa-server:3000", "reachable": true, "version": "v9.8.0", "devices": 1},
  "sync": {"enabled": true, "interval_minutes": 15, "last_run": {...}}
}
```

### Tarefas (pendências)

```bash
# o agente cria a partir de action_items
curl -s -X POST "https://monitor.exemplo.com/api/v1/tasks" \
  -H "X-API-Key: sk_xxx" -H "Content-Type: application/json" \
  -d '{"chat_jid":"5511999999999@s.whatsapp.net","title":"Ligar para Maria e confirmar sexta 15h","owner":"recepcao","due_at":"2026-11-05","priority":1}'

# listar pendentes
curl -s -H "X-API-Key: sk_xxx" "https://monitor.exemplo.com/api/v1/tasks?status=pending"

# concluir
curl -s -X PATCH "https://monitor.exemplo.com/api/v1/tasks/12" \
  -H "X-API-Key: sk_xxx" -H "Content-Type: application/json" -d '{"status":"done"}'
```

### Aviso ao gestor / transcrição / execuções

```bash
curl -s -X POST "https://monitor.exemplo.com/api/v1/notify/manager" \
  -H "X-API-Key: sk_xxx" -H "Content-Type: application/json" -d '{"day":"2026-11-05"}'

curl -s -X POST "https://monitor.exemplo.com/api/v1/transcriptions/run" \
  -H "X-API-Key: sk_xxx"

curl -s -H "X-API-Key: sk_xxx" "https://monitor.exemplo.com/api/v1/agent-runs"
```

> `GET /api/v1/stats/daily` e `GET /api/v1/digest` agora trazem `atendimento`
> (tempo de 1ª resposta, % respondidas, sem resposta +24h, contato fora do horário).

### Funil e tendências

```bash
# mover o lead no funil
curl -s -X PATCH "https://monitor.exemplo.com/api/v1/conversations/5511999999999%40s.whatsapp.net/stage" \
  -H "X-API-Key: sk_xxx" -H "Content-Type: application/json" \
  -d '{"stage":"agendado","source":"manual"}'

# funil do dia + conversão
curl -s -H "X-API-Key: sk_xxx" "https://monitor.exemplo.com/api/v1/funnel?date=2026-11-05"

# série dos últimos 30 dias
curl -s -H "X-API-Key: sk_xxx" "https://monitor.exemplo.com/api/v1/stats/series?days=30"
```

Estágios: `novo`, `qualificado`, `agendado`, `atendido`, `fechado`, `perdido`.
`POST /api/v1/analyses` aceita o campo opcional `stage` e atualiza o funil sozinho.

## Códigos de erro

| status | significado |
|---|---|
| 401 | API key ausente/inválida |
| 403 | chave `ro` em rota de escrita |
| 404 | conversa/relatório inexistente |
| 422 | payload inválido (validação Pydantic) |
| 502 | GOWA inacessível |

## Paginação e limites

- `limit` máximo: 2000 (conversas), 5000 (mensagens), 200 (relatórios).
- `digest` corta transcrições por orçamento de caracteres (`max_chars`, padrão 60000)
  e marca `truncated: true` na conversa afetada.
- Datas são sempre no fuso `TIMEZONE` (padrão `America/Sao_Paulo`); timestamps na
  resposta são UTC ISO-8601 (`...Z`).