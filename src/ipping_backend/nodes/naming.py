"""What a node is called when nobody named it: where it is and whose network it is.

An administrator may give a node a name (a node that enrolled with one keeps it). A node with no name is called after its place and its network, for example
`Sydney, AU - OVH SAS · AS16276`, so a node needs no name to be told apart, and the name follows the data (the IP databases fill the place and the
provider in as they learn the address).
"""

from __future__ import annotations


def node_label(
    name: str | None,
    *,
    city: str | None,
    region: str | None,
    country: str | None,
    provider: str | None,
    asn: int | None,
    node_id: str,
) -> str:
    """The name a node is shown by: its own name, or "City, CC - Provider · ASn" built from what is known of it."""
    if name and name.strip():
        return name.strip()
    place = ", ".join(part for part in (city or region, country) if part)
    network = " · ".join(part for part in (provider, f"AS{asn}" if asn else None) if part)
    label = " - ".join(part for part in (place, network) if part)
    return label or f"Node {node_id[:6]}"
