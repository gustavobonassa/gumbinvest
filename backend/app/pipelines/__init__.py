"""Automated collectors: log into a source, download an export, feed the importer.

The manual loop this replaces — sign into the broker, export a file, drag it
onto the Importar page — is exactly a pipeline's run. Each pipeline knows one
source (B3 today; Avenue, Clear, price scrapers later) and produces files the
existing importer already understands, so all the invariants that protect a
manual upload (idempotent dedup, loud parser failures, raw-line audit trail)
protect the automated path for free.

Same registry bargain as ``app.importer.pdf``: a new source is a new module
that calls :func:`register` — the runner, the API, the schedule and the
Configurações screen pick it up without changes.
"""
from __future__ import annotations

from app.pipelines.base import (
    Pipeline,
    PipelineCancelled,
    PipelineError,
    PipelineInputTimeout,
    all_pipelines,
    get_pipeline,
    register,
)

# Import for the side effect of registering. Deliberately at the bottom:
# a pipeline module may import from .base.
from app.pipelines import b3 as _b3  # noqa: E402,F401

__all__ = [
    "Pipeline",
    "PipelineCancelled",
    "PipelineError",
    "PipelineInputTimeout",
    "all_pipelines",
    "get_pipeline",
    "register",
]
