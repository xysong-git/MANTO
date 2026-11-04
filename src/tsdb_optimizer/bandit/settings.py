"""Strict experiment configuration; deployment flags cannot be enabled here."""
from pathlib import Path
from typing import Literal
import yaml
from .model import Config


class ExperimentSettings(Config):
    schema_version: Literal[1] = 1
    seed: int = 20261102
    online_enabled: Literal[False] = False
    llm_enabled: Literal[False] = False


def load_settings(path=None):
    path = Path(path) if path else Path(__file__).resolve().parents[3]/'configs/bandit.yaml'
    return ExperimentSettings.model_validate(yaml.safe_load(path.read_text()))
