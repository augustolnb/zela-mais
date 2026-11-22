"""Card "Últimos alertas" do painel: cada alerta vira um item compacto.

O texto que vai para o WhatsApp começa com "Aviso:", "Atenção:" ou
"[SIMULAÇÃO]". No painel o nível já aparece no selo e a simulação numa
etiqueta, então esses prefixos saem para o card ficar mais leve.
"""

import re
from datetime import datetime, timedelta
from html import escape

from zela.models.alertas import Alerta
from zela.streamlit_app.estilo import badge_nivel

_PREFIXOS = re.compile(r"^\s*(?:\[SIMULAÇÃO\]\s*|Aviso:\s*|Atenção:\s*)+")


def texto_alerta(mensagem: str) -> str:
    return _PREFIXOS.sub("", mensagem).strip()


def quando(ts: datetime, agora: datetime) -> str:
    hora = ts.strftime("%H:%M")
    if ts.date() == agora.date():
        return f"hoje, {hora}"
    if ts.date() == (agora - timedelta(days=1)).date():
        return f"ontem, {hora}"
    return f"{ts.strftime('%d/%m')}, {hora}"


def item_alerta(alerta: Alerta, agora: datetime) -> str:
    nivel = alerta.nivel.value
    texto = escape(texto_alerta(alerta.mensagem))
    etiqueta = '<span class="zela-alerta-simulacao">simulação</span>' if alerta.simulado else ""
    return (
        f'<div class="zela-alerta zela-alerta-{nivel}">'
        f'<div class="zela-alerta-topo">{badge_nivel(nivel)}{etiqueta}'
        f'<span class="zela-alerta-quando">{quando(alerta.timestamp, agora)}</span></div>'
        f'<div class="zela-alerta-texto" title="{texto}">{texto}</div>'
        "</div>"
    )


def lista_alertas(alertas: list[Alerta], agora: datetime, maximo: int) -> str:
    recentes = sorted(alertas, key=lambda a: a.timestamp, reverse=True)[:maximo]
    return '<div class="zela-alertas">' + "".join(item_alerta(a, agora) for a in recentes) + "</div>"
