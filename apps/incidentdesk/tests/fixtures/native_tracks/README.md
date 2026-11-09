# Actual worker response (synthetic producer case)

`response.json` was captured on 2026-11-06 from the real in-process worker's
authenticated GET `/cases/mig02-relative/tracks/staged-tracks?frame=68`, using the
unchanged MIG-02 local case. It is synthetic producer evidence, not model output
or footage analysis. It was not hand-built or rewritten. The app tests consume
these bytes through the transport seam to exercise its reader against actual
worker output; a separate adversarial response checks HTML escaping.

Capture from the worktree root in WSL, with the configured worker Python and local
test cases root in environment variables (the exact machine command is recorded
in the donor ticket result):

```sh
PYTHONPATH=.:src "$RECONSTRUCTION_PYTHON" - <<'PY'
import os
from pathlib import Path
from integration.tools_worker.app import create_app
from integration.tools_worker.test_worker import request
response = request(create_app(Path(os.environ['INCIDENTDESK_TEST_CASES_ROOT']), 'test-local-token'), '/cases/mig02-relative/tracks/staged-tracks?frame=68')
assert response.status_code == 200, response.text
Path('apps/incidentdesk/tests/fixtures/native_tracks/response.json').write_bytes(response.content)
print(f'Captured {len(response.content)} bytes from actual worker HTTP response at source frame 68')
PY
```

Actual final line: `Captured 493 bytes from actual worker HTTP response at source frame 68`.
The path/token above are a test configuration and a public test credential, not
the worker's configured runtime token. No detector/model was run.
