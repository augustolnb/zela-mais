"""Telemetria para o hub `crm-monstrao` (heartbeat e execuções do Zela+).

Porta do `hub_client` do ag-monitor-skills. Só stdlib.
Fala o protocolo `/api/v1/heartbeats` e `/api/v1/executions` do hub, sem usar o SDK
Python dele: aquele SDK não envia `provider` em `llmUsage` (o hub rejeita) e bufferiza
respostas 4xx, travando o buffer para sempre na primeira linha rejeitada.

Fail-open por contrato: nenhuma função levanta exceção. Execução com falha transitória
(rede, 5xx, 429, 401/403 — token errado é config a corrigir, não payload ruim) vai para um
buffer em disco (no máximo `_MAX_BUFFER` linhas, as mais antigas saem primeiro), reenviado
em ordem antes do próximo envio, no máximo `_MAX_ESVAZIAR` linhas por chamada (nunca prende
o chamador por muito tempo). Uma linha que recebe 5xx `_MAX_TENTATIVAS` vezes é descartada (rede caída não conta), para
um payload que sempre dá 5xx não travar o buffer. Os demais 4xx são descartados com log.
Heartbeat nunca vai para o buffer: reenviado depois, não diz nada sobre o passado.

Sem `MONSTRAO_URL`/`MONSTRAO_TOKEN`, `configured()` é False e nada é enviado.
As funções são síncronas (o agendador do Zela+ roda em threads).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TIMEOUT_S = 5
_MAX_BUFFER = 1000
_MAX_ESVAZIAR = 20
_MAX_TENTATIVAS = 5
_DEFAULT_BUFFER_PATH = "/app/data/monstrao-buffer.jsonl"
_LOCK = threading.Lock()


def configured() -> bool:
    return bool(os.environ.get("MONSTRAO_URL") and os.environ.get("MONSTRAO_TOKEN"))


def _buffer_path() -> str:
    return os.environ.get("MONSTRAO_BUFFER_PATH") or _DEFAULT_BUFFER_PATH


def _enviar(path: str, payload: dict) -> str:
    """'ok' | 'descartar' (4xx permanente) | 'erro_servidor' (5xx) | 'tentar_depois' (rede, 401/403/429)."""
    url = os.environ["MONSTRAO_URL"].rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, default=str).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['MONSTRAO_TOKEN']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S):
            return "ok"
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in (401, 403, 429):
            logger.warning("hub rejeitou %s (HTTP %s); descartado", path, exc.code)
            return "descartar"
        return "erro_servidor" if exc.code >= 500 else "tentar_depois"
    except Exception:  # rede, timeout, URL inválida: nunca propaga
        return "tentar_depois"


def _ler_buffer() -> list[str]:
    try:
        with open(_buffer_path(), encoding="utf-8") as fh:
            return [linha for linha in fh.read().splitlines() if linha.strip()]
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("falha ao ler o buffer do hub: %s", exc)
        return []


def _escrever_buffer(linhas: list[str]) -> None:
    caminho = _buffer_path()
    try:
        if not linhas:
            if os.path.exists(caminho):
                os.remove(caminho)
            return
        os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
        with open(caminho, "w", encoding="utf-8") as fh:
            fh.write("\n".join(linhas[-_MAX_BUFFER:]) + "\n")
    except OSError as exc:
        logger.warning("falha ao gravar o buffer do hub: %s", exc)


def _linha(path: str, payload: dict, tentativas: int = 0) -> str:
    return json.dumps({"path": path, "payload": payload, "tentativas": tentativas},
                      ensure_ascii=False, default=str)


def _gravar_no_buffer(path: str, payload: dict) -> None:
    _escrever_buffer(_ler_buffer() + [_linha(path, payload)])


def _esvaziar_buffer() -> bool:
    """Reenvia o buffer em ordem. Devolve False se parou numa falha transitória."""
    linhas = _ler_buffer()
    if not linhas:
        return True
    restantes: list[str] = []
    for i, linha in enumerate(linhas):
        if i >= _MAX_ESVAZIAR:
            restantes = linhas[i:]
            break
        try:
            entrada = json.loads(linha)
            resultado = _enviar(entrada["path"], entrada["payload"])
        except (ValueError, KeyError, TypeError):
            logger.warning("linha corrompida no buffer do hub; ignorada")
            continue
        if resultado == "tentar_depois":
            # Hub inalcançável ou token a corrigir: não é culpa da linha, não gasta tentativa.
            restantes = linhas[i:]
            break
        if resultado == "erro_servidor":
            tentativas = int(entrada.get("tentativas") or 0) + 1
            if tentativas >= _MAX_TENTATIVAS:
                logger.warning("hub: execução %s descartada após %s tentativas",
                               entrada["payload"].get("externalRef"), tentativas)
                restantes = linhas[i + 1:]
            else:
                restantes = [_linha(entrada["path"], entrada["payload"], tentativas)] + linhas[i + 1:]
            break
    _escrever_buffer(restantes)
    return not restantes


def _send(path: str, payload: dict, *, bufferizar: bool = True) -> None:
    if not configured():
        return
    try:
        with _LOCK:
            if not _esvaziar_buffer():
                if bufferizar:
                    _gravar_no_buffer(path, payload)
                return
            if _enviar(path, payload) in ("tentar_depois", "erro_servidor") and bufferizar:
                _gravar_no_buffer(path, payload)
    except Exception:  # fail-open: telemetria nunca derruba o Zela+
        logger.warning("falha inesperada na telemetria do hub", exc_info=True)


def heartbeat(status: str = "ok", version: str | None = None) -> None:
    payload: dict = {"status": status}
    if version:
        payload["version"] = version
    _send("/api/v1/heartbeats", payload, bufferizar=False)


def record_execution(
    *,
    external_ref: str,
    trigger: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    input_summary: dict | None = None,
    output_summary: dict | None = None,
    error: tuple[str, str] | None = None,
    llm_usage: list[dict] | None = None,
    quota: dict | None = None,
    version: str | None = None,
) -> None:
    payload: dict = {
        "externalRef": external_ref,
        "trigger": trigger,
        "status": status,
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
    }
    if input_summary is not None:
        payload["inputSummary"] = input_summary
    if output_summary is not None:
        payload["outputSummary"] = output_summary
    if error is not None:
        payload["error"] = {"type": error[0][:120] or "Erro", "message": error[1][:4000] or "-"}
    if llm_usage:
        payload["llmUsage"] = llm_usage
    if quota:
        payload["quota"] = quota
    if version:
        payload["version"] = version
    _send("/api/v1/executions", payload)
