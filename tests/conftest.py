from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
LOOKUP = ROOT / "capabilities" / "lookup_savings_balance" / "1.0.0.yaml"
LOGIN = ROOT / "capabilities" / "login" / "1.0.0.yaml"


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


@pytest.fixture
def lookup_data() -> dict[str, Any]:
    """A fresh, mutable copy of the reference artifact, for building invalid variants."""
    return copy.deepcopy(load_yaml(LOOKUP))


@pytest.fixture
def root() -> Path:
    return ROOT
