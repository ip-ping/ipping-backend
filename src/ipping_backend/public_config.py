"""What the web interface needs to know before it draws itself, for anyone: no secrets, no sessions."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .deps import Db
from .runtime_settings import load_settings

router = APIRouter(prefix="/api/v1", tags=["config"])


class LanguagesOut(BaseModel):
    enabled: list[str] = Field(
        description="The languages a visitor can choose, in the interface's order"
    )
    default: str = Field(
        description="Used when the visitor has not chosen one and the browser's is not followed or not on"
    )
    auto_detect: bool = Field(
        description="Follow the browser's language until the visitor chooses one"
    )


class PublicConfig(BaseModel):
    languages: LanguagesOut


@router.get("/config", response_model=PublicConfig)
async def public_config(db: Db) -> PublicConfig:
    """The interface's language settings (an administrator changes them under Settings)."""
    settings = await load_settings(db)
    return PublicConfig(
        languages=LanguagesOut(
            enabled=settings.languages_enabled,
            default=settings.default_language,
            auto_detect=settings.language_auto_detect,
        )
    )
