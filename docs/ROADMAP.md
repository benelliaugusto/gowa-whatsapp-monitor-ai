# Plano de melhorias do monitor-ia

> Situação: v2 no ar (Easypanel + GOWA), painel do gestor, API para agentes de IA,
> análise diária por conversa e relatório. Este documento prioriza o que aumenta o
> valor real do sistema — não o que é tecnicamente mais bonito.

> **Status (nov/2026): Fases 0 e 1 entregues.** Transcrição local de áudio, tarefas
> acionáveis, agente em cron (`python -m app.agent`, com suporte a Anthropic, OpenAI
> e OpenCode Go), aviso por WhatsApp ao gestor, métricas de atendimento, backup
> automático, **funil de leads com histórico e conversão**, **tendências de 14 dias**
> e **busca/filtros no painel**. Próximo passo: Fase 2 (alertas em tempo real,
> follow-up sugerido e no-show).

## 1. Onde estamos (inventário honesto)

| capacidade | hoje | limitação |
|---|---|---|
| captura de mensagens | webhook + sync de histórico | só texto; **áudio/imagem/documento viram `[audio]`** |
| transcrição/digest | `GET /api/v1/digest` (texto pronto p/ LLM) | depende de alguém rodar o agente |
| análise por conversa | `POST /api/v1/analyses` (classificação, score, resumo) | não vira ação |
| relatório do gestor | `POST /api/v1/reports` (markdown no painel) | passivo; não chega até o gestor |
| tarefas/alertas | não existe | `action_items` do relatório ficam no papel |
| métricas de atendimento | contadores do dia (mensagens/conversas) | falta tempo de resposta, conversas sem resposta |
| funil comercial | classificação solta por conversa | não há estágio, histórico nem conversão |
| tendências | só o dia selecionado | sem 7/30 dias, sem comparativo |
| acesso | 1 senha (Basic Auth) | sem usuários, papéis nem log de acesso |
| integração | nenhuma | não cruza com a agenda da clínica |
| confiabilidade | sync a cada 15 min | sem backup automático nem alerta de falha |
| LGPD | nada específico | sem retenção/expurgo/anonimização |

## 2. O critério de "mais útil"

O gestor de clínica não precisa de mais um relatório para ler: precisa de quatro coisas.

1. **Saber o que fazer hoje** — quem espera resposta, qual lead está esfriando, qual
   remarcação está solta. (Ação, não informação.)
2. **Saber se o atendimento está bom** — tempo de resposta, conversas abandonadas,
   objeções repetidas, qualidade das respostas da recepção.
3. **Não perder dinheiro** — lead quente sem retorno, no-show, cancelamento sem
   reagendamento, áudio ignorado.
4. **Confiar no número** — dado completo (inclusive áudio) e sistema de pé.

Cada item abaixo é justificado por um desses quatro. Se não justifica, não entra.

---

## 3. Fase 0 — Ganhos rápidos (1ª semana)

Alto impacto, esforço pequeno. Ordem sugerida de execução: 1 → 2 → 3 → 4 → 5 → 6.

### 3.1 Transcrição de áudios (maior lacuna atual)

Clínica recebe muito áudio ("queria saber o valor da limpeza") e hoje o registro é
literalmente `[audio]` — a análise fica cega.

- Baixar a mídia pelo GOWA (`GET /message/{id}/download`) no webhook/sync.
- Transcrever (Whisper API ou `faster-whisper` local) e gravar em
  `messages.body` com `msg_type = "audio_transcrito"`.
- Mesmo tratamento para legenda de imagem (já vem no payload) e documento (nome).
- Backfill: transcrever os áudios já no banco.
- **Esforço**: 1–2 dias · **Impacto**: altíssimo · **Decisão**: API (pago, simples)
  ou local (grátis, usa CPU).

### 3.2 Tarefas acionáveis

Transformar sugestões em trabalho rastreável.

- Nova tabela `tasks(id, chat_jid, title, owner, due_at, status, source, created_at)`.
- API: `POST/PATCH /api/v1/tasks` (o agente cria a partir de `action_items`),
  `GET /api/v1/tasks?status=pending`.
- Painel: bloco "Pendências de hoje" no topo, com botão "concluir" e link para a conversa.
- **Esforço**: 1 dia · **Impacto**: alto (muda o uso de "ler" para "agir").

### 3.3 Ciclo automatizado (o relatório precisa aparecer sozinho)

Hoje o relatório só existe se alguém rodar `examples/agent_claude.py`.

- Serviço **Cron** no Easypanel rodando o agente 2×/dia (12h e 19h, fuso da clínica),
  com a chave `rw` do monitor.
- Publicar como `published` e deixar o link no painel.
- **Esforço**: 0,5 dia · **Impacto**: alto (elimina a dependência humana).

### 3.4 Resumo por WhatsApp para o gestor

O gestor já vive no WhatsApp; o painel é o detalhe.

- Às 19h: `POST {GOWA}/send/message` com 5 linhas (conversas, leads quentes,
  agendamentos, pendências) + link do relatório.
- **Esforço**: 0,5 dia · **Impacto**: alto (adoção real).

### 3.5 Métricas de atendimento

Barato de calcular e responde "a recepção está dando conta?".

- Tempo até a 1ª resposta, tempo médio de resposta (só horário comercial),
  conversas sem resposta > 24h, mensagens fora do horário, taxa de resposta.
- Expor em `/api/v1/stats/daily` e no painel (cards + lista "sem resposta").
- **Esforço**: 0,5–1 dia · **Impacto**: alto.

### 3.6 Confiabilidade e segurança

- Backup diário do SQLite (`sqlite3 .backup` + upload para storage/S3) e do volume
  do GOWA (sem ele, o WhatsApp desemparelha).
- Alerta se a sincronização falhar 2× seguidas (mensagem ao gestor/admin).
- Repositório **privado** + rotação do token usado no push.
- **Esforço**: 0,5 dia · **Impacto**: alto (evita perder histórico).

---

## 4. Fase 1 — Fechar o ciclo comercial (2–3 semanas)

### 4.1 Funil de lead com histórico

- `lead_stage` na conversa (`novo → qualificado → agendado → atendido → fechado/perdido`)
  e `lead_events` com data/hora de cada mudança (quem mudou: agente ou humano).
- API `PATCH /api/v1/conversations/{jid}/stage`; o agente atualiza no ciclo diário.
- Painel: funil (quantos em cada etapa) e conversão por etapa/período.
- **Esforço**: 2 dias · **Impacto**: alto (o gestor enxerga o funil, não a conversa solta).

### 4.2 Tendências e comparativos

- Gráficos de 7/14/30 dias (mensagens, conversas, agendamentos, tempo de resposta)
  e variação vs. período anterior.
- **Esforço**: 1 dia · **Impacto**: médio-alto.

### 4.3 Metas e ritmo

- Meta diária/semanal de agendamentos definida pelo gestor; painel mostra ritmo
  ("faltam 3 para a meta") e alerta quando o dia está fraco.
- **Esforço**: 0,5 dia · **Impacto**: médio.

### 4.4 Exportação

- PDF do relatório (para imprimir/repassar) e CSV de conversas/análises.
- **Esforço**: 0,5 dia · **Impacto**: médio.

### 4.5 Busca, filtros e utilidades do painel

- Buscar por nome/telefone; filtrar por classificação, "sem resposta", períodos.
- Nota interna por conversa; botão "abrir no WhatsApp" (`wa.me/<telefone>`).
- **Esforço**: 1 dia · **Impacto**: médio (uso diário).

---

## 5. Fase 2 — Proatividade (3–4 semanas)

### 5.1 Alertas em tempo real

- Lead classificado como quente e sem resposta há 30 min → WhatsApp para
  gestor/recepção; conversa parada há 48h com agendamento pendente → tarefa.
- Regras configuráveis (horário comercial, janela, destinatários).
- **Esforço**: 2 dias · **Impacto**: alto (recupera venda que hoje esfria).

### 5.2 Sugestão de follow-up

- O agente redige a mensagem de retomada (rascunho no painel), **sem envio
  automático** — envio só com aprovação humana (risco de banimento do WhatsApp).
- **Esforço**: 1 dia · **Impacto**: médio-alto.

### 5.3 Qualidade do atendimento

- Scorecard por conversa (clareza, tom, tempo, informação correta) e ranking por
  atendente — **requer identificar quem respondeu** (ver 6.3).
- **Esforço**: 2–3 dias · **Impacto**: médio (gestão de pessoas).

### 5.4 No-show e reagendamento

- Cruzar "agendado" com ausência de comparecimento (mensagem no dia) e, quando
  houver integração de agenda, com o status real. Disparar tarefa de reativação.
- **Esforço**: 1–2 dias (+ integração) · **Impacto**: alto.

---

## 6. Fase 3 — Integrações e escala (1–2 meses)

### 6.1 Agenda da clínica

- Integrar com o sistema de gestão (Clinicorp, Feegow, iClinic, Google Calendar…):
  cruzar conversa × agendamento real → relatório passa a falar de **receita** e
  no-show real, não de estimativa.
- **Esforço**: 3–5 dias (depende de a API existir) · **Impacto**: transformador.

### 6.2 Multiusuário e LGPD

- `users` + sessão/login, papéis (gestor, recepção), log de acesso a dados sensíveis.
- Retenção configurável, expurgo, anonimização para relatórios antigos.
- **Esforço**: 2–3 dias · **Impacto**: alto (dado de paciente exige isso).

### 6.3 Identificar o atendente

- Hoje toda mensagem enviada é "Clinica". Opções: um número por atendente
  (multi-device no GOWA) ou marcação manual por período na recepção.
- **Esforço**: 1–2 dias · **Impacto**: alto (pré-requisito de 5.3).

### 6.4 Postgres e multi-unidade

- Migrar quando passar de ~1M mensagens; `device_id` como tenant para mais de
  uma unidade/número.
- **Esforço**: 3–5 dias · **Impacto**: futuro.

---

## 7. Melhorias técnicas transversais

- **CI no GitHub Actions**: `ruff` + `scripts/smoke_test.py` em cada push (hoje o
  teste é manual).
- **Migrações versionadas** (hoje `CREATE TABLE IF NOT EXISTS` + colunas).
- **Fila para o webhook**: insert síncrono pode perder evento se o processo cair
  entre o 200 e o commit; fila durável + retry.
- **Observabilidade**: logs JSON, `/metrics` (Prometheus), Sentry opcional.
- **Cache/normalização de contatos**: avatares e nomes do WhatsApp hoje não
  aparecem no painel (campo existe, mas não é usado) — usar ou remover.
- **Rate limit** nas rotas de API (proteção contra loop de agente).

## 8. Priorização (impacto × esforço)

| # | melhoria | impacto | esforço | prioridade |
|---|---|---|---|---|
| 3.1 | transcrição de áudio | 5 | 1–2 d | **P0** |
| 3.2 | tarefas acionáveis | 5 | 1 d | **P0** |
| 3.3 | ciclo automatizado (cron) | 5 | 0,5 d | **P0** |
| 3.4 | resumo por WhatsApp | 4 | 0,5 d | **P0** |
| 3.5 | métricas de atendimento | 4 | 0,5–1 d | **P0** |
| 3.6 | backup + alerta de falha | 4 | 0,5 d | **P0** |
| 4.1 | funil de lead | 5 | 2 d | **P1** |
| 4.2 | tendências | 4 | 1 d | **P1** |
| 4.5 | busca/filtros/notas | 3 | 1 d | **P1** |
| 5.1 | alertas em tempo real | 5 | 2 d | **P1** |
| 4.4 | exportação PDF/CSV | 3 | 0,5 d | P2 |
| 4.3 | metas | 3 | 0,5 d | P2 |
| 5.2 | follow-up sugerido | 3 | 1 d | P2 |
| 5.3 | scorecard de qualidade | 3 | 2–3 d | P2 |
| 6.1 | integração agenda | 5 | 3–5 d | P2 |
| 6.2 | multiusuário/LGPD | 4 | 2–3 d | P2 |
| 6.3 | identificar atendente | 3 | 1–2 d | P2 |
| 6.4 | Postgres/multi-unidade | 2 | 3–5 d | P3 |
| 7 | CI, fila, métricas | 3 | 2–3 d | P1/P2 |

## 9. Como saber se ficou mais útil

- Conversas do dia com análise: **> 95%** (hoje depende de execução manual).
- Lead quente sem resposta há mais de 1h: **0** (hoje invisível).
- Tempo médio de 1ª resposta no horário comercial: **< 10 min**.
- Tarefas concluídas por dia: **> 0** (prova de que virou ação).
- Relatório publicado antes das 19h: **todos os dias úteis**.
- Acessos ao painel por semana: acompanhar via `api_calls`/log (mede adoção).
- Falhas de sincronização sem alerta: **0**.

## 10. Decisões que dependem de você

1. **Transcrição**: API (OpenAI/Whisper, ~US$ 0,006/min, simples) ou local
   (`faster-whisper`, sem custo, exige CPU/RAM no servidor)?
2. **Onde roda o agente**: no seu PC (hoje) ou em um serviço **Cron do Easypanel**
   (recomendado — não depende do seu computador ligado)?
3. **Agenda**: a clínica usa algum sistema (Clinicorp/Feegow/iClinic/outro)? Tem API?
   Isso define se 6.1 é viável e o quanto o relatório pode falar de receita.
4. **Atendentes**: quantos respondem o WhatsApp? Um número só ou vários?
5. **Horário comercial** da clínica (para métricas de fora do horário e alertas).

## 11. Sequência sugerida de entregas

- **Semana 1**: 3.3 + 3.4 + 3.6 (relatório aparecendo sozinho e confiável).
- **Semana 2**: 3.1 (transcrição) + 3.5 (métricas de atendimento).
- **Semana 3**: 3.2 (tarefas) + 4.1 (funil).
- **Semana 4**: 5.1 (alertas) + 4.2 (tendências).
- Depois: integrações e multiusuário.
