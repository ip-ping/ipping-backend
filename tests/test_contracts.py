from __future__ import annotations

import importlib.util
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def test_committed_contracts_are_up_to_date() -> None:
    """If this fails: uv run python scripts/export_contracts.py"""
    spec = importlib.util.spec_from_file_location(
        "export_contracts", CONTRACTS.parent / "scripts" / "export_contracts.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, content in module.build().items():
        assert (CONTRACTS / name).read_text() == content, f"{name} is stale"
