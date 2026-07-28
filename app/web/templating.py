"""Настройка Jinja2: фильтры форматирования доступны прямо в шаблонах."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.core.fmt import FILTERS

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters.update(FILTERS)
templates.env.globals["PROTOCOL_COLORS"] = {
    "uniswap_v3": "#f5427e",
    "fluid_lending": "#2fa8ff",
    "fluid_vault": "#7c5cff",
}
