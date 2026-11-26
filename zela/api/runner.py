from datetime import datetime, timezone

from google.adk.runners import InMemoryRunner
from google.genai import types

from zela.agents.orchestrator import root_agent
from zela.api import telemetria

_APP_NAME = "zela"


def obter_runner_zela() -> InMemoryRunner:
    return InMemoryRunner(agent=root_agent, app_name=_APP_NAME)


def processar_mensagem(runner, id_idoso: str, texto: str, agora: datetime) -> str:
    """Processa uma mensagem do idoso através do agente orquestrador e
    retorna o texto da resposta. `runner` é injetado para permitir testes
    sem um Runner ADK real (ver nota de risco de API no cabeçalho da task)."""
    sessao = runner.session_service.get_session_sync(
        app_name=_APP_NAME, user_id=id_idoso, session_id=id_idoso
    )
    if sessao is None:
        sessao = runner.session_service.create_session_sync(
            app_name=_APP_NAME, user_id=id_idoso, session_id=id_idoso
        )
    contexto = f"[contexto do sistema: idoso_id={id_idoso}; agora={agora.isoformat()}]\n"
    mensagem = types.Content(role="user", parts=[types.Part(text=contexto + texto)])

    texto_resposta = ""
    inicio = datetime.now(timezone.utc)
    eventos = []
    try:
        for evento in runner.run(user_id=id_idoso, session_id=sessao.id, new_message=mensagem):
            eventos.append(evento)
            if evento.is_final_response() and evento.content and evento.content.parts:
                texto_resposta = evento.content.parts[0].text or ""
    except Exception as exc:
        telemetria.registrar_conversa(inicio, eventos, exc)
        raise
    telemetria.registrar_conversa(inicio, eventos, None)

    return texto_resposta or "Desculpe, não consegui processar sua mensagem agora."
