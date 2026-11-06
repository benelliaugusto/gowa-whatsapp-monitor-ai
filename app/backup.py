"""Backup consistente do banco SQLite (para o serviço Cron do Easypanel).

    python -m app.backup --keep 14

Grava em /data/backups/monitor-AAAAMMDD-HHMMSS.db usando a API de backup do
SQLite (segura com WAL ativo) e remove os arquivos além da retenção.
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from datetime import datetime

from .config import settings


def fazer_backup(destino_dir: str | None = None, keep: int | None = None) -> str:
    origem = settings.database_path
    if not os.path.exists(origem):
        raise FileNotFoundError(f"banco nao encontrado: {origem}")

    destino_dir = destino_dir or settings.backup_dir
    keep = int(keep or settings.backup_keep)
    os.makedirs(destino_dir, exist_ok=True)

    base = os.path.join(destino_dir, f"monitor-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    destino = f"{base}.db"
    contador = 1
    while os.path.exists(destino):
        contador += 1
        destino = f"{base}-{contador}.db"

    with sqlite3.connect(origem) as src, sqlite3.connect(destino) as dst:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with sqlite3.connect(destino) as verificacao:
        integridade = verificacao.execute("PRAGMA integrity_check").fetchone()[0]
        total = verificacao.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if integridade != "ok":
        raise RuntimeError(f"backup corrompido: {integridade}")
    print(f"[backup] integridade ok ({total} mensagens)")

    # retenção
    arquivos = sorted(glob.glob(os.path.join(destino_dir, "monitor-*.db")))
    for antigo in arquivos[:-keep] if keep > 0 else []:
        try:
            os.remove(antigo)
        except OSError:
            pass

    return destino


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup do banco do monitor-ia")
    parser.add_argument("--dir", default=None, help="diretório de destino (padrão BACKUP_DIR)")
    parser.add_argument("--keep", type=int, default=None, help="quantos backups manter (padrão BACKUP_KEEP)")
    args = parser.parse_args()

    caminho = fazer_backup(args.dir, args.keep)
    tamanho = os.path.getsize(caminho) / (1024 * 1024)
    print(f"[backup] ok: {caminho} ({tamanho:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())