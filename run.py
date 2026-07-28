#!/usr/bin/env python3
"""Запуск дашборда:  .venv/bin/python run.py"""

from __future__ import annotations

import uvicorn

from app import config

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT,
                reload=False, log_level="info")
