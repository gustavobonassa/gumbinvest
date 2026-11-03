"""O provedor da assinatura: tradução do stream, disponibilidade e portões.

Nada aqui executa o CLI de verdade — a disponibilidade dele é propriedade da
máquina, e um teste que só passa em quem tem Claude Code instalado não é teste.
O que se cobre é o que o GumbInvest escreveu: o dialeto NDJSON virando os
eventos da rota, o achatamento do histórico, e o que as rotas respondem quando o
provedor está indisponível.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import AppSetting
from app.main import app
from app.services import claude_code
from app.services.ai_providers import AI_PROVIDERS, is_configured, unavailable_reason


# ---------------------------------------------------------------------------
# Tradução do stream


def _texts(events: list[dict]) -> list[tuple[str, str]]:
    return [item for event in events for item in claude_code._translate(event)]


def test_text_deltas_become_text_events() -> None:
    events = [
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Olá"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " mundo"}}},
    ]
    assert _texts(events) == [("text", "Olá"), ("text", " mundo")]


def test_block_starts_become_status_events() -> None:
    """Pensamento e busca viram texto de progresso; o início do texto limpa."""
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "thinking"}}},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "server_tool_use"}}},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
    ]
    kinds = _texts(events)
    assert [kind for kind, _ in kinds] == ["status", "status", "status"]
    assert kinds[0][1] == "Analisando…"
    assert "Pesquisando" in kinds[1][1]
    assert kinds[2][1] == ""  # limpa o status quando a resposta começa


def test_empty_text_deltas_are_dropped() -> None:
    event = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": ""}},
    }
    assert _texts([event]) == []


def test_thinking_deltas_never_leak_to_the_user() -> None:
    """O conteúdo do raciocínio não é resposta — só o progresso é mostrado."""
    event = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
    }
    assert _texts([event]) == []


def test_rate_limit_warns_only_when_not_allowed() -> None:
    allowed = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
    assert _texts([allowed]) == []

    tight = {"type": "rate_limit_event", "rate_limit_info": {"status": "warning"}}
    kind, message = _texts([tight])[0]
    assert kind == "status"
    assert "assinatura" in message.lower()


def test_result_error_surfaces_as_error() -> None:
    event = {"type": "result", "is_error": True, "result": "quota estourada"}
    kind, message = _texts([event])[0]
    assert kind == "error"
    assert "quota estourada" in message


def test_successful_result_is_not_an_event() -> None:
    """O texto já veio pelos deltas; o result final não deve duplicá-lo."""
    assert _texts([{"type": "result", "is_error": False, "result": "Olá"}]) == []


# ---------------------------------------------------------------------------
# Histórico


def test_single_user_turn_is_sent_verbatim() -> None:
    assert claude_code._flatten([{"role": "user", "content": "Oi"}]) == "Oi"


def test_multi_turn_history_is_labelled_and_left_open() -> None:
    """``claude -p`` é stateless: o histórico vai inteiro, rotulado, num prompt."""
    prompt = claude_code._flatten(
        [
            {"role": "user", "content": "Qual o preço?"},
            {"role": "assistant", "content": "R$ 10."},
            {"role": "user", "content": "E ontem?"},
        ]
    )
    assert "Usuário: Qual o preço?" in prompt
    assert "Assistente: R$ 10." in prompt
    assert prompt.endswith("Usuário:")  # deixa o turno aberto para o modelo


# ---------------------------------------------------------------------------
# System prompt fora da linha de comando


#: Maior que o teto de 32.767 caracteres da linha de comando do Windows — o
#: tamanho de uma carteira real no system prompt do Analista IA.
_HUGE_SYSTEM = "Carteira: ação ITSA4 — preço médio R$ 10,00\n" * 2_000


class _FakeProcess:
    """Popen falso que lê o arquivo de system prompt enquanto "roda"."""

    seen: dict = {}

    def __init__(self, argv: list[str], **kwargs) -> None:
        import io
        from pathlib import Path

        path = argv[argv.index("--system-prompt-file") + 1]
        _FakeProcess.seen = {
            "argv": argv,
            "path": path,
            "content": Path(path).read_text(encoding="utf-8"),
        }
        self.stdin = io.StringIO()
        result = '{"type": "result", "is_error": false, "result": "ok"}\n'
        self.stdout = io.StringIO(result)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover - nunca trava aqui
        pass


def _assert_system_went_by_file() -> None:
    from pathlib import Path

    seen = _FakeProcess.seen
    assert _HUGE_SYSTEM not in seen["argv"]
    assert "--system-prompt" not in seen["argv"]
    assert sum(len(arg) for arg in seen["argv"]) < 2_000
    assert seen["content"] == _HUGE_SYSTEM  # utf-8 intacto
    assert not Path(seen["path"]).exists()  # apagado depois da chamada


def test_stream_passes_huge_system_prompt_by_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regressão do WinError 206: o Analista IA com carteira grande não abria."""
    monkeypatch.setattr(claude_code, "cli_path", lambda **_: "claude")
    monkeypatch.setattr(claude_code.subprocess, "Popen", _FakeProcess)

    events = list(
        claude_code.stream_events(
            _HUGE_SYSTEM, [{"role": "user", "content": "Oi"}], "sonnet", search=False
        )
    )

    assert not [e for e in events if e[0] == "error"]
    _assert_system_went_by_file()


def test_call_json_passes_huge_system_prompt_by_file(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        _FakeProcess(argv)
        return subprocess.CompletedProcess(argv, 0, '{"result": "ok"}', "")

    monkeypatch.setattr(claude_code, "cli_path", lambda **_: "claude")
    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)

    assert claude_code.call_json(
        _HUGE_SYSTEM, [{"role": "user", "content": "Oi"}], "sonnet", search=True
    ) == "ok"
    _assert_system_went_by_file()


# ---------------------------------------------------------------------------
# Disponibilidade


def test_missing_cli_is_not_configured_and_says_why() -> None:
    entry = AI_PROVIDERS["claude_code"]
    assert is_configured(entry) is False
    assert "Claude Code" in unavailable_reason(entry)


def test_console_login_does_not_count_as_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logado por chave de API do Console não é usar os créditos do plano.

    Deixar isso passar como "conectado" cobraria do usuário exatamente do jeito
    que ele estava tentando evitar.
    """
    monkeypatch.setattr(
        claude_code,
        "status",
        lambda **_: claude_code.CliStatus(
            installed=True, logged_in=True, method="console", reason="cobrado como API."
        ),
    )
    entry = AI_PROVIDERS["claude_code"]
    assert is_configured(entry) is False
    assert "API" in unavailable_reason(entry)


def test_connected_subscription_is_configured_without_any_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        claude_code,
        "status",
        lambda **_: claude_code.CliStatus(
            installed=True, logged_in=True, method="claude.ai", email="a@b.c", plan="max"
        ),
    )
    entry = AI_PROVIDERS["claude_code"]
    assert entry["key_setting"] is None  # nada de segredo para guardar
    assert is_configured(entry) is True
    assert unavailable_reason(entry) == ""


# ---------------------------------------------------------------------------
# Rotas


def test_chat_refuses_with_the_real_reason_not_a_missing_key(db: Session) -> None:
    """O 503 precisa dizer o que fazer: aqui não existe chave para informar."""
    db.merge(AppSetting(key="ai_provider", value={"value": "claude_code"}))
    db.commit()
    with TestClient(app) as client:
        response = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "oi"}]})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "chave" not in detail.lower()
    assert "Claude Code" in detail


def test_status_route_reports_the_missing_cli() -> None:
    with TestClient(app) as client:
        response = client.get("/api/ai/claude-code/status")
    assert response.status_code == 200
    body = response.json()
    assert body["installed"] is False
    assert body["uses_subscription"] is False
    assert body["install_url"]


def test_models_route_returns_the_curated_aliases() -> None:
    """Sem catálogo remoto: os apelidos de família são a resposta final."""
    with TestClient(app) as client:
        response = client.get("/api/ai/models", params={"provider": "claude_code"})
    assert response.status_code == 200
    body = response.json()
    assert body["live"] is False
    assert body["models"] == AI_PROVIDERS["claude_code"]["models"]
