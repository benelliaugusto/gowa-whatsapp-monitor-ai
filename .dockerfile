FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.110.0 \
    uvicorn==0.28.0 \
    openai==1.14.0 \
    requests \
    python-multipart \
    tzdata

RUN cat << 'PYEOF' > main.py
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
import sqlite3
import requests
import os
import json
import time
import html as html_lib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

app = FastAPI()
DATABASE = "/data/mensagens.db"
GOWA_URL = os.getenv("GOWA_URL", "http://gowa-server:3000")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TZ_BR = ZoneInfo("America/Sao_Paulo")
os.makedirs("/data", exist_ok=True)


def esc(value):
    if value is None: return ""
    return html_lib.escape(str(value))


def normalizar_remetente(remetente):
    if not remetente: return ""
    return remetente.split("@")[0]


def formatar_timestamp_br(ts):
    if not ts: return datetime.now(TZ_BR).strftime("%Y-%m-%d %H:%M:%S")
    try:
        ts_str = str(ts)
        if "T" in ts_str:
            dt_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            num = float(ts_str)
            if num > 1_000_000_000_000: num = num / 1000.0
            dt_utc = datetime.fromtimestamp(num, tz=timezone.utc)
        return dt_utc.astimezone(TZ_BR).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now(TZ_BR).strftime("%Y-%m-%d %H:%M:%S")


def formatar_hora_conversa(ts_str):
    if not ts_str: return ""
    try:
        dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        agora = datetime.now()
        if dt.date() == agora.date(): return dt.strftime("%H:%M")
        if (agora.date() - dt.date()).days == 1: return "Ontem"
        return dt.strftime("%d/%m")
    except Exception: return ""


def formatar_hora_msg(ts_str):
    if not ts_str: return ""
    try:
        dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        agora = datetime.now()
        if dt.date() == agora.date(): return dt.strftime("%H:%M")
        if (agora.date() - dt.date()).days == 1: return "Ontem " + dt.strftime("%H:%M")
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ts_str[11:16] if len(ts_str) > 11 else ts_str


def badge_class(classificacao):
    c = (classificacao or "").lower()
    if "quente" in c or "🔥" in c: return "badge-hot"
    if "frio" in c or "❄" in c: return "badge-cold"
    if "fechado" in c or "✅" in c: return "badge-closed"
    if "sem analise" in c or "pendente" in c or "⏳" in c: return "badge-pending"
    return "badge-neutral"


def extrair_lista(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for chave in ("results", "data", "messages", "items"):
            valor = payload.get(chave)
            if isinstance(valor, list): return [x for x in valor if isinstance(x, dict)]
            if isinstance(valor, dict):
                for sub in ("data", "results", "messages", "items"):
                    sv = valor.get(sub)
                    if isinstance(sv, list): return [x for x in sv if isinstance(x, dict)]
    return []


def extrair_pagination(payload):
    if isinstance(payload, dict):
        r = payload.get("results")
        if isinstance(r, dict):
            pag = r.get("pagination")
            if isinstance(pag, dict): return pag
    return {}


def extrair_chat_info(payload):
    if isinstance(payload, dict):
        r = payload.get("results")
        if isinstance(r, dict):
            ci = r.get("chat_info")
            if isinstance(ci, dict): return ci
    return {}


def extrair_texto(m):
    if not isinstance(m, dict): return ""
    for k in ("content", "body", "text"):
        v = m.get(k)
        if isinstance(v, str) and v.strip(): return v
    msg = m.get("message")
    if isinstance(msg, dict):
        c = msg.get("conversation")
        if isinstance(c, str) and c.strip(): return c
    mt = m.get("media_type") or ""
    if mt:
        emoji = {"image": "📷", "video": "🎥", "audio": "🎵", "ptt": "🎵",
                 "document": "📎", "sticker": "🎨", "gif": "🎞️"}.get(mt, "📎")
        fn = m.get("filename") or ""
        return f"{emoji} {mt.capitalize()}: {fn}" if fn else f"{emoji} {mt.capitalize()}"
    return ""


def extrair_from_me(m):
    if not isinstance(m, dict): return False
    return bool(m.get("is_from_me") or m.get("from_me") or m.get("fromMe"))


def extrair_timestamp(m):
    if not isinstance(m, dict): return ""
    return m.get("timestamp") or m.get("Timestamp") or m.get("time") or ""


def extrair_gowa_id(m):
    if not isinstance(m, dict): return None
    for k in ("id", "ID", "message_id", "messageId"):
        v = m.get(k)
        if isinstance(v, str) and v.strip(): return v
    return None


def init_db():
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mensagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gowa_id TEXT,
                device_id TEXT,
                remetente TEXT,
                mensagem TEXT,
                tipo TEXT DEFAULT 'in',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analises_leads (
                remetente TEXT PRIMARY KEY, classificacao TEXT,
                resumo TEXT, orientacao TEXT, ultima_atualizacao DATETIME
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contatos (
                remetente TEXT PRIMARY KEY, nome TEXT,
                avatar_url TEXT, device_id TEXT, atualizado_em DATETIME
            )
        """)
        # Migrações
        cur.execute("PRAGMA table_info(mensagens)")
        cols = {r[1] for r in cur.fetchall()}
        if "gowa_id" not in cols:
            cur.execute("ALTER TABLE mensagens ADD COLUMN gowa_id TEXT")
            print("[migrate] coluna gowa_id adicionada em mensagens")
        for tabela in ("contatos", "analises_leads"):
            cur.execute(f"PRAGMA table_info({tabela})")
            cols = {r[1] for r in cur.fetchall()}
            if tabela == "contatos" and "device_id" not in cols:
                cur.execute("ALTER TABLE contatos ADD COLUMN device_id TEXT")
            if tabela == "contatos" and "avatar_url" not in cols:
                cur.execute("ALTER TABLE contatos ADD COLUMN avatar_url TEXT")
        # Índices para dedup rápida
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gowa_id ON mensagens(gowa_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_remetente ON mensagens(remetente)")
        conn.commit()


init_db()


def inserir_mensagem(cur, device_id, remetente, texto, tipo, ts, gowa_id=None):
    """Insere se não existir. Retorna True se inseriu.
    Chave primária de dedup: gowa_id. Fallback: (remetente, mensagem, timestamp)."""
    if gowa_id:
        cur.execute("SELECT id FROM mensagens WHERE gowa_id = ?", (gowa_id,))
        if cur.fetchone(): return False
    cur.execute("SELECT id FROM mensagens WHERE remetente=? AND mensagem=? AND timestamp=?",
                (remetente, texto, ts))
    if cur.fetchone(): return False
    cur.execute("""
        INSERT INTO mensagens (gowa_id, device_id, remetente, mensagem, tipo, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (gowa_id, device_id, remetente, texto, tipo, ts))
    return True


@app.get("/favicon.ico")
def favicon(): return Response(status_code=204)


@app.get("/debug/chat/{numero}")
def debug_chat(numero: str, limit: int = 20):
    chat_jid = f"{numero}@s.whatsapp.net"
    try:
        r = requests.get(f"{GOWA_URL}/chat/{chat_jid}/messages", params={"limit": limit, "offset": 0}, timeout=15)
        j = r.json()
        return JSONResponse({
            "status_code": r.status_code,
            "lista_extraida_count": len(extrair_lista(j)),
            "pagination": extrair_pagination(j),
            "chat_info": extrair_chat_info(j),
            "primeiro_item": extrair_lista(j)[0] if extrair_lista(j) else None,
        })
    except Exception as e:
        return JSONResponse({"erro": str(e)})


@app.get("/avatar/{numero}")
def proxy_avatar(numero: str, device_id: Optional[str] = None):
    headers = {"X-Device-Id": device_id} if device_id else {}
    try:
        r = requests.get(f"{GOWA_URL}/user/avatar", params={"phone": numero}, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                url = None
                if isinstance(data.get("results"), dict): url = data["results"].get("url")
                elif isinstance(data.get("url"), str): url = data["url"]
                if url:
                    img = requests.get(url, timeout=5)
                    if img.status_code == 200:
                        return Response(content=img.content, media_type="image/jpeg")
    except Exception as e:
        print(f"[avatar] {e}")
    inicial = numero[0] if numero else "?"
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="#10b981"/><text x="50" y="58" font-size="44" font-family="Inter,sans-serif" font-weight="700" text-anchor="middle" dominant-baseline="middle" fill="white">{inicial}</text></svg>'
    return Response(content=svg, media_type="image/svg+xml")


def obter_dispositivos():
    try:
        r = requests.get(f"{GOWA_URL}/devices", timeout=5)
        if r.status_code == 200: return extrair_lista(r.json())
    except Exception: pass
    return []


@app.post("/webhook")
async def receber_webhook(request: Request):
    try:
        dados = await request.json()
        device_id = dados.get("device_id")
        event_type = (dados.get("event") or "").lower()
        payload = dados.get("payload") or {}
        tipo = "out" if (payload.get("from_me") or payload.get("fromMe") or "sent" in event_type) else "in"
        remetente = normalizar_remetente(payload.get("from") or payload.get("chat_id") or payload.get("to") or "")
        texto = extrair_texto({"message": payload.get("message"), "content": payload.get("body")})
        if texto and len(texto) < 5000:
            from_name = payload.get("from_name") or payload.get("push_name")
            if remetente:
                avatar = f"/avatar/{remetente}?device_id={device_id}" if device_id else f"/avatar/{remetente}"
                with sqlite3.connect(DATABASE) as conn:
                    conn.execute("""
                        INSERT INTO contatos (remetente, nome, device_id, avatar_url, atualizado_em)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(remetente) DO UPDATE SET
                            nome=CASE WHEN ? != '' THEN ? ELSE contatos.nome END,
                            device_id=excluded.device_id, avatar_url=excluded.avatar_url, atualizado_em=CURRENT_TIMESTAMP
                    """, (remetente, from_name.strip() if from_name else remetente, device_id, avatar, from_name, from_name))
                    conn.commit()
            ts = formatar_timestamp_br(payload.get("timestamp") or payload.get("t"))
            gowa_id = extrair_gowa_id(payload)
            with sqlite3.connect(DATABASE) as conn:
                inserir_mensagem(conn.cursor(), device_id, remetente, texto, tipo, ts, gowa_id)
                conn.commit()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def sincronizar_contatos_gowa():
    dispositivos = obter_dispositivos() or [{"device_id": None}]
    for d in dispositivos:
        dev_id = d.get("device_id") if isinstance(d, dict) else None
        headers = {"X-Device-Id": dev_id} if dev_id else {}
        try:
            r = requests.get(f"{GOWA_URL}/chats", headers=headers, params={"limit": 100}, timeout=10)
            if r.status_code == 200:
                itens = extrair_lista(r.json())
                with sqlite3.connect(DATABASE) as conn:
                    for item in itens:
                        jid = item.get("jid") or item.get("JID") or ""
                        if "@s.whatsapp.net" not in jid: continue
                        num = normalizar_remetente(jid)
                        nome = item.get("name") or item.get("push_name") or num
                        avatar = f"/avatar/{num}?device_id={dev_id}" if dev_id else f"/avatar/{num}"
                        conn.execute("""
                            INSERT INTO contatos (remetente, nome, avatar_url, device_id, atualizado_em)
                            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(remetente) DO UPDATE SET
                                nome=CASE WHEN excluded.nome != excluded.remetente THEN excluded.nome ELSE contatos.nome END,
                                avatar_url=excluded.avatar_url,
                                device_id=excluded.device_id, atualizado_em=CURRENT_TIMESTAMP
                        """, (num, nome, avatar, dev_id))
                    conn.commit()
        except Exception as e:
            print(f"[sync] {e}")


@app.post("/puxar-historico")
def puxar_historico(chat: str = Form(None), full: str = Form("false")):
    puxar_tudo = (full.lower() == "true")

    if not chat:
        with sqlite3.connect(DATABASE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT remetente FROM contatos")
            chats_alvo = [r[0] for r in cur.fetchall()]
        redirect_base = "/"
        max_paginas, limite = 1, 20
        modo = "GERAL"
    else:
        chats_alvo = [normalizar_remetente(chat)]
        redirect_base = f"/?chat={normalizar_remetente(chat)}"
        if puxar_tudo:
            max_paginas, limite = 100, 50
            modo = "COMPLETO"
        else:
            max_paginas, limite = 2, 20
            modo = "RECENTES"

    print(f"[hist] ====== {modo} — {len(chats_alvo)} chat(s) ======")
    sincronizar_contatos_gowa()
    total_inseridas = 0
    total_servidor = 0

    for c in chats_alvo:
        if not c: continue
        try:
            dev_id = None
            with sqlite3.connect(DATABASE) as conn:
                cur = conn.cursor()
                cur.execute("SELECT device_id FROM mensagens WHERE remetente=? AND device_id IS NOT NULL ORDER BY id DESC LIMIT 1", (c,))
                row = cur.fetchone()
                if row: dev_id = row[0]
                else:
                    cur.execute("SELECT device_id FROM contatos WHERE remetente=?", (c,))
                    row = cur.fetchone()
                    if row: dev_id = row[0]
            headers = {"X-Device-Id": dev_id} if dev_id else {}

            offset = 0
            pagina = 0
            msgs = []
            total_declarado = None

            while pagina < max_paginas:
                r = requests.get(f"{GOWA_URL}/chat/{c}@s.whatsapp.net/messages",
                                 headers=headers, params={"limit": limite, "offset": offset}, timeout=20)
                if r.status_code != 200:
                    print(f"[hist] {c} HTTP {r.status_code}")
                    break
                try: j = r.json()
                except: break
                data = extrair_lista(j)
                pag = extrair_pagination(j)
                if pag.get("total") is not None: total_declarado = pag["total"]

                ci = extrair_chat_info(j)
                if ci.get("name"):
                    with sqlite3.connect(DATABASE) as conn:
                        conn.execute("UPDATE contatos SET nome=? WHERE remetente=? AND (nome IS NULL OR nome=?)",
                                     (ci["name"], c, c))
                        conn.commit()

                print(f"[hist] {c} pág {pagina+1}: {len(data)} (offset={offset}, total={total_declarado})")

                if not data: break
                msgs.extend(data)

                if total_declarado is not None:
                    if offset + len(data) >= total_declarado: break
                else:
                    if len(data) < limite: break

                offset += len(data)
                pagina += 1
                time.sleep(0.2)

            print(f"[hist] {c}: {len(msgs)} do servidor")
            total_servidor += len(msgs)

            msgs.sort(key=lambda x: str(extrair_timestamp(x)))
            inseridas = 0
            with sqlite3.connect(DATABASE) as conn:
                cur = conn.cursor()
                for m in msgs:
                    texto = extrair_texto(m)
                    if not texto or len(texto) > 5000: continue
                    tipo = "out" if extrair_from_me(m) else "in"
                    ts = formatar_timestamp_br(extrair_timestamp(m))
                    gid = extrair_gowa_id(m)
                    if inserir_mensagem(cur, dev_id or "sincronizado", c, texto, tipo, ts, gid):
                        total_inseridas += 1
                        inseridas += 1
                conn.commit()
            print(f"[hist] {c} ✓ {inseridas} novas")
        except Exception as e:
            print(f"[hist] {c} EXCEPTION: {type(e).__name__}: {e}")

    print(f"[hist] ====== FIM: {total_inseridas} novas / {total_servidor} servidor ======")
    sep = "&" if "?" in redirect_base else "?"
    return RedirectResponse(url=f"{redirect_base}{sep}ok={total_inseridas}&srv={total_servidor}&modo={modo}", status_code=303)


# ============================================================
# HTML/CSS (definido fora das f-strings para evitar conflito)
# ============================================================

HEAD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GOWA Monitor — WhatsApp Business</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100vh; overflow: hidden; font-family: 'Inter', system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  body { background: #f1f5f9; color: #0f172a; }

  .app { display: flex; height: 100vh; }

  /* ============ SIDEBAR ============ */
  .sidebar { width: 420px; background: #fff; border-right: 1px solid #e2e8f0; display: flex; flex-direction: column; flex-shrink: 0; }

  .sidebar-header {
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
    padding: 18px 20px; color: white;
    display: flex; justify-content: space-between; align-items: center;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand-icon {
    width: 40px; height: 40px; background: rgba(255,255,255,0.22);
    border-radius: 11px; display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(10px);
  }
  .brand h1 { font-size: 16px; font-weight: 700; margin: 0; letter-spacing: -0.3px; }
  .brand p { font-size: 11px; opacity: 0.85; margin: 2px 0 0; font-weight: 500; }

  .btn-ia {
    background: rgba(255,255,255,0.18); border: 1px solid rgba(255,255,255,0.28);
    color: white; padding: 9px 14px; border-radius: 9px;
    font-size: 12px; font-weight: 600; cursor: pointer;
    display: flex; align-items: center; gap: 6px; transition: all 0.15s;
    font-family: inherit;
  }
  .btn-ia:hover { background: rgba(255,255,255,0.3); transform: translateY(-1px); }

  .sidebar-body { padding: 14px 16px; border-bottom: 1px solid #f1f5f9; display: flex; flex-direction: column; gap: 10px; }

  .search-box { position: relative; }
  .search-box svg { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); width: 16px; height: 16px; color: #94a3b8; }
  .search-box input {
    width: 100%; padding: 10px 12px 10px 38px;
    border: 1px solid #e2e8f0; border-radius: 10px;
    font-size: 13px; background: #f8fafc; outline: none;
    transition: all 0.15s; font-family: inherit; color: #0f172a;
  }
  .search-box input:focus { border-color: #10b981; background: #fff; box-shadow: 0 0 0 3px rgba(16,185,129,0.1); }
  .search-box input::placeholder { color: #94a3b8; }

  .channel-select select {
    width: 100%; padding: 10px 32px 10px 12px;
    border: 1px solid #e2e8f0; border-radius: 10px;
    font-size: 13px; background: #fff; cursor: pointer; outline: none;
    font-family: inherit; font-weight: 500; color: #334155;
    appearance: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2.5'><polyline points='6 9 12 15 18 9'/></svg>");
    background-repeat: no-repeat; background-position: right 12px center;
  }

  .action-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .action-btn {
    padding: 9px 12px; border-radius: 9px;
    font-size: 12px; font-weight: 600; cursor: pointer; border: 1px solid;
    font-family: inherit; display: flex; align-items: center; justify-content: center; gap: 6px;
    transition: all 0.15s;
  }
  .action-btn.primary { background: #ecfdf5; color: #047857; border-color: #a7f3d0; }
  .action-btn.primary:hover { background: #d1fae5; }
  .action-btn.secondary { background: #f8fafc; color: #475569; border-color: #e2e8f0; }
  .action-btn.secondary:hover { background: #f1f5f9; }

  .conversations { flex: 1; overflow-y: auto; background: #fff; }
  .conversations::-webkit-scrollbar { width: 6px; }
  .conversations::-webkit-scrollbar-thumb { background: #e2e8f0; border-radius: 3px; }

  .conv-item {
    display: flex; align-items: center; padding: 12px 16px;
    gap: 12px; cursor: pointer;
    border-bottom: 1px solid #f8fafc;
    transition: background 0.12s;
    text-decoration: none; color: inherit; position: relative;
  }
  .conv-item:hover { background: #f8fafc; }
  .conv-item.active { background: #ecfdf5; }
  .conv-item.active::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: #10b981; }

  .conv-avatar { width: 48px; height: 48px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: #f1f5f9; }
  .conv-info { flex: 1; min-width: 0; }
  .conv-top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .conv-name { font-weight: 600; font-size: 14px; color: #0f172a; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .conv-time { font-size: 11px; color: #94a3b8; flex-shrink: 0; font-weight: 500; }
  .conv-preview { font-size: 12.5px; color: #64748b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 2px; }

  .conv-badge { font-size: 9.5px; padding: 2px 7px; border-radius: 6px; font-weight: 700; flex-shrink: 0; }

  .sidebar-footer { padding: 10px; border-top: 1px solid #f1f5f9; text-align: center; }
  .btn-danger-text {
    background: none; border: none; color: #ef4444;
    font-size: 11px; cursor: pointer; font-family: inherit; font-weight: 500;
    padding: 6px 12px; border-radius: 6px; transition: background 0.15s;
    display: inline-flex; align-items: center; gap: 5px;
  }
  .btn-danger-text:hover { background: #fef2f2; }

  /* ============ CHAT AREA ============ */
  .chat-area {
    flex: 1; display: flex; flex-direction: column;
    background: #efeae2; position: relative;
  }
  .chat-area::before {
    content: ''; position: absolute; inset: 0;
    background-image: radial-gradient(circle, rgba(255,255,255,0.35) 1px, transparent 1px);
    background-size: 24px 24px; pointer-events: none; opacity: 0.5;
  }

  .chat-header {
    background: #fff; padding: 12px 24px;
    border-bottom: 1px solid #e2e8f0;
    display: flex; justify-content: space-between; align-items: center;
    gap: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    z-index: 10; position: relative;
  }
  .chat-user { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .chat-avatar { width: 44px; height: 44px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: #f1f5f9; }
  .chat-user-info { min-width: 0; }
  .chat-name { font-weight: 700; font-size: 15px; margin: 0; color: #0f172a; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chat-subtitle { font-size: 12px; color: #64748b; margin: 2px 0 0; font-weight: 500; }

  .chat-actions { display: flex; gap: 6px; align-items: center; flex-shrink: 0; }
  .chat-btn {
    padding: 7px 12px; border-radius: 8px;
    font-size: 11.5px; font-weight: 600; cursor: pointer;
    border: 1px solid; font-family: inherit;
    display: inline-flex; align-items: center; gap: 5px;
    transition: all 0.15s; text-decoration: none;
  }
  .chat-btn.blue { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
  .chat-btn.blue:hover { background: #dbeafe; }
  .chat-btn.purple { background: #f5f3ff; color: #6d28d9; border-color: #ddd6fe; }
  .chat-btn.purple:hover { background: #ede9fe; }
  .chat-btn.gray { background: #f8fafc; color: #475569; border-color: #e2e8f0; }
  .chat-btn.gray:hover { background: #f1f5f9; }

  .ia-panel {
    background: linear-gradient(135deg, #f0fdf4 0%, #ecfdf5 100%);
    border-bottom: 1px solid #d1fae5;
    padding: 14px 24px; position: relative; z-index: 5;
    display: flex; gap: 20px; align-items: center; flex-wrap: wrap;
  }
  .ia-block { display: flex; flex-direction: column; gap: 4px; }
  .ia-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; color: #059669; }
  .ia-badge { display: inline-block; padding: 4px 10px; border-radius: 8px; font-size: 12px; font-weight: 700; }
  .ia-item { font-size: 12.5px; color: #334155; line-height: 1.5; flex: 1; min-width: 200px; }
  .ia-item strong { color: #0f172a; font-weight: 600; }

  .messages {
    flex: 1; overflow-y: auto;
    padding: 24px 5%;
    display: flex; flex-direction: column; gap: 4px;
    position: relative; z-index: 1;
  }
  .messages::-webkit-scrollbar { width: 8px; }
  .messages::-webkit-scrollbar-track { background: transparent; }
  .messages::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
  .messages::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

  .msg {
    max-width: 65%; padding: 8px 12px 6px;
    border-radius: 12px; font-size: 14px; line-height: 1.45;
    box-shadow: 0 1px 1px rgba(0,0,0,0.06);
    word-wrap: break-word; margin-bottom: 2px;
    animation: fadeIn 0.2s ease-out;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  .msg-in { background: #fff; align-self: flex-start; border-top-left-radius: 4px; }
  .msg-out { background: #d9fdd3; align-self: flex-end; border-top-right-radius: 4px; }
  .msg-text { white-space: pre-wrap; color: #0f172a; }
  .msg-time { font-size: 10px; color: #94a3b8; text-align: right; margin-top: 3px; font-weight: 500; }

  .empty-state {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    color: #94a3b8; gap: 14px; padding: 40px;
    position: relative; z-index: 1;
  }
  .empty-state svg { width: 72px; height: 72px; opacity: 0.6; }
  .empty-state h3 { margin: 0; font-size: 16px; font-weight: 600; color: #64748b; }
  .empty-state p { margin: 0; font-size: 13px; text-align: center; max-width: 300px; line-height: 1.5; }

  /* ============ TOAST ============ */
  .toast {
    position: fixed; top: 20px; right: 20px; z-index: 1000;
    padding: 14px 18px; border-radius: 12px;
    box-shadow: 0 10px 25px rgba(0,0,0,0.1);
    display: flex; gap: 12px; align-items: flex-start;
    animation: slideIn 0.35s cubic-bezier(0.16, 1, 0.3, 1);
    max-width: 360px;
  }
  @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
  .toast.success { background: #ecfdf5; border-left: 4px solid #10b981; color: #065f46; }
  .toast.warning { background: #fffbeb; border-left: 4px solid #f59e0b; color: #92400e; }
  .toast-title { font-weight: 700; font-size: 13px; margin-bottom: 4px; }
  .toast-body { font-size: 12px; line-height: 1.45; }
  .toast-close { background: none; border: none; cursor: pointer; font-size: 18px; color: inherit; opacity: 0.5; padding: 0 0 0 8px; }

  /* ============ BADGES ============ */
  .badge-hot { background: #fee2e2; color: #991b1b; }
  .badge-cold { background: #dbeafe; color: #1e40af; }
  .badge-closed { background: #d1fae5; color: #065f46; }
  .badge-pending { background: #fef3c7; color: #92400e; }
  .badge-neutral { background: #e2e8f0; color: #334155; }

  /* Contacts page */
  .page-container { max-width: 1000px; margin: 0 auto; padding: 32px; }
  .card { background: #fff; border-radius: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); padding: 28px; }
  .page-title { font-size: 22px; font-weight: 700; margin: 0 0 20px; color: #0f172a; }
  .contact-row { display: flex; align-items: center; gap: 14px; padding: 12px 8px; border-bottom: 1px solid #f1f5f9; }
  .contact-row:last-child { border-bottom: none; }
</style>
</head>
"""

JS_SCRIPT = """<script>
document.addEventListener('DOMContentLoaded', () => {
  const chatEl = document.getElementById('chat-container');
  if (chatEl) chatEl.scrollTop = chatEl.scrollHeight;
  const search = document.getElementById('search-input');
  if (search) {
    search.addEventListener('input', e => {
      const q = e.target.value.toLowerCase().trim();
      document.querySelectorAll('.conv-item').forEach(el => {
        el.style.display = (!q || el.dataset.search.includes(q)) ? '' : 'none';
      });
    });
  }
});
</script>"""


# ============================================================
# PAINEL PRINCIPAL
# ============================================================
@app.get("/", response_class=HTMLResponse)
def painel_gestor(chat: str = None, device_id: str = None, ok: str = None, srv: str = None, modo: str = None):
    chat = normalizar_remetente(chat) if chat else None
    dispositivos = obter_dispositivos()

    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        where_dev = "AND c.device_id = ?" if device_id else ""
        params_dev = (device_id,) if device_id else ()
        cur.execute(f"""
            SELECT c.remetente, c.nome, c.avatar_url, MAX(m.timestamp) as ultima_msg,
                   (SELECT mensagem FROM mensagens WHERE remetente=c.remetente ORDER BY timestamp DESC LIMIT 1) as preview
            FROM contatos c
            INNER JOIN mensagens m ON c.remetente = m.remetente
            WHERE c.remetente IS NOT NULL {where_dev}
            GROUP BY c.remetente
            ORDER BY ultima_msg DESC
        """, params_dev)
        contatos = [dict(r) for r in cur.fetchall()]
        if not chat and contatos: chat = contatos[0]["remetente"]
        cur.execute("SELECT * FROM analises_leads")
        analises = {r["remetente"]: dict(r) for r in cur.fetchall()}
        historico = []
        if chat:
            cur.execute("SELECT * FROM mensagens WHERE remetente=? ORDER BY timestamp ASC", (chat,))
            historico = [dict(r) for r in cur.fetchall()]

    analise = analises.get(chat) or {"classificacao": "⏳ Sem Analise", "resumo": "Clique em Rodar IA.", "orientacao": "Aguardando."}

    # Toast
    toast_html = ""
    if ok is not None and srv is not None:
        tipo = "success" if int(ok) > 0 else "warning"
        toast_html = f'''
        <div class="toast {tipo}" id="toast">
            <div style="flex:1">
                <div class="toast-title">✅ Sincronização concluída</div>
                <div class="toast-body">Modo <strong>{esc(modo or "")}</strong>: GOWA retornou <strong>{esc(srv)}</strong>, inseridas <strong>{esc(ok)}</strong> novas.</div>
            </div>
            <button class="toast-close" onclick="document.getElementById('toast').remove()">×</button>
        </div>'''

    # ========== SIDEBAR ==========
    dev_options = "".join([f'<option value="{d.get("device_id")}" {"selected" if d.get("device_id")==device_id else ""}>{esc(d.get("name") or d.get("device_id"))}</option>' for d in dispositivos if isinstance(d, dict)])

    sidebar = f'''
    <aside class="sidebar">
        <div class="sidebar-header">
            <div class="brand">
                <div class="brand-icon">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                </div>
                <div>
                    <h1>GOWA Monitor</h1>
                    <p>WhatsApp Business</p>
                </div>
            </div>
            <form action="/atualizar-tudo" method="post">
                <button class="btn-ia" type="submit">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/></svg>
                    Rodar IA
                </button>
            </form>
        </div>
        <div class="sidebar-body">
            <div class="search-box">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
                <input id="search-input" type="text" placeholder="Buscar conversa...">
            </div>
            <div class="channel-select">
                <select onchange="window.location.href='/?device_id='+this.value+'&chat={chat or ''}'">
                    <option value="">📱 Todos os Canais</option>
                    {dev_options}
                </select>
            </div>
            <div class="action-buttons">
                <form action="/puxar-historico" method="post">
                    <button class="action-btn primary" type="submit" style="width:100%">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
                        Sincronizar
                    </button>
                </form>
                <a href="/contatos" class="action-btn secondary" style="text-decoration:none">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                    Contatos
                </a>
            </div>
        </div>
        <div class="conversations">'''

    if not contatos:
        sidebar += '<div style="padding:32px 20px;text-align:center;color:#94a3b8;font-size:13px;">Nenhuma conversa ainda.<br>Clique em <strong>Sincronizar</strong>.</div>'
    else:
        for c in contatos:
            ativo = "active" if c["remetente"] == chat else ""
            prev = c["preview"] or ""
            if len(prev) > 40: prev = prev[:40] + "..."
            avatar = c["avatar_url"] or f"/avatar/{c['remetente']}"
            hora = formatar_hora_conversa(c["ultima_msg"])
            analise_c = (analises.get(c["remetente"]) or {}).get("classificacao", "")
            bc = badge_class(analise_c) if analise_c else ""
            badge_html = f'<span class="conv-badge {bc}">{esc(analise_c)}</span>' if analise_c and "sem analise" not in analise_c.lower() else ""
            search_data = (f"{c['nome'] or ''} {c['remetente']}").lower()
            sidebar += f'''
            <a href="/?chat={c['remetente']}&device_id={device_id or ''}" class="conv-item {ativo}" data-search="{esc(search_data)}">
                <img src="{avatar}" class="conv-avatar" loading="lazy" onerror="this.onerror=null;this.src='/avatar/{c['remetente']}'">
                <div class="conv-info">
                    <div class="conv-top">
                        <span class="conv-name">{esc(c['nome'] or c['remetente'])}</span>
                        <span class="conv-time">{hora}</span>
                    </div>
                    <div class="conv-top">
                        <span class="conv-preview">{esc(prev) or '<em style="color:#cbd5e1">Sem mensagens de texto</em>'}</span>
                        {badge_html}
                    </div>
                </div>
            </a>'''

    sidebar += '''
        </div>
        <div class="sidebar-footer">
            <form action="/admin/limpar-banco" method="post" onsubmit="return confirm('Apagar TODAS as conversas e recriar o banco? Esta ação é irreversível.')">
                <button class="btn-danger-text" type="submit">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                    Limpar Banco (Debug)
                </button>
            </form>
        </div>
    </aside>'''

    # ========== CHAT ==========
    if chat:
        nome_chat = next((c["nome"] for c in contatos if c["remetente"] == chat), chat)
        avatar_chat = next((c["avatar_url"] for c in contatos if c["remetente"] == chat), f"/avatar/{chat}")
        bc_main = badge_class(analise["classificacao"])

        bubbles = ""
        if not historico:
            bubbles = """
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                <h3>Nenhuma mensagem ainda</h3>
                <p>Clique em <strong>Recentes</strong> ou <strong>Completo</strong> para puxar o histórico.</p>
            </div>"""
        else:
            for m in historico:
                cls = "msg-out" if m["tipo"] == "out" else "msg-in"
                hora = formatar_hora_msg(m["timestamp"])
                bubbles += f'''
                <div class="msg {cls}">
                    <div class="msg-text">{esc(m['mensagem'])}</div>
                    <div class="msg-time">{hora}</div>
                </div>'''

        chat_html = f'''
        <main class="chat-area">
            <header class="chat-header">
                <div class="chat-user">
                    <img src="{avatar_chat}" class="chat-avatar" onerror="this.onerror=null;this.src='/avatar/{chat}'">
                    <div class="chat-user-info">
                        <h2 class="chat-name">{esc(nome_chat)}</h2>
                        <p class="chat-subtitle">{esc(chat)} • {len(historico)} mensagens</p>
                    </div>
                </div>
                <div class="chat-actions">
                    <form action="/puxar-historico" method="post" style="margin:0">
                        <input type="hidden" name="chat" value="{esc(chat)}">
                        <input type="hidden" name="full" value="false">
                        <button class="chat-btn blue" type="submit" title="Últimas 40 mensagens">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                            Recentes
                        </button>
                    </form>
                    <form action="/puxar-historico" method="post" style="margin:0">
                        <input type="hidden" name="chat" value="{esc(chat)}">
                        <input type="hidden" name="full" value="true">
                        <button class="chat-btn purple" type="submit" title="Todo o histórico">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="2" y="4" width="20" height="5" rx="1"/><path d="M4 9v9a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9"/><path d="M10 13h4"/></svg>
                            Completo
                        </button>
                    </form>
                    <a href="/debug/chat/{esc(chat)}" target="_blank" class="chat-btn gray" title="Ver JSON do GOWA">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="m8 2 1.88 1.88"/><path d="M14.12 3.88 16 2"/><path d="M9 7.13v-1a3 3 0 1 1 6 0v1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/><path d="M12 20v-9"/></svg>
                        Debug
                    </a>
                    <span class="ia-badge {bc_main}" style="margin-left:4px">{esc(analise['classificacao'])}</span>
                </div>
            </header>
            <div class="ia-panel">
                <div class="ia-block">
                    <span class="ia-label">Classificação</span>
                    <span class="ia-badge {bc_main}">{esc(analise['classificacao'])}</span>
                </div>
                <div class="ia-item">
                    <div class="ia-label">💡 Resumo Comercial</div>
                    {esc(analise['resumo'])}
                </div>
                <div class="ia-item">
                    <div class="ia-label">🎯 Próximo Passo</div>
                    {esc(analise['orientacao'])}
                </div>
            </div>
            <div class="messages" id="chat-container">
                {bubbles}
            </div>
        </main>'''
    else:
        chat_html = '''
        <main class="chat-area">
            <div class="empty-state" style="flex:1">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                <h3>Selecione uma conversa</h3>
                <p>Escolha um contato na barra lateral para ver as mensagens.</p>
            </div>
        </main>'''

    return HTMLResponse(HEAD_HTML + "<body>" + toast_html + '<div class="app">' + sidebar + chat_html + '</div>' + JS_SCRIPT + "</body></html>")


# ============================================================
# PÁGINA DE CONTATOS
# ============================================================
@app.get("/contatos", response_class=HTMLResponse)
def pagina_contatos():
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT c.*, COUNT(m.id) as total_msgs
            FROM contatos c
            INNER JOIN mensagens m ON c.remetente = m.remetente
            GROUP BY c.remetente ORDER BY c.nome ASC
        """)
        contatos = [dict(r) for r in cur.fetchall()]

    rows = ""
    for c in contatos:
        num = c['remetente']
        nome = c['nome'] or num
        avatar = c['avatar_url'] or f"/avatar/{num}"
        rows += f'''
        <div class="contact-row">
            <img src="{avatar}" style="width:44px;height:44px;border-radius:50%;object-fit:cover;background:#f1f5f9" loading="lazy" onerror="this.onerror=null;this.src='/avatar/{num}'">
            <div style="flex:1;min-width:0">
                <form action="/contatos/editar" method="post" style="display:flex;gap:8px;align-items:center;margin:0">
                    <input type="hidden" name="remetente" value="{esc(num)}">
                    <input type="text" name="novo_nome" value="{esc(nome)}" style="border:1px solid #e2e8f0;border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit;flex:1;max-width:320px;outline:none;font-weight:600;color:#0f172a">
                    <button type="submit" style="background:#10b981;color:#fff;border:none;padding:6px 12px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit">Salvar</button>
                </form>
                <div style="font-size:12px;color:#64748b;margin-top:4px">{esc(num)} • {c['total_msgs']} mensagens</div>
            </div>
            <a href="https://wa.me/{esc(num)}" target="_blank" style="background:#25D366;color:#fff;padding:8px 14px;border-radius:9px;text-decoration:none;font-size:12px;font-weight:600;display:inline-flex;align-items:center;gap:6px">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
                WhatsApp
            </a>
        </div>'''

    html = HEAD_HTML + f'''
    <body>
    <div class="page-container">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
            <h1 class="page-title" style="margin:0">👥 Gerenciar Contatos</h1>
            <a href="/" style="color:#10b981;text-decoration:none;font-size:14px;font-weight:600">← Voltar ao painel</a>
        </div>
        <div class="card">
            <p style="color:#64748b;font-size:13px;margin:0 0 20px">Edite o nome de exibição, abra o WhatsApp Web diretamente ou sincronize novos contatos.</p>
            {rows if contatos else '<p style="color:#94a3b8;font-size:13px;text-align:center;padding:30px">Nenhum contato com mensagens ainda.</p>'}
        </div>
        <div style="margin-top:20px">
            <form action="/puxar-historico" method="post">
                <button type="submit" style="background:#3b82f6;color:#fff;border:none;padding:10px 20px;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit">🔄 Sincronizar Contatos</button>
            </form>
        </div>
    </div>
    </body></html>'''
    return HTMLResponse(html)


@app.post("/contatos/editar")
def editar_contato(remetente: str = Form(...), novo_nome: str = Form(...)):
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("UPDATE contatos SET nome=? WHERE remetente=?", (novo_nome.strip(), remetente))
        conn.commit()
    return RedirectResponse(url="/contatos", status_code=303)


@app.post("/admin/limpar-banco")
def limpar_banco():
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS mensagens")
        cur.execute("DROP TABLE IF EXISTS analises_leads")
        cur.execute("DROP TABLE IF EXISTS contatos")
        conn.commit()
    init_db()
    return RedirectResponse(url="/", status_code=303)


# ============================================================
# IA EM LOTES DE 5
# ============================================================
@app.post("/atualizar-tudo")
def atualizar_tudo():
    if not OPENAI_API_KEY:
        return HTMLResponse("<script>alert('Configure OPENAI_API_KEY.');window.location.href='/';</script>")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        with sqlite3.connect(DATABASE) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT remetente, MAX(timestamp) as ultima FROM mensagens GROUP BY remetente ORDER BY ultima DESC")
            contatos = [r["remetente"] for r in cur.fetchall()]
            for i in range(0, len(contatos), 5):
                lote = contatos[i:i+5]
                for c in lote:
                    cur.execute("SELECT tipo, mensagem FROM mensagens WHERE remetente=? ORDER BY timestamp ASC LIMIT 50", (c,))
                    msgs = cur.fetchall()
                    if not msgs: continue
                    conv = "\n".join(f"{'[Lead]' if m['tipo']=='in' else '[Voce]'}: {m['mensagem']}" for m in msgs)
                    prompt = f"Analise a conversa e responda SOMENTE JSON válido com: classificacao, resumo, orientacao.\n\n{conv}"
                    r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], response_format={"type":"json_object"})
                    dados = json.loads(r.choices[0].message.content)
                    cur.execute("""
                        INSERT INTO analises_leads (remetente, classificacao, resumo, orientacao, ultima_atualizacao)
                        VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                        ON CONFLICT(remetente) DO UPDATE SET
                            classificacao=excluded.classificacao, resumo=excluded.resumo,
                            orientacao=excluded.orientacao, ultima_atualizacao=CURRENT_TIMESTAMP
                    """, (c, dados.get("classificacao","—"), dados.get("resumo",""), dados.get("orientacao","")))
                conn.commit()
                time.sleep(1)
    except Exception as e:
        print(f"[IA] {e}")
        return HTMLResponse(f"<script>alert('Erro IA: {esc(str(e))}');window.location.href='/';</script>")
    return RedirectResponse(url="/", status_code=303)
PYEOF

RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]