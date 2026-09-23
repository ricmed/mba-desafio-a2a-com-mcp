"""O agente por dentro: um host MCP de verdade, falando HTTP com o servidor.

Nada de importar a funcao da tool: existem dois processos, e este fala com o
outro pelo transporte. O detalhe que decide o desafio esta em `chamar_reserva`:
a chamada usa `session.call_tool(..., allow_input_required=True)`, que devolve o
`InputRequiredResult` cru. O atalho `client.call_tool()` responderia a
elicitation sozinho pelo callback e fecharia o ciclo aqui dentro -- a Task nunca
pausaria, e a ponte deixaria de existir.
"""

from __future__ import annotations

import secrets
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client
from mcp.types import CallToolResult, ElicitResult, InputRequiredResult

FERRAMENTA_DE_RESERVA = "reservar_sala"
URI_DA_POLITICA = "politica://uso"


async def _nunca_perguntado(context: Any, params: Any) -> ElicitResult:
    """Canario: registrar este callback e o que declara a capability de elicitation.

    No SDK v2 um callback e uma capability -- e ele que faz o `_meta` sair com
    `{"elicitation": {"form": {}}}`. Ele nao deve rodar nunca, porque todas as
    chamadas pedem o `input_required` cru. Se rodar, a ponte foi curto-circuitada.
    """
    raise RuntimeError(
        "o callback de elicitation foi chamado: alguma chamada MCP esta resolvendo "
        "a pergunta sozinha em vez de devolver o input_required para a Task A2A"
    )


class HostMCP:
    """Sessao MCP viva, compartilhada entre os requests A2A.

    Manter o objeto cliente vivo entre chamadas e normal e recomendado. O que
    seria proibido e inferir versao, capabilities ou contexto de um request
    anterior -- e isso nao acontece: cada request carrega o proprio `_meta`.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.ferramentas: list[str] = []
        self.versao_da_politica: str = ""
        self._pilha = AsyncExitStack()
        self._cliente: Client | None = None

    # -- ciclo de vida ------------------------------------------------------ #

    async def abrir(self) -> None:
        """Conecta, descobre as tools e le a politica. Nesta ordem."""
        self._cliente = await self._pilha.enter_async_context(
            Client(self.url, elicitation_callback=_nunca_perguntado)
        )

        # Descoberta em runtime: a lista de tools vem do servidor, nunca do codigo.
        catalogo = await self._cliente.list_tools()
        self.ferramentas = [ferramenta.name for ferramenta in catalogo.tools]
        if FERRAMENTA_DE_RESERVA not in self.ferramentas:
            raise RuntimeError(
                f"o servidor MCP nao expoe {FERRAMENTA_DE_RESERVA!r}; "
                f"descobri {self.ferramentas}"
            )

        # O resource e escolha da aplicacao: o agente decide le-lo e de onde tira
        # a versao que vai carimbar no artifact.
        politica = await self._cliente.read_resource(URI_DA_POLITICA)
        texto = "".join(getattr(bloco, "text", "") for bloco in politica.contents)
        self.versao_da_politica = texto.splitlines()[0].split(":", 1)[1].strip()

        print(
            f"[agente] tools descobertas: {self.ferramentas} | "
            f"politica {self.versao_da_politica}",
            file=sys.stderr,
            flush=True,
        )

    async def fechar(self) -> None:
        await self._pilha.aclose()

    # -- chamadas ----------------------------------------------------------- #

    def _meta(self, trace_id: str) -> dict[str, str]:
        """Trace context no `_meta`: mesmo trace-id do cliente A2A, span novo."""
        return {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}

    async def chamar_reserva(
        self,
        argumentos: dict[str, Any],
        trace_id: str,
        *,
        input_responses: dict[str, ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """Um `tools/call` de reserva, ida ou retomada.

        Na retomada, `argumentos` precisa ser identico ao da ida: o `requestState`
        sela um digest de `name` + `arguments`, e qualquer divergencia e recusada
        pelo servidor. O id de JSON-RPC novo sai do contador da propria sessao.
        """
        assert self._cliente is not None, "HostMCP usado antes de abrir()"
        return await self._cliente.session.call_tool(
            FERRAMENTA_DE_RESERVA,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(trace_id),
            allow_input_required=True,
        )


def texto_do_resultado(resultado: CallToolResult) -> str:
    """Junta os blocos de texto de um resultado de tool."""
    return " ".join(getattr(bloco, "text", "") for bloco in resultado.content).strip()


def alternativas_do_pedido(pedido: Any) -> list[str]:
    """Extrai, do `requestedSchema` da elicitation, as salas oferecidas.

    Com duas ou mais alternativas o schema traz `enum`; com uma so, `const`.
    A ordem e a que o servidor calculou, e e ela que vai para o cliente A2A.
    """
    esquema = pedido.params.requested_schema or {}
    campo = (esquema.get("properties") or {}).get("sala") or {}
    if "enum" in campo:
        return list(campo["enum"])
    if "const" in campo:
        return [campo["const"]]
    return []
