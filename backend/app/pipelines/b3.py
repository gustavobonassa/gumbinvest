"""B3 Área do Investidor: sign in, export the movimentação, import it.

Exactly the walk the user does by hand — investidor.b3.com.br, CPF and
password, the verification code B3 mails or texts, Extrato → Movimentação,
"Exportar" — driven by a real Chrome under Patchright. The portal sits behind
a Cloudflare Turnstile challenge that detects and loops a plain Playwright
browser forever, so the automation runs headed, through the user's own Chrome,
via Patchright's un-detected fork (see :func:`_launch_context`). The download
is the same report the Importar page accepts, so the importer's parser,
classifier and dedup do all the real work; this module only fetches the file.

Two things keep it honest against a site we do not control:

* Selectors are looked up by visible role/placeholder/text with several
  candidates each, and every step logs what it is about to do — when B3
  redesigns a screen, the log names the step that stopped matching and a
  screenshot + HTML dump land in the debug folder. A mis-scripted click fails
  loudly; it can never import wrong data, because only the importer writes.
* The session (cookies) is persisted next to the data, so a code is asked on
  the first run and then only when B3 expires the device — a weekly schedule
  usually stays inside that window.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from app.core.config import settings
from app.core.dates import local_today
from app.pipelines.base import Pipeline, PipelineError, PipelineSpec, register

PORTAL_URL = "https://www.investidor.b3.com.br/"
EXTRATO_URL = "https://www.investidor.b3.com.br/extrato/movimentacao"

#: How far behind the last successful run the export should start. Generous:
#: a late-settling event (IPO allocation, corporate action) appearing days
#: after its date must still be caught, and overlap costs nothing — dedup
#: imports it as zero new rows.
OVERLAP = timedelta(days=45)

#: Earliest date the "histórico completo" backfill asks for. B3's investor
#: movimentação begins 01/11/2019 — the filter's own date picker states that as
#: its minimum, so this is exactly it.
FULL_HISTORY_START = date(2019, 11, 1)

#: Bumped whenever the login/export walk changes, and logged at run start so a
#: run's log proves which server code produced it.
_BUILD = "1.4.3"

_STEP_TIMEOUT = 45_000  # ms — Angular screens on a slow day


class B3Pipeline(Pipeline):
    spec = PipelineSpec(
        key="b3",
        name="B3 — Área do Investidor",
        description=(
            "Entra na Área do Investidor com seu CPF, baixa o extrato de "
            "movimentação e importa as novidades. Se a B3 pedir um código de "
            "verificação, ele é solicitado aqui na tela."
        ),
        credentials=(
            ("b3_cpf", "CPF"),
            ("b3_password", "Senha da Área do Investidor"),
        ),
        schedule="Semanal, segunda-feira de manhã",
    )

    def run(self, ctx) -> dict:
        playwright_api = _require_playwright()
        since = self._since(ctx)
        # A build marker: if this string is absent from a run's log, that run
        # executed an older installed server — quit the app fully and reinstall.
        ctx.log(f"Coleta B3 (build {_BUILD}). Movimentações desde {since.strftime('%d/%m/%Y')}.")

        with playwright_api() as p:
            # A persistent context (a real user-data dir) rather than a fresh
            # incognito one: it is what lets Cloudflare's clearance cookie
            # survive between weekly runs, so the challenge is solved rarely
            # instead of every time.
            context = _launch_context(p, ctx)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(_STEP_TIMEOUT)
                # Move the window off-screen so the weekly run does not steal the
                # desktop. It stays a real, on-state window (not minimised) so
                # Chromium never throttles the challenge JS; _pass_cloudflare
                # brings it back only if a human has to solve one.
                if not settings.pipeline_show_browser:
                    _move_window(page, offscreen=True)
                self._sign_in(page, ctx)
                payload, filename = self._export(page, ctx, since)
            finally:
                context.close()

        result = ctx.import_file(payload, filename)
        ctx.log(
            f"Importação concluída: {result.rows_imported} novas, "
            f"{result.rows_duplicate} já conhecidas, {result.rows_failed} com erro."
        )

        # The ledger now holds these movements; the spreadsheet was a courier,
        # not a record. Delete it so downloads don't pile up (and are not
        # re-imported by the startup scan of the auto-import folder). Kept only
        # when a row failed, so that file is there to inspect.
        if result.rows_failed == 0:
            try:
                (ctx.downloads_dir() / filename).unlink(missing_ok=True)
            except OSError:
                pass

        return {
            "file": filename,
            "rows_imported": result.rows_imported,
            "rows_duplicate": result.rows_duplicate,
            "rows_failed": result.rows_failed,
            "rows_warned": result.rows_warned,
            "since": since.isoformat(),
        }

    # -- steps ---------------------------------------------------------------

    def _since(self, ctx):
        # "Baixar histórico completo": go back to when B3 first has movements,
        # so a new user's whole history lands in one run. Dedup makes the
        # overlap with later incremental runs free.
        if ctx.options.get("full_history"):
            return FULL_HISTORY_START
        last = ctx.last_success_at()
        if last is None:
            return local_today() - timedelta(days=365)
        return last.date() - OVERLAP

    def _sign_in(self, page, ctx) -> None:
        ctx.log("Abrindo a Área do Investidor.")
        _guarded(ctx, page, "abrir o portal", lambda: page.goto(PORTAL_URL, wait_until="domcontentloaded"))
        self._pass_cloudflare(page, ctx)

        # A stored session may land straight on the logged-in shell.
        if self._logged_in(page):
            ctx.log("Sessão anterior ainda válida — sem novo login.")
            return

        self._authenticate(page, ctx)

    def _authenticate(self, page, ctx) -> None:
        """The credential walk itself: CPF, password, the B2C verification code.

        Split out from :meth:`_sign_in` because the export step needs it too — a
        stale token bounces the extrato back to ``/login``, and re-running this
        on that page recovers without starting the whole run over.
        """
        # The portal is an Angular SPA: `domcontentloaded` fires long before the
        # login control renders, and the landing page hides it behind an
        # "Entrar" button. Settle the app, then reach the CPF field — clicking
        # into the login flow if that is what stands between here and it.
        self._reach_login_form(page, ctx)

        ctx.log("Informando o CPF.")
        cpf = re.sub(r"\D", "", settings.b3_cpf)

        def fill_cpf():
            field = _fill_first(page, _CPF_FIELDS, cpf)
            _submit_step(page, field)

        _guarded(ctx, page, "encontrar o campo de CPF", fill_cpf)

        ctx.log("Informando a senha.")

        def fill_password() -> None:
            # The password field is on a second step in some flows, so it may
            # render only after the CPF is submitted — wait, then fill (and
            # raise loudly if it never arrives).
            _wait_visible(page, _PASSWORD_FIELDS, _STEP_TIMEOUT)
            field = _fill_first(page, _PASSWORD_FIELDS, settings.b3_password)
            _submit_step(page, field)

        _guarded(ctx, page, "encontrar o campo de senha", fill_password)

        # Three ways forward from here: straight in, a verification code, or
        # an error toast about the credentials. Poll for whichever comes first.
        outcome = _guarded(ctx, page, "concluir o login", lambda: self._await_login_outcome(page))
        if outcome == "code":
            self._solve_challenge(page, ctx)
        elif outcome == "error":
            raise PipelineError(
                "A B3 recusou o acesso — confira o CPF e a senha nas credenciais desta automação."
            )
        # Let the SPA finish the token exchange before anyone navigates away —
        # a hard jump to the extrato mid-handshake is what bounces to /login.
        _settle_spa(page)
        page.wait_for_timeout(2_000)
        ctx.log("Login concluído.")

    def _reach_login_form(self, page, ctx) -> None:
        """Get from the freshly loaded portal to a visible CPF field.

        Two obstacles, both timing: the SPA has to finish bootstrapping, and
        the CPF form usually lives one "Entrar" click away from the landing
        page. Wait for the app to render, take the CPF field if it is already
        there, otherwise click the entry button and wait for it to appear.
        """
        _settle_spa(page)
        _dismiss_cookies(page, ctx)
        if _wait_visible(page, _CPF_FIELDS, 8_000) is not None:
            return

        entry = _first_visible(page, _ENTER_BUTTONS)
        if entry is not None:
            ctx.log("Abrindo a tela de login.")
            _guarded(ctx, page, "abrir a tela de login", entry.click)
            _settle_spa(page)
        # Either the click opened the form, or the field simply took longer than
        # the first short wait — give it the full step timeout before failing,
        # so the diagnostic dump reflects a fully rendered page.
        _wait_visible(page, _CPF_FIELDS, _STEP_TIMEOUT)

    def _await_login_outcome(self, page) -> str:
        for _ in range(_STEP_TIMEOUT // 500):
            if self._logged_in(page):
                return "ok"
            if _first_visible(page, _CODE_FIELDS) is not None:
                return "code"
            if _first_visible(page, _LOGIN_ERRORS) is not None:
                return "error"
            page.wait_for_timeout(500)
        raise PipelineError("A tela de login não avançou — a B3 pode ter mudado o fluxo de acesso.")

    def _logged_in(self, page) -> bool:
        """A positive read of an authenticated session, not merely "not login".

        Login is federated to Azure AD B2C (``b2clogin.com``) for the password
        and the OTP, so a URL there is mid-flow, never done. Back on the portal
        and off the ``/login`` route counts only with a marker that the
        unauthenticated landing page (served from the same root) never shows.
        """
        url = page.url
        if "b2clogin.com" in url or "/login" in url:
            return False
        if "investidor.b3.com.br" not in url:
            return False
        if any(seg in url for seg in ("extrato", "meus-investimentos", "posicao", "carteira", "home")):
            return True
        return _first_visible(page, ("text=/meus investimentos|minha carteira|sair|meu perfil/i",)) is not None

    def _await_return_from_b2c(self, page) -> bool:
        """After the OTP, wait for B2C to hand the session back to the portal."""
        for _ in range(_STEP_TIMEOUT // 500):
            url = page.url
            if "b2clogin.com" not in url and "investidor.b3.com.br" in url and "/login" not in url:
                return True
            if _first_visible(page, _LOGIN_ERRORS) is not None:
                return False
            page.wait_for_timeout(500)
        return False

    def _pass_cloudflare(self, page, ctx) -> None:
        """Wait out (or, headful, let the user solve) the Cloudflare challenge.

        B3 fronts the portal with a Cloudflare "managed challenge". With a real
        Chrome, a persistent profile and the automation signals masked, it
        usually clears itself within a few seconds. A stubborn one shown in a
        visible window can be solved by hand — the run is already parked here,
        watching for the real page to appear. Headless, an unclearable
        challenge is a hard stop with a message that says why.
        """
        if not _is_cloudflare(page):
            return
        ctx.log("A B3 apresentou a verificação de segurança da Cloudflare. Aguardando…", level="warning")
        headless = settings.pipeline_headless
        # Longer when a human might be solving it in a visible window.
        deadline_ms = _STEP_TIMEOUT if headless else 180_000
        waited = 0
        prompted = False
        while waited < deadline_ms:
            page.wait_for_timeout(2_000)
            waited += 2_000
            if not _is_cloudflare(page):
                ctx.log("Verificação da Cloudflare concluída.")
                return
            if not headless and not prompted and waited >= 20_000:
                # It did not auto-clear; the window may be parked off-screen, so
                # bring it back and ask the person at the screen to help.
                prompted = True
                _move_window(page, offscreen=False)
                ctx.log(
                    "A verificação não passou sozinha. Trouxe a janela do navegador para a "
                    "frente — resolva o desafio nela e a coleta segue assim que a página da B3 aparecer.",
                    level="warning",
                )
        raise PipelineError(
            "A verificação de segurança da Cloudflare não foi concluída. "
            + (
                "Execute a coleta com a janela visível (no aplicativo desktop) para resolvê-la manualmente."
                if headless
                else "Tente novamente; se persistir, a B3 pode estar bloqueando este acesso."
            )
        )

    def _solve_challenge(self, page, ctx) -> None:
        """The 2FA screen: ask the human, type the code, confirm."""
        ctx.log("A B3 pediu um código de verificação.", level="warning")
        code = ctx.request_input(
            "A B3 enviou um código de verificação para seu e-mail ou celular."
        )
        code = re.sub(r"\D", "", code)
        if not code:
            raise PipelineError("O código informado estava vazio.")

        def type_code():
            # One box per digit, or a single field. Type real keystrokes rather
            # than fill(): B2C's verify button stays disabled until its own
            # keyboard-event validation runs, which a bulk fill() skips.
            boxes = page.locator("input[maxlength='1']:visible")
            if boxes.count() >= 4:
                for index, digit in enumerate(code[: boxes.count()]):
                    boxes.nth(index).press_sequentially(digit, delay=30)
                return boxes.nth(0)
            field = _first_visible(page, _CODE_FIELDS)
            if field is None:
                raise PipelineError("campo de código não encontrado")
            field.click()
            try:
                field.fill("")  # clear any stale value before typing
            except Exception:  # noqa: BLE001 — a number input can refuse fill("")
                pass
            field.press_sequentially(code, delay=40)
            return field

        field = _guarded(ctx, page, "digitar o código de verificação", type_code)
        # Wait for the (now valid) verify button to enable; press Enter if it
        # never surfaces — B2C submits the OTP on Enter too.
        _submit_with(page, field, _CONFIRM_BUTTONS)

        # Success here is B2C redirecting back to the portal, not a field on the
        # B2C page — so wait for the hand-off rather than for a logged-in marker
        # that only the portal's own SPA will eventually render.
        if not _guarded(ctx, page, "validar o código", lambda: self._await_return_from_b2c(page)):
            raise PipelineError("A B3 não aceitou o código de verificação. Execute de novo e confira o código.")

    def _export(self, page, ctx, since) -> tuple[bytes, str]:
        ctx.log("Abrindo o extrato de movimentação.")
        _guarded(ctx, page, "abrir o extrato", lambda: page.goto(EXTRATO_URL, wait_until="domcontentloaded"))
        _settle_spa(page)

        # A stale or half-finished session makes the SPA's auth guard bounce the
        # extrato back to /login. Re-authenticate on the spot (which may ask for
        # a fresh code) and come back, rather than failing a run that is one
        # login away from done.
        if "/login" in page.url or _first_visible(page, _CPF_FIELDS) is not None:
            ctx.log("A sessão não estava ativa; refazendo o login.", level="warning")
            self._authenticate(page, ctx)
            _guarded(ctx, page, "abrir o extrato", lambda: page.goto(EXTRATO_URL, wait_until="domcontentloaded"))
            _settle_spa(page)

        page.wait_for_timeout(3_000)  # the Angular shell fetches the grid after load

        # Best effort: widen the period filter to cover `since`. The filter is
        # the most redesign-prone part of the page, so failing to set it only
        # narrows the export to the site's default period — never fails the
        # run. The weekly cadence plus dedup make the default window enough in
        # the common case; the log says which of the two happened.
        full = bool(ctx.options.get("full_history"))
        try:
            self._set_period(page, ctx, since, full)
            ctx.log(f"Período ajustado para começar em {since.strftime('%d/%m/%Y')}.")
        except Exception:  # noqa: BLE001 — degraded, not broken; see above
            if full:
                # For a full backfill the period is the whole point, so if it
                # cannot be set, capture the filter controls (to wire them) and
                # say plainly that the export may be only the default window.
                _dump(ctx, page, "filtro de periodo")
                ctx.log(
                    "Não consegui ajustar o período para o histórico completo — o arquivo pode "
                    "conter apenas o período padrão do site. As capturas em pipelines-debug/b3 "
                    "mostram o filtro.",
                    level="warning",
                )
            else:
                ctx.log(
                    "Não consegui ajustar o período do filtro — exportando o período padrão do site.",
                    level="warning",
                )
            # A half-set filter can leave its modal open, and an open modal sits
            # over the export button. Dismiss it so the download can proceed.
            _close_filter_if_open(page)

        ctx.log("Exportando a planilha.")

        # The export is a three-move sequence (confirmed live): open the format
        # panel, pick Excel among its radios, then the panel's own "Baixar"
        # button starts the download. Opening is skipped when the panel is
        # already showing (its format radios are the tell).
        if _first_visible(page, _FORMAT_RADIOS) is None:
            # A wide filter makes B3 reload a large grid, and the "Baixar
            # extrato" button is gone until that settles — so wait for it to
            # come back (enabled) before clicking, generously, because years of
            # movements take a while to load.
            _settle_spa(page)
            if _wait_enabled(page, _EXPORT_OPEN_BUTTONS, 60_000) is None:
                ctx.log("Aguardando o extrato terminar de carregar…", level="warning")
            _guarded(
                ctx, page, "abrir opções de exportação",
                lambda: _click_first(page, _EXPORT_OPEN_BUTTONS),
            )
            _wait_visible(page, _FORMAT_RADIOS, 12_000)

        self._choose_excel(page, ctx)

        def download():
            # A full-history file is large and generated server-side, so give the
            # download far longer than a normal step to begin.
            with page.expect_download(timeout=120_000) as pending:
                _click_first(page, _DOWNLOAD_CONFIRM_BUTTONS)
            return pending.value

        handle = _guarded(ctx, page, "exportar o extrato", download)
        filename = f"b3-movimentacao-{local_today().isoformat()}.xlsx"
        target = ctx.downloads_dir() / filename
        handle.save_as(str(target))
        ctx.log(f"Arquivo salvo em {target.name}.")
        return target.read_bytes(), filename

    def _choose_excel(self, page, ctx) -> None:
        """Tick the Excel format so the download is the .xlsx the importer reads.

        A radio, so ``check`` is the right verb; a label click is the fallback
        for the case where the input itself is visually replaced. Best effort —
        if the panel offers no choice, the site's default format stands and the
        run continues (and the log says so).
        """
        try:
            page.locator("#excel").check(timeout=4_000)
            ctx.log("Formato Excel selecionado.")
            return
        except Exception:  # noqa: BLE001 — fall back to the label
            pass
        label = _first_visible(page, ("label[for='excel']", "text=/excel|xlsx|planilha/i"))
        if label is not None:
            try:
                label.click(timeout=4_000)
                ctx.log("Formato Excel selecionado.")
            except Exception:  # noqa: BLE001 — default format will have to do
                ctx.log("Não consegui escolher o formato; usando o padrão do site.", level="warning")

    def _set_period(self, page, ctx, since, full: bool) -> None:
        """Widen the movimentação period to start at ``since``, then apply it.

        The default view is one month, so a full backfill must open the period
        filter (a modal behind the funnel / "Filtrar"), type a start date and,
        where present, an end date, and confirm. The modal's date inputs render
        only once it opens, so on a full run its controls are dumped the moment
        it is open — a wrong field selector is then a one-line fix, not another
        blind round.
        """
        # Open the filter modal unless a date field is already on screen. Each
        # step logs, so a run's log pinpoints exactly where the filter breaks.
        if _first_visible(page, _DATE_START_FIELDS) is None:
            ctx.log("Abrindo o filtro de período.")
            _click_first(page, _FILTER_OPEN_BUTTONS, timeout=8_000)
            _wait_visible(page, _DATE_START_FIELDS, 8_000)
        if full:
            _dump(ctx, page, "filtro aberto")

        start = _first_visible(page, _DATE_START_FIELDS)
        if start is None:
            raise PipelineError("campo de data inicial não encontrado no filtro")

        # Only the start moves: the picker's end is already the latest available
        # day, and typing today would exceed its stated maximum and be rejected.
        if not _enter_date(page, start, since, ctx):
            ctx.log("O campo de data inicial não aceitou a data.", level="warning")

        # FILTRAR enables only once the typed date is valid; wait for it, and
        # fall back to Enter (which the filter form also submits on).
        apply = _wait_enabled(page, _FILTER_APPLY_BUTTONS, 6_000)
        if apply is not None:
            apply.click(timeout=8_000)
            ctx.log("Filtro aplicado.")
        else:
            ctx.log("O botão FILTRAR não habilitou; tentando Enter no campo.", level="warning")
            start.press("Enter")
        page.wait_for_timeout(2_500)  # the grid refetches for the new range

        # Log the range B3 now shows, so a filter that silently did not apply is
        # visible in the run log rather than discovered as a short export.
        shown = _first_visible(page, (".b3-filtrar__data", "text=/Exibindo/i"))
        if shown is not None:
            try:
                ctx.log(f"Período exibido pela B3: {(shown.inner_text() or '').strip()[:70]}")
            except Exception:  # noqa: BLE001 — the log line is a nicety
                pass


# -- selector candidates ------------------------------------------------------
# Ordered from the most specific thing B3 ships today to generic fallbacks.
# When the site changes, add the new shape at the front — the tail keeps old
# screenshots reproducible.

_CPF_FIELDS = (
    # The names B3 ships today (confirmed against the live /login page).
    "input[name='cpf-cnpj']",
    "#documento-mobile",
    "#documento-desktop",
    # Older / generic fallbacks kept behind them.
    "input[formcontrolname='cpf']",
    "input[name='cpf']",
    "#cpf",
    "input[placeholder*='CPF' i]",
    "input[aria-label*='CPF' i]",
    "input[id*='documento' i]",
    "input[id*='cpf' i]",
    "input[name*='cpf' i]",
    "input[name*='usuario' i]",
    "input[name='username']",
    "input[formcontrolname*='documento' i]",
    "input[inputmode='numeric']",
)
#: The OneTrust consent banner overlays the page and intercepts the first
#: click, so it is dismissed before anything else is touched.
_COOKIE_BUTTONS = (
    "#onetrust-accept-btn-handler",
    "button:has-text('ACEITAR TODOS OS COOKIES')",
    "button:has-text('Aceitar todos os cookies')",
    "button:has-text('Aceitar')",
)
#: The landing page keeps the CPF form one click away, behind an entry button.
_ENTER_BUTTONS = (
    "a:has-text('Entrar')",
    "button:has-text('Entrar')",
    "a:has-text('Acessar')",
    "button:has-text('Acessar')",
    "a:has-text('Fazer login')",
    "button:has-text('Fazer login')",
    "[aria-label*='entrar' i]",
    "a[href*='login' i]",
)
_PASSWORD_FIELDS = (
    "input[type='password']",
    "input[formcontrolname*='senha' i]",
    "input[name*='senha' i]",
    "input[name='password']",
)
_CODE_FIELDS = (
    # Azure AD B2C's OTP screen (confirmed live): a single numeric input, for
    # the code B3 e-mails or texts.
    "#INPUT_OTP_EMAIL",
    "#INPUT_OTP_SMS",
    "input[id*='OTP' i]",
    "input[type='number']",
    "input[formcontrolname*='codigo' i]",
    "input[name*='codigo' i]",
    "input[placeholder*='código' i]",
    "input[autocomplete='one-time-code']",
)
_LOGIN_ERRORS = (
    "text=/senha inválida|senha incorreta|dados inválidos|usuário ou senha/i",
    "text=/código inválido|código incorreto|código expirado|code is incorrect/i",
)
_NEXT_BUTTONS = (
    # The form's own submit first, so a look-alike marketing "Entrar" CTA on the
    # same page is never chosen over it.
    "form button[type='submit']",
    "button[type='submit']",
    "button:has-text('Continuar')",
    "button:has-text('Entrar')",
    "button:has-text('Acessar')",
    "button:has-text('Avançar')",
    "button[type='submit']",
)
_CONFIRM_BUTTONS = (
    # B2C's OTP verify button (confirmed live).
    "#Btn_VERIFY_CODE",
    "button:has-text('VERIFICAR CÓDIGO')",
    "button:has-text('Verificar')",
    "button:has-text('Confirmar')",
    "button:has-text('Validar')",
    "button:has-text('Continuar')",
    "button[type='submit']",
)
# The period filter (from the movimentação page markup): a funnel that opens a
# modal. The mobile layout shows an icon-only button (`.filtrar-mobile__botao`
# / `icon="filter_list"`); the wider one an aria-labelled "Filtrar".
_FILTER_OPEN_BUTTONS = (
    "button[aria-label='Filtrar']",
    ".filtrar-mobile__botao button",
    ".filtrar-mobile__botao",
    "b3-button[icon='filter_list'] button",
    "b3-button[icon='filter_list']",
    ".b3-filtrar__botoes--botao",
    "button:has-text('Filtrar')",
    "button[aria-label*='filtr' i]",
)
#: The composite date picker's start field (confirmed live). The generic
#: fallbacks stay behind it for a future redesign.
_DATE_START_FIELDS = (
    "#datepicker-composto_start",
    "input[id*='datepicker' i][id*='start' i]",
    "input[id*='start' i][placeholder*='DD/MM' i]",
    "input[placeholder='DD/MM/AAAA']",
    "input[formcontrolname*='dataInicio' i]",
    "input[placeholder*='início' i]",
    "input[placeholder*='dd/mm' i]",
)
#: The apply button inside the filter modal ("FILTRAR", aria-label "Filtrar").
_FILTER_APPLY_BUTTONS = (
    ".filter-apply-handler",
    "button[aria-label='Filtrar'][type='submit']",
    "button:has-text('FILTRAR')",
    "button:has-text('Aplicar')",
    "button:has-text('Buscar')",
    "button[aria-label='Filtrar']",
)
# The export panel (confirmed live): the page's "Baixar extrato" button opens
# it, two radios choose the format, and the panel's own "Baixar" starts the
# download. The exact aria-label is what tells the panel's confirm button apart
# from the page button that shares the word "Baixar".
_EXPORT_OPEN_BUTTONS = (
    "button[aria-label='Baixar extrato']",
    "button:has-text('Exportar')",
    "button[aria-label*='exportar' i]",
    "button[aria-label*='download' i]",
)
_FORMAT_RADIOS = (
    "#excel",
    "#pdf",
    "input[type='radio'][name='plan']",
)
_DOWNLOAD_CONFIRM_BUTTONS = (
    "button[aria-label='Baixar']",
    "button[aria-label='Baixar extrato']",
    "button:has-text('BAIXAR')",
    "button:has-text('Baixar')",
)


# -- plumbing -----------------------------------------------------------------


def _require_playwright():
    # Patchright, not Playwright: B3 sits behind Cloudflare Turnstile, which
    # detects a plain Playwright browser through the DevTools protocol it is
    # driven over and loops the "verificação de segurança" forever. Patchright
    # is a drop-in fork that patches exactly those tells (the Runtime.enable
    # CDP leak and friends) — the only reason the login is reachable at all.
    if settings.desktop_mode and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        from app.desktop.paths import data_root

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(data_root() / "browsers")
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        raise PipelineError(
            "O Patchright não está instalado neste servidor. Instale com "
            "`pip install patchright` e `patchright install chromium`."
        ) from exc
    return sync_playwright


def _profile_dir() -> Path:
    """The persistent Chrome profile — home of the Cloudflare clearance cookie.

    Persistent (a real user-data dir) so a cleared challenge survives between
    weekly runs. When Cloudflare hardens against a profile it can become
    "poisoned" — looping the challenge forever; deleting this folder resets it.
    """
    path = Path(settings.auto_import_dir) / "b3" / ".chrome-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _launch_context(p, ctx):
    """A persistent context launched the way Patchright wants for max stealth.

    Patchright's guidance is deliberately the opposite of hand-rolled stealth:
    use *real* Chrome (``channel='chrome'``), a persistent profile, a headed
    window, and **no** custom launch args or init scripts — each of those is
    itself a detectable tell, and the fork already hides the automation at the
    protocol level. So this passes almost nothing and lets Patchright do the
    work. Real Chrome falls back to Patchright's patched Chromium (downloaded
    on first use) only when no system Chrome exists.
    """
    headless = settings.pipeline_headless

    def build(channel: str | None, allow_install: bool):
        kwargs = dict(
            user_data_dir=str(_profile_dir()),
            channel=channel,
            headless=headless,
            accept_downloads=True,
            locale="pt-BR",
            timezone_id=settings.timezone,
            no_viewport=True,
        )
        try:
            return p.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # noqa: BLE001 — missing browser build, only
            if not allow_install or "install" not in str(exc).lower():
                raise
            ctx.log("Baixando o navegador da automação (algumas centenas de MB, só na primeira vez)…")
            try:
                _install_chromium()
            except Exception as install_exc:  # noqa: BLE001 — one readable line
                raise PipelineError(
                    "Não foi possível baixar o navegador da automação. Verifique a "
                    "conexão e tente de novo; em um servidor próprio, rode "
                    "`patchright install chromium`."
                ) from install_exc
            return p.chromium.launch_persistent_context(**kwargs)

    try:
        context = build("chrome", allow_install=False)
    except Exception:  # noqa: BLE001 — no system Chrome; use the patched build
        ctx.log("Chrome não encontrado, usando o navegador embutido do Patchright.", level="warning")
        context = build(None, allow_install=True)

    context.set_default_timeout(_STEP_TIMEOUT)
    return context


def _install_chromium() -> None:
    if getattr(sys, "frozen", False):
        # No `python -m` inside a PyInstaller bundle — call the bundled node
        # driver the way the CLI would. Private API, pinned by requirements.
        from patchright._impl._driver import compute_driver_executable, get_driver_env

        driver = compute_driver_executable()
        command = [str(part) for part in (driver if isinstance(driver, (tuple, list)) else (driver,))]
        subprocess.run(
            [*command, "install", "chromium"], check=True, capture_output=True, env=get_driver_env()
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "patchright", "install", "chromium"],
            check=True,
            capture_output=True,
        )


def _is_cloudflare(page) -> bool:
    """Whether the current page is a Cloudflare interstitial, not B3's own.

    Reads the title and body text — the challenge renders inside an iframe
    whose contents we cannot query, but the host page always says it is
    "verificação de segurança" / "Just a moment" while it holds.
    """
    try:
        title = (page.title() or "").lower()
        if "just a moment" in title or "attention required" in title:
            return True
        body = (page.inner_text("body", timeout=2_000) or "").lower()
    except Exception:  # noqa: BLE001 — mid-navigation; treat as "cannot tell yet"
        return False
    return "verificação de segurança" in body or "verifying you are human" in body


def _dismiss_cookies(page, ctx) -> None:
    """Click away the consent banner if it is up; harmless when it is not."""
    button = _first_visible(page, _COOKIE_BUTTONS)
    if button is None:
        return
    try:
        button.click(timeout=5_000)
        ctx.log("Aviso de cookies dispensado.")
        page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001 — the banner is not load-bearing
        pass


def _settle_spa(page) -> None:
    """Let the Angular app finish bootstrapping after a navigation.

    ``networkidle`` is the honest signal that the SPA stopped fetching, but B3's
    analytics beacons can keep the network faintly busy forever, so it is capped
    and its timeout is not an error — a short settle afterwards covers the
    render tick.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # noqa: BLE001 — beacons never idle; the wait below is enough
        pass
    page.wait_for_timeout(1_500)


def _wait_visible(page, selectors: tuple[str, ...], timeout_ms: int):
    """Poll for the first of ``selectors`` to become visible; None on timeout.

    Unlike :func:`_first_visible` (a single glance), this waits — the tool for a
    control an SPA renders a beat after navigation.
    """
    waited = 0
    step = 250
    while True:
        found = _first_visible(page, selectors)
        if found is not None:
            return found
        if waited >= timeout_ms:
            return None
        page.wait_for_timeout(step)
        waited += step


def _first_visible(page, selectors: tuple[str, ...], require_enabled: bool = False):
    """First *visible* element across the candidate selectors.

    Scans every match of each selector, not just the first — B3's responsive
    login renders the CPF field (and others) twice, a ``-mobile`` copy and a
    ``-desktop`` copy, with one hidden by CSS at any width. Taking ``.first``
    would grab the hidden twin and wrongly conclude the field is absent, so
    this walks the matches and returns the one actually on screen.
    """
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
        except Exception:  # noqa: BLE001 — an invalid/absent selector is just "no"
            continue
        # Capped: a pathological selector must not turn one glance into hundreds.
        for index in range(min(count, 8)):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                # A disabled submit is the norm on B3's login until the field
                # validates — skipping it lets the search fall through to the
                # button that is actually clickable (or to the Enter fallback).
                if require_enabled and not candidate.is_enabled():
                    continue
                return candidate
            except Exception:  # noqa: BLE001 — stale/detached node, try the next
                continue
    return None


def _fill_first(page, selectors: tuple[str, ...], value: str):
    """Fill the first visible matching field; return it (for a later submit)."""
    field = _first_visible(page, selectors)
    if field is None:
        raise PipelineError(f"nenhum campo encontrado ({selectors[0]}…)")
    field.fill(value)
    return field


def _move_window(page, *, offscreen: bool) -> None:
    """Park the browser window off-screen, or bring it back into view.

    Off-screen (far negative x) rather than minimised on purpose: a minimised or
    occluded window is marked hidden and Chromium throttles its timers, which
    would stall the very challenge JS we need to run. An off-screen window in
    the normal state keeps running full speed, just out of sight.
    """
    try:
        cdp = page.context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        bounds = (
            {"left": -32000, "top": 0, "width": 1280, "height": 1000}
            if offscreen
            else {"left": 60, "top": 60, "width": 1280, "height": 1000}
        )
        cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
        if not offscreen:
            page.bring_to_front()
    except Exception:  # noqa: BLE001 — window management is a nicety, never the run
        pass


def _close_filter_if_open(page) -> None:
    """Dismiss the period-filter modal if it is still up (it blocks export)."""
    if _first_visible(page, _DATE_START_FIELDS) is None:
        return
    back = _first_visible(page, ("button[aria-label='Voltar para extrato']", "button:has-text('VOLTAR')"))
    try:
        if back is not None:
            back.click(timeout=4_000)
        else:
            page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001 — best effort; the export step will report if it stays
        pass


def _enter_date(page, field, value, ctx) -> bool:
    """Put ``value`` into B3's masked dd/mm/yyyy field, verifying it stuck.

    Three strategies, each checked against the field's own value, because the
    mask (ngx-mask-style) reacts differently to each: real keystrokes of the
    eight digits (the mask adds the slashes — typing them too makes
    "01//11//2019"); a formatted ``fill``; and, last, setting the value in the
    DOM and firing the input/change/blur the framework listens for. Returns
    whether the field ends up showing the intended date.
    """
    target = value.strftime("%d/%m/%Y")
    digits = value.strftime("%d%m%Y")

    def current() -> str:
        try:
            return (field.input_value() or "").strip()
        except Exception:  # noqa: BLE001 — a odd control may not expose a value
            return ""

    def clear() -> None:
        field.click()
        field.press("Control+A")
        field.press("Delete")

    # 1) keystrokes — how a masked field expects to be filled
    clear()
    field.press_sequentially(digits, delay=60)
    page.wait_for_timeout(300)
    if current() == target:
        ctx.log(f"Data inicial digitada: {current()}.")
        return True

    # 2) a formatted fill
    try:
        clear()
        field.fill(target)
        page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001 — some masks reject fill; try the last way
        pass
    if current() == target:
        ctx.log(f"Data inicial preenchida: {current()}.")
        return True

    # 3) set it in the DOM and fire what the framework binds to
    try:
        field.evaluate(
            "(el, v) => { const set = Object.getOwnPropertyDescriptor("
            "window.HTMLInputElement.prototype, 'value').set; set.call(el, v);"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " el.dispatchEvent(new Event('blur', {bubbles: true})); }",
            target,
        )
        page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001 — nothing more to try
        pass
    ctx.log(f"Data inicial após tentativas: {current() or '(vazio)'}.", level="warning")
    return current() == target


def _wait_enabled(page, selectors: tuple[str, ...], timeout_ms: int):
    """Like :func:`_wait_visible`, but only an *enabled* match counts.

    The button that submits a login step enables a beat after the field
    validates, so a single glance races that transition — this waits for it.
    """
    waited = 0
    step = 250
    while True:
        found = _first_visible(page, selectors, require_enabled=True)
        if found is not None:
            return found
        if waited >= timeout_ms:
            return None
        page.wait_for_timeout(step)
        waited += step


def _submit_with(page, field, buttons: tuple[str, ...]) -> None:
    """Advance a one-field step: click the step's enabled button, or press Enter.

    The button enables only once the field is valid (and the page may carry a
    look-alike CTA), so an enabled real button is preferred — waited for, not
    glanced at — and Enter, which these forms also honour, is the fallback that
    needs no button at all.
    """
    button = _wait_enabled(page, buttons, 12_000)
    if button is not None:
        button.click(timeout=_STEP_TIMEOUT)
    else:
        field.press("Enter")


def _submit_step(page, field) -> None:
    """The CPF/password submit: the login form's own next button, or Enter."""
    _submit_with(page, field, _NEXT_BUTTONS)


def _click_first(page, selectors: tuple[str, ...], timeout: int | None = None, require_enabled: bool = True) -> None:
    button = _first_visible(page, selectors, require_enabled=require_enabled)
    if button is None:
        raise PipelineError(f"nenhum botão encontrado ({selectors[0]}…)")
    button.click(timeout=timeout or _STEP_TIMEOUT)


def _guarded(ctx, page, step: str, action):
    """Run one step; on failure, dump the evidence and fail with its name.

    The screenshot and HTML are what turn "a B3 mudou o site" from a guessing
    game into a diff: open the dump, find the new selector, add it to the
    candidate tuple above.
    """
    try:
        return action()
    except PipelineError:
        _dump(ctx, page, step)
        raise
    except Exception as exc:  # noqa: BLE001 — turn Playwright noise into one sentence
        _dump(ctx, page, step)
        raise PipelineError(
            f"Falhou ao {step}. A tela da B3 pode ter mudado — a captura em "
            f"{_dump_name(step)} mostra o que o robô estava vendo."
        ) from exc


def _dump_name(step: str) -> str:
    return re.sub(r"\W+", "-", step.lower()).strip("-")


#: In-page script: list every input and button the user can actually see, with
#: the attributes a selector is built from. This is what turns "campo não
#: encontrado" into the exact selector to add — far more useful than scrolling
#: a minified SPA's serialized DOM.
_CONTROLS_JS = """
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const attrs = (el) => ['type','name','id','placeholder','formcontrolname','aria-label','autocomplete','inputmode']
    .map((a) => (el.getAttribute(a) ? `${a}="${el.getAttribute(a)}"` : ''))
    .filter(Boolean).join(' ');
  const inputs = [...document.querySelectorAll('input')].filter(visible)
    .map((el) => `input ${attrs(el)}`);
  const buttons = [...document.querySelectorAll('button, a[role=button], [role=button]')].filter(visible)
    .map((el) => `button "${(el.innerText || '').trim().slice(0, 40)}" ${attrs(el)}`);
  return { url: location.href, inputs, buttons };
}
"""


def _dump(ctx, page, step: str) -> None:
    name = _dump_name(step)
    try:
        page.screenshot(path=str(ctx.debug_dir() / f"{name}.png"), full_page=True)
        (ctx.debug_dir() / f"{name}.html").write_text(page.content(), encoding="utf-8")
        ctx.log(f"Capturas salvas em pipelines-debug/b3/{name}.png|.html.", level="warning")
    except Exception:  # noqa: BLE001 — evidence is best effort, never the failure
        pass
    # The control inventory is the actionable half: it names the fields and
    # buttons that were on screen, so a wrong selector is a one-line fix.
    try:
        controls = page.evaluate(_CONTROLS_JS)
        lines = [f"url: {controls['url']}"]
        lines += [f"  {row}" for row in controls["inputs"]] or ["  (nenhum input visível)"]
        lines += [f"  {row}" for row in controls["buttons"][:25]]
        (ctx.debug_dir() / f"{name}.controls.txt").write_text("\n".join(lines), encoding="utf-8")
        ctx.log("Controles visíveis: " + (", ".join(controls["inputs"]) or "nenhum campo"), level="warning")
    except Exception:  # noqa: BLE001 — diagnostics are best effort
        pass


register(B3Pipeline())
