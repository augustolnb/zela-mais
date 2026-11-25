from types import SimpleNamespace

import pytest

from zela.api import telemetria
from zela.integrations import monstrao


class _Registro(dict):
    """Lê o que já foi enviado esperando a fila de envio esvaziar."""

    def __getitem__(self, chave):
        telemetria.aguardar_envios()
        return super().__getitem__(chave)


@pytest.fixture
def enviados(monkeypatch):
    registro = _Registro(execucoes=[], heartbeats=[])
    monkeypatch.setattr(monstrao, "record_execution", lambda **kw: dict.__getitem__(registro, "execucoes").append(kw))
    monkeypatch.setattr(monstrao, "heartbeat",
                        lambda status="ok", version=None: dict.__getitem__(registro, "heartbeats").append(status))
    return registro


def test_tarefa_com_itens_vira_execucao_de_sucesso(enviados):
    tarefa = telemetria.monitorar("verificar_lembretes", lambda: ["a", "b"])
    assert tarefa() == ["a", "b"]
    (e,) = enviados["execucoes"]
    assert e["trigger"] == "verificar_lembretes" and e["status"] == "succeeded"
    assert e["output_summary"] == {"itens": 2}


def test_rodada_vazia_nao_e_enviada(enviados):
    telemetria.monitorar("verificar_lembretes", lambda: [])()
    telemetria.monitorar("processar_episodios", lambda: None)()
    assert enviados["execucoes"] == []


def test_excecao_vai_so_com_o_nome_da_classe_e_e_relancada(enviados):
    def quebra():
        raise ValueError("falhou ao enviar para +5563999990000")

    with pytest.raises(ValueError):
        telemetria.monitorar("verificar_riscos", quebra)()
    (e,) = enviados["execucoes"]
    assert e["status"] == "failed"
    assert e["error"] == ("ValueError", "falha na tarefa verificar_riscos")
    assert "5563" not in repr(e)


def test_telemetria_quebrada_nao_afeta_a_tarefa(monkeypatch):
    def explode(**kw):
        raise RuntimeError("hub")

    monkeypatch.setattr(monstrao, "record_execution", explode)
    assert telemetria.monitorar("x", lambda: [1])() == [1]


class _Waha:
    def __init__(self, status):
        self._status = status

    def status_sessao(self):
        return self._status


def test_whatsapp_conectado_manda_heartbeat_ok(enviados):
    telemetria.checar_whatsapp(_Waha("WORKING"))
    assert enviados["heartbeats"] == ["ok"] and enviados["execucoes"] == []


@pytest.mark.parametrize("status", ["SCAN_QR_CODE", "FAILED", None])
def test_whatsapp_fora_manda_degradado_e_execucao_com_falha(enviados, status):
    telemetria.checar_whatsapp(_Waha(status))
    assert enviados["heartbeats"] == ["degraded"]
    (e,) = enviados["execucoes"]
    assert e["trigger"] == "whatsapp" and e["status"] == "failed"
    assert e["error"][0] == "WhatsAppDesconectado"


def test_avisos_a_familia_registram_contagens(enviados):
    telemetria.registrar_avisos(enviadas=1, falhas=0, esgotadas=0)
    telemetria.registrar_avisos(enviadas=0, falhas=1, esgotadas=1)
    telemetria.registrar_avisos(enviadas=0, falhas=0, esgotadas=0)
    ok, falha = enviados["execucoes"]
    assert ok["trigger"] == "aviso_familia" and ok["status"] == "succeeded"
    assert ok["output_summary"] == {"enviadas": 1, "falhas": 0, "esgotadas": 0}
    assert falha["status"] == "failed" and falha["error"][0] == "AvisoFamiliaFalhou"


def _evento(prompt, saida, cache=None, pensamento=None):
    return SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=saida,
        cached_content_token_count=cache, thoughts_token_count=pensamento,
    ))


def test_uso_soma_eventos_e_separa_cache(monkeypatch):
    monkeypatch.setenv("ZELA_LLM_PROVEDOR", "deepseek")
    eventos = [_evento(100, 20, cache=40), _evento(50, 10, pensamento=5), SimpleNamespace(usage_metadata=None)]
    assert telemetria.uso_dos_eventos(eventos) == [{
        "provider": "deepseek", "model": "deepseek-chat",
        "inputTokens": 110, "outputTokens": 30, "cacheReadTokens": 40, "thinkingTokens": 5,
    }]


def test_uso_com_gemini_e_sem_tokens(monkeypatch):
    monkeypatch.delenv("ZELA_LLM_PROVEDOR", raising=False)
    assert telemetria.uso_dos_eventos([SimpleNamespace()]) == []
    (u,) = telemetria.uso_dos_eventos([_evento(10, 2, pensamento=3)])
    assert u["provider"] == "google" and u["model"].startswith("gemini")
    # Gemini nativo conta o raciocínio fora de candidates; o LiteLLM (DeepSeek) já inclui.
    assert u["outputTokens"] == 5 and u["thinkingTokens"] == 3


def test_conversa_vira_execucao_com_uso(enviados, monkeypatch):
    monkeypatch.setenv("ZELA_LLM_PROVEDOR", "deepseek")
    from datetime import datetime, timezone
    telemetria.registrar_conversa(datetime.now(timezone.utc), [_evento(10, 2)], erro=None)
    (e,) = enviados["execucoes"]
    assert e["trigger"] == "conversa" and e["status"] == "succeeded"
    assert e["llm_usage"][0]["inputTokens"] == 10


def test_hub_lento_nao_atrasa_a_tarefa(monkeypatch):
    import time

    monkeypatch.setattr(monstrao, "record_execution", lambda **kw: time.sleep(1.5))
    inicio = time.monotonic()
    telemetria.monitorar("x", lambda: [1])()
    telemetria.registrar_avisos(enviadas=1, falhas=0, esgotadas=0)
    assert time.monotonic() - inicio < 0.3
    telemetria.aguardar_envios()


def test_fila_cheia_descarta_sem_bloquear(monkeypatch):
    import threading
    import time

    liberar = threading.Event()
    monkeypatch.setattr(monstrao, "record_execution", lambda **kw: liberar.wait(5))
    inicio = time.monotonic()
    for _ in range(telemetria._MAX_FILA + 50):
        telemetria.registrar_avisos(enviadas=1, falhas=0, esgotadas=0)
    assert time.monotonic() - inicio < 0.5
    liberar.set()
    telemetria.aguardar_envios()


# Status aceitos pelo hub (crm-monstrao packages/core/src/ingest/schema.ts EXECUTION_STATUSES).
_STATUS_DO_HUB = {"running", "succeeded", "failed", "timeout", "cancelled"}


def test_todo_status_enviado_e_aceito_pelo_hub(enviados):
    telemetria.monitorar("a", lambda: [1])()
    with pytest.raises(ValueError):
        telemetria.monitorar("b", lambda: (_ for _ in ()).throw(ValueError()))()
    telemetria.checar_whatsapp(_Waha(None))
    telemetria.registrar_avisos(enviadas=1, falhas=0, esgotadas=0)
    telemetria.registrar_avisos(enviadas=0, falhas=1, esgotadas=0)
    from datetime import datetime, timezone
    telemetria.registrar_conversa(datetime.now(timezone.utc), [], erro=None)
    telemetria.registrar_conversa(datetime.now(timezone.utc), [], erro=RuntimeError())
    assert {e["status"] for e in enviados["execucoes"]} <= _STATUS_DO_HUB
