"""Existing browser scenarios start with a genuine share session (ADR 0010).

Only setup changed: all original assertions still run behind the real access gate.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from incident_report.access import AccessStore
from incident_report.config import Settings


def authorized_client(app: FastAPI, database_url: str | None = None) -> TestClient:
    store = AccessStore(database_url or Settings.from_env().database_url)
    link = store.create_link()
    client = TestClient(app, base_url="https://testserver")
    response = client.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]})
    assert response.status_code == 200
    return client
