"""O agente por fora: identidade publica, Tasks e o envelope JSON-RPC do A2A v1.0.

Aqui nao ha regra de negocio de sala nenhuma. Conflito, politica e alternativas
sao decisao do servidor MCP; este modulo cuida de identidade, estado e produto
da Task -- o que o A2A tem e o MCP nao.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"

TERMINAIS = {COMPLETED, CANCELED, FAILED}


def agent_card(url_do_endpoint: str) -> dict[str, Any]:
    """Agent Card na forma da v1.0.

    Na v1.0 url, transporte e versao vivem dentro de `supportedInterfaces[]`, com
    o transporte em `protocolBinding`. `preferredTransport` e
    `additionalInterfaces` sao da v0.x e nao existem aqui.
    """
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": url_do_endpoint, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": (
                    "Reserva uma sala em um intervalo. Se houver conflito, pergunta "
                    "qual alternativa usar."
                ),
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Envelope JSON-RPC
# --------------------------------------------------------------------------- #


def resultado(identificador: Any, valor: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identificador, "result": valor}


def falha(identificador: Any, codigo: int, mensagem: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identificador, "error": {"code": codigo, "message": mensagem}}


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


def _identidade(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def mensagem_do_agente(texto: str, task_id: str, context_id: str) -> dict[str, Any]:
    return {
        "messageId": _identidade("msg"),
        "role": "ROLE_AGENT",
        "parts": [{"text": texto}],
        "taskId": task_id,
        "contextId": context_id,
    }


class Tarefas:
    """Store em memoria das Tasks, e das pausas que ficam ao lado delas.

    As duas tabelas sao separadas de proposito. A Task e o objeto publico, que
    vai inteiro para o cliente A2A. A pausa guarda o `requestState` e o que o
    agente precisa para retomar, e nunca e serializada para ninguem: e assim que
    o estado opaco do MCP fica invisivel do lado A2A.
    """

    def __init__(self) -> None:
        self._tarefas: dict[str, dict[str, Any]] = {}
        self._pausas: dict[str, dict[str, Any]] = {}

    # -- Task ---------------------------------------------------------------- #

    def abrir(self, mensagem_do_usuario: dict[str, Any], trace_id: str) -> dict[str, Any]:
        task_id, context_id = _identidade("task"), _identidade("ctx")
        tarefa = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": SUBMITTED},
            "history": [mensagem_do_usuario],
            "artifacts": [],
        }
        self._tarefas[task_id] = tarefa
        self._pausas[task_id] = {"trace_id": trace_id}
        return tarefa

    def buscar(self, task_id: str) -> dict[str, Any] | None:
        return self._tarefas.get(task_id)

    def estado(self, tarefa: dict[str, Any]) -> str:
        return tarefa["status"]["state"]

    def anotar(self, tarefa: dict[str, Any], mensagem: dict[str, Any]) -> None:
        tarefa["history"].append(mensagem)

    def mover(self, tarefa: dict[str, Any], estado: str, texto: str | None = None) -> None:
        """Move a Task de estado, opcionalmente com uma mensagem de status.

        A mensagem entra tambem no historico, porque e la que o cliente A2A
        procura o que aconteceu -- inclusive a mensagem exata da tool quando o
        pedido falhou.
        """
        tarefa["status"] = {"state": estado}
        if texto is not None:
            mensagem = mensagem_do_agente(texto, tarefa["id"], tarefa["contextId"])
            tarefa["status"]["message"] = mensagem
            self.anotar(tarefa, mensagem)
        if estado in TERMINAIS:
            self._pausas.pop(tarefa["id"], None)

    def anexar_reserva(self, tarefa: dict[str, Any], reserva: dict[str, Any]) -> None:
        """Produto da Task: o artifact com o JSON da reserva criada."""
        tarefa["artifacts"].append(
            {
                "artifactId": _identidade("art"),
                "name": "reserva",
                "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}],
            }
        )

    # -- pausa --------------------------------------------------------------- #

    def trace_id(self, task_id: str) -> str:
        return self._pausas.get(task_id, {}).get("trace_id", secrets.token_hex(16))

    def guardar_pausa(
        self, task_id: str, chave: str, request_state: str, opcoes: list[str], argumentos: dict[str, Any]
    ) -> None:
        pausa = self._pausas.setdefault(task_id, {})
        pausa.update(
            {
                "chave": chave,
                "request_state": request_state,
                "opcoes": opcoes,
                "argumentos": argumentos,
            }
        )

    def pausa(self, task_id: str) -> dict[str, Any] | None:
        pausa = self._pausas.get(task_id)
        return pausa if pausa and "request_state" in pausa else None
