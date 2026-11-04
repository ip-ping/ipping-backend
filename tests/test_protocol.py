from __future__ import annotations

import json
from pathlib import Path

import pytest

from ipping_backend.nodes import protocol

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def _fixtures(direction: str) -> list[Path]:
    return sorted((CONTRACTS / "fixtures" / direction).glob("*.json"))


@pytest.mark.parametrize("path", _fixtures("node-to-backend"), ids=lambda p: p.stem)
def test_node_frames_parse(path: Path) -> None:
    frame = protocol.node_to_backend.validate_python(json.loads(path.read_text()))
    assert frame.type == json.loads(path.read_text())["type"]


@pytest.mark.parametrize("path", _fixtures("backend-to-node"), ids=lambda p: p.stem)
def test_backend_frames_parse(path: Path) -> None:
    frame = protocol.backend_to_node.validate_python(json.loads(path.read_text()))
    assert frame.type == json.loads(path.read_text())["type"]


def test_unknown_fields_are_ignored_for_forward_compatibility() -> None:
    frame = protocol.node_to_backend.validate_python({"type": "ping", "added_in_v1_1": True})
    assert isinstance(frame, protocol.Ping)


@pytest.mark.parametrize(
    "bad",
    [
        {
            "type": "sample",
            "task_id": "t",
            "attempt_id": "a",
            "seq": -1,
            "status": "ok",
            "rtt_us": 5,
        },
        {"type": "sample", "task_id": "t", "attempt_id": "a", "seq": 0, "status": "weird"},
        {"type": "hello", "protocol_versions": [], "node_version": "1", "capabilities": {}},
        {"type": "nonsense"},
    ],
)
def test_invalid_node_frames_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError subclasses ValueError
        protocol.node_to_backend.validate_python(bad)


def test_task_needs_exactly_one_destination() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        protocol.TaskTarget(ip="8.8.8.8", hostname="example.com")
    with pytest.raises(ValueError):  # noqa: PT011
        protocol.TaskTarget()
