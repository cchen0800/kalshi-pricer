"""CLI entrypoint: load config + .env, run the engine loop."""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.engine import EngineConfig, run

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> EngineConfig:
    load_dotenv()
    raw = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    return EngineConfig(
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 30)),
        edge_threshold_cents=float(raw.get("edge_threshold_cents", 5.0)),
        vol_window_minutes=int(raw.get("vol_window_minutes", 60)),
        db_path=str(raw.get("db_path", "./pricer.db")),
        series=str(raw.get("series", "KXBTCD")),
    )


if __name__ == "__main__":
    run(load_config())
