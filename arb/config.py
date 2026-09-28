from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


class Config(dict):
    """dict with attribute-style section access: cfg.risk['max_trade_pct']"""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f)
    return Config(raw)
