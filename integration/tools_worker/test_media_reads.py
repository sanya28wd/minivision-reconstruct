"""Replay hot reads verify changed evidence without opening the media connector."""

import asyncio
from collections import Counter
import os

import httpx
import pytest

from integration.tools_worker.test_replay import replay_cases
from integration.tools_worker.test_worker import request


@pytest.mark.parametrize("changed", ["source", "proxy"])
def test_range_reads_cache_hashes_and_reverify_changes(tmp_path, monkeypatch, changed):
    from integration.tools_worker import app as worker, evidence, replay

    cases = replay_cases(tmp_path)
    counts = Counter()
    actual_hash = evidence.sha256_file

    def counted(path):
        counts[path] += 1
        return actual_hash(path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Hot reads must not open connector or rescan the case")

    monkeypatch.setattr(evidence, "sha256_file", counted)
    monkeypatch.setattr(replay, "ReplayMediaConnector", forbidden)
    monkeypatch.setattr(worker, "select_case", forbidden)
    app = worker.create_app(cases, "test-local-token")
    path = "/cases/mig02-multi-review/replay/week8-incident"

    async def ranges():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            for start in (0, 16, 32):
                response = await client.get(
                    path + "/video",
                    headers={"Authorization": "Bearer test-local-token", "Range": f"bytes={start}-{start + 15}"},
                )
                assert response.status_code == 206, response.text
                assert len(response.content) == 16

    asyncio.run(ranges())
    source = cases / "mig02-multi-review/original/mig02-multi-review-staged-synthetic.avi"
    proxy = cases / "mig02-multi-review/analysis/reconstruction/week8-incident/replay.mp4"
    assert counts[source] == 1
    assert counts[proxy] == 1
    assert request(app, path + "/assets/workspace.js").status_code == 200
    assert request(app, path + "/frame/0").status_code == 200
    assert counts[source] == 1
    target = source if changed == "source" else proxy
    stat = target.stat()
    with target.open("r+b") as stream:
        first = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first[0] ^ 1]))
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
    refused = request(app, path + "/video")
    assert refused.status_code == 422
    assert counts[target] == 2
