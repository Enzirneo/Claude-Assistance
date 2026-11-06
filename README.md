# ccwatch

Painel para acompanhar o uso do Claude Code: contexto de cada conversa, limites do plano
(sessão de 5h e semana), conta, modelo, esforço e um veredito de "pode seguir ou não".

É a versão em janelas do `ccwatch.py` (terminal). Abre um **hub** com todas as conversas e
uma **janela por chat**, pequena o bastante para empilhar duas na tela (cada uma ocupa no
máximo metade da altura).

Sem dependências em tempo de execução. Só lê arquivos locais do Claude Code:

| Arquivo | Para quê |
|---|---|
| `~/.claude.json` | conta logada e cache do `/usage` |
| `~/.claude/projects/<proj>/<id>.jsonl` | transcript (tokens, modelo, esforço) |
| `~/.claude/projects/<proj>/<id>/custom-title.json` | nome do chat |
| `~/.claude/.credentials.json` | token OAuth para consultar o uso do plano |

## Requisitos

- Windows com **Edge** ou **Chrome** (as janelas são abertas em modo app).
- **Python 3.10+** (para rodar do código ou gerar o `.exe`).
- Claude Code instalado e com login feito.

## Usar

Rodar direto do código:

```
python server.py
```

Ou gerar o executável (precisa do PyInstaller, o `build.bat` instala):

```
build.bat
```

Isso cria `ccwatch.exe` na raiz, com ícone próprio. É só abrir (ou criar um atalho para ele).
O `.exe` não vai para o git; gere de novo em cada máquina.

- **Hub:** lista todos os chats (contexto, tokens, modelo, gasto e atividade). Clique em um
  para abrir a janela dele.
- **Janela de chat:** quatro blocos: Contexto, Limites do plano, Conta e modelo, Veredito.
  O layout depende da largura (empilhado, 2×2 ou 4 lado a lado). Ao abrir, ela se ajusta ao
  conteúdo, sem passar de metade da altura útil da tela. Se você mover ou redimensionar, ela
  respeita.
- O servidor encerra sozinho uns 3 minutos depois de fechar todas as janelas.

## Estrutura

```
server.py      servidor HTTP local (127.0.0.1:8765) + abertura das janelas
core.py        leitura dos transcripts, contexto, gasto acumulado e uso do plano
web/           interface (index.html = hub, chat.html = janela de chat)
icon.ico       ícone do .exe e do atalho
build.bat      gera o ccwatch.exe com PyInstaller
ccwatch_terminal.py   versão original para terminal
```

## Limites do plano e HTTP 429

O uso do plano vem de uma API do Claude que **limita consultas com facilidade** (HTTP 429).
O app consulta a cada 60s, recua (até 5 min) quando falha, e guarda o último valor bom em
`%LOCALAPPDATA%\ccwatch\usage.json`, mostrando-o como "dados de X atrás". Se você rodar o
`ccwatch_terminal.py` ao mesmo tempo, os dois competem pelo mesmo limite. Prefira um só.

## Variáveis de ambiente (testes)

| Variável | Efeito |
|---|---|
| `CCWATCH_NO_WINDOW=1` | sobe só o servidor, sem abrir janelas |
| `CCWATCH_PORT=8799` | usa outra porta (padrão 8765) |

Log de erros e perfil do navegador ficam em `%LOCALAPPDATA%\ccwatch`.

## Segurança

O servidor escuta apenas em `127.0.0.1`, rejeita `Host` diferente de localhost e só aceita
`POST /api/open` com `Content-Type: application/json`. O token OAuth é lido do disco e usado
apenas para consultar o uso do plano; nunca é enviado para a interface.
