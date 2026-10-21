import json
from types import SimpleNamespace

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError
from pydantic import SecretStr

from gwc.db.migrate import upgrade
from gwc.garmin import client as client_mod
from gwc.garmin.client import TOKEN_FILE, AuthRequired, GarminAuth, LoginState, is_rate_limit_error
from gwc.garmin.guard import SyncGuard


class FakeSession:
    def __init__(self):
        self.sent = []

    def request(self, method, url, *args, **kwargs):
        self.sent.append((method, url))
        return SimpleNamespace(status_code=200)


class FakeLibGarmin:
    """Stands in for garminconnect.Garmin."""

    instances: list["FakeLibGarmin"] = []
    behavior: dict = {}

    def __init__(self, email=None, password=None, return_on_mfa=False, retry_attempts=3):
        self.email = email
        self.return_on_mfa = return_on_mfa
        self.full_name = "Test Person"
        self.display_name = "tester"
        self.profile_loaded = False
        self.client = SimpleNamespace(_api_session=FakeSession(), dump=self._dump)
        FakeLibGarmin.instances.append(self)

    def _dump(self, path):
        from pathlib import Path

        Path(path, TOKEN_FILE).write_text(json.dumps({"di_token": "x"}))

    def login(self, tokenstore=None):
        action = FakeLibGarmin.behavior.get("login")
        if isinstance(action, Exception):
            raise action
        if tokenstore:
            self.profile_loaded = True
            return None, None
        return ("needs_mfa", None) if action == "mfa" else (None, None)

    def resume_login(self, _state, code):
        if code != "123456":
            raise GarminConnectAuthenticationError("MFA code rejected")
        self.profile_loaded = True
        return None, None

    def _load_profile_and_settings(self):
        self.profile_loaded = True


@pytest.fixture
def auth(isolated_settings, monkeypatch):
    upgrade()
    FakeLibGarmin.instances = []
    FakeLibGarmin.behavior = {}
    monkeypatch.setattr(client_mod, "Garmin", FakeLibGarmin)
    settings = isolated_settings.model_copy(
        update={"garmin_email": "me@example.com", "garmin_password": SecretStr("pw")}
    )
    settings.ensure_dirs()
    return GarminAuth(settings, SyncGuard(settings.tz, sleep=lambda _: None))


def test_connect_requires_credentials(isolated_settings):
    upgrade()
    auth = GarminAuth(isolated_settings, SyncGuard(isolated_settings.tz))
    result = auth.connect()
    assert result.state is LoginState.FAILED
    assert "GARMIN_EMAIL" in result.message


def test_connect_without_mfa_saves_tokens(auth):
    result = auth.connect()
    assert result.state is LoginState.CONNECTED
    assert auth.has_tokens()
    assert FakeLibGarmin.instances[-1].profile_loaded
    assert auth.display_name() == "Test Person"
    assert auth.guard.logins_last_24h() == 1


def test_mfa_flow_with_retry(auth):
    FakeLibGarmin.behavior["login"] = "mfa"
    assert auth.connect().state is LoginState.NEEDS_MFA
    assert auth.mfa_pending()
    assert not auth.has_tokens()

    wrong = auth.submit_mfa("000000")
    assert wrong.state is LoginState.NEEDS_MFA
    assert auth.mfa_pending()

    right = auth.submit_mfa(" 123456 ")
    assert right.state is LoginState.CONNECTED
    assert not auth.mfa_pending()
    assert auth.has_tokens()


def test_bad_password(auth):
    FakeLibGarmin.behavior["login"] = GarminConnectAuthenticationError("401 Unauthorized")
    result = auth.connect()
    assert result.state is LoginState.FAILED
    assert not auth.has_tokens()


def test_login_rate_limit_locks_out(auth):
    FakeLibGarmin.behavior["login"] = GarminConnectTooManyRequestsError("429")
    result = auth.connect()
    assert result.state is LoginState.BLOCKED
    assert auth.guard.active_lockout() is not None
    assert auth.connect().state is LoginState.BLOCKED
    assert len(FakeLibGarmin.instances) == 1  # second attempt never reached Garmin


def test_login_budget(auth):
    FakeLibGarmin.behavior["login"] = GarminConnectAuthenticationError("401 Unauthorized")
    for _ in range(3):
        assert auth.connect().state is LoginState.FAILED
    result = auth.connect()
    assert result.state is LoginState.BLOCKED
    assert len(FakeLibGarmin.instances) == 3


def test_get_client_uses_tokens_only(auth):
    with pytest.raises(AuthRequired):
        auth.get_client()
    auth.connect()
    auth.invalidate_client()
    garmin = auth.get_client()
    assert garmin.email is None  # never re-sent credentials
    assert auth.get_client() is garmin  # cached


def test_rejected_tokens_raise_auth_required(auth):
    auth.connect()
    auth.invalidate_client()
    FakeLibGarmin.behavior["login"] = GarminConnectAuthenticationError("token rejected")
    with pytest.raises(AuthRequired):
        auth.get_client()


def test_requests_are_spaced_and_recorded(auth):
    auth.connect()
    garmin = FakeLibGarmin.instances[-1]
    garmin.client._api_session.request("GET", "https://connectapi.garmin.com/weight-service/x")
    garmin.client._api_session.request("GET", "https://connectapi.garmin.com/usersummary/y")
    assert len(garmin.client._api_session.sent) == 2
    # 1 login + 2 API requests
    assert auth.guard.requests_last_24h() == 3


def test_disconnect_removes_tokens(auth):
    auth.connect()
    auth.disconnect()
    assert not auth.has_tokens()
    assert auth.display_name() is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (GarminConnectTooManyRequestsError("x"), True),
        (_e := Exception("x"), False),
    ],
)
def test_is_rate_limit_error(exc, expected):
    assert is_rate_limit_error(exc) is expected


def test_cloudflare_403_counts_as_rate_limit():
    exc = Exception("blocked")
    exc.response = SimpleNamespace(status_code=403, text="<html>Cloudflare challenge</html>")
    assert is_rate_limit_error(exc)
    exc.response = SimpleNamespace(status_code=403, text='{"message": "forbidden"}')
    assert not is_rate_limit_error(exc)
