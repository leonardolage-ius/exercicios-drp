"""Utilitários de data e hora. Tudo é gravado em ISO 8601 com fuso America/Sao_Paulo."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FUSO = ZoneInfo("America/Sao_Paulo")


def agora() -> datetime:
    return datetime.now(FUSO).replace(microsecond=0)


def para_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FUSO).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return para_local(dt).isoformat() if dt else None


def de_iso(texto: str | None) -> datetime | None:
    if not texto:
        return None
    texto = texto.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(texto)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO)
    return para_local(dt)


def br(dt: datetime | str | None) -> str:
    """Data e hora no formato brasileiro (dd/mm/aaaa hh:mm:ss)."""
    if isinstance(dt, str):
        dt = de_iso(dt)
    return para_local(dt).strftime("%d/%m/%Y %H:%M:%S") if dt else "—"


def duracao(segundos: float | None) -> str:
    """Formata segundos como 1h18min55s."""
    if segundos is None:
        return "—"
    negativo = segundos < 0
    s = int(round(abs(segundos)))
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    if h:
        txt = f"{h}h{m:02d}min{s:02d}s"
    elif m:
        txt = f"{m}min{s:02d}s"
    else:
        txt = f"{s}s"
    return ("-" if negativo else "") + txt


def interpretar_horario(texto: str, referencia: datetime | None = None) -> datetime:
    """Interpreta o horário digitado pelo executante.

    Aceita: vazio (agora), "HH:MM", "HH:MM:SS" (no dia de hoje, horário de Brasília)
    ou data e hora completas em ISO 8601 (ex.: 2027-03-15T13:45:00Z, em UTC,
    como a saída de rds_task_status).
    """
    texto = (texto or "").strip()
    if not texto:
        return agora()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", texto):
        partes = [int(p) for p in texto.split(":")] + [0]
        base = referencia or agora()
        dt = base.replace(hour=partes[0], minute=partes[1], second=partes[2], microsecond=0)
        if dt > agora() + timedelta(minutes=1):
            raise ValueError("horário no futuro")
        return dt
    dt = de_iso(texto)
    if dt is None:
        raise ValueError("horário inválido")
    return dt
