from datetime import datetime

import pytest

from zela.models.alertas import Alerta, CanalAlerta, NivelAlerta
from zela.streamlit_app.alertas import item_alerta, lista_alertas, texto_alerta, quando

AGORA = datetime(2026, 11, 21, 10, 0)


def _alerta(mensagem: str, nivel: str = "info", simulado: bool = False, ts: datetime = AGORA) -> Alerta:
    return Alerta(
        nivel=NivelAlerta(nivel), destinatario="Família", canal=CanalAlerta.WHATSAPP,
        mensagem=mensagem, timestamp=ts, simulado=simulado,
    )


@pytest.mark.parametrize(
    ("mensagem", "esperado"),
    [
        ("Aviso: Sono da noite fora do habitual: 10.4h (média 5.9h).",
         "Sono da noite fora do habitual: 10.4h (média 5.9h)."),
        ("[SIMULAÇÃO] Acionamento de serviço de emergência para Ana.",
         "Acionamento de serviço de emergência para Ana."),
        ("Atenção: Ana não respondeu.", "Ana não respondeu."),
        ("Tudo bem? Detectamos: ESP32 parou.", "Tudo bem? Detectamos: ESP32 parou."),
    ],
)
def test_texto_alerta_tira_prefixos_repetidos(mensagem, esperado):
    assert texto_alerta(mensagem) == esperado


@pytest.mark.parametrize(
    ("ts", "esperado"),
    [
        (datetime(2026, 11, 21, 8, 59), "hoje, 08:59"),
        (datetime(2026, 11, 20, 23, 5), "ontem, 23:05"),
        (datetime(2026, 11, 18, 7, 0), "18/11, 07:00"),
    ],
)
def test_quando_usa_hoje_ontem_ou_data(ts, esperado):
    assert quando(ts, AGORA) == esperado


def test_item_alerta_tem_classe_do_nivel_selo_e_horario():
    html = item_alerta(_alerta("Aviso: ESP32 parou.", nivel="critico"), AGORA)
    assert 'class="zela-alerta zela-alerta-critico"' in html
    assert "CRÍTICO" in html
    assert "hoje, 10:00" in html
    assert "ESP32 parou." in html
    assert "Aviso:" not in html


def test_item_alerta_escapa_html_da_mensagem():
    html = item_alerta(_alerta("Aviso: <script>x</script> & cia"), AGORA)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; cia" in html


def test_item_alerta_simulado_ganha_etiqueta():
    html = item_alerta(_alerta("[SIMULAÇÃO] Acionamento.", simulado=True), AGORA)
    assert "zela-alerta-simulacao" in html
    assert "[SIMULAÇÃO]" not in html


def test_item_alerta_mostra_texto_completo_no_title():
    html = item_alerta(_alerta('Aviso: texto "longo"'), AGORA)
    assert 'title="texto &quot;longo&quot;"' in html


def test_lista_alertas_mais_recente_primeiro():
    antigo = _alerta("Aviso: antigo", ts=datetime(2026, 11, 21, 7, 0))
    novo = _alerta("Aviso: novo", ts=datetime(2026, 11, 21, 9, 0))
    html = lista_alertas([antigo, novo], AGORA, maximo=10)
    assert html.index("novo") < html.index("antigo")
    assert html.startswith('<div class="zela-alertas">')


def test_lista_alertas_respeita_maximo():
    alertas = [_alerta(f"Aviso: n{i}", ts=datetime(2026, 11, 21, i, 0)) for i in range(5)]
    html = lista_alertas(alertas, AGORA, maximo=2)
    assert html.count('class="zela-alerta ') == 2
    assert "n4" in html and "n3" in html and "n2" not in html
