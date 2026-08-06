"""Public 13F portfolios of famous investors — see app/market/superinvestors."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from app.core.logging import get_logger
from app.market import superinvestors

router = APIRouter(prefix="/investors", tags=["investors"])
logger = get_logger(__name__)


@router.get("", response_model=None, summary="Curated list of investors with public portfolios")
def list_investors() -> list[dict]:
    return superinvestors.list_investors()


@router.get("/{slug}", response_model=None, summary="One investor's latest 13F portfolio")
def investor_wallet(slug: str) -> dict:
    try:
        return superinvestors.wallet(slug)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"investidor {slug} não catalogado") from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except httpx.HTTPError as exc:
        logger.warning("investors: EDGAR unavailable for %s: %s", slug, exc)
        raise HTTPException(
            status_code=503, detail="SEC EDGAR indisponível no momento; tente novamente."
        ) from None
