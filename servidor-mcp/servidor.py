"""Servidor MCP da central de salas, em Streamable HTTP.

Tres tools, um resource e o ciclo completo de MRTR na reserva. O ponto central
esta em `escolha_de_sala`: quando o intervalo pedido conflita, o resolver nao
pergunta nada de forma sincrona -- ele devolve um marcador `Elicit`, e o
framework termina a resposta com `resultType: input_required`, a elicitation em
form mode e um `requestState` selado. Nao existe canal de volta no transporte
stateless, e nao e disso que o MRTR precisa: quem volta com um request novo e o
cliente.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

import dominio

# --------------------------------------------------------------------------- #
# Segredo do requestState
# --------------------------------------------------------------------------- #

TTL_DO_REQUEST_STATE = 900  # 15 minutos, dentro da janela de 5 a 30 exigida.

COMO_GERAR = 'Gere a sua com: python -c "import secrets; print(secrets.token_hex(32))"'


def carregar_env() -> None:
    """Le o `.env` da raiz para o ambiente, sem dependencia externa.

    Uma variavel ja definida no ambiente sempre ganha do arquivo: exportar na
    mao continua funcionando, e o `.env` e so a conveniencia de nao ter que
    reexportar a cada terminal novo. O arquivo e opcional e esta no .gitignore.
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


def _segredo() -> str:
    """Le REQUEST_STATE_SECRET do ambiente e exige 32 bytes de aleatoriedade.

    Nunca ha segredo no codigo: o repositorio e publico, e um `requestState`
    assinado com chave conhecida nao protege nada.
    """
    carregar_env()
    valor = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not valor:
        sys.exit(
            "REQUEST_STATE_SECRET nao esta definida.\n"
            f"{COMO_GERAR}\n"
            "Depois, exporte no terminal ou grave na raiz do projeto um arquivo .env com:\n"
            "  REQUEST_STATE_SECRET=<o valor gerado>"
        )
    try:
        bruto = bytes.fromhex(valor)
    except ValueError:
        bruto = valor.encode()
    if len(bruto) < 32:
        sys.exit(
            f"REQUEST_STATE_SECRET tem {len(bruto)} bytes; sao necessarios ao menos 32.\n"
            f"{COMO_GERAR}"
        )
    return valor


mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=TTL_DO_REQUEST_STATE),
)


# --------------------------------------------------------------------------- #
# Modelos de saida: e deles que o SDK deriva outputSchema e structuredContent
# --------------------------------------------------------------------------- #


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class SalaEscolhida(BaseModel):
    """Escolha ja resolvida, quando a sala pedida estava livre e nada foi perguntado."""

    sala: str


# --------------------------------------------------------------------------- #
# O resolver: onde o MRTR nasce
# --------------------------------------------------------------------------- #

PERGUNTA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


async def escolha_de_sala(sala: str, inicio: str, fim: str) -> SalaEscolhida | Elicit[Any]:
    """Decide em qual sala a reserva vai cair, perguntando ao cliente se preciso.

    Roda antes do corpo da tool, com os mesmos argumentos validados que ele
    recebe. Tres saidas possiveis:

    - sala livre: devolve a escolha pronta, sem elicitation nenhuma;
    - conflito com alternativas: devolve `Elicit`, e o framework transforma isso
      em `input_required` com o `requestState` selado;
    - conflito sem alternativa: erro de execucao, com a mensagem do enunciado.

    O corpo reroda a cada rodada, entao as alternativas sao sempre recalculadas
    pelo servidor. A resposta gravada no `requestState` so e consultada quando a
    pergunta e refeita com o mesmo texto: o calculo do servidor sempre ganha do
    que o cliente ecoa de volta.
    """
    momento_inicial, momento_final = dominio.validar(sala, inicio, fim)

    if dominio.livre(sala, momento_inicial, momento_final):
        return SalaEscolhida(sala=sala)

    opcoes = dominio.alternativas(sala, momento_inicial, momento_final)
    if not opcoes:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)

    # `Literal` com as alternativas na ordem da regra vira exatamente o
    # `requestedSchema` plano do contrato: `enum` com duas ou mais, `const` com
    # uma so.
    modelo = create_model(
        "EscolhaDeSala",
        sala=(
            Literal[tuple(opcoes)],  # type: ignore[valid-type]
            Field(description="Sala alternativa escolhida"),
        ),
    )
    return Elicit(PERGUNTA, modelo)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool()
def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut(**sala) for sala in dominio.SALAS.values()])


@mcp.tool()
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    momento_inicial, momento_final = dominio.validar(sala, inicio, fim)
    colisoes = dominio.conflitos(sala, momento_inicial, momento_final)
    return Disponibilidade(
        sala=sala,
        livre=not colisoes,
        conflitos=[
            ConflitoOut(
                id=reserva["id"],
                inicio=reserva["inicio"],
                fim=reserva["fim"],
                responsavel=reserva["responsavel"],
            )
            for reserva in colisoes
        ],
    )


@mcp.tool()
async def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    # A anotacao embrulhada e proposital: na forma desembrulhada uma recusa
    # viraria erro de execucao, e recusar nao e erro -- e uma conclusao sem reserva.
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado")

    reserva = dominio.criar_reserva(escolha.data.sala, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=dominio.POLITICA_VERSAO,
    )


# --------------------------------------------------------------------------- #
# Resource
# --------------------------------------------------------------------------- #


@mcp.resource("politica://uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    """Politica de uso das salas da Hill Valley Tech."""
    return dominio.POLITICA_TEXTO


# --------------------------------------------------------------------------- #
# Logging de cada request no stderr
# --------------------------------------------------------------------------- #


def com_log_de_requests(app):  # type: ignore[no-untyped-def]
    """Envolve o app ASGI para registrar metodo, id e traceparent de cada request.

    E o instrumento de depuracao do desafio: e aqui que se ve o `tools/list`
    antes do primeiro `tools/call`, o trace-id que o cliente A2A propagou, e que
    o id do retry difere do id do request que pediu o input.
    """

    async def middleware(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope.get("method") != "POST":
            await app(scope, receive, send)
            return

        corpo = bytearray()
        mensagens = []
        while True:
            mensagem = await receive()
            mensagens.append(mensagem)
            if mensagem["type"] != "http.request":
                break
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body", False):
                break

        try:
            pedido = json.loads(bytes(corpo))
            meta = (pedido.get("params") or {}).get("_meta") or {}
            print(
                f"[mcp] method={pedido.get('method')} id={pedido.get('id')} "
                f"traceparent={meta.get('traceparent')}",
                file=sys.stderr,
                flush=True,
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            print(f"[mcp] corpo nao interpretavel ({len(corpo)} bytes)", file=sys.stderr, flush=True)

        pendentes = iter(mensagens)

        async def replay():  # type: ignore[no-untyped-def]
            try:
                return next(pendentes)
            except StopIteration:
                return await receive()

        await app(scope, replay, send)

    return middleware


def main() -> None:
    import uvicorn

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    caminho = os.environ.get("MCP_PATH", "/mcp")

    # `json_response`: cada POST e respondido com um corpo JSON unico, nao com SSE.
    # `stateless_http`: nenhum estado de sessao entre requests, que e o modelo do
    # MCP v2 -- versao e capabilities vem no `_meta` de cada request, sempre.
    app = mcp.streamable_http_app(
        streamable_http_path=caminho,
        json_response=True,
        stateless_http=True,
        host=host,
    )
    print(f"[mcp] central-de-salas em http://{host}:{porta}{caminho}", file=sys.stderr, flush=True)
    uvicorn.run(com_log_de_requests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
