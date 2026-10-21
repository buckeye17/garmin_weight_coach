"""Process-wide Garmin singletons shared by the web app and the scheduler."""

from functools import lru_cache

from gwc.config import get_settings
from gwc.garmin.client import GarminAuth
from gwc.garmin.guard import SyncGuard
from gwc.garmin.sync import SyncRunner, SyncService


@lru_cache
def get_guard() -> SyncGuard:
    return SyncGuard(tz=get_settings().tz)


@lru_cache
def get_auth() -> GarminAuth:
    return GarminAuth(get_settings(), get_guard())


@lru_cache
def get_sync_service() -> SyncService:
    return SyncService(get_auth(), get_guard(), tz=get_settings().tz)


@lru_cache
def get_runner() -> SyncRunner:
    return SyncRunner(get_sync_service())
