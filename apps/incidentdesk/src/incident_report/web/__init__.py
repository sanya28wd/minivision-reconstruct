"""Web layer: server-rendered pages that call seam 1 and render only from the Report object."""

from incident_report.web.app import create_app

__all__ = ["create_app"]
