"""A ponte: o agente que e host MCP por dentro e servidor A2A por fora.

Nenhum dos dois protocolos tem sessao. O MCP resolve isso com o `requestState`,
que volta pelas maos do cliente; o A2A resolve com a Task, que tem identidade,
estado e produto. Este modulo e o ponto onde os dois se encontram:

- `_resposta_da_tool` traduz o `input_required` do MCP em
  `TASK_STATE_INPUT_REQUIRED`, guardando o `requestState` ao lado da Task;
- `_continuar` faz o caminho de volta, repetindo o `tools/call` original com um
  id de JSON-RPC novo e o `requestState` ecoado sem modificacao.

Nao ha LLM em lugar nenhum: o pedido chega em formato fixo e a decisao e por
regra, entao o mesmo pedido produz sempre o mesmo resultado.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.types import ElicitResult, InputRequiredResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import a2a
from host_mcp import HostMCP, alternativas_do_pedido, texto_do_resultado

# `reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>` e `escolha=<valor>`.
# O valor vai ate o proximo `chave=` ou ate o fim, para aceitar nome com espaco.
CAMPOS = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)")

CAMPOS_DA_RESERVA = ("sala", "inicio", "fim", "responsavel")
RECUSAR = "recusar"

INVALID_PARAMS = -32602
TASK_NAO_ENCONTRADA = -32001
TASK_EM_ESTADO_TERMINAL = -32002

tarefas = a2a.Tarefas()
host: HostMCP
tranca = anyio.Lock()


def carregar_env() -> None:
    """Le o `.env` da raiz para o ambiente, sem dependencia externa.

    O agente nao usa segredo nenhum -- so as variaveis de endereco. Uma variavel
    ja definida no ambiente sempre ganha do arquivo.
    """
    arquivo = Path(__file__).resolve().parents[1] / ".env"
    if not arquivo.exists():
        return
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip("\"'"))


def _campos(texto: str) -> dict[str, str]:
    return {chave: valor.strip() for chave, valor in CAMPOS.findall(texto)}


def _texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    return " ".join(parte.get("text", "") for parte in mensagem.get("parts") or []).strip()


def _trace_id(request: Request) -> str:
    """Trace-id do cliente A2A, para propagar ate o servidor MCP.

    O span-id pode ser novo a cada request MCP; o trace-id, nao.
    """
    partes = (request.headers.get("traceparent") or "").split("-")
    if len(partes) >= 3 and len(partes[1]) == 32:
        return partes[1]
    return os.urandom(16).hex()


def _linha_de_alternativas(opcoes: list[str]) -> str:
    """A pausa, byte a byte. Sem prefixo e sem saudacao: ela e comparada literalmente."""
    return "alternativas: " + ", ".join(opcoes)


# --------------------------------------------------------------------------- #
# A ponte
# --------------------------------------------------------------------------- #


async def _resposta_da_tool(
    tarefa: dict[str, Any], resposta: Any, argumentos: dict[str, Any]
) -> None:
    """Traduz o desfecho de um `tools/call` para o estado da Task.

    E aqui que o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`: o
    agente nao responde a pergunta por conta propria nem trava esperando, ele
    interrompe a Task, devolve a pergunta ao cliente A2A e guarda o
    `requestState` ligado aquela Task.
    """
    if isinstance(resposta, InputRequiredResult):
        chave, pedido = next(iter(resposta.input_requests.items()))
        opcoes = alternativas_do_pedido(pedido)
        tarefas.guardar_pausa(
            tarefa["id"], chave, resposta.request_state or "", opcoes, argumentos
        )
        tarefas.mover(tarefa, a2a.INPUT_REQUIRED, _linha_de_alternativas(opcoes))
        return

    if resposta.is_error:
        # Erro de execucao da tool: a mensagem exata do servidor MCP chega ao
        # cliente A2A pelo historico da Task.
        tarefas.mover(tarefa, a2a.FAILED, texto_do_resultado(resposta))
        return

    dados = resposta.structured_content or {}
    if not dados.get("reservado"):
        tarefas.mover(tarefa, a2a.CANCELED, f"Reserva nao realizada: {dados.get('motivo')}.")
        return

    tarefas.anexar_reserva(
        tarefa,
        {
            "reserva": dados["reserva"],
            "sala": dados["sala"],
            "inicio": dados["inicio"],
            "fim": dados["fim"],
            "responsavel": dados["responsavel"],
            # A versao vem do resource que o agente leu, nao do que a tool devolveu.
            "politica": host.versao_da_politica,
        },
    )
    tarefas.mover(
        tarefa,
        a2a.COMPLETED,
        f"Reserva {dados['reserva']} confirmada na {dados['sala']}.",
    )


async def _abrir(mensagem: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """Pedido novo: abre a Task e faz a ida ao servidor MCP."""
    tarefa = tarefas.abrir(mensagem, trace_id)
    campos = _campos(_texto_da_mensagem(mensagem))

    if any(campo not in campos for campo in CAMPOS_DA_RESERVA):
        tarefas.mover(
            tarefa,
            a2a.FAILED,
            "Pedido invalido: use reservar sala=<id> inicio=<iso8601> "
            "fim=<iso8601> responsavel=<nome>",
        )
        return tarefa

    argumentos = {campo: campos[campo] for campo in CAMPOS_DA_RESERVA}
    tarefas.mover(tarefa, a2a.WORKING)
    async with tranca:
        resposta = await host.chamar_reserva(argumentos, trace_id)
    await _resposta_da_tool(tarefa, resposta, argumentos)
    return tarefa


async def _continuar(tarefa: dict[str, Any], mensagem: dict[str, Any]) -> dict[str, Any]:
    """Resposta a pausa: o `requestState` volta ao servidor MCP com um id novo."""
    tarefas.anotar(tarefa, mensagem)
    pausa = tarefas.pausa(tarefa["id"])
    if pausa is None:
        tarefas.mover(tarefa, a2a.FAILED, "Nao ha pergunta pendente para esta Task.")
        return tarefa

    escolha = _campos(_texto_da_mensagem(mensagem)).get("escolha", "")
    if escolha != RECUSAR and escolha not in pausa["opcoes"]:
        # Escolha fora do enum: a Task continua pausada e a lista se repete,
        # identica. Nada vai para o servidor MCP.
        tarefas.mover(tarefa, a2a.INPUT_REQUIRED, _linha_de_alternativas(pausa["opcoes"]))
        return tarefa

    if escolha == RECUSAR:
        elicitacao = ElicitResult(action="decline")
    else:
        elicitacao = ElicitResult(action="accept", content={"sala": escolha})

    tarefas.mover(tarefa, a2a.WORKING)
    async with tranca:
        resposta = await host.chamar_reserva(
            # Argumentos identicos aos da ida: o `requestState` sela um digest
            # deles, e qualquer divergencia e recusada pelo servidor.
            pausa["argumentos"],
            tarefas.trace_id(tarefa["id"]),
            # Mesma chave que veio no `inputRequests`, e o estado opaco ecoado
            # sem modificacao: o agente guarda e devolve, nunca abre.
            input_responses={pausa["chave"]: elicitacao},
            request_state=pausa["request_state"],
        )
    await _resposta_da_tool(tarefa, resposta, pausa["argumentos"])
    return tarefa


# --------------------------------------------------------------------------- #
# Metodos A2A
# --------------------------------------------------------------------------- #


async def _send_message(identificador: Any, params: dict[str, Any], request: Request) -> dict[str, Any]:
    mensagem = params.get("message") or {}
    task_id = mensagem.get("taskId")

    if not task_id:
        return a2a.resultado(identificador, {"task": await _abrir(mensagem, _trace_id(request))})

    tarefa = tarefas.buscar(task_id)
    if tarefa is None:
        return a2a.falha(identificador, TASK_NAO_ENCONTRADA, f"Task desconhecida: {task_id}")

    estado = tarefas.estado(tarefa)
    if estado in a2a.TERMINAIS:
        # Estado terminal e definitivo: uma Task concluida nao volta a trabalhar.
        return a2a.falha(
            identificador,
            TASK_EM_ESTADO_TERMINAL,
            f"A Task {task_id} ja terminou em {estado} e nao aceita novas mensagens",
        )
    if estado != a2a.INPUT_REQUIRED:
        return a2a.falha(
            identificador, INVALID_PARAMS, f"A Task {task_id} nao esta esperando resposta"
        )

    return a2a.resultado(identificador, {"task": await _continuar(tarefa, mensagem)})


async def _get_task(identificador: Any, params: dict[str, Any]) -> dict[str, Any]:
    tarefa = tarefas.buscar(params.get("id") or "")
    if tarefa is None:
        return a2a.falha(
            identificador, TASK_NAO_ENCONTRADA, f"Task desconhecida: {params.get('id')}"
        )
    return a2a.resultado(identificador, {"task": tarefa})


async def rpc(request: Request) -> JSONResponse:
    pedido = await request.json()
    identificador, metodo = pedido.get("id"), pedido.get("method")
    params = pedido.get("params") or {}

    if metodo == "SendMessage":
        return JSONResponse(await _send_message(identificador, params, request))
    if metodo == "GetTask":
        return JSONResponse(await _get_task(identificador, params))
    return JSONResponse(a2a.falha(identificador, -32601, f"Metodo desconhecido: {metodo}"))


async def card(request: Request) -> JSONResponse:
    return JSONResponse(a2a.agent_card(_url_do_endpoint()))


# --------------------------------------------------------------------------- #
# Subida
# --------------------------------------------------------------------------- #


def _url_do_endpoint() -> str:
    host_a2a = os.environ.get("A2A_HOST", "127.0.0.1")
    porta = os.environ.get("A2A_PORT", "7300")
    return f"http://{host_a2a}:{porta}/a2a"


@asynccontextmanager
async def ciclo_de_vida(app: Starlette):  # type: ignore[no-untyped-def]
    """Abre a sessao MCP antes de atender qualquer request A2A.

    A descoberta acontece aqui: o `tools/list` e a leitura do resource sao os
    primeiros requests que o servidor MCP registra, antes de qualquer
    `tools/call`.
    """
    global host
    carregar_env()
    host = HostMCP(os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp"))
    await host.abrir()
    try:
        yield
    finally:
        await host.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", card, methods=["GET"]),
        Route("/a2a", rpc, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


def main() -> None:
    carregar_env()
    host_a2a = os.environ.get("A2A_HOST", "127.0.0.1")
    porta = int(os.environ.get("A2A_PORT", "7300"))
    print(f"[agente] Central de Salas em http://{host_a2a}:{porta}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=host_a2a, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
