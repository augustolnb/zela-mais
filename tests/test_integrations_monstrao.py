"""`core/hub_client.py`: telemetria fail-open para o hub crm-monstrao (só stdlib)."""

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zela.integrations import monstrao as hub_client


class _Servidor:
    def __init__(self, status=201, atraso=0.0):
        self.status = status
        self.atraso = atraso
        self.recebidos: list[tuple[str, dict, dict]] = []
        servidor = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if servidor.atraso:
                    time.sleep(servidor.atraso)
                servidor.recebidos.append((self.path, dict(self.headers), corpo))
                self.send_response(servidor.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def parar(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ambiente(monkeypatch, tmp_path):
    buffer = tmp_path / "buffer.jsonl"
    monkeypatch.setenv("MONSTRAO_TOKEN", "t")
    monkeypatch.setenv("MONSTRAO_BUFFER_PATH", str(buffer))

    def apontar(url):
        monkeypatch.setenv("MONSTRAO_URL", url)

    return apontar, buffer


@pytest.fixture
def servidor():
    s = _Servidor()
    yield s
    s.parar()


def _linhas(buffer):
    return buffer.read_text().splitlines() if buffer.exists() else []


def _execucao(ref="r1", **kw):
    agora = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    hub_client.record_execution(external_ref=ref, trigger="telegram", started_at=agora,
                                finished_at=agora, status="succeeded", **kw)


def test_sem_configuracao_nao_faz_nada(monkeypatch, tmp_path):
    monkeypatch.delenv("MONSTRAO_URL", raising=False)
    monkeypatch.setenv("MONSTRAO_TOKEN", "t")
    monkeypatch.setenv("MONSTRAO_BUFFER_PATH", str(tmp_path / "b.jsonl"))
    assert hub_client.configured() is False
    hub_client.heartbeat()
    _execucao()
    assert not (tmp_path / "b.jsonl").exists()


def test_heartbeat_envia_bearer_e_corpo(ambiente, servidor):
    apontar, _ = ambiente
    apontar(servidor.url)
    assert hub_client.configured() is True
    hub_client.heartbeat("ok", version="abc")
    caminho, headers, corpo = servidor.recebidos[0]
    assert caminho == "/api/v1/heartbeats"
    assert headers["Authorization"] == "Bearer t"
    assert corpo == {"status": "ok", "version": "abc"}


def test_execucao_usa_camel_case_e_provider(ambiente, servidor):
    apontar, _ = ambiente
    apontar(servidor.url)
    uso = [{"provider": "deepseek", "model": "deepseek-chat", "inputTokens": 10, "outputTokens": 5}]
    _execucao(input_summary={"etapa": "gerar"}, error=("DocgenErro", "falhou"), llm_usage=uso)
    caminho, _, corpo = servidor.recebidos[0]
    assert caminho == "/api/v1/executions"
    assert corpo["externalRef"] == "r1" and corpo["trigger"] == "telegram"
    assert corpo["startedAt"] == "2026-11-22T12:00:00+00:00"
    assert corpo["inputSummary"] == {"etapa": "gerar"}
    assert corpo["error"] == {"type": "DocgenErro", "message": "falhou"}
    assert corpo["llmUsage"] == uso


def test_execucao_sem_uso_nao_envia_llm_usage(ambiente, servidor):
    apontar, _ = ambiente
    apontar(servidor.url)
    _execucao(llm_usage=[])
    assert "llmUsage" not in servidor.recebidos[0][2]


def test_hub_fora_do_ar_grava_no_buffer_e_nao_levanta(ambiente):
    apontar, buffer = ambiente
    apontar("http://127.0.0.1:9")  # porta fechada
    _execucao()
    assert len(_linhas(buffer)) == 1


def test_buffer_e_reenviado_em_ordem_quando_hub_volta(ambiente, servidor):
    apontar, buffer = ambiente
    apontar("http://127.0.0.1:9")
    _execucao("r1")
    _execucao("r2")
    apontar(servidor.url)
    _execucao("r3")
    assert [c["externalRef"] for _, _, c in servidor.recebidos] == ["r1", "r2", "r3"]
    assert _linhas(buffer) == []


def test_4xx_descarta_sem_bufferizar(ambiente):
    apontar, buffer = ambiente
    s = _Servidor(status=400)
    try:
        apontar(s.url)
        _execucao()
        assert len(s.recebidos) == 1 and _linhas(buffer) == []
    finally:
        s.parar()


def test_4xx_no_buffer_nao_trava_o_resto(ambiente, servidor):
    apontar, buffer = ambiente
    buffer.write_text(json.dumps({"path": "/api/v1/executions", "payload": {"externalRef": "velho"}}) + "\n")
    s400 = _Servidor(status=400)
    try:
        apontar(s400.url)
        _execucao("r1")  # o velho e o r1 são rejeitados com 400 e descartados
        assert _linhas(buffer) == []
    finally:
        s400.parar()


def test_429_vai_para_o_buffer(ambiente):
    apontar, buffer = ambiente
    s = _Servidor(status=429)
    try:
        apontar(s.url)
        _execucao()
        assert len(_linhas(buffer)) == 1
    finally:
        s.parar()


def test_buffer_limitado_a_1000_linhas(ambiente):
    apontar, buffer = ambiente
    apontar("http://127.0.0.1:9")
    for i in range(1005):
        hub_client._gravar_no_buffer("/api/v1/executions", {"externalRef": f"r{i}"})
    linhas = _linhas(buffer)
    assert len(linhas) == 1000
    assert json.loads(linhas[0])["payload"]["externalRef"] == "r5"


def test_timeout_nao_passa_de_5s(ambiente):
    apontar, buffer = ambiente
    s = _Servidor(atraso=8)
    try:
        apontar(s.url)
        t0 = time.monotonic()
        _execucao()
        assert time.monotonic() - t0 < 6
        assert len(_linhas(buffer)) == 1
    finally:
        s.parar()


def test_heartbeat_nunca_vai_para_o_buffer(ambiente):
    apontar, buffer = ambiente
    apontar("http://127.0.0.1:9")
    hub_client.heartbeat()
    assert _linhas(buffer) == []


def test_esvaziamento_limitado_por_chamada(ambiente, servidor):
    apontar, buffer = ambiente
    for i in range(50):
        hub_client._gravar_no_buffer("/api/v1/executions", {"externalRef": f"b{i}"})
    apontar(servidor.url)
    _execucao("novo")
    assert len(servidor.recebidos) == hub_client._MAX_ESVAZIAR
    linhas = _linhas(buffer)
    assert len(linhas) == 50 - hub_client._MAX_ESVAZIAR + 1
    # ordem preservada: o envio novo fica na fila atrás do que sobrou
    assert json.loads(linhas[-1])["payload"]["externalRef"] == "novo"


def test_linha_com_5xx_permanente_e_descartada_apos_tentativas(ambiente):
    apontar, buffer = ambiente
    buffer.write_text(json.dumps({"path": "/api/v1/executions", "payload": {"externalRef": "veneno"},
                                  "tentativas": hub_client._MAX_TENTATIVAS - 1}) + "\n")
    s = _Servidor(status=500)
    try:
        apontar(s.url)
        _execucao("r1")
        refs = [json.loads(linha)["payload"]["externalRef"] for linha in _linhas(buffer)]
        assert "veneno" not in refs and refs == ["r1"]
    finally:
        s.parar()


def test_401_vai_para_o_buffer(ambiente):
    apontar, buffer = ambiente
    s = _Servidor(status=401)
    try:
        apontar(s.url)
        _execucao()
        assert len(_linhas(buffer)) == 1
    finally:
        s.parar()


def test_execucao_envia_quota_so_quando_ha(ambiente, servidor):
    apontar, _ = ambiente
    apontar(servidor.url)
    _execucao(quota={"fiveHour": {"usedPct": 38.0}})
    _execucao(quota=None)
    assert servidor.recebidos[0][2]["quota"] == {"fiveHour": {"usedPct": 38.0}}
    assert "quota" not in servidor.recebidos[1][2]

