"""Backward-compatible access to validated settings."""

from src.config import Settings, get_settings


SETTINGS: Settings = get_settings()
CONFIG = SETTINGS.as_legacy_config()

