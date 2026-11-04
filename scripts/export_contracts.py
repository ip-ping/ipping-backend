"""Write the machine-readable contracts other repositories build against.

    uv run python scripts/export_contracts.py          # regenerate
    uv run python scripts/export_contracts.py --check  # fail if the committed files are stale

* contracts/openapi.json: the REST API; the frontend generates its types from this.
* contracts/node-protocol-v1.schema.json: the WebSocket frames; the Rust node's tests parse
  contracts/fixtures/ against the same definitions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ipping_backend import __version__
from ipping_backend.config import Settings
from ipping_backend.main import create_app
from ipping_backend.nodes import protocol

ROOT = Path(__file__).resolve().parent.parent / "contracts"


def build() -> dict[str, str]:
    app = create_app(Settings(database_url="sqlite+aiosqlite://"))
    openapi = app.openapi()
    node_protocol = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ipping node protocol",
        "description": "JSON text frames exchanged over the node WebSocket.",
        "protocol_version": protocol.PROTOCOL_VERSION,
        "backend_version": __version__,
        "node_to_backend": protocol.node_to_backend.json_schema(),
        "backend_to_node": protocol.backend_to_node.json_schema(),
    }
    return {
        "openapi.json": json.dumps(openapi, indent=2, sort_keys=True) + "\n",
        "node-protocol-v1.schema.json": json.dumps(node_protocol, indent=2, sort_keys=True) + "\n",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, content in build().items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.write_text(content)
            print(f"wrote {path.relative_to(ROOT.parent)}")
    if stale:
        print(
            "stale contracts (run scripts/export_contracts.py): " + ", ".join(stale),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
