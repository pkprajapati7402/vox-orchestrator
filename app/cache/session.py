"""Cross-instance session state (Upstash Redis, with an in-process fallback).

One call = one session key. Anything the websocket handler needs to recover
after a reconnect (or that another worker needs to see) lives here; the DB
remains the durable record.
"""

from __future__ import annotations

import abc
import json
import time
from typing import Any

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

SESSION_PREFIX = "vox:session:"


class SessionStore(abc.ABC):
    @abc.abstractmethod
    async def get(self, key: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def set(self, key: str, value: dict[str, Any], ttl: int | None = None) -> None: ...

    @abc.abstractmethod
    async def delete(self, key: str) -> None: ...

    async def update(
        self, key: str, patch: dict[str, Any], ttl: int | None = None
    ) -> dict[str, Any]:
        current = await self.get(key) or {}
        current.update(patch)
        await self.set(key, current, ttl)
        return current

    async def ping(self) -> bool:  # pragma: no cover - overridden
        return True

    async def aclose(self) -> None:  # pragma: no cover - overridden
        return None


class MemorySessionStore(SessionStore):
    """Single-process fallback used when REDIS_URL is empty (dev, tests, CI)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, dict[str, Any]]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at < time.time():
            self._data.pop(key, None)
            return None
        return dict(value)

    async def set(self, key: str, value: dict[str, Any], ttl: int | None = None) -> None:
        expires_at = time.time() + ttl if ttl else None
        self._data[key] = (expires_at, dict(value))

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


class RedisSessionStore(SessionStore):
    """Upstash/Redis-backed store (JSON values, TTL enforced by Redis)."""

    def __init__(self, url: str, default_ttl: int = 3600) -> None:
        from redis.asyncio import from_url

        self._redis = from_url(url, encoding="utf-8", decode_responses=True)
        self.default_ttl = default_ttl

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - corrupt entry
            log.warning("session.corrupt_entry", key=key)
            return None

    async def set(self, key: str, value: dict[str, Any], ttl: int | None = None) -> None:
        await self._redis.set(key, json.dumps(value, default=str), ex=ttl or self.default_ttl)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception as exc:  # noqa: BLE001
            log.warning("session.redis_unavailable", error=str(exc)[:200])
            return False

    async def aclose(self) -> None:
        await self._redis.aclose()


_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """Redis when configured, in-memory otherwise."""
    global _store
    if _store is None:
        settings = get_settings()
        if settings.redis_url:
            try:
                _store = RedisSessionStore(settings.redis_url, settings.session_ttl_seconds)
                log.info("session.store", backend="redis")
            except Exception as exc:  # noqa: BLE001 - never fail startup over cache
                log.warning("session.redis_init_failed", error=str(exc)[:200])
                _store = MemorySessionStore()
        else:
            _store = MemorySessionStore()
            log.info("session.store", backend="memory")
    return _store


async def close_session_store() -> None:
    global _store
    if _store is not None:
        await _store.aclose()
    _store = None


def session_key(call_id: str) -> str:
    return f"{SESSION_PREFIX}{call_id}"


__all__ = [
    "MemorySessionStore",
    "RedisSessionStore",
    "SessionStore",
    "close_session_store",
    "get_session_store",
    "session_key",
]
