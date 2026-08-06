"""Probe the bulk data sources the asset universe would be built from.

Read-only. Nothing here is imported by the application and nothing is written to
the database — this exists to answer "does this URL still exist, what shape is
it, and how big" *before* a schema is frozen around the answer. Shipping a
column no live source can fill is the same failure as guessing a classifier
rule: confidently wrong beats visibly ignorant, so the columns are decided from
this output rather than from memory.

    cd backend && python scripts/universe_spike.py
    python scripts/universe_spike.py --only cvm
    python scripts/universe_spike.py --full          # actually download the big files

By default large bodies are probed with a Range request: enough to confirm the
content type and the magic bytes without pulling tens of megabytes. ``--full``
downloads them and reports the real member list of each zip, which is what the
parsers will need.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# Run as ``python scripts/universe_spike.py``, so the interpreter puts *this*
# directory on the path rather than the backend root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402

TIMEOUT = 120.0

#: A browser-shaped agent. B3's proxies answer 403 to httpx's default, and the
#: SEC wants a contact address (see app/market/superinvestors.py).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Bodies above this are probed with a Range request unless --full is passed.
PEEK_BYTES = 4096


@dataclass(slots=True)
class Probe:
    """One candidate endpoint and what we hope to learn from it."""

    group: str
    name: str
    url: str
    #: What the parser would need out of this; printed so a failure is legible.
    wants: str
    headers: dict[str, str] = field(default_factory=dict)
    #: Directory index: list the hrefs instead of dumping the body.
    listing: bool = False
    #: Expected to be a zip: report the member table.
    archive: bool = False


def _b64(payload: dict) -> str:
    """B3's proxies take one base64'd JSON blob as the path segment."""
    return base64.b64encode(json.dumps(payload).encode()).decode()


def probes() -> list[Probe]:
    sec_headers = {"User-Agent": settings.sec_user_agent}
    b3_headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
    year = time.gmtime().tm_year

    return [
        # -- controls: proven by the app's own working code -------------------
        Probe(
            "control",
            "SEC company_tickers.json",
            "https://www.sec.gov/files/company_tickers.json",
            wants="ticker -> {cik_str, ticker, title} for every US filer",
            headers=sec_headers,
        ),
        # -- CVM: directory indexes first; filenames are read FROM them -------
        Probe(
            "cvm",
            "CIA_ABERTA registry dir",
            "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/",
            wants="a registry CSV: CNPJ, codigo CVM, situacao, setor",
            listing=True,
        ),
        Probe(
            "cvm",
            "DFP (annual statements) dir",
            "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/",
            wants="yearly zips of BPA/BPP/DRE/DFC line items",
            listing=True,
        ),
        Probe(
            "cvm",
            "ITR (quarterly statements) dir",
            "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/",
            wants="same as DFP but quarterly — the fresher fundamentals",
            listing=True,
        ),
        Probe(
            "cvm",
            "FII registry dir",
            "https://dados.cvm.gov.br/dados/FII/CAD/DADOS/",
            wants="FII CNPJ, segmento, administrador",
            listing=True,
        ),
        Probe(
            "cvm",
            "FII monthly report dir",
            "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/",
            wants="patrimonio liquido, cotas, vacancia, distribuicoes",
            listing=True,
        ),
        # -- B3 COTAHIST: prices/volume for the whole exchange ----------------
        Probe(
            "cotahist",
            f"COTAHIST_A{year} (bvmf host)",
            f"https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{year}.ZIP",
            wants="fixed-width 245-byte daily records for every listed ticker",
            headers={"User-Agent": BROWSER_UA},
            archive=True,
        ),
        Probe(
            "cotahist",
            f"COTAHIST_A{year - 1} (bvmf host)",
            f"https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{year - 1}.ZIP",
            wants="the prior year — needed for a trailing 12-month window",
            headers={"User-Agent": BROWSER_UA},
            archive=True,
        ),
        Probe(
            "cotahist",
            f"COTAHIST_A{year} (arquivos host)",
            f"https://arquivos.b3.com.br/rapinegocios/tickercsv/COTAHIST_A{year}.ZIP",
            wants="alternate host, in case bvmf has been retired",
            headers={"User-Agent": BROWSER_UA},
            archive=True,
        ),
        # -- B3 identity ------------------------------------------------------
        Probe(
            "b3",
            "listed companies (page 1)",
            "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/"
            "GetInitialCompanies/"
            + _b64({"language": "pt-br", "pageNumber": 1, "pageSize": 20}),
            wants="company name, CNPJ, ticker root, segment — paged",
            headers=b3_headers,
        ),
        Probe(
            "b3",
            "listed funds (page 1)",
            "https://sistemaswebb3-listados.b3.com.br/fundsProxy/fundsCall/"
            "GetListedFundsSIG/"
            + _b64({"typeFund": 7, "pageNumber": 1, "pageSize": 20}),
            wants="the FII list — same proxy family as the dividend endpoint we use",
            headers=b3_headers,
        ),
        Probe(
            "brapi",
            "available tickers",
            f"{settings.brapi_base_url.rstrip('/')}/available",
            wants="a flat list of every B3 ticker — the cheapest identity source if real",
            headers={"User-Agent": BROWSER_UA},
        ),
    ]


# ---------------------------------------------------------------------------
# Reporting


def _decode(raw: bytes) -> str:
    """The encoding ladder the CVM/B3 files need (treasury.py uses the same)."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


_HREF = re.compile(r'href="([^"?][^"]*)"', re.IGNORECASE)


def _report_listing(body: str) -> None:
    """Print a directory index's entries — this is how filenames get discovered."""
    names = [name for name in _HREF.findall(body) if not name.startswith("/")]
    names = [name for name in names if name not in ("../", "./")]
    if not names:
        print("      (no hrefs found — not a directory index?)")
        print(f"      first 300 chars: {body[:300]!r}")
        return
    print(f"      {len(names)} entries:")
    for name in names[:40]:
        print(f"        {name}")
    if len(names) > 40:
        print(f"        … and {len(names) - 40} more")


def _report_archive(raw: bytes) -> None:
    """Print a zip's member table — the parser needs the member names."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            print(f"      {len(members)} member(s):")
            for member in members[:20]:
                print(
                    f"        {member.filename}  "
                    f"{member.file_size:,} B uncompressed "
                    f"({member.compress_size:,} B stored)"
                )
            if members:
                with archive.open(members[0]) as handle:
                    head = handle.read(760)
                print("      first records of member 0:")
                text = _decode(head)
                for line in text.splitlines()[:3]:
                    print(f"        len={len(line):>4}  {line[:120]!r}")
    except zipfile.BadZipFile:
        print("      NOT a valid zip (got HTML or an error page?)")
        print(f"      first 300 bytes: {raw[:300]!r}")


def _report_body(raw: bytes, content_type: str) -> None:
    text = _decode(raw)
    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            print(f"      body looks like JSON but did not parse: {text[:300]!r}")
            return
        if isinstance(parsed, dict):
            keys = list(parsed)[:12]
            print(f"      JSON object, {len(parsed)} key(s); first: {keys}")
            if keys:
                print(f"      sample value: {json.dumps(parsed[keys[0]], ensure_ascii=False)[:300]}")
        elif isinstance(parsed, list):
            print(f"      JSON array, {len(parsed)} item(s)")
            if parsed:
                print(f"      first item: {json.dumps(parsed[0], ensure_ascii=False)[:300]}")
        return
    lines = text.splitlines()
    print(f"      {len(lines)} line(s) in this sample")
    for line in lines[:3]:
        print(f"        {line[:200]!r}")


def run(probe: Probe, *, full: bool) -> None:
    print(f"\n  [{probe.group}] {probe.name}")
    print(f"      url:   {probe.url[:150]}")
    print(f"      wants: {probe.wants}")

    headers = dict(probe.headers)
    peek = not full and (probe.archive or probe.listing is False)
    if peek and probe.archive:
        headers["Range"] = f"bytes=0-{PEEK_BYTES - 1}"

    started = time.monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            response = client.get(probe.url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — a probe failing is a result, not a crash
        print(f"      FAILED after {time.monotonic() - started:.1f}s: {type(exc).__name__}: {exc}")
        return
    elapsed = time.monotonic() - started

    content_type = response.headers.get("content-type", "?")
    length = response.headers.get("content-range") or response.headers.get("content-length", "?")
    print(
        f"      -> {response.status_code} {content_type} "
        f"len={length} bytes_read={len(response.content):,} in {elapsed:.1f}s"
    )
    if response.status_code >= 400:
        print(f"      body head: {response.content[:200]!r}")
        return

    if probe.listing:
        _report_listing(_decode(response.content))
    elif probe.archive:
        if full:
            _report_archive(response.content)
        else:
            magic = response.content[:2]
            verdict = "PK — looks like a zip" if magic == b"PK" else f"magic={magic!r} — NOT a zip"
            print(f"      {verdict}  (re-run with --full for the member table)")
    else:
        # Not truncated: these are JSON bodies and a partial one never parses.
        _report_body(response.content, content_type)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="probe one group: control, cvm, cotahist, b3, brapi")
    parser.add_argument(
        "--full",
        action="store_true",
        help="download large bodies in full and report zip members (slow, tens of MB)",
    )
    args = parser.parse_args()

    selected = [p for p in probes() if not args.only or p.group == args.only]
    if not selected:
        groups = sorted({p.group for p in probes()})
        parser.error(f"no probes in group {args.only!r}; known groups: {groups}")

    print(f"Probing {len(selected)} endpoint(s){' (full download)' if args.full else ''}")
    print(f"SEC User-Agent: {settings.sec_user_agent!r}")
    if "admin@example.com" in settings.sec_user_agent:
        print("  ^ still the placeholder — the US leg must refuse to run until this is set")

    for probe in selected:
        run(probe, full=args.full)

    print("\nDone. Paste this output into the PR — this is what freezes the column list.")


if __name__ == "__main__":
    main()
