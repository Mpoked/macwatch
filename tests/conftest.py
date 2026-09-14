import json
import socket
from pathlib import Path

import pytest

from macwatch.config import AppConfig, parse_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Testy nesmí sahat na síť. Jakýkoli pokus o spojení test shodí."""

    def guard(*args, **kwargs):
        raise RuntimeError("test se pokusil o síťové spojení")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture
def config_data() -> dict:
    return json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))


@pytest.fixture
def cfg(config_data, tmp_path) -> AppConfig:
    return parse_config(config_data, tmp_path)


@pytest.fixture
def rules(cfg):
    return cfg.parsing


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def read_fixture(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")
