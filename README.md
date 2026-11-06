# monitor-ia

Painel de conversas do WhatsApp para o gestor da clínica + **API de leitura e
escrita para agentes de IA**. O monitor não chama IA: ele guarda as conversas
(GOWA), entrega um *digest* pronto para o LLM e recebe dele as análises e o
relatório do dia.

```
WhatsApp ──► GOWA (gowa-server) ──webhook──► monitor-ia ──► SQLite (/data/monitor.db)
                    ▲                             │
                    └──── GET /chats,             │  GET  /api/v1/digest   (texto do dia)
                          /chat/{jid}/messages ◄──┘  POST /api/v1/analyses (por conversa)
                                                      POST /api/v1/reports  (relatório do gestor)
                                                             ▲
                                    Claude Code / Grok / n8n / script ─┘  (a IA do cliente)
```

O que você ganha em relação à v1 (Dockerfile único com `main.py` embutido):

| | v1 (arquivo `.dockerfile`) | v2 (este projeto) |
|---|---|---|
| Código | 1.000 linhas dentro de um heredoc | pacote `app/` modular, revisável |
| IA | chamada síncrona à OpenAI dentro da requisição | **API pull**: a sua IA lê e grava (Claude/Grok/n8n) |
| API | nenhuma | REST versionada + Swagger em `/docs` |
| Dados | `mensagens` sem índice/limpeza | schema com índices, WAL, dedup por `msg_uid` |
| Segurança | aberta | API keys (`ro`/`rw`), Basic Auth no painel, HMAC no webhook |
| Deploy | colar Dockerfile na UI | repositório GitHub → build Dockerfile |

## Estrutura

```
.
├── Dockerfile                # imagem de produção (python:3.12-slim)
├── docker-compose.yml        # teste local: gowa + monitor-ia
├── requirements.txt
├── .env.example              # todas as variáveis documentadas
├── app/
│   ├── main.py               # FastAPI: lifespan, scheduler, middleware
│   ├── config.py             # settings (12-factor)
│   ├── db.py                 # SQLite: schema, consultas, migrations idempotentes
│   ├── gowa.py               # cliente GOWA + sincronização de histórico
│   ├── webhook.py            # recebe eventos do GOWA (HMAC + dedup)
│   ├── transcription.py      # transcrição local de áudios (faster-whisper)
│   ├── notify.py             # avisos ao gestor pelo WhatsApp
│   ├── agent.py              # agente em cron: digest -> LLM -> análises/relatório/tarefas
│   ├── backup.py             # backup do SQLite (cron)
│   ├── api.py                # /api/v1 para os agentes de IA
│   ├── schemas.py            # contrato Pydantic (OpenAPI)
│   ├── digest.py             # conversas -> texto compacto para o LLM
│   ├── dashboard.py          # painel do gestor (server-side)
│   ├── templating.py         # Jinja2 + markdown sanitizado
│   ├── security.py           # API keys, Basic Auth, HMAC
│   ├── timeutil.py           # UTC no banco, America/Sao_Paulo na borda
│   ├── templates/            # painel: painel, conversa, relatórios, status
│   └── static/style.css
├── docs/
│   ├── AGENT_PROMPT.md       # contrato/prompt do agente (servido em /api/v1/contract)
│   ├── API.md                # referência com curl
│   ├── EASYPANEL.md          # passo a passo do deploy
│   └── ROADMAP.md            # plano de melhorias priorizado
├── examples/agent_claude.py  # agente de ponta a ponta (Claude ou Grok)
└── scripts/smoke_test.py     # 43 verificações de fumaça
```

## Rodando local (5 minutos)

Pré-requisitos: Docker + `docker compose`.

```bash
cp .env.example .env
# edite .env: GOWA_BASIC_AUTH, GOWA_WEBHOOK_SECRET, API_KEYS, DASHBOARD_PASSWORD
docker compose up --build
```

- Painel: <http://localhost:8000> (`gestor` / senha do `.env`)
- Swagger: <http://localhost:8000/docs>
- GOWA: <http://localhost:3000> (pareie o número pelo QR do GOWA)

Sem Docker (Python 3.12):

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export DATABASE_PATH=./data/monitor.db
export API_KEYS="claude:rw:chave-local" DASHBOARD_PASSWORD=admin
uvicorn app.main:app --reload --port 8000
```

## Deploy no Easypanel

Resumo: **repositório privado no GitHub → New Service → App → Build: Dockerfile**.
O guia completo (env, volume, domínio, rede interna, backup, troubleshooting) está
em [`docs/EASYPANEL.md`](docs/EASYPANEL.md).

Pontos que mais quebram:

1. **Volume** montado em `/data` (senão o histórico se perde a cada deploy).
2. `GOWA_URL` apontando para o host interno: `http://<projeto>_gowa-server:3000`
   (valide no Shell do serviço com `getent hosts`).
3. `WHATSAPP_WEBHOOK` do GOWA = `https://seu-dominio/wa-hook` e
   `GOWA_WEBHOOK_SECRET` = `WHATSAPP_WEBHOOK_SECRET`.
4. `API_KEYS` definido (sem chave, `/api/v1` responde 401 — é o esperado).

> Zip não é uma origem de build do Easypanel; se GitHub não for possível, use
> imagem no Docker Hub/GHCR (`docker build` + `push`) e "Source: Docker image".

## API para a IA

Documentação completa: [`docs/API.md`](docs/API.md) · contrato do agente:
[`docs/AGENT_PROMPT.md`](docs/AGENT_PROMPT.md) · Swagger: `/docs`.

| método | rota | para quê |
|---|---|---|
| GET | `/api/v1/digest?date=YYYY-MM-DD&format=text` | conversas do dia em texto pronto para o LLM |
| GET | `/api/v1/conversations/{jid}/transcript?date=` | transcrição de uma conversa |
| GET | `/api/v1/conversations?date=` | lista com contadores |
| GET | `/api/v1/stats/daily?date=` | KPIs do dia + métricas de atendimento |
| GET | `/api/v1/stats/series?days=30` | série diária para tendências |
| GET | `/api/v1/funnel?date=` | funil de leads com conversão |
| PATCH | `/api/v1/conversations/{jid}/stage` | move o lead no funil (com histórico) |
| POST | `/api/v1/analyses` | upsert da análise de uma conversa no dia |
| POST | `/api/v1/reports` | publica o relatório do gestor (idempotente) |
| PATCH | `/api/v1/reports/{id}` | atualiza/despublica |
| POST | `/api/v1/sync` | força sincronização com o GOWA |
| GET | `/api/v1/tasks` | pendências da recepção (com filtro de status) |
| POST | `/api/v1/tasks` | cria tarefa (o agente converte `action_items` em tarefas) |
| PATCH | `/api/v1/tasks/{id}` | conclui/atualiza tarefa |
| POST | `/api/v1/transcriptions/run` | força transcrição de áudios pendentes (backfill) |
| GET | `/api/v1/transcriptions/stats` | fila de transcrição (pending/done/failed) |
| POST | `/api/v1/notify/manager` | envia o resumo do dia ao gestor pelo WhatsApp |
| GET/POST | `/api/v1/agent-runs` | histórico/registro das execuções do agente (cron) |
| GET | `/api/v1/health` | saúde (público) |

Exemplo (o "contrato" do dia):

```bash
BASE=https://monitor.sua-clinica.com.br
KEY=sk_monitor_xxx

# 1) o agente lê o dia
curl -s -H "X-API-Key: $KEY" "$BASE/api/v1/digest?date=$(date +%F)&format=text" > digest.txt

# 2) grava a análise de uma conversa
curl -s -X POST "$BASE/api/v1/analyses" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"chat_jid":"5511999999999@s.whatsapp.net","classification":"lead_quente","summary":"Pediu valor e fechou sexta 15h.","score":92,"agent":"claude-code"}'

# 3) publica o relatório
curl -s -X POST "$BASE/api/v1/reports" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"period_start\":\"$(date +%F)\",\"title\":\"Relatório do dia\",\"body_md\":\"## Resumo\\n\\n- 12 conversas\",\"idempotency_key\":\"daily-$(date +%F)\"}"
```

## Conectando o seu agente

1. Gere uma chave: `openssl rand -hex 24` e coloque em `API_KEYS`
   (`claude:rw:<chave>,grok:ro:<chave>`).
2. Dê ao agente o texto de [`docs/AGENT_PROMPT.md`](docs/AGENT_PROMPT.md) como
   instrução (ou aponte para `GET /api/v1/contract`).
3. Rode o exemplo completo:

```bash
export MONITOR_URL=https://monitor.sua-clinica.com.br
export MONITOR_API_KEY=sk_monitor_xxx
export LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-xxx LLM_MODEL=claude-sonnet-4-5
python examples/agent_claude.py --date 2026-11-05 --dry-run   # ensaio
python examples/agent_claude.py --date 2026-11-05             # publica
```

4. Agende no que preferir: cron do seu servidor, GitHub Actions, um serviço
   "Cron" do próprio Easypanel ou a ferramenta de agendamento do seu agente.

Para Grok/xAI basta trocar para `LLM_PROVIDER=openai`,
`LLM_BASE_URL=https://api.x.ai/v1`, `LLM_API_KEY=xai-...`, `LLM_MODEL=grok-4`.

**OpenCode Go/Console** (assinatura ou pay-as-you-go) também funciona — é um gateway
compatível com OpenAI/Anthropic:

```bash
LLM_PROVIDER=opencode
LLM_API_KEY=<chave do Console>
LLM_MODEL=deepseek-v4.1-flash     # ou glm-5.3-flash, claude-haiku-5-5, kimi-k3...
LLM_BASE_URL=https://opencode.ai/inference/go   # padrão do agente; use /inference no pay-as-you-go
```

O agente envia `User-Agent` próprio e o header `x-opencode-session` (exigidos pelo
gateway para Go) e soma o custo reportado em `usage.cost` no registro da execução.

Gateways sofrem com upstream saturado (HTTP 429 `overloaded`, 5xx). O agente já
trata isso: **retry com backoff exponencial** (`LLM_MAX_RETRIES`, padrão 3) e
**fallback de modelo** (`LLM_FALLBACK_MODELS`, no OpenCode o padrão é
`glm-5.3-flash`). Só depois de esgotar as tentativas a execução falha — e mesmo
assim as análises já publicadas ficam salvas.

### Painel: funil, tendências e busca

O painel do gestor agora mostra: KPIs do dia, **funil de leads** (`novo`,
`qualificado`, `agendado`, `atendido`, `fechado`, `perdido`) com taxa de conversão,
**gráfico dos últimos 14 dias**, cartão de **atendimento** (1ª resposta, % respondidas,
sem resposta +24h, fora do horário), **pendências** com botão de concluir, busca por
nome/telefone e filtros por estágio e "sem resposta". Na página da conversa há um
seletor para mover o lead no funil e o histórico de mudanças.

O estágio é atualizado automaticamente pelo agente (`stage` na análise) ou
manualmente (`PATCH /api/v1/conversations/{jid}/stage`).

### Ações pelo painel (`/acoes`)

O gestor não precisa de terminal: a página **Ações** (e o topo do painel) tem botão
para cada função do sistema — cada clique registra uma linha em `action_runs` com
status e saída:

| botão | o que faz |
|---|---|
| **Sincronizar WhatsApp** | puxa o histórico recente do GOWA (dias opcional) |
| **Transcrever áudios** | processa a fila de áudio (inclui os marcados como `skipped`) |
| **Rodar IA do dia** | executa o agente em background: análises + relatório + tarefas (+ aviso opcional) |
| **Avisar gestor** | envia o resumo do dia pelo WhatsApp |
| **Backup agora** | cópia consistente do SQLite com checagem de integridade |

Para o botão **Rodar IA** funcionar, o serviço **monitor-ia** precisa das variáveis
do agente (o processo roda dentro do próprio container do monitor):

```
MONITOR_API_URL=http://127.0.0.1:8000   # mesmo container: localhost funciona
MONITOR_API_KEY=<chave rw do API_KEYS>
LLM_PROVIDER=opencode                   # ou anthropic/openai
LLM_API_KEY=...
LLM_MODEL=deepseek-v4.1-flash
LLM_BASE_URL=https://opencode.ai/inference/go
```

> No **Cron** (outro container) o `MONITOR_API_URL` é o host interno
> (`http://<projeto>_monitor-ia:8000`). São contextos diferentes — não confunda.

### Automação no Easypanel (dois serviços Cron)

O monitor já roda dentro da imagem: use a **mesma imagem/repositório** em serviços
`Cron` apontando para os comandos abaixo.

| cron | comando | função |
|---|---|---|
| `0 12,19 * * *` | `python -m app.agent --notify` | analisa o dia, publica relatório e avisa o gestor |
| `0 3 * * *` | `python -m app.backup --keep 14` | backup consistente do SQLite |

O agente precisa de `MONITOR_API_URL` (host interno do serviço monitor, ex.:
`http://projeto_monitor-ia:8000` — **não** `127.0.0.1`, pois o Cron é outro
container), `MONITOR_API_KEY` (chave `rw`) e das variáveis `LLM_*`. Se o dia não
tiver conversas, ele não publica relatório vazio — apenas registra a execução em
`/status`.

> Backup no mesmo volume protege contra corrupção do banco, não contra perda do
> volume. Para proteção real, copie `/data/backups` para fora (S3, outro host).

### Transcrição de áudios

Com `TRANSCRIBE_ENABLED=true`, mensagens de áudio entram como `pending` e um worker
em background transcreve (CPU, `faster-whisper`) e substitui o `[audio]` pelo texto
real — o digest e a IA passam a "ouvir" o paciente. O modelo fica em
`/data/models` (volume), então é baixado uma única vez. Para forçar o backfill:
`POST /api/v1/transcriptions/run`.

## Variáveis de ambiente

Detalhes e defaults em [`.env.example`](.env.example).

| variável | padrão | para quê |
|---|---|---|
| `GOWA_URL` | `http://gowa-server:3000` | endereço do GOWA (rede interna do Easypanel) |
| `GOWA_BASIC_AUTH` | — | `usuario:senha` do `APP_BASIC_AUTH` do GOWA |
| `GOWA_DEVICE_ID` | — | slot multi-device (vazio = descobre em `/devices`) |
| `GOWA_WEBHOOK_SECRET` | — | valida `X-Hub-Signature-256` do webhook |
| `API_KEYS` | — | `nome:escopo:chave,...` (`rw`/`ro`) |
| `DASHBOARD_PASSWORD` | — | Basic Auth do painel (vazio = painel aberto) |
| `DATABASE_PATH` | `/data/monitor.db` | SQLite (precisa estar no volume) |
| `TIMEZONE` | `America/Sao_Paulo` | fuso dos relatórios e do "dia" |
| `SYNC_INTERVAL_MINUTES` | `15` | sincronização automática (0 desliga) |
| `SYNC_WINDOW_DAYS` | `3` | janela puxada do histórico a cada sync |
| `DIGEST_MAX_CHARS` | `60000` | orçamento de contexto do digest |
| `TRANSCRIBE_ENABLED` | `true` | transcrição local de áudios (`faster-whisper`) |
| `WHISPER_MODEL` | `small` | modelo do whisper (`tiny`…`large-v3`) |
| `WHISPER_MODEL_DIR` | `/data/models` | cache do modelo (deve ficar no volume) |
| `BUSINESS_HOURS_START/END` | `08:00`/`18:00` | horário comercial para as métricas |
| `MANAGER_WHATSAPP` | — | número do gestor para os avisos (só dígitos com DDI, **número real que usa WhatsApp**) |
| `LLM_MAX_RETRIES` | `3` | tentativas por modelo em caso de 429/5xx |
| `LLM_FALLBACK_MODELS` | — | modelos alternativos separados por vírgula (OpenCode: `glm-5.3-flash`) |
| `BACKUP_DIR` / `BACKUP_KEEP` | `/data/backups` / `14` | backup automático do banco |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL` | — | provedor do agente em cron (inclui `opencode`) |
| `LOG_LEVEL` | `INFO` | verbosidade |

## Como os dados entram

1. **Tempo real** — GOWA envia cada `message` para `POST /wa-hook` (assinado);
   o monitor grava com dedup por `msg_uid`.
2. **Histórico** — a cada `SYNC_INTERVAL_MINUTES` (e no boot) o monitor lista
   `/chats` e puxa `/chat/{jid}/messages` dos últimos `SYNC_WINDOW_DAYS` dias.
   Isso recupera o que chegou com o monitor fora do ar.
3. **Manual** — botão "Sincronizar agora" no painel ou `POST /api/v1/sync`.

Dedup é por `msg_uid` (`gowa:<id da mensagem>`), então webhook + histórico não
duplicam.

## Testes

```bash
pip install -r requirements.txt
python scripts/smoke_test.py
```

O script cria um SQLite temporário, popula 2 conversas, sobe a app com
`TestClient` e valida 77 pontos: auth (`ro`/`rw`/401/403), digest, transcrições,
upsert de análise, idempotência de relatório, painel HTML (markdown sanitizado),
webhook assinado/duplicado, tarefas, métricas de atendimento, fila de transcrição,
telemetria do agente, funil de leads, séries e busca/filtros.

## Operação

- **Logs**: tabela `api_calls` (chamadas de escrita da API) e `sync_runs`
  (cada sincronização com contagem). Visíveis em `/status`.
- **Backup**: `sqlite3 /data/monitor.db ".backup '/data/backup.db'"` e também o
  volume `/app/storages` do GOWA (perder = desemparelhar o WhatsApp).
- **Retenção**: o banco cresce ~1 MB por mil mensagens; para limpar conversas
  antigas use SQL direto (`DELETE FROM messages WHERE local_date < ...`).
- **Multi-device**: vários números no GOWA; o monitor sincroniza todos os slots
  `logged_in` e guarda `device_id` em cada mensagem.

## Segurança

- `/api/v1` exige API key (escopo `ro` só lê; `rw` escreve).
- Painel protegido por Basic Auth quando `DASHBOARD_PASSWORD` está definido —
  obrigatório, pois há dados de pacientes.
- Webhook validado por HMAC (rejeita POST forjado de terceiros).
- Markdown dos relatórios é sanitizado com `nh3` antes de renderizar (o conteúdo
  vem de um agente externo).
- Todas as chamadas de escrita da API ficam auditadas em `api_calls`.

## Aviso

GOWA é um cliente **não oficial** do WhatsApp. Usar pode levar ao bloqueio do
número. Use um número dedicado da clínica, respeite os limites de envio e não
faça disparo em massa. Os dados de conversas são sensíveis: mantenha o painel
atrás de senha/HTTPS e trate conforme a LGPD.

## Roadmap sugerido

Plano priorizado, com impacto, esforço e critérios de sucesso:
[`docs/ROADMAP.md`](docs/ROADMAP.md).

- Relatórios semanais/mensais agregados (`kind: weekly` já é aceito pela API).
- Envio automático do relatório por e-mail/WhatsApp ao gestor às 19h.
- Postgres em vez de SQLite se passar de ~1 M mensagens.
- Múltiplas clínicas (tenant por `device_id`) com escopo de chave por tenant.