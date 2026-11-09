"""Process-local idempotency: serialize one resource, permit independent resources."""

from collections import OrderedDict
from hashlib import sha256
import json
from threading import Lock
from uuid import UUID


class WriteConflict(ValueError):
    """The caller reused a completed action ID with different input."""


class IdempotentWrites:
    def __init__(self):
        self._registry = Lock()
        self._locks = {}
        self._completed = OrderedDict()

    def lookup(self, scope, body):
        identity = (*scope, str(UUID(body["action_id"])))
        digest = sha256(
            json.dumps(body, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        with self._registry:
            previous = self._completed.get(identity)
            if previous is None:
                return None
            old_digest, result = previous
            if old_digest != digest:
                raise WriteConflict("Action ID reused with different input")
            return result

    def execute(self, scope, body, operation):
        identity = (*scope, str(UUID(body["action_id"])))
        digest = sha256(
            json.dumps(body, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        with self._registry:
            lock = self._locks.setdefault(tuple(scope), Lock())
        with lock:
            with self._registry:
                previous = self._completed.get(identity)
                if previous is not None:
                    old_digest, result = previous
                    if old_digest != digest:
                        raise WriteConflict("Action ID reused with different input")
                    return result
            result = operation()
            with self._registry:
                self._completed[identity] = (digest, result)
                # Existing process-lifetime budget; this is not a durable job store.
                while len(self._completed) > 256:
                    self._completed.popitem(last=False)
            return result
