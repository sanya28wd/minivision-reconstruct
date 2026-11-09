"""Local-only database configuration for workspace entry points."""

import os

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def local_database(value: str) -> str:
    params = conninfo_to_dict(value)
    if params.get("service") or os.environ.get("PGSERVICE"):
        raise ValueError("Reconstruction requires an explicit local database")
    host = params.get("host", os.environ.get("PGHOST", "127.0.0.1"))
    address = params.get("hostaddr", os.environ.get("PGHOSTADDR", ""))
    if host not in ("localhost", "127.0.0.1", "::1") or address not in ("", "127.0.0.1", "::1"):
        raise ValueError("Reconstruction database must be local")
    params["host"] = host
    params["hostaddr"] = address or ("::1" if host == "::1" else "127.0.0.1")
    return make_conninfo(**{key: str(value) for key, value in params.items() if value is not None})
