"""CLI entrypoint: load config + .env, run the engine loop."""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.engine import EngineConfig, run

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: str | Path | None = None) -> EngineConfig:
    load_dotenv()
    cfg_path = Path(path) if path else CONFIG_PATH
    raw = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    return EngineConfig(
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 30)),
        fast_poll_interval_seconds=int(raw.get("fast_poll_interval_seconds", 3)),
        edge_threshold_cents=float(raw.get("edge_threshold_cents", 5.0)),
        vol_window_minutes=int(raw.get("vol_window_minutes", 60)),
        db_path=str(raw.get("db_path", "./pricer.db")),
        series=str(raw.get("series", "KXBTCD")),
        vol_estimator=str(raw.get("vol_estimator", "yang_zhang")),
        calibrator_path=str(raw.get("calibrator_path", "./calibrator.json")),
        match_vol_window_to_horizon=bool(raw.get("match_vol_window_to_horizon", False)),
        spot_drift_per_year=float(raw.get("spot_drift_per_year", 0.0)),
    )


if __name__ == "__main__":
    run(load_config())
