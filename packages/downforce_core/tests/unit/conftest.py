"""Keep SDK process configuration local to the unit test that changes it."""

from collections.abc import Iterator

import pytest
from fastf1 import Cache  # type: ignore[import-untyped]


@pytest.fixture(autouse=True)
def isolate_fastf1_cache_configuration(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Ingestion legitimately enables the process cache. Evidence capture instead
    # requires a fresh process; test order must not simulate a reused CLI process.
    # Neither provider quotas nor durable access-denial state are reset here.
    fields = (
        "_CACHE_DIR",
        "_IGNORE_VERSION",
        "_FORCE_RENEW",
        "_requests_session_cached",
        "_default_cache_enabled",
        "_tmp_disabled",
        "_ci_mode",
    )
    original = Cache._requests_session_cached
    for field in fields:
        monkeypatch.setattr(Cache, field, getattr(Cache, field))
    yield
    current = Cache._requests_session_cached
    if current is not None and current is not original:
        current.close()
