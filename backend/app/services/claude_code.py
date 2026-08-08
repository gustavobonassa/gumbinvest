"""Falar com o Claude Code local, para usar os créditos da assinatura Anthropic.

A assinatura Claude (Pro/Max) não emite chave de API e não é acessível pela API
de Mensagens — o único caminho é o CLI ``claude``, que já resolve a autenticação:
``claude auth login --claudeai`` abre o navegador, o usuário autoriza no site da
Anthropic e a credencial fica no perfil do usuário. Depois disso qualquer
processo do mesmo usuário do sistema que rode ``claude -p`` usa essa credencial.

Por que subprocesso e não o ``claude-agent-sdk``: nenhuma dependência nova no
requirements, nada de binário embutido para o PyInstaller resolver, e funciona
com qualquer instalação de Claude Code que o usuário já tenha.

Decisões que vieram de medir o CLI real, não da documentação:

* **O prompt vai por stdin, nunca por argv.** O contexto de uma carteira inteira
  estoura limites de linha de comando, e caracteres de JSON são mutilados pelo
  shell no caminho.
* **``--system-prompt`` substitui o do Claude Code**, derrubando o overhead de
  ~7.300 para ~1.200 tokens de input por chamada. Como o GumbInvest manda o seu
  próprio system prompt, isso é economia direta de cota da assinatura.
* **Busca web exige ``--tools`` E ``--allowed-tools``.** Só com ``--tools`` a
  ferramenta é oferecida mas negada por permissão, e o modelo responde pedindo
  autorização em vez de pesquisar.
* **``--bare`` é proibido aqui**: ele força ``ANTHROPIC_API_KEY`` e nunca lê o
  OAuth, ou seja, quebra exatamente o caso de uso desta integração.
* **``--json-schema`` não é usado**: combinado com ``--output-format json`` ele
  devolve saída vazia com exit 0. O ``extract_json`` de ``ai_research`` já cobre
  o caso para todos os provedores.

``authMethod`` do ``auth status`` é o portão real: ``claude.ai`` é assinatura;
``console`` é chave de API do Console, onde a promessa de "usar os créditos do
plano" não vale e a tela precisa dizer isso.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Uma chamada de research com busca web leva dezenas de segundos; o teto
#: acompanha o TIMEOUT de ai_research para os dois caminhos falharem junto.
CALL_TIMEOUT = 300.0
#: `auth status` é uma leitura local — se demorar isso, algo está travado.
STATUS_TIMEOUT = 20.0

SEARCH_TOOLS = "WebSearch,WebFetch"

INSTALL_URL = "https://claude.com/download"


class ClaudeCodeError(Exception):
    """Falha ao falar com o CLI; ``str(exc)`` é seguro para mostrar (pt-BR)."""


@dataclass(frozen=True)
class CliStatus:
    """O que a tela de Configurações precisa para escolher o que mostrar."""

    installed: bool
    logged_in: bool
    #: "claude.ai" (assinatura) | "console" (chave de API) | "" (desconhecido)
    method: str = ""
    email: str = ""
    plan: str = ""
    reason: str = ""

    @property
    def uses_subscription(self) -> bool:
        """Conectado *e* pela assinatura — não por chave de API do Console."""
        return self.logged_in and self.method == "claude.ai"


# ---------------------------------------------------------------------------
# Localizar o CLI

_MISSING = object()
_cli_cache: str | None | object = _MISSING


def cli_path(*, refresh: bool = False) -> str | None:
    """Caminho do executável ``claude``, ou None se não estiver instalado.

    Em cache porque é consultado a cada chamada e a cada render da tela; o botão
    "Verificar novamente" passa ``refresh=True`` depois que o usuário instala.
    """
    global _cli_cache
    if not refresh and _cli_cache is not _MISSING:
        return _cli_cache  # type: ignore[return-value]
    found = shutil.which("claude")
    if found is None and os.name == "nt":
        # Instalações antigas via npm deixam um shim .cmd em vez do .exe nativo.
        for name in ("claude.exe", "claude.cmd", "claude.bat"):
            found = shutil.which(name)
            if found:
                break
    _cli_cache = found
    return found


def _neutral_cwd() -> str:
    """Diretório de trabalho sem projeto, para o CLI não achar um CLAUDE.md.

    Rodar no diretório do servidor faria o Claude Code auto-descobrir memória e
    instruções do projeto e enfiá-las no prompt — contexto errado e cota gasta à
    toa.
    """
    path = Path(tempfile.gettempdir()) / "gumbinvest-ai"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _popen_kwargs() -> dict:
    kwargs: dict = {
        "cwd": _neutral_cwd(),
        "encoding": "utf-8",
        "errors": "replace",
        "text": True,
    }
    if os.name == "nt":
        # Sem isso, cada chamada pisca uma janela de console na frente do app.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


# ---------------------------------------------------------------------------
# Autenticação


#: O estado de login é consultado a cada GET /settings e a cada chamada de IA,
#: e cada consulta é um subprocesso. O login muda raramente, então vale um TTL
#: curto; quem precisa de leitura fresca (polling do botão Conectar, botão
#: "Verificar novamente") passa ``refresh=True``.
_STATUS_TTL = 30.0
_status_cache: tuple[float, CliStatus] | None = None


def status(*, refresh: bool = False) -> CliStatus:
    """Estado de instalação e login, para a tela e para os portões das rotas."""
    global _status_cache
    if not refresh and _status_cache is not None:
        cached_at, cached = _status_cache
        if time.monotonic() - cached_at < _STATUS_TTL:
            return cached
    result = _status_uncached(refresh=refresh)
    _status_cache = (time.monotonic(), result)
    return result


def _status_uncached(*, refresh: bool = False) -> CliStatus:
    binary = cli_path(refresh=refresh)
    if binary is None:
        return CliStatus(
            installed=False,
            logged_in=False,
            reason="Claude Code não encontrado nesta máquina.",
        )
    try:
        completed = subprocess.run(  # noqa: S603 — argv fixo, sem shell
            [binary, "auth", "status", "--json"],
            capture_output=True,
            timeout=STATUS_TIMEOUT,
            **_popen_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return CliStatus(
            installed=True, logged_in=False, reason="O Claude Code não respondeu a tempo."
        )
    except OSError as exc:  # binário existe mas não executa
        logger.warning("claude auth status falhou: %s", exc)
        return CliStatus(
            installed=True, logged_in=False, reason="Não foi possível executar o Claude Code."
        )

    try:
        data = json.loads(completed.stdout or "{}")
    except ValueError:
        return CliStatus(
            installed=True,
            logged_in=False,
            reason="Resposta inesperada do Claude Code ao checar a conta.",
        )

    if not data.get("loggedIn"):
        return CliStatus(installed=True, logged_in=False, reason="Conta Anthropic não conectada.")

    method = str(data.get("authMethod") or "")
    connected = CliStatus(
        installed=True,
        logged_in=True,
        method=method,
        email=str(data.get("email") or ""),
        plan=str(data.get("subscriptionType") or ""),
    )
    if method != "claude.ai":
        # Logado, mas por chave de API do Console: o consumo vai para créditos
        # pagos, não para o plano. Dizer isso é melhor do que mostrar "conectado"
        # e cobrar do jeito que o usuário estava tentando evitar.
        return replace(
            connected,
            reason=(
                "O Claude Code está conectado por chave de API do Console, não pela "
                "assinatura — o consumo será cobrado como API."
            ),
        )
    return connected


def start_login() -> None:
    """Dispara ``claude auth login --claudeai`` e volta na hora.

    O ``--claudeai`` escolhe a conta de assinatura sem passar pelo seletor
    interativo. O processo abre o navegador e fica esperando o callback em
    localhost; a tela acompanha o resultado consultando :func:`status`, então
    aqui não se espera o término.
    """
    binary = cli_path(refresh=True)
    if binary is None:
        raise ClaudeCodeError(
            "Claude Code não encontrado. Instale-o e tente novamente."
        )
    try:
        subprocess.Popen(  # noqa: S603 — argv fixo, sem shell
            [binary, "auth", "login", "--claudeai"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_popen_kwargs(),
        )
    except OSError as exc:
        logger.exception("claude auth login falhou ao iniciar")
        raise ClaudeCodeError("Não foi possível iniciar o login do Claude Code.") from exc


def logout() -> None:
    global _status_cache
    _status_cache = None  # a tela deve refletir a desconexão na hora
    binary = cli_path()
    if binary is None:
        return
    try:
        subprocess.run(  # noqa: S603 — argv fixo, sem shell
            [binary, "auth", "logout"],
            capture_output=True,
            timeout=STATUS_TIMEOUT,
            **_popen_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("claude auth logout falhou: %s", exc)


# ---------------------------------------------------------------------------
# Chamadas ao modelo


def _argv(binary: str, model: str, system: str, search: bool, *, stream: bool) -> list[str]:
    argv = [
        binary,
        "-p",  # prompt vem pelo stdin
        "--model",
        model,
        "--system-prompt",
        system,
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if search:
        # Os dois são necessários: --tools oferece, --allowed-tools autoriza.
        argv += ["--tools", SEARCH_TOOLS, "--allowed-tools", SEARCH_TOOLS]
    else:
        argv += ["--tools", ""]
    if stream:
        argv += ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
    else:
        argv += ["--output-format", "json"]
    return argv


def _flatten(messages: list[dict]) -> str:
    """Histórico multi-turno num único prompt.

    ``claude -p`` é stateless por chamada e o histórico já vive no banco
    (``AiChat.messages``), então não se usa ``--resume``: o estado fica de um
    lado só.
    """
    if len(messages) == 1 and messages[0].get("role") == "user":
        return str(messages[0].get("content") or "")
    parts = []
    for message in messages:
        who = "Usuário" if message.get("role") == "user" else "Assistente"
        parts.append(f"{who}: {message.get('content') or ''}")
    parts.append("Usuário:")
    return "\n\n".join(parts)


def _fail(returncode: int, stderr: str) -> ClaudeCodeError:
    detail = (stderr or "").strip().splitlines()
    tail = detail[-1] if detail else ""
    if "credit balance" in tail.lower() or "rate limit" in tail.lower():
        return ClaudeCodeError(
            "Limite da sua assinatura Anthropic atingido. Tente novamente mais tarde."
        )
    return ClaudeCodeError(
        f"O Claude Code falhou (código {returncode}). {tail}".strip()
    )


def call_json(system: str, messages: list[dict], model: str, *, search: bool) -> str:
    """Uma resposta completa do modelo, como texto cru.

    Usada pelo caminho de research (Carteira IA, Aporte Inteligente, eventos
    corporativos), que espera um JSON dentro do texto.
    """
    binary = cli_path()
    if binary is None:
        raise ClaudeCodeError("Claude Code não encontrado nesta máquina.")
    try:
        completed = subprocess.run(  # noqa: S603 — argv fixo, sem shell
            _argv(binary, model, system, search, stream=False),
            input=_flatten(messages),
            capture_output=True,
            timeout=CALL_TIMEOUT,
            **_popen_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCodeError(
            "A chamada ao Claude Code excedeu o tempo limite. Tente novamente."
        ) from exc
    except OSError as exc:
        raise ClaudeCodeError("Não foi possível executar o Claude Code.") from exc

    if completed.returncode != 0:
        raise _fail(completed.returncode, completed.stderr)
    try:
        body = json.loads(completed.stdout or "{}")
    except ValueError as exc:
        raise ClaudeCodeError("Resposta inesperada do Claude Code.") from exc
    if body.get("is_error"):
        raise ClaudeCodeError(
            f"O Claude Code retornou erro: {body.get('result') or 'sem detalhes'}"
        )
    return str(body.get("result") or "")


def stream_events(
    system: str, messages: list[dict], model: str, *, search: bool
) -> Iterator[tuple[str, str]]:
    """Eventos ``(tipo, valor)`` de uma resposta em streaming.

    Tipos: ``status`` (texto de progresso ou vazio para limpar), ``text``
    (delta a exibir), ``error``. Traduz o NDJSON do CLI para o mesmo vocabulário
    que a rota de chat já emite por SSE, para o dialeto do Claude Code não
    vazar para dentro da rota.
    """
    binary = cli_path()
    if binary is None:
        yield ("error", "Claude Code não encontrado nesta máquina.")
        return

    argv = _argv(binary, model, system, search, stream=True)
    try:
        process = subprocess.Popen(  # noqa: S603 — argv fixo, sem shell
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_popen_kwargs(),
        )
    except OSError:
        logger.exception("claude -p falhou ao iniciar")
        yield ("error", "Não foi possível executar o Claude Code.")
        return

    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(_flatten(messages))
        process.stdin.close()
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            yield from _translate(event)
        returncode = process.wait(timeout=CALL_TIMEOUT)
        if returncode != 0:
            stderr = process.stderr.read() if process.stderr else ""
            yield ("error", str(_fail(returncode, stderr)))
    except subprocess.TimeoutExpired:
        process.kill()
        yield ("error", "A chamada ao Claude Code excedeu o tempo limite.")
    except OSError:
        logger.exception("claude -p falhou durante o stream")
        yield ("error", "A comunicação com o Claude Code foi interrompida.")
    finally:
        if process.poll() is None:
            process.kill()


def _translate(event: dict) -> Iterator[tuple[str, str]]:
    """Um evento do NDJSON do CLI para zero ou mais eventos nossos."""
    kind = event.get("type")

    if kind == "stream_event":
        inner = event.get("event") or {}
        inner_type = inner.get("type")
        if inner_type == "content_block_start":
            block = (inner.get("content_block") or {}).get("type")
            if block == "thinking":
                yield ("status", "Analisando…")
            elif block in ("server_tool_use", "tool_use"):
                yield ("status", "Pesquisando na web…")
            elif block == "text":
                yield ("status", "")
        elif inner_type == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text") or ""
                if text:
                    yield ("text", text)
        return

    if kind == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        if info.get("status") not in (None, "allowed"):
            yield (
                "status",
                "Limite da assinatura Anthropic quase no fim — a resposta pode ser cortada.",
            )
        return

    if kind == "result" and event.get("is_error"):
        yield ("error", f"O Claude Code retornou erro: {event.get('result') or 'sem detalhes'}")
