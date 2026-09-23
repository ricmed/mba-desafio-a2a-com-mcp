"""Dominio da central de salas: dados, regras da politica e mensagens de erro.

Cinco salas, uma lista de reservas em memoria e tres regras de uso. Tudo vem dos
JSON de `dados/`, que o desafio proibe alterar. A persistencia e proposital e
deliberadamente burra: reservas novas vivem na lista em memoria e nao sobrevivem
a um restart, porque o unico estado que precisa atravessar reinicio e o
`requestState`, e ele viaja com o cliente.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

DADOS = Path(__file__).resolve().parents[1] / "dados"

# As mensagens abaixo sao a fonte unica de verdade do enunciado: o validador exige
# o texto exato dentro do conteudo do resultado.
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_DE_ALTERNATIVAS = 3

SALAS: dict[str, dict[str, Any]] = {
    sala["id"]: sala for sala in json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))

POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _instante(valor: str) -> datetime:
    try:
        return datetime.fromisoformat(valor)
    except ValueError as exc:
        raise ToolError(ERRO_INTERVALO) from exc


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras da politica e devolve o intervalo ja convertido.

    A ordem importa: um intervalo invertido como 10:00 -> 09:00 esta dentro da
    janela e tem duracao negativa, entao a checagem de intervalo precisa vir
    antes da de duracao para que a mensagem certa chegue ao cliente.
    """
    if sala not in SALAS:
        raise ToolError(ERRO_SALA.format(sala=sala))

    momento_inicial, momento_final = _instante(inicio), _instante(fim)
    if momento_final <= momento_inicial:
        raise ToolError(ERRO_INTERVALO)

    fuso = momento_inicial.tzinfo
    if momento_inicial.timetz().replace(tzinfo=None) < ABERTURA or (
        momento_final.astimezone(fuso).timetz().replace(tzinfo=None) > FECHAMENTO
    ):
        raise ToolError(ERRO_JANELA)

    if momento_final - momento_inicial > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)

    return momento_inicial, momento_final


def conflitos(sala: str, inicio: datetime, fim: datetime) -> list[dict[str, Any]]:
    """Reservas da sala que se sobrepoem ao intervalo."""
    return [
        reserva
        for reserva in RESERVAS
        if reserva["sala"] == sala
        and _instante(reserva["inicio"]) < fim
        and inicio < _instante(reserva["fim"])
    ]


def livre(sala: str, inicio: datetime, fim: datetime) -> bool:
    return not conflitos(sala, inicio, fim)


def alternativas(sala: str, inicio: datetime, fim: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade igual ou maior que a pedida.

    No maximo tres, ordenadas por capacidade crescente e, no empate, por id em
    ordem alfabetica. E esta ordem que vira o `enum` da elicitation e, mais
    tarde, a linha `alternativas:` que o agente devolve ao cliente A2A.
    """
    minimo = SALAS[sala]["capacidade"]
    candidatas = [
        outra
        for outra in SALAS.values()
        if outra["id"] != sala and outra["capacidade"] >= minimo and livre(outra["id"], inicio, fim)
    ]
    candidatas.sort(key=lambda outra: (outra["capacidade"], outra["id"]))
    return [outra["id"] for outra in candidatas[:MAXIMO_DE_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict[str, Any]:
    """Grava a reserva na lista em memoria e devolve o registro criado."""
    proximo = max((int(r["id"].removeprefix("res-")) for r in RESERVAS), default=0) + 1
    reserva = {
        "id": f"res-{proximo:04d}",
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return reserva
