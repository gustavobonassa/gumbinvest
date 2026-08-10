"""Pipeline contract and registry.

A pipeline is a class with a :class:`PipelineSpec` (what the Configurações
screen shows) and a ``run(ctx)`` method (what the worker thread executes).
Everything stateful — logging, the 2FA hand-off, cancellation, handing a
downloaded file to the importer — goes through the :class:`RunContext` the
runner provides, so a pipeline body reads as the sequence of steps a human
would do and nothing else.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.config import settings


class PipelineError(Exception):
    """A failure with a message fit for the screen (pt-BR, says what to do)."""


class PipelineCancelled(Exception):
    """The user asked the run to stop; not a failure."""


class PipelineInputTimeout(PipelineError):
    """Nobody answered an input request in time (a scheduled run, usually)."""


@dataclass(frozen=True, slots=True)
class PipelineSpec:
    """What the UI needs to draw a pipeline before it ever runs."""

    key: str
    name: str
    description: str
    #: ``(secret_key, label)`` pairs — each becomes a write-only credential
    #: field on the Automações tab. Keys must exist in ``secrets.SECRET_KEYS``.
    credentials: tuple[tuple[str, str], ...] = ()
    #: Human sentence about the schedule ("Semanal, segunda de manhã").
    schedule: str = ""


class Pipeline(ABC):
    """One automated source. Subclass, set ``spec``, implement ``run``."""

    spec: PipelineSpec

    def is_configured(self) -> bool:
        """Every credential present (stored through the UI or via env)."""
        return all(bool(getattr(settings, key, "")) for key, _ in self.spec.credentials)

    @abstractmethod
    def run(self, ctx) -> dict:  # noqa: ANN001 — RunContext, avoided to keep imports one-way
        """Do the collection; return the result dict the history table shows.

        Raise :class:`PipelineError` with a readable message for anything the
        user must know about; any other exception is reported generically.
        """


_REGISTRY: dict[str, Pipeline] = {}


def register(pipeline: Pipeline) -> Pipeline:
    _REGISTRY[pipeline.spec.key] = pipeline
    return pipeline


def get_pipeline(key: str) -> Pipeline:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"unknown pipeline: {key}") from exc


def all_pipelines() -> list[Pipeline]:
    return list(_REGISTRY.values())
