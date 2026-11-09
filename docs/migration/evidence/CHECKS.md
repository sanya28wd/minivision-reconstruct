# MIG-01 check evidence

Offline runs; local PostgreSQL and local test HTTP fixtures only. No live model calls. Full local logs remain under `.git/mig-*.txt`.

The full gate predates the last 13 migration tests and two additional resource files. Its 27 missing-resource failures were subsequently addressed; the targeted rerun passed. The database failures remain unresolved.

## mig-format-final.txt

```text
207 files already formatted
```

## mig-lint-final.txt

```text
All checks passed!
```

## mig-mypy-final.txt

```text
Success: no issues found in 202 source files
```

## mig-new-tests.txt

```text
.............                                                            [100%]
13 passed in 1.83s
```

## mig-repaired-tests.txt

```text
........................................................................ [ 53%]
..............................................................           [100%]
134 passed in 0.62s
```

## mig-final-collect.txt

```text
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
2770 tests collected in 1.57s
```

## mig-counts.txt

```text
Excluded collected IDs: 464
Unexpected imported IDs: 0
New migration tests: 13
Expected final collection: 2770
```

## mig-identity.txt

```text
Committed runtime/test donor blobs checked: 312
Mismatches: []
```

## mig-check-all.txt

```text
ERROR tests/test_web_feedback.py::test_feedback_content_length_above_limit_returns_413_and_leaves_store_unchanged
ERROR tests/test_web_feedback.py::test_feedback_chunked_body_above_limit_returns_413
ERROR tests/test_web_feedback.py::test_feedback_bad_content_length_returns_400
ERROR tests/test_web_feedback.py::test_feedback_exactly_at_body_limit_succeeds
44 failed, 2249 passed, 1 warning, 464 errors in 292.09s (0:04:52)

FAILED: pytest
check_all: 3 passed, then FAILED at: pytest
```

## mig-protected.txt

```text
PROTECTED apps/incidentdesk/scripts/guard_test_integrity.py
PROTECTED apps/incidentdesk/scripts/guard_utf8.py
PROTECTED apps/incidentdesk/scripts/guardlib.py
FAIL 27 protected path(s) changed without owner approval. Protected paths are never edited by hand; if this change is intended, the owner adds the approval label to the PR and CI re-runs.
```

## mig-integrity.txt

```text
test-integrity guard: comparing c66000af3709..99215b0a7af6 (base main (merge-base with head))
OK no test was deleted, skipped, xfail-marked or loosened.
```

## mig-conflicts-final.txt

```text
OK changed text files passed.
```

## mig-utf8-final.txt

```text
OK changed text files passed.
```

## mig-secrets.txt

```text
secret guard: scanning c66000af3709..99215b0a7af6 (base main (merge-base with head))
OK no secrets found in added lines.
```

## mig-deps.txt

```text
Offline: exact donor 347a778 lock verified; no index requests made.
OK pinned import lock is unchanged and every import is declared.
```

## mig-defaults.txt

```text
evidence-defaults guard: checked 17 file(s)
OK no model or dataclass field has a default value.
```

## mig-migrations.txt

```text
migration-immutability guard: checked 14 migration(s), 4 immutable table(s)
OK every immutable table also blocks TRUNCATE.
```

## mig-wsl.txt

```text
```

## mig-wsl-sync.txt

```text
```

## Fork unchanged

Command: `git diff --stat main -- src scripts configs tests evaluation data pyproject.toml uv.lock README.md IMPLEMENTATION_PLAN.md`
Output: empty.

## Replay HTTP check

NOT RUN: required PostgreSQL connection timed out. No HTTP 200 or saved sample HTML is claimed.

## Baseline limit

MIG-00 inventory contains literal BASELINE_AGENT / BASELINE_LINUX placeholders rather than measured counts; Linux equality cannot be established. Existing WSL environment lacks pytest, and offline locked sync lacks cached torch wheels.
