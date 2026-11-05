"""Jinja2 + filtros do painel (markdown seguro, badges, telefone, datas)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import markdown as md
import nh3
from fastapi.templating import Jinja2Templates

from .config import settings
from .timeutil import fmt_date_br, fmt_datetime_br, fmt_hhmm, fmt_short, parse_ts, today_local

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "b", "i", "u", "s", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "code", "pre",
    "table", "thead", "tbody", "tr", "th", "td", "a", "span", "div", "small",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title", "target"},
    "th": {"align"},
    "td": {"align"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
}


def render_markdown(texto: str | None) -> str:
    """Markdown -> HTML já sanitizado (o conteúdo vem de agentes externos)."""
    if not texto:
        return ""
    html = md.markdown(texto, extensions=["tables", "fenced_code", "sane_lists"])
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, link_rel="noopener noreferrer")


def badge_class(classification: str | None) -> str:
    texto = (classification or "").lower()
    regras = [
        (("quente", "hot", "urgen"), "badge-hot"),
        (("morno", "warm", "interess"), "badge-warm"),
        (("frio", "cold", "sem_resposta"), "badge-cold"),
        (("agend", "confirm", "fechad"), "badge-ok"),
        (("cancel", "perdid", "risco", "reclam"), "badge-danger"),
        (("suporte", "duvida", "dúvida"), "badge-info"),
    ]
    for agulhas, classe in regras:
        if any(agulha in texto for agulha in agulhas):
            return classe
    return "badge-neutral"


def phone_fmt(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if digits.startswith("55") and len(digits) in (12, 13):
        ddd, numero = digits[2:4], digits[4:]
        if len(numero) == 9:
            return f"+55 ({ddd}) {numero[:5]}-{numero[5:]}"
        if len(numero) == 8:
            return f"+55 ({ddd}) {numero[:4]}-{numero[4:]}"
    return f"+{digits}" if len(digits) > 11 else digits


def json_pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _hhmm(value) -> str:
    parsed = parse_ts(value)
    return fmt_hhmm(parsed) if parsed else ""


templates.env.filters.update(
    {
        "md": render_markdown,
        "badge": badge_class,
        "phone": phone_fmt,
        "data_br": fmt_date_br,
        "datahora_br": fmt_datetime_br,
        "curto": fmt_short,
        "hhmm": _hhmm,
        "json": json_pretty,
    }
)
templates.env.globals.update(
    app_name=settings.app_name,
    app_version=settings.app_version,
    fuso=settings.timezone,
    hoje=today_local,
)