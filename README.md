# A Ponte: um agente A2A com MCP por dentro

Dois processos, dois protocolos, e uma costura entre eles.

- **`servidor-mcp/`** — servidor MCP em Streamable HTTP, com três tools, um resource e o
  ciclo completo de MRTR na reserva.
- **`agente/`** — um agente que é *host MCP* por dentro, consumindo aquele servidor por
  HTTP, e *servidor A2A v1.0* por fora, descobrível pelo Agent Card.

Stack: Python 3.10+ com o SDK oficial `mcp` v2 (`2.2.0`, alinhado à revisão `2026-07-28`
da spec). A camada A2A é escrita sobre Starlette, que já vem como dependência do SDK.
Não há LLM no caminho de execução: o pedido chega em formato fixo e a decisão é por
regra, então o mesmo pedido produz sempre o mesmo resultado.

---

## Como rodar

A partir de um clone limpo:

```bash
uv sync
```

Gere o segredo que protege o `requestState`. São 32 bytes de aleatoriedade, e ele
**nunca** vai para o repositório — este aqui é público:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Agora torne esse valor visível para o servidor. Há dois caminhos, e o primeiro é o
recomendado porque vale para qualquer terminal.

**Pelo arquivo `.env`** — copie o modelo e troque o valor. O `.env` está no `.gitignore`,
então ele nunca vai para o repositório:

```bash
cp .env.example .env
```

Abra o `.env` e deixe a linha assim, com o valor que você gerou:

```
REQUEST_STATE_SECRET=<o valor gerado acima>
```

**Ou exportando no terminal**, se você preferir não ter arquivo. Lembre que a variável
vale só para aquele terminal, e o servidor precisa subir no mesmo. No Linux ou macOS:

```bash
export REQUEST_STATE_SECRET=<o valor gerado acima>
```

No Windows, em PowerShell:

```powershell
$env:REQUEST_STATE_SECRET = "<o valor gerado acima>"
```

Uma variável exportada no ambiente sempre ganha do que estiver no `.env`.

Suba os dois processos, **cada um em um terminal**, deixando o stderr do servidor MCP
visível. O servidor exige o segredo; o agente, não.

```bash
uv run python servidor-mcp/servidor.py
```

```bash
uv run python agente/agente.py
```

O servidor MCP atende em `http://127.0.0.1:7301/mcp` e o agente em
`http://127.0.0.1:7300`, com o card em `/.well-known/agent-card.json` e o endpoint
JSON-RPC em `/a2a`. Dá para parametrizar por variável de ambiente (`MCP_HOST`,
`MCP_PORT`, `MCP_PATH`, `A2A_HOST`, `A2A_PORT`, `MCP_URL`), e os padrões são esses.

Com os dois no ar, em um terceiro terminal:

```bash
python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

> Rode sempre com os dois processos recém-iniciados. As reservas criadas por uma execução
> mudam o resultado da seguinte.

---

## Onde a ponte acontece

A ponte tem duas pontas, e as duas estão em [`agente/agente.py`](agente/agente.py).

**A ida — o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`.** Em
[`_resposta_da_tool`](agente/agente.py), quando a chamada devolve um `InputRequiredResult`
em vez de um `CallToolResult`, o agente não responde a pergunta por conta própria nem
trava esperando: ele lê a chave e o `enum` da elicitation, chama
`tarefas.guardar_pausa(...)` para amarrar o `requestState` àquela Task, e move a Task para
`TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: <ids>`. A Task interrompida é a
tradução, no vocabulário do A2A, de uma resposta do MCP que terminou pedindo informação.

O que torna isso possível está em [`agente/host_mcp.py`](agente/host_mcp.py): a chamada usa
`session.call_tool(..., allow_input_required=True)`, que entrega o `input_required` **cru**.
O atalho `client.call_tool()` resolveria a elicitation sozinho pelo callback do cliente e
fecharia o ciclo dentro do agente — a Task nunca pausaria, e a ponte deixaria de existir.
O `elicitation_callback` registrado ali serve só para declarar a capability (no SDK v2 um
callback *é* uma capability) e levanta erro se algum dia for chamado, como canário.

**A volta — o `requestState` retorna ao servidor.** Em
[`_continuar`](agente/agente.py), a mensagem `escolha=<valor>` vira um `ElicitResult` e o
agente repete o `tools/call` original: os **mesmos** argumentos da ida, a **mesma** chave
que veio no `inputRequests`, e o `requestState` ecoado sem modificação. O id de JSON-RPC é
novo, porque são requests independentes — ele sai do contador da própria sessão do SDK, e
dá para conferir isso no stderr do servidor MCP.

Para o agente o `requestState` é opaco: ele guarda, ecoa e nunca abre nem interpreta,
mesmo o conteúdo sendo legível.

---

## Decisões técnicas

**Como o `requestState` é protegido.** Pela `RequestStateSecurity` do SDK, com a chave
vinda de `REQUEST_STATE_SECRET` — nunca do código. O codec embutido é AES-256-GCM, ou
seja, AEAD: além de detectar adulteração, cifra o conteúdo. Trocar um caractere sequer do
token faz o servidor responder `-32602` com `Invalid or expired requestState`, e o motivo
real fica só no log. O envelope também sela um digest de `name` + `arguments` do request
original, então um retry com argumentos adulterados é recusado pelo mesmo caminho: os
valores adulterados não têm como tomar efeito.

**Por quanto tempo ele vale.** 15 minutos (`TTL_DO_REQUEST_STATE` em
[`servidor-mcp/servidor.py`](servidor-mcp/servidor.py)), dentro da janela de 5 a 30
exigida. Passado esse prazo, o retry é recusado com o mesmo `-32602`.

**Onde o estado das Tasks foi guardado.** Em memória, em
[`agente/a2a.py`](agente/a2a.py), na classe `Tarefas` — e em **duas tabelas separadas de
propósito**. `_tarefas` guarda o objeto público, que vai inteiro para o cliente A2A;
`_pausas` guarda o `requestState`, a chave da elicitation, as alternativas e os argumentos
originais, e nunca é serializada para ninguém. É essa separação que garante que o
`requestState` não vaze no card, no artifact nem em nenhuma mensagem devolvida. O estado
pausado é por Task, então duas Tasks pausadas ao mesmo tempo terminam cada uma com a sua
reserva, sem trocar de `requestState`.

**O servidor MCP não guarda nada entre as rodadas.** Todo o estado do pedido interrompido
viaja no `requestState`, o que é justamente o que permite um retry apresentado depois de o
processo ter sido reiniciado funcionar — desde que o mesmo `REQUEST_STATE_SECRET` esteja
no ambiente. As reservas, essas sim, vivem em memória e não sobrevivem a um restart, como
o enunciado permite.

**Onde as regras moram.** Conflito, política e cálculo de alternativas são decisão
exclusiva do servidor MCP, em [`servidor-mcp/dominio.py`](servidor-mcp/dominio.py). O
agente traduz protocolo, não domínio: ele não sabe o que é uma janela de uso nem como se
ordena uma alternativa. Uma consequência prática disso é que o resolver recalcula as
alternativas a cada rodada, e o cálculo do servidor sempre ganha do que o cliente ecoa de
volta no `requestState`.

**Propagação de trace.** O trace-id que chega no header `traceparent` do cliente A2A é
guardado na Task e reemitido no `traceparent` dentro do `_meta` de todos os requests MCP
daquela Task, com span-id novo a cada um. O stderr do servidor MCP registra método, id e
traceparent de cada request recebido.

---

## Saída do validador

Execução com os dois processos recém-iniciados, terminando com código de saída `0`:

```
trace-id desta execucao: c4adc63670a0e9f10a180c8efe5f1dfb
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

### As verificações que o validador não faz

Três exigências dependem de reiniciar processo ou de ler log, e ficam no fluxo do
avaliador. Todas foram conferidas:

**O `traceparent` no stderr do servidor MCP.** O trace-id impresso pelo validador aparece
nos requests que o agente emitiu por conta daquelas chamadas A2A:

```
[mcp] method=tools/list id=2 traceparent=None
[mcp] method=resources/read id=3 traceparent=None
...
[mcp] method=tools/call id=4 traceparent=00-c4adc63670a0e9f10a180c8efe5f1dfb-a0b2b111ca877850-01
[mcp] method=tools/call id=5 traceparent=00-c4adc63670a0e9f10a180c8efe5f1dfb-b1742dcf2e6b7505-01
[mcp] method=tools/call id=6 traceparent=00-c4adc63670a0e9f10a180c8efe5f1dfb-56ac26dc5c3cefb1-01
```

O `tools/list` e o `resources/read` da descoberta são os primeiros requests do agente,
antes de qualquer `tools/call`. E o par acima é uma reserva que passou pela pausa: `id=5`
é o request que pediu o input, `id=6` é o retry — ids diferentes, como a spec exige.

**O `requestState` sobrevivendo a um restart.** Pedindo uma reserva em conflito direto ao
servidor MCP, matando o processo, subindo outro com o mesmo `REQUEST_STATE_SECRET` e
enviando o retry com o `requestState` intacto:

```
retomada  -> complete {"reserva": "res-0003", "reservado": true, "sala": "sala-fusca", ...}
```

O servidor recém-iniciado nunca viu aquele pedido, e ainda assim o concluiu: o estado
estava todo dentro do token.

**Adulteração e capability ausente.** Trocando um caractere do `requestState`, e depois
repetindo o mesmo conflito sem declarar `elicitation` nas capabilities:

```
adulterado     -> {"code": -32602, "message": "Invalid or expired requestState", ...}
sem capability -> http 400 {"code": -32021, "message": "Client did not declare the form
                  elicitation capability required by resolver '__main__:escolha_de_sala'",
                  "data": {"requiredCapabilities": {"elicitation": {"form": {}}}}}
```

---

## Estrutura

```
.
├── pyproject.toml         versões travadas (mcp==2.2.0)
├── uv.lock
├── .env.example           modelo do segredo; o .env real não vai para o repositório
├── dados/                 do starter, não alterado
├── validador/             do starter, não alterado
├── exemplos/              do starter, não alterado
├── servidor-mcp/
│   ├── servidor.py        MCPServer, tools, resource, o resolver do MRTR, log de stderr
│   └── dominio.py         salas, reservas em memória, regras e mensagens exatas
└── agente/
    ├── agente.py          a ponte: SendMessage, GetTask e a máquina de estados da Task
    ├── host_mcp.py        o agente como host MCP: descoberta, política e chamadas
    └── a2a.py             Agent Card, store de Tasks e o envelope JSON-RPC
```
