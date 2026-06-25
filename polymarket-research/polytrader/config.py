"""Configuration loading and validation.

Two layers:
  * ``config.yaml`` holds the *analytical* parameters (ranking weights, filters,
    backtest knobs).  These are versioned so methodology changes are auditable.
  * Environment variables (``POLYTRADER_*``) hold *runtime/connection* settings
    (database URL, data source, seed) so deployments differ without code edits.

Access everything through :func:`get_config`.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

# Repository root (this file lives in <root>/polytrader/config.py)
ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Typed views over the YAML blocks.  We keep them permissive (extra allowed) so
# adding a knob in config.yaml does not require a code change to read it.
# --------------------------------------------------------------------------- #
class _Block(BaseModel):
    model_config = {"extra": "allow"}

    def __getitem__(self, item: str) -> Any:  # dict-style access convenience
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


class RankingConfig(_Block):
    weights: Dict[str, float]
    profitability: Dict[str, float]
    consistency: Dict[str, float]
    risk: Dict[str, float]
    longevity: Dict[str, float]
    bootstrap_iterations: int = 1000
    winsorize_pct: float = 0.01

    @field_validator("weights")
    @classmethod
    def _weights_sum_to_one(cls, v: Dict[str, float]) -> Dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"ranking.weights must sum to 1.0, got {total:.4f}")
        return v


class Config(BaseModel):
    """Fully-resolved configuration object."""

    # runtime (env-overridable)
    database_url: str
    data_source: str
    seed: int
    as_of: Optional[str] = None
    clob_api_key: Optional[str] = None

    # analytical blocks (from yaml)
    collect: _Block
    eligibility: _Block
    ranking: RankingConfig
    clustering: _Block
    correlation: _Block
    backtest: _Block
    small_account: _Block
    signals: _Block
    categories: Dict[str, List[str]]

    @property
    def rng(self):
        """A seeded numpy Generator — the single source of randomness."""
        import numpy as np

        return np.random.default_rng(self.seed)


def _load_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def get_config(config_path: Optional[str] = None) -> Config:
    """Load and cache the resolved configuration.

    Resolution order for runtime fields: explicit env var > config.yaml > default.
    """
    cfg_path = Path(
        config_path
        or os.environ.get("POLYTRADER_CONFIG")
        or (ROOT / "config.yaml")
    )
    raw = _load_yaml(cfg_path)
    run = raw.get("run", {})

    database_url = os.environ.get(
        "POLYTRADER_DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'polytrader.db'}"
    )
    data_source = os.environ.get("POLYTRADER_DATA_SOURCE", run.get("data_source", "synthetic"))
    seed = int(os.environ.get("POLYTRADER_SEED", run.get("seed", 42)))
    as_of = os.environ.get("POLYTRADER_AS_OF", run.get("as_of"))

    return Config(
        database_url=database_url,
        data_source=data_source,
        seed=seed,
        as_of=as_of,
        clob_api_key=os.environ.get("POLYTRADER_CLOB_API_KEY") or None,
        collect=_Block(**raw.get("collect", {})),
        eligibility=_Block(**raw.get("eligibility", {})),
        ranking=RankingConfig(**raw["ranking"]),
        clustering=_Block(**raw.get("clustering", {})),
        correlation=_Block(**raw.get("correlation", {})),
        backtest=_Block(**raw.get("backtest", {})),
        small_account=_Block(**raw.get("small_account", {})),
        signals=_Block(**raw.get("signals", {})),
        categories=raw.get("categories", {}),
    )


def reset_config_cache() -> None:
    """Clear the cached config (used in tests that mutate env)."""
    get_config.cache_clear()
