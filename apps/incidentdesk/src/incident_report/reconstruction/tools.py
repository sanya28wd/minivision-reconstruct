"""Legacy tool links and attached-import native tool links."""

import uuid

TOOLS = (("Track review", "tracks"), ("Calibration", "calibration"), ("Reconstruction replay", "replay"))


def tool_links() -> tuple[tuple[str, str], ...]:
    return tuple((label, f"/reconstruct/{slug}/") for label, slug in TOOLS)


def native_links(incident: uuid.UUID, attachment: uuid.UUID) -> tuple[tuple[str, str], ...]:
    return tuple((label, f"/reports/{incident}/tools/{attachment}/{slug}") for label, slug in TOOLS)


def unavailable(tool: str) -> str:
    if tool not in {slug for _, slug in TOOLS}:
        raise ValueError("Unknown reconstruction tool")
    return "Reconstruction tools are not running. Start the local tools and Caddy proxy, then open this address through Caddy."
