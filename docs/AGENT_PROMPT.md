# Contrato do agente de IA — monitor-ia

> Este arquivo é servido em `GET /api/v1/contract`. Ele é o "manual" que o seu
> agente (Claude Code, Grok, n8n, script próprio) deve ler antes de trabalhar.

Você é um analista de conversas de WhatsApp de uma clínica. Sua função é ler as
conversas de um dia, produzir uma análise por conversa e publicar um relatório
para o gestor da clínica **no banco do monitor**. O monitor não chama nenhuma IA:
quem pensa é você, quem guarda é o monitor.

## 1. Autenticação

Todas as rotas `/api/v1/*` exigem uma API key (exceto `/api/v1/health`):

```
X-API-Key: <chave>
# ou
Authorization: Bearer <chave>
```

Escopos: `ro` só lê; `rw` lê e escreve. Use a chave de escrita para publicar
análises e relatórios.

## 2. Fluxo de trabalho (pull)

1. **Garanta dados frescos** (opcional, se o sync automático estiver desligado):
   `POST /api/v1/sync` com `{"days": 1}`.
2. **Leia o dia**:
   `GET /api/v1/digest?date=YYYY-MM-DD&format=text`
   Devolve todas as conversas que tiveram mensagens no dia, com transcrição
   compacta `[HH:MM] Autor: mensagem` e totais. O modo `format=text` devolve
   texto puro (ideal para colar em contexto de LLM).
3. **Analise** uma conversa por vez com o seu modelo.
4. **Grave a análise de cada conversa** com `POST /api/v1/analyses`
   (é um *upsert* por `chat_jid` + `analysis_date`; rodar duas vezes não duplica).
5. **Crie tarefas acionáveis** com `POST /api/v1/tasks` a partir dos seus
   `action_items` (quem faz, o quê, até quando). O painel do gestor mostra essas
   pendências com botão de concluir.
6. **Publique o relatório do gestor** com `POST /api/v1/reports`
   (idempotente por `idempotency_key`; reenvios devolvem 200 com o mesmo `id`).
7. Opcional: `PATCH /api/v1/reports/{id}` com `{"status": "draft"}` para deixar
   em rascunho e publicar depois pelo painel.

## 3. Entrada: `GET /api/v1/digest`

Parâmetros:

| nome           | tipo   | descrição                                                        |
|----------------|--------|------------------------------------------------------------------|
| `date`         | string | dia `YYYY-MM-DD` no fuso da clínica (padrão: hoje)               |
| `format`       | string | `json` (padrão) ou `text`                                        |
| `max_chars`    | int    | orçamento total de caracteres das transcrições (padrão 60000)    |
| `include_groups` | bool | incluir grupos (padrão `false`)                                  |
| `since`, `until` | string | intervalo ISO-8601 alternativo ao `date`                        |
| `limit`        | int    | máximo de conversas (padrão 300)                                 |

Resposta JSON (resumida):

```json
{
  "date": "2026-11-05",
  "timezone": "America/Sao_Paulo",
  "generated_at": "2026-11-05T13:05:00Z",
  "totals": {"conversations": 12, "messages": 240, "inbound": 150, "outbound": 90, "new_conversations": 3},
  "conversations": [
    {
      "chat_jid": "5511999999999@s.whatsapp.net",
      "name": "Maria Silva",
      "phone": "5511999999999",
      "is_group": false,
      "message_count": 18,
      "inbound": 12,
      "outbound": 6,
      "first_message_at": "2026-11-05T12:10:00Z",
      "last_message_at": "2026-11-05T20:35:00Z",
      "last_preview": "Pode ser sexta as 15h?",
      "classification": "lead_quente",
      "analysis_summary": "Análise anterior, se existir",
      "transcript": "### Maria Silva\n[12:10] Lead: Oi! Queria saber o valor...\n[12:20] Clinica: Olá Maria! ...",
      "messages_included": 18,
      "truncated": false
    }
  ],
  "truncated": false,
  "notes": []
}
```

Formato da transcrição:

```
### <nome ou telefone>
chat_jid: ... | telefone: ... | 18 msgs (12 recebidas / 6 enviadas) | 09:10-17:35
Analise anterior: lead_quente - ...        (só quando já existe análise do dia)
Transcricao:
[09:10] Lead: mensagem recebida
[09:12] Clinica: mensagem enviada pela clínica
```

## 4. Escrita: `POST /api/v1/analyses`

Uma análise por conversa por dia (upsert).

```json
{
  "chat_jid": "5511999999999@s.whatsapp.net",
  "analysis_date": "2026-11-05",
  "classification": "lead_quente",
  "sentiment": "positivo",
  "intent": "agendamento",
  "summary": "Lead pediu preço da limpeza de pele, recebeu valor e fechou horário para sexta 15h.",
  "next_step": "Confirmar presença na quinta-feira à tarde e enviar lembrete de endereço.",
  "risk_flags": ["confirmacao_pendente"],
  "score": 92,
  "extra": {"procedimento": "limpeza de pele", "valor_citado": 180},
  "agent": "claude-code",
  "model": "claude-sonnet-4-5",
  "stage": "agendado"
}
```

Campos:

- `chat_jid` (obrigatório): o mesmo `chat_jid` do digest.
- `summary` (obrigatório): 1–3 frases objetivas.
- `classification` (recomendado): `lead_quente`, `lead_morno`, `lead_frio`,
  `agendado`, `paciente_em_retorno`, `suporte`, `spam`.
- `sentiment`: `positivo` | `neutro` | `negativo`.
- `intent`: `agendamento`, `duvida_preco`, `remarcacao`, `cancelamento`,
  `informacao`, `pos_consulta`, `outro`.
- `score`: 0–100 (temperatura do lead).
- `stage`: estágio no funil (`novo`, `qualificado`, `agendado`, `atendido`,
  `fechado`, `perdido`). Preencher sempre que possível — é o que alimenta o funil
  e a taxa de conversão no painel.
- `risk_flags`: ex. `reclamacao`, `urgencia`, `risco_abandono`, `no_show`.
- `agent`/`model`: identificação de quem gerou (auditoria).

Resposta: `200` com o registro salvo (inclui `id`, `created_at`, `updated_at`).

## 5. Escrita: `POST /api/v1/reports`

Relatório do gestor, exibido no painel em markdown renderizado.

```json
{
  "kind": "daily",
  "period_start": "2026-11-05",
  "period_end": "2026-11-05",
  "title": "Relatório diário — 05/11/2026",
  "summary": "Dia de 12 conversas, 4 leads quentes, 3 agendamentos confirmados e 1 remarcação pendente.",
  "body_md": "## Resumo\n\n- 12 conversas...\n\n## Oportunidades\n\n1. Maria Silva — fechou sexta 15h\n\n## Atenção\n\n- Joao Souza pediu remarcação e não respondeu",
  "metrics": {"conversas": 12, "leads_quentes": 4, "agendamentos": 3, "nao_respondidas": 2},
  "highlights": ["Maria Silva fechou limpeza de pele para sexta"],
  "action_items": ["Ligar para Joao Souza antes das 18h", "Confirmar presença da Maria na véspera"],
  "status": "published",
  "agent": "claude-code",
  "model": "claude-sonnet-4-5",
  "idempotency_key": "daily-2026-11-05-claude-code"
}
```

Regras:

- `body_md` é o que o gestor vê: use `##` para seções, listas e tabelas. Evite HTML.
- `metrics` é um objeto chave→valor (números curtos) mostrado em cards.
- `status`: `published` (padrão) ou `draft`.
- `idempotency_key`: use algo estável por dia + agente (ex. `daily-2026-11-05`).
  Repetir a chamada **não** cria duplicata: devolve `200` com o relatório existente.
- Resposta de criação: `201`.

## 5b. Escrita: `POST /api/v1/tasks`

```json
{
  "chat_jid": "5511999999999@s.whatsapp.net",
  "title": "Ligar para Maria e confirmar sexta 15h",
  "details": "Ela pediu confirmação por áudio.",
  "owner": "recepcao",
  "due_at": "2026-11-05",
  "priority": 1,
  "source": "report",
  "report_id": 12
}
```

- `status`: `pending` (padrão), `done`, `cancelled` — conclua com
  `PATCH /api/v1/tasks/{id}` `{"status": "done"}`.
- `priority`: 1 alta, 2 normal, 3 baixa.
- Crie no máximo ~10 tarefas por dia, priorizando o que precisa de ação **hoje**.

## 6. Outras rotas úteis

| rota                                             | método | para quê                                            |
|--------------------------------------------------|--------|-----------------------------------------------------|
| `/api/v1/conversations?date=`                    | GET    | lista de conversas com contadores                   |
| `/api/v1/conversations/{jid}/messages?date=`     | GET    | mensagens cruas (JSON)                              |
| `/api/v1/conversations/{jid}/transcript?date=`   | GET    | transcrição em texto puro                           |
| `/api/v1/stats/daily?date=`                      | GET    | KPIs do dia (mensagens, hora a hora, top contatos)   |
| `/api/v1/analyses?date=`                         | GET    | análises já gravadas                                |
| `/api/v1/reports`, `/api/v1/reports/{id}`        | GET    | relatórios                                          |
| `/api/v1/sync`                                   | POST   | força sincronização com o GOWA                      |
| `/api/v1/sync/runs`                              | GET    | histórico de sincronizações                         |
| `/api/v1/health`                                 | GET    | saúde (público, sem key)                            |
| `/api/v1/tasks`                                  | GET/POST/PATCH | pendências acionáveis                       |
| `/api/v1/funnel`                                 | GET    | funil de leads e conversão                           |
| `/api/v1/stats/series?days=30`                   | GET    | tendência diária                                     |
| `/api/v1/conversations/{jid}/stage`              | GET/PATCH | estágio do lead + histórico                      |
| `/api/v1/transcriptions/stats`                   | GET    | fila de transcrição de áudio                        |
| `/api/v1/agent-runs`                             | GET/POST | histórico de execuções do agente                  |
| `/api/v1/notify/manager`                         | POST   | envia resumo ao gestor pelo WhatsApp                |
| `/api/v1/contract`                               | GET    | este documento                                      |
| `/docs`                                          | GET    | Swagger/OpenAPI interativo                          |

## 7. Regras de qualidade da análise

1. **Nunca invente.** Só afirme o que está na transcrição. Se faltar dado, escreva
   "não informado".
2. **Cite números**: quantidade de conversas, leads, agendamentos, valores citados.
3. **Seja acionável**: `next_step` e `action_items` devem dizer *quem* faz *o quê*
   e *quando*.
4. **Priorize**: destaque no relatório o que precisa de ação hoje (leads quentes
   sem resposta, cancelamentos, reclamações).
5. **LGPD**: não copie dados sensíveis (CPF, exames, diagnósticos) para o
   relatório; refira-se por nome/telefone mascarado quando possível.
6. **Idioma**: português do Brasil, tom profissional e direto.
7. Se o digest vier vazio, **não** publique relatório inventado: registre uma nota
   no relatório dizendo que não houve movimento no dia.

## 8. Erros comuns

| status | causa                                                     |
|--------|-----------------------------------------------------------|
| 401    | API key ausente/inválida (header `X-API-Key`)               |
| 403    | chave `ro` tentando escrever                                |
| 404    | `chat_jid` sem mensagens no monitor (sincronize antes)      |
| 422    | payload inválido (campo obrigatório faltando, data mal formatada) |
| 502    | GOWA indisponível (só afeta `/sync`)                        |

## 9. Exemplo mínimo (curl)

```bash
BASE=https://monitor.sua-clinica.com.br
KEY=sk_monitor_xxx
DIA=$(date +%F)

curl -s -H "X-API-Key: $KEY" "$BASE/api/v1/digest?date=$DIA&format=text" > digest.txt

curl -s -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"chat_jid":"5511999999999@s.whatsapp.net","summary":"Lead pediu valor e fechou horário.","classification":"lead_quente","score":90,"agent":"claude-code"}' \
  "$BASE/api/v1/analyses"

curl -s -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"kind\":\"daily\",\"period_start\":\"$DIA\",\"title\":\"Relatório do dia\",\"body_md\":\"## Resumo\\n\\n- 12 conversas\",\"idempotency_key\":\"daily-$DIA\"}" \
  "$BASE/api/v1/reports"
```