"""O que o Zela+ conta ao hub crm-monstrao (spec crm-monstrao 2026-11-25-zela-no-hub).

Nada pessoal sai daqui: só nome da tarefa, contagens, nome da classe da exceção e tokens.
Mensagens de exceção ficam de fora (as do httpx trazem a URL, que pode ter o telefone).
Fail-open: nenhuma função levanta exceção por causa da telemetria.
"""

from __future__ import annotations

import functools
import logging
import os
import uuid
from datetime import datetime, timezone

from zela.integrations import monstrao

logger = logging.getLogger(__name__)

INTERVALO_HEARTBEAT_MINUTOS = 5


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _versao() -> str | None:
    return os.environ.get("APP_VERSION") or None


def _registrar(trigger: str, inicio: datetime, status: str, **extra) -> None:
    try:
        monstrao.record_execution(
            external_ref=f"{trigger}-{uuid.uuid4().hex[:16]}",
            trigger=trigger,
            started_at=inicio,
            finished_at=_agora(),
            status=status,
            version=_versao(),
            **extra,
        )
    except Exception:  # telemetria nunca derruba tarefa
        logger.warning("falha ao registrar telemetria de %s", trigger, exc_info=True)


def monitorar(nome: str, fn):
    """Envolve uma tarefa do agendador: lista não vazia → sucesso; exceção → falha (e relança)."""

    @functools.wraps(fn)
    def tarefa(*args, **kwargs):
        inicio = _agora()
        try:
            resultado = fn(*args, **kwargs)
        except Exception as exc:
            _registrar(nome, inicio, "failed", error=(type(exc).__name__, f"falha na tarefa {nome}"))
            raise
        if isinstance(resultado, (list, tuple)) and resultado:
            _registrar(nome, inicio, "success", output_summary={"itens": len(resultado)})
        return resultado

    return tarefa


def checar_whatsapp(waha_client) -> None:
    """Heartbeat: ok com a sessão do WAHA em WORKING; senão degraded + execução com falha."""
    inicio = _agora()
    try:
        status = waha_client.status_sessao()
    except Exception:
        status = None
    conectado = status == "WORKING"
    try:
        monstrao.heartbeat("ok" if conectado else "degraded", _versao())
    except Exception:
        logger.warning("falha ao enviar heartbeat ao hub", exc_info=True)
    if not conectado:
        _registrar(
            "whatsapp", inicio, "failed",
            error=("WhatsAppDesconectado", f"sessão do WhatsApp: {status or 'sem resposta'}"),
        )


def registrar_avisos(*, enviadas: int, falhas: int, esgotadas: int, inicio: datetime | None = None) -> None:
    """Uma rodada de avisos à família. Qualquer falha de envio conta como execução com falha."""
    if not (enviadas or falhas or esgotadas):
        return
    resumo = {"enviadas": enviadas, "falhas": falhas, "esgotadas": esgotadas}
    if falhas or esgotadas:
        _registrar("aviso_familia", inicio or _agora(), "failed", output_summary=resumo,
                   error=("AvisoFamiliaFalhou", f"{falhas} envio(s) à família falharam, {esgotadas} sem nova tentativa"))
    else:
        _registrar("aviso_familia", inicio or _agora(), "success", output_summary=resumo)


def _modelo_atual() -> tuple[str, str]:
    if os.environ.get("ZELA_LLM_PROVEDOR", "gemini").lower() == "deepseek":
        return "deepseek", "deepseek-chat"
    from zela.agents.orchestrator import MODELO_GEMINI

    return "google", MODELO_GEMINI


def uso_dos_eventos(eventos) -> list[dict]:
    """Soma o `usage_metadata` dos eventos do ADK. Entrada sem o cache (o hub cobra à parte);
    a saída inclui o raciocínio (o hub trata `thinkingTokens` como parte da saída)."""
    prompt = saida = cache = pensamento = 0
    for evento in eventos:
        uso = getattr(evento, "usage_metadata", None)
        if uso is None:
            continue
        prompt += getattr(uso, "prompt_token_count", None) or 0
        saida += getattr(uso, "candidates_token_count", None) or 0
        cache += getattr(uso, "cached_content_token_count", None) or 0
        pensamento += getattr(uso, "thoughts_token_count", None) or 0
    if not (prompt or saida):
        return []
    provider, model = _modelo_atual()
    # Gemini nativo deixa o raciocínio fora de candidates; o LiteLLM (DeepSeek) segue a OpenAI
    # e já o inclui em completion_tokens.
    if provider == "google":
        saida += pensamento
    return [{
        "provider": provider, "model": model,
        "inputTokens": max(prompt - cache, 0), "outputTokens": saida,
        "cacheReadTokens": cache, "thinkingTokens": pensamento,
    }]


def registrar_conversa(inicio: datetime, eventos, erro: BaseException | None) -> None:
    try:
        uso = uso_dos_eventos(eventos)
    except Exception:
        uso = []
    if erro is None:
        _registrar("conversa", inicio, "success", llm_usage=uso)
    else:
        _registrar("conversa", inicio, "failed", llm_usage=uso,
                   error=(type(erro).__name__, "falha ao processar mensagem"))
