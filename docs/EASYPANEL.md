# Deploy no Easypanel (v2.x)

Este documento cobre o caminho recomendado: **repositório no GitHub → serviço
"App" com Dockerfile**. No fim, as alternativas (imagem Docker e compose).

## 0. Resumo da decisão: GitHub ou zip?

**Use GitHub (privado).** Motivos:

| | GitHub | zip / Dockerfile colado |
|---|---|---|
| Atualização | `git push` + deploy automático | reupload manual a cada mudança |
| Histórico/rollback | commits, tags, diff | nenhum |
| Revisão de código | PR/review | impossível |
| Build reprodutível | sim (Dockerfile versionado) | só se colar o Dockerfile inteiro |
| Esforço inicial | ~10 min (uma vez) | ~5 min |

O Easypanel **não tem upload de zip** como origem de build. As origens nativas
são: GitHub, imagem de registry (Docker Hub/GHCR) e compose. O `Dockerfile`
colado direto na UI (como você fez na v1, com o `main.py` embutido em heredoc)
funciona, mas vira um arquivo de ~1000 linhas impossível de manter.

Se por algum motivo o GitHub não for possível, a segunda melhor opção é:
build local → push para o Docker Hub → serviço "Docker image" no Easypanel.

## 1. Subir o código no GitHub

Na pasta do projeto (onde está o `Dockerfile`):

```bash
git init
git add .
git commit -m "monitor-ia v2: painel + API para agentes de IA"
git branch -M main
git remote add origin git@github.com:SEU-USUARIO/monitor-ia.git
git push -u origin main
```

> Use repositório **privado**. O `Dockerfile` já está na raiz, que é o que o
> Easypanel espera. O arquivo `.dockerfile` da v1 pode ser apagado do repositório.

## 2. Serviço do GOWA (`gowa-server`)

Você já tem. Só confira/ajuste as variáveis (aba **Environment**):

```
APP_BASIC_AUTH=admin:SENHA-FORTE
APP_PORT=3000
TZ=UTC
WHATSAPP_WEBHOOK=https://monitor.SEU-DOMINIO.com.br/wa-hook
WHATSAPP_WEBHOOK_SECRET=COLE-AQUI-UM-SEGREDO-ALEATORIO-32-BYTES
WHATSAPP_WEBHOOK_EVENTS=message
```

- Gere o segredo com `openssl rand -hex 32`.
- Use a **tag fixa** da imagem (ex.: `v9.8.0`), não `latest`.
- Volume: `/app/storages` (sessão/WhatsApp) e `/app/statics` (mídia/QR).
  **Sem esse volume o pareamento se perde a cada deploy.**
- Depois de salvar, reinicie o GOWA e pareie o número (aba de login/QR do GOWA).

## 3. Serviço do monitor (`monitor-ia`)

Na mesma **project** do Easypanel: **New Service → App**.

**General**
- Nome do serviço: `monitor-ia`
- (opcional) Réplicas: 1 — **não use mais de 1** (SQLite)

**Source**
- Provider: **GitHub**
- Repositório: `SEU-USUARIO/monitor-ia` · Branch: `main`
- Build Path: `/` (raiz do repositório)
- Auto deploy: ligado (webhook do GitHub) — opcional

**Build**
- Builder: **Dockerfile**
- Dockerfile path: `Dockerfile`

**Environment** (cole/ajuste; detalhes em `.env.example`)

```
TIMEZONE=America/Sao_Paulo
DATABASE_PATH=/data/monitor.db

# rede interna do Easypanel: <projeto>_<servico>
GOWA_URL=http://SUA-PROJECT_gowa-server:3000
GOWA_BASIC_AUTH=admin:SENHA-FORTE-DO-GOWA
GOWA_WEBHOOK_SECRET=O-MESMO-SEGREDO-DO-GOWA

SYNC_ENABLED=true
SYNC_ON_STARTUP=true
SYNC_INTERVAL_MINUTES=15
SYNC_WINDOW_DAYS=3

# transcrição local de áudios (o modelo fica no volume, baixa uma vez)
TRANSCRIBE_ENABLED=true
WHISPER_MODEL=small
WHISPER_MODEL_DIR=/data/models
WHISPER_INTERVAL_SECONDS=60

# horário comercial e avisos ao gestor
BUSINESS_HOURS_START=08:00
BUSINESS_HOURS_END=18:00
BUSINESS_DAYS=1,2,3,4,5
MANAGER_WHATSAPP=5511999999999

# backup
BACKUP_DIR=/data/backups
BACKUP_KEEP=14

# API dos agentes de IA — nome:escopo:chave
API_KEYS=claude:rw:COLE-UMA-CHAVE-LONGA-ALEATORIA,grok:ro:COLE-OUTRA-CHAVE

DASHBOARD_USER=gestor
DASHBOARD_PASSWORD=SENHA-DO-PAINEL
```

Gere as chaves com `openssl rand -hex 24` — elas são o "login" do seu agente.

> **Nome do host interno:** o Easypanel usa `<project>_<service>`, trocando `-`
> por `_` quando necessário. Confirme no **Shell** do serviço `monitor-ia`:
> ```bash
> getent hosts SUA-PROJECT_gowa-server
> curl -s -u admin:SENHA http://SUA-PROJECT_gowa-server:3000/health
> ```
> Se não resolver, tente `http://gowa-server:3000`. Ajuste `GOWA_URL` até o
> `GET /api/v1/health` mostrar `"gowa": {"reachable": true}`.

**Storage**
- Mount: **Volume** (gerenciado) em `/data` — guarda `monitor.db`.
- Alternativa: Bind para um path do host. Em ambos, o diretório precisa ser
  gravável pelo processo (a imagem roda como root, então funciona por padrão).

**Domains**
- Host: `monitor.SEU-DOMINIO.com.br`
- Path: `/`
- Internal protocol: `HTTP` · **Target port: `8000`**
- HTTPS: ligado (Let's Encrypt automático), marque como *primary*.

**Deploy** e acompanhe os logs. Esperado:

```
banco inicializado em /data/monitor.db
sincronizacao automatica a cada 15 min
Uvicorn running on http://0.0.0.0:8000
```

**Ports (published)** — não é necessário publicar porta TCP; o domínio já resolve.

## 4. Validar a instalação

```bash
# 1) saúde (público)
curl -s https://monitor.SEU-DOMINIO.com.br/api/v1/health | jq

# 2) o GOWA aparece como alcançável?
#    "gowa": {"reachable": true, "version": "v9.8.0", "devices": 1}

# 3) o webhook está chegando? (sincronize e veja se entram mensagens)
curl -s -X POST https://monitor.SEU-DOMINIO.com.br/api/v1/sync \
  -H "X-API-Key: SUA-CHAVE-rw" -H "Content-Type: application/json" -d '{"days": 2}'

# 4) o digest do dia
curl -s -H "X-API-Key: SUA-CHAVE-rw" \
  "https://monitor.SEU-DOMINIO.com.br/api/v1/digest?date=$(date +%F)&format=text" | head -40

# 5) painel do gestor
#    https://monitor.SEU-DOMINIO.com.br  (login: DASHBOARD_USER/DASHBOARD_PASSWORD)
```

Envios em tempo real: mande uma mensagem para o número pareado e confira
`GET /api/v1/health` → `database.messages` aumentando, ou o log do monitor
(`webhook: +1 mensagem em ...`).

## 5. Conectar o agente de IA

O agente (Claude Code, Grok, n8n, script Python) roda **fora** do Easypanel
(na sua máquina, notebook ou outro serviço) e conversa com:

```
https://monitor.SEU-DOMINIO.com.br/api/v1/digest?date=YYYY-MM-DD&format=text
```

com `X-API-Key: <chave rw>`. Depois grava em `/api/v1/analyses` e
`/api/v1/reports`. O prompt/contrato pronto está em
[`docs/AGENT_PROMPT.md`](AGENT_PROMPT.md) e existe um exemplo executável em
[`examples/agent_claude.py`](../examples/agent_claude.py).

Alternativa: o próprio GOWA expõe um **MCP server** (`http://gowa:3000/mcp`) com
leitura de chats — útil para um agente explorar, mas para gerar relatórios
diários a API do monitor é melhor (digest pronto, persistência e idempotência).

## 6. Alternativa: imagem Docker (sem GitHub)

```bash
docker build -t SEU-USUARIO/monitor-ia:2.0.0 .
docker push SEU-USUARIO/monitor-ia:2.0.0
```

No Easypanel: New Service → App → **Source: Docker image**
`SEU-USUARIO/monitor-ia:2.0.0`, mesma porta/volume/env acima. A desvantagem é que
cada alteração exige build+push manual.

## 7. Alternativa: serviço Compose

O `docker-compose.yml` do repositório sobe GOWA + monitor juntos; no Easypanel
isso equivale a um serviço do tipo **Compose**, colando o arquivo e definindo as
variáveis. Funciona, mas você perde a gestão individual dos dois serviços
(domínio, logs, volumes) — prefira dois serviços "App".

## 8. Atualizações

```bash
git add -A && git commit -m "ajuste no relatório" && git push
```

Com auto deploy ligado, o Easypanel rebuilda sozinho. Para controlar, desligue
auto deploy e use o botão **Deploy**.

Migrações de banco: `app/db.py` usa `CREATE TABLE IF NOT EXISTS` e os índices são
idempotentes — atualizar a aplicação não perde dados do volume.

## 8.1 Serviços Cron (agente e backup)

No Easypanel, **New Service → Cron** usando **a mesma imagem** do monitor
(mesmo repositório, build Dockerfile). Cada cron recebe as variáveis do monitor
(da aba Environment) mais as específicas.

**Agente de IA** (analisa o dia e avisa o gestor):

```
Schedule: 0 12,19 * * *
Command:  python -m app.agent --notify
```

Variáveis extras:

```
# IMPORTANTE: o Cron roda em outro container. Use o host interno do SERVIÇO
# monitor-ia (não 127.0.0.1). Confirme no Shell: getent hosts <projeto>_monitor-ia
MONITOR_API_URL=http://<projeto>_monitor-ia:8000
MONITOR_API_KEY=<a mesma chave rw usada na API>
LLM_PROVIDER=anthropic
LLM_API_KEY=<chave do provedor>
LLM_MODEL=claude-sonnet-4-5
# Grok/xAI: LLM_PROVIDER=openai, LLM_BASE_URL=https://api.x.ai/v1, LLM_MODEL=grok-4
# OpenCode Go: LLM_PROVIDER=opencode, LLM_MODEL=deepseek-v4.1-flash,
#              LLM_BASE_URL=https://opencode.ai/inference/go (chave do Console)
MANAGER_WHATSAPP=5511999999999
```

**Backup do banco**:

```
Schedule: 0 3 * * *
Command:  python -m app.backup --keep 14
```

O agente registra cada execução em `/api/v1/agent-runs`, visível no painel
(`/status`). Se o dia não tiver conversas ele não publica relatório vazio.

> A imagem cresce (~+250 MB) por causa do `faster-whisper`. Se não quiser
> transcrição, defina `TRANSCRIBE_ENABLED=false` — a biblioteca continua na
> imagem, mas nenhum modelo é carregado.

## 9. Backup

O estado inteiro vive em `/data/monitor.db` (+ `-wal`/`-shm`). Backup consistente:

```bash
# no Shell do serviço monitor-ia
sqlite3 /data/monitor.db ".backup '/data/backup-$(date +%F).db'"
```

Baixe pelo gerenciador de arquivos do Easypanel ou copie via `docker cp` no host.
Guarde também o volume do GOWA (`/app/storages`): perdê-lo desemparelha o número.

## 10. Checklist de segurança

- [ ] `API_KEYS` definido (sem chave, `/api/v1` responde 401 — e é o esperado)
- [ ] Escopo `ro` para agentes que só leem; `rw` só para quem publica
- [ ] `DASHBOARD_PASSWORD` definido (painel tem dados de pacientes)
- [ ] `GOWA_WEBHOOK_SECRET` igual nos dois serviços (rejeita POST forjado)
- [ ] `APP_BASIC_AUTH` forte no GOWA (senão qualquer um lê o WhatsApp)
- [ ] HTTPS no domínio do monitor e do GOWA
- [ ] Volume `/data` e `/app/storages` com backup
- [ ] Repositório privado; `.env` **nunca** commitado (`.gitignore` já cobre)

## 11. Problemas comuns

| sintoma | causa provável | correção |
|---|---|---|
| `gowa.reachable: false` | `GOWA_URL` errado/sem rede interna | teste `getent hosts`/`curl` no Shell; use `<projeto>_gowa-server` |
| webhook 401 no log do GOWA | segredo diferente entre GOWA e monitor | igualar `WHATSAPP_WEBHOOK_SECRET` = `GOWA_WEBHOOK_SECRET` |
| chegou mensagem mas o painel está vazio | sincronização ainda não rodou | botão "Sincronizar agora" ou `POST /api/v1/sync` |
| `/api/v1/*` sempre 401 | `API_KEYS` vazio ou header errado | definir `API_KEYS` e enviar `X-API-Key` |
| painel abre mas dados somem no deploy | volume não montado em `/data` | criar o volume e redeployar |
| datas erradas | fuso | `TIMEZONE=America/Sao_Paulo` no monitor; GOWA em UTC (padrão) |
| 502 em `/api/v1/sync` | GOWA fora do ar ou senha errada | ver logs/`/app/info` do GOWA |