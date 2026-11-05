"""Datas e horas.

Regra do projeto: tudo é persistido em UTC ISO-8601 ("...Z") e convertido para o
fuso da clínica (TIMEZONE) na borda de leitura. Cada mensagem também guarda
local_date/local_hour para consultas diárias rápidas.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import settings

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return iso(utc_now())


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _from_epoch(value: float) -> datetime:
    if value >= 1e17:      # nanossegundos
        value /= 1e9
    elif value >= 1e14:    # microssegundos
        value /= 1e6
    elif value >= 1e11:    # milissegundos
        value /= 1e3
    return datetime.fromtimestamp(value, tz=UTC)


def parse_ts(value) -> datetime | None:
    """Aceita ISO-8601 (com Z), epoch em s/ms/us/ns, date e datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _from_epoch(float(text))

    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(settings.tz)


def local_now() -> datetime:
    return to_local(utc_now())


def local_date_of(dt: datetime) -> str:
    return to_local(dt).strftime("%Y-%m-%d")


def today_local() -> str:
    return local_now().strftime("%Y-%m-%d")


def resolve_date(value: str | None) -> str:
    """Normaliza para YYYY-MM-DD; vazio/ inválido = hoje no fuso da clínica."""
    if not value:
        return today_local()
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        parsed = parse_ts(text)
        return local_date_of(parsed) if parsed else today_local()


def day_bounds(date_str: str) -> tuple[datetime, datetime]:
    """Início (inclusivo) e fim (exclusivo) do dia local, em UTC."""
    day = datetime.strptime(resolve_date(date_str), "%Y-%m-%d")
    start = day.replace(tzinfo=settings.tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def fmt_hhmm(dt: datetime | None) -> str:
    return to_local(dt).strftime("%H:%M") if dt else ""


def fmt_date_br(value) -> str:
    """Formata data para dd/mm/aaaa.

    Valores "YYYY-MM-DD" são datas civis (período de relatório, dia do painel) e
    NÃO passam por conversão de fuso — senão virariam o dia anterior no Brasil.
    """
    if isinstance(value, datetime):
        return to_local(value).strftime("%d/%m/%Y")
    texto = str(value or "").strip()
    if len(texto) == 10 and texto[4] == "-" and texto[7] == "-":
        try:
            return datetime.strptime(texto, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            pass
    parsed = parse_ts(value)
    return to_local(parsed).strftime("%d/%m/%Y") if parsed else ""


def fmt_datetime_br(value) -> str:
    parsed = parse_ts(value) if not isinstance(value, datetime) else value
    return to_local(parsed).strftime("%d/%m/%Y %H:%M") if parsed else ""


def fmt_short(value) -> str:
    parsed = parse_ts(value) if not isinstance(value, datetime) else value
    if not parsed:
        return ""
    local = to_local(parsed)
    today = local_now().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if local.date() == today - timedelta(days=1):
        return "Ontem"
    return local.strftime("%d/%m")