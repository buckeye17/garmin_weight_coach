"""The only module that talks to Garmin: login, 2FA, token storage, request accounting.

Every HTTP request the API session sends passes through `SyncGuard` (spacing,
budgets, lockouts). Credential logins count as one login attempt each; the
library's internal SSO strategy chain isn't instrumented request by request.
"""

import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from gwc.config import Settings
from gwc.garmin.guard import SyncGuard
from gwc.prefs import delete_pref, get_pref, set_pref

log = logging.getLogger(__name__)

TOKEN_FILE = "garmin_tokens.json"


class GarminApi(Protocol):
    """The subset of `garminconnect.Garmin` the sync uses (lets tests supply a fake)."""

    def get_body_composition(self, startdate: str, enddate: str | None = None) -> Any: ...
    def get_activities_by_date(self, startdate: str, enddate: str | None = None) -> Any: ...
    def get_user_summary(self, cdate: str) -> Any: ...
    def get_sleep_data(self, cdate: str) -> Any: ...
    def get_sync_timestamp(self) -> Any: ...


class AuthRequired(Exception):
    """No usable Garmin tokens; the user must connect from Settings."""


class LoginState(StrEnum):
    CONNECTED = "connected"
    NEEDS_MFA = "needs_mfa"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class LoginResult:
    state: LoginState
    message: str | None = None


def is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        return True
    if status == 403:
        # Cloudflare bot challenges come back as 403 HTML pages.
        body = (getattr(response, "text", "") or "")[:2000].lower()
        return "cloudflare" in body or "challenge" in body
    return False


class GarminAuth:
    """Owns the in-process Garmin client, including a login paused for an MFA code."""

    def __init__(self, settings: Settings, guard: SyncGuard) -> None:
        self.settings = settings
        self.guard = guard
        self._lock = threading.RLock()
        self._client: Garmin | None = None
        self._pending: Garmin | None = None

    # ------------------------------------------------------------------ state

    @property
    def token_dir(self) -> Path:
        return self.settings.garmin_token_dir

    def has_tokens(self) -> bool:
        return (self.token_dir / TOKEN_FILE).is_file()

    def has_credentials(self) -> bool:
        return bool(self.settings.garmin_email and self.settings.garmin_password)

    def mfa_pending(self) -> bool:
        return self._pending is not None

    def _local(self, dt) -> str:
        return f"{dt.astimezone(self.settings.tz):%a %H:%M}"

    def display_name(self) -> str | None:
        return get_pref("garmin.display_name")

    # ------------------------------------------------------------------ login

    def _new_garmin(self, *, with_credentials: bool) -> Garmin:
        kwargs: dict[str, Any] = {"return_on_mfa": True, "retry_attempts": 1}
        if with_credentials:
            kwargs["email"] = self.settings.garmin_email
            kwargs["password"] = self.settings.garmin_password.get_secret_value()
        garmin = Garmin(**kwargs)
        self._instrument(garmin)
        return garmin

    def _instrument(self, garmin: Garmin) -> None:
        session = garmin.client._api_session
        original = session.request
        guard = self.guard

        def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            guard.before_request()
            status = None
            try:
                response = original(method, url, *args, **kwargs)
                status = response.status_code
                return response
            finally:
                guard.record_request(urlparse(url).path, status)

        session.request = request

    def connect(self) -> LoginResult:
        """Start a credential login. May pause for an MFA code (see `submit_mfa`)."""
        with self._lock:
            if not self.has_credentials():
                return LoginResult(
                    LoginState.FAILED, "Add GARMIN_EMAIL and GARMIN_PASSWORD to .env first."
                )
            decision = self.guard.check_login()
            if not decision.allowed:
                return LoginResult(LoginState.BLOCKED, decision.reason)

            self._pending = None
            garmin = self._new_garmin(with_credentials=True)
            self.guard.record_request("sso/login", None, is_login=True)
            try:
                needs_mfa, _ = garmin.login()
            except GarminConnectTooManyRequestsError:
                lockout = self.guard.register_rate_limit(is_login=True)
                return LoginResult(
                    LoginState.BLOCKED,
                    f"Garmin rate-limited the login. Paused until {self._local(lockout.until)}.",
                )
            except GarminConnectAuthenticationError as e:
                return LoginResult(LoginState.FAILED, _first_line(e))
            except GarminConnectConnectionError as e:
                return LoginResult(LoginState.FAILED, _first_line(e))

            if needs_mfa:
                self._pending = garmin
                return LoginResult(LoginState.NEEDS_MFA, "Enter the code Garmin sent you.")
            # With return_on_mfa the library returns before loading the profile.
            return self._finish_login(garmin)

    def submit_mfa(self, code: str) -> LoginResult:
        with self._lock:
            garmin = self._pending
            if garmin is None:
                return LoginResult(LoginState.FAILED, "No login is waiting for a code.")
            try:
                garmin.resume_login(None, code.strip())
            except GarminConnectTooManyRequestsError:
                self._pending = None
                lockout = self.guard.register_rate_limit(is_login=True)
                return LoginResult(
                    LoginState.BLOCKED,
                    f"Garmin rate-limited the login. Paused until {self._local(lockout.until)}.",
                )
            except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
                # A wrong code leaves the pending login intact so the user can retry.
                return LoginResult(LoginState.NEEDS_MFA, f"Code not accepted: {_first_line(e)}")
            self._pending = None
            return self._finish_login(garmin, profile_loaded=True)

    def cancel_mfa(self) -> None:
        with self._lock:
            self._pending = None

    def _finish_login(self, garmin: Garmin, *, profile_loaded: bool = False) -> LoginResult:
        try:
            if not profile_loaded:
                garmin._load_profile_and_settings()
            self.token_dir.mkdir(parents=True, exist_ok=True)
            garmin.client.dump(str(self.token_dir))
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            return LoginResult(LoginState.FAILED, f"Logged in but setup failed: {_first_line(e)}")
        self._client = garmin
        set_pref("garmin.display_name", garmin.full_name or garmin.display_name)
        log.info("Connected to Garmin")
        return LoginResult(LoginState.CONNECTED)

    def disconnect(self) -> None:
        with self._lock:
            self._client = None
            self._pending = None
            (self.token_dir / TOKEN_FILE).unlink(missing_ok=True)
            delete_pref("garmin.display_name")

    # ------------------------------------------------------------------ client

    def get_client(self) -> GarminApi:
        """Return an authenticated client from cached tokens; never does a credential login."""
        with self._lock:
            if self._client is not None:
                return self._client
            if not self.has_tokens():
                raise AuthRequired("Garmin is not connected")
            garmin = self._new_garmin(with_credentials=False)
            try:
                garmin.login(str(self.token_dir))
            except GarminConnectAuthenticationError as e:
                raise AuthRequired(_first_line(e)) from e
            self._client = garmin
            return garmin

    def invalidate_client(self) -> None:
        with self._lock:
            self._client = None


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:300]
