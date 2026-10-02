import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from googich_takeaway.web.app import COOKIE, WebSettings, create_app
from googich_takeaway.web.auth import LoginThrottle

PASSWORD = "correct horse battery"  # noqa: S105 - test fixture
ORIGIN = {"Origin": "http://testserver"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
        self.mono = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make(tmp_path: Path, clock: Clock) -> TestClient:
    app = create_app(
        WebSettings(tmp_path / "state.db"),
        clock=clock,
        throttle=LoginThrottle(clock=clock.monotonic),
        start_worker=False,
    )
    return TestClient(app, follow_redirects=False)


def set_up(client: TestClient) -> None:
    token = client.app.state.setup_token  # type: ignore[attr-defined]
    response = client.post(
        "/setup",
        data={"token": token, "password": PASSWORD, "confirm": PASSWORD},
        headers=ORIGIN,
    )
    assert response.status_code == 303


def csrf_of(client: TestClient) -> str:
    page = client.get("/").text
    found = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert found
    return found[1]


def test_health_is_open_and_hardened(tmp_path: Path, clock: Clock) -> None:
    response = make(tmp_path, clock).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_fresh_install_leads_to_setup(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    assert client.get("/").headers["location"] == "/login"
    assert client.get("/login").headers["location"] == "/setup"
    assert client.get("/setup").status_code == 200


def test_setup_token_is_only_in_the_log(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="googich.web"):
        client = make(tmp_path, clock)
    token = client.app.state.setup_token  # type: ignore[attr-defined]
    assert token in caplog.text
    assert token not in client.get("/setup").text


@pytest.mark.parametrize(
    ("token", "password", "confirm", "message"),
    [
        ("wrong", PASSWORD, PASSWORD, "setup token is not right"),
        (None, PASSWORD, PASSWORD + "x", "do not match"),
        (None, "short", "short", "at least 12"),
    ],
)
def test_setup_rejects(
    tmp_path: Path, clock: Clock, token: str | None, password: str, confirm: str, message: str
) -> None:
    client = make(tmp_path, clock)
    real = client.app.state.setup_token  # type: ignore[attr-defined]
    response = client.post(
        "/setup",
        data={"token": token or real, "password": password, "confirm": confirm},
        headers=ORIGIN,
    )
    assert response.status_code == 400
    assert message in response.text


def test_setup_logs_in_and_then_closes(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    cookie = client.cookies.get(COOKIE)
    assert cookie
    page = client.get("/")
    assert page.status_code == 200
    assert "Bring your Google Photos home" in page.text
    assert client.get("/setup").headers["location"] == "/login"
    assert client.app.state.setup_token is None  # type: ignore[attr-defined]


def test_session_cookie_flags(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    token = client.app.state.setup_token  # type: ignore[attr-defined]
    response = client.post(
        "/setup", data={"token": token, "password": PASSWORD, "confirm": PASSWORD}, headers=ORIGIN
    )
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header


def test_login_logout_and_session_rotation(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    first = client.cookies.get(COOKIE)
    assert client.post("/login", data={"password": "nope"}, headers=ORIGIN).status_code == 401
    assert client.post("/login", data={"password": PASSWORD}, headers=ORIGIN).status_code == 303
    second = client.cookies.get(COOKIE)
    assert second != first
    stale = TestClient(client.app, cookies={COOKIE: first}, follow_redirects=False)
    assert stale.get("/").status_code == 303  # old session was ended at login

    csrf = csrf_of(client)
    assert client.post("/logout", headers=ORIGIN).status_code == 403  # no CSRF token
    assert client.post("/logout", data={"csrf_token": csrf}, headers=ORIGIN).status_code == 303
    after = TestClient(client.app, cookies={COOKIE: second}, follow_redirects=False)
    assert after.get("/").status_code == 303


def test_cross_site_posts_are_refused(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    csrf = csrf_of(client)
    assert client.post("/logout", data={"csrf_token": csrf}).status_code == 403  # no Origin
    evil = {"Origin": "https://evil.example"}
    assert client.post("/logout", data={"csrf_token": csrf}, headers=evil).status_code == 403
    assert client.post("/login", data={"password": PASSWORD}, headers=evil).status_code == 403


def test_csrf_header_works_for_htmx(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    csrf = csrf_of(client)
    response = client.post("/logout", headers={**ORIGIN, "X-CSRF-Token": csrf})
    assert response.status_code == 303


def test_htmx_without_session_gets_redirect_header(tmp_path: Path, clock: Clock) -> None:
    response = make(tmp_path, clock).get("/", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


def test_repeated_failures_are_throttled(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    for _ in range(5):
        assert client.post("/login", data={"password": "x"}, headers=ORIGIN).status_code == 401
    assert client.post("/login", data={"password": "x"}, headers=ORIGIN).status_code == 401
    blocked = client.post("/login", data={"password": PASSWORD}, headers=ORIGIN)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "31"
    clock.mono += 31
    assert client.post("/login", data={"password": PASSWORD}, headers=ORIGIN).status_code == 303


def test_sessions_expire(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    clock.now += timedelta(days=8)
    assert client.get("/").status_code == 303


def test_database_holds_only_hashes(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    cookie = client.cookies.get(COOKIE)
    assert cookie
    dump = "\n".join(sqlite3.connect(tmp_path / "state.db").iterdump())
    assert PASSWORD not in dump
    assert cookie not in dump
    assert "$argon2id$" in dump


def test_pages_have_no_inline_script(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    for page in (client.get("/setup").text,):
        assert re.findall(r"<script(?![^>]*\bsrc=)", page) == []


def test_referrer_policy_lets_browsers_send_origin(tmp_path: Path, clock: Clock) -> None:
    # With "no-referrer", browsers send "Origin: null" on same-site form posts.
    assert make(tmp_path, clock).get("/healthz").headers["referrer-policy"] == "same-origin"


def test_null_origin_is_refused(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    token = client.app.state.setup_token  # type: ignore[attr-defined]
    response = client.post(
        "/setup",
        data={"token": token, "password": PASSWORD, "confirm": PASSWORD},
        headers={"Origin": "null"},
    )
    assert response.status_code == 403


def test_health_answers_head_for_monitors(tmp_path: Path, clock: Clock) -> None:
    assert make(tmp_path, clock).head("/healthz").status_code == 200


def proxied(tmp_path: Path, clock: Clock, trusted: tuple[str, ...]) -> TestClient:
    app = create_app(
        WebSettings(tmp_path / "state.db", trusted_proxies=trusted),
        clock=clock,
        throttle=LoginThrottle(clock=clock.monotonic),
        start_worker=False,
    )
    # The test client plays the proxy at 192.0.2.10; the headers below describe the browser.
    return TestClient(
        app, base_url="http://googich.example", follow_redirects=False, client=("192.0.2.10", 5000)
    )


HTTPS_PROXY = {
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": "192.0.2.50",
    "Origin": "https://googich.example",
}


def test_behind_a_trusted_https_proxy_forms_work_and_cookies_are_secure(
    tmp_path: Path, clock: Clock
) -> None:
    client = proxied(tmp_path, clock, ("192.0.2.10",))
    token = client.app.state.setup_token  # type: ignore[attr-defined]
    response = client.post(
        "/setup",
        data={"token": token, "password": PASSWORD, "confirm": PASSWORD},
        headers=HTTPS_PROXY,
    )
    assert response.status_code == 303
    assert "secure" in response.headers["set-cookie"].lower()


def test_behind_an_untrusted_https_proxy_forms_still_work(tmp_path: Path, clock: Clock) -> None:
    client = proxied(tmp_path, clock, ())  # no GOOGICH_TRUSTED_PROXIES
    token = client.app.state.setup_token  # type: ignore[attr-defined]
    response = client.post(
        "/setup",
        data={"token": token, "password": PASSWORD, "confirm": PASSWORD},
        headers=HTTPS_PROXY,
    )
    assert response.status_code == 303
    # The https claim is not believed, so the cookie is not marked secure.
    assert "secure" not in response.headers["set-cookie"].lower()


def test_untrusted_proxy_gets_a_hint_in_the_log(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    client = proxied(tmp_path, clock, ())
    token = client.app.state.setup_token  # type: ignore[attr-defined]
    with caplog.at_level(logging.INFO, logger="googich.web"):
        client.post(
            "/setup",
            data={"token": token, "password": PASSWORD, "confirm": PASSWORD},
            headers=HTTPS_PROXY,
        )
    assert "GOOGICH_TRUSTED_PROXIES=192.0.2.10" in caplog.text  # the hint names the proxy


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("https://googich.example", "googich.example", True),  # proxy forwards plain http
        ("http://googich.example", "googich.example", True),
        ("http://192.0.2.10:8080", "192.0.2.10:8080", True),  # direct on the LAN
        ("https://googich.example:443", "googich.example", True),
        ("https://googich.example:8443", "googich.example:8443", True),
        ("https://evil.example", "googich.example", False),
        ("https://googich.example.evil.example", "googich.example", False),
        ("http://googich.example:8000", "googich.example", False),  # another service, same host
        ("http://192.0.2.10:9000", "192.0.2.10:8080", False),
        ("null", "googich.example", False),
        ("file://googich.example", "googich.example", False),
    ],
)
def test_origin_check(tmp_path: Path, clock: Clock, origin: str, host: str, allowed: bool) -> None:
    client = TestClient(
        create_app(WebSettings(tmp_path / "state.db"), clock=clock, start_worker=False),
        base_url=f"http://{host}",
        follow_redirects=False,
    )
    response = client.post("/login", data={"password": "x"}, headers={"Origin": origin})
    assert (response.status_code != 403) is allowed


def test_throttle_counts_attempts_as_they_start() -> None:
    """Guesses sent together all count: the sixth waits even if none has finished."""
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    assert [throttle.attempt("a") for _ in range(6)] == [0.0] * 6  # 5 free, the 6th sets a lock
    assert throttle.attempt("a") == 30.0
    assert throttle.attempt("b") == 0.0  # per address
    throttle.succeeded("a")
    assert throttle.attempt("a") == 0.0


def test_throttle_forgets_old_addresses() -> None:
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0], forget_after=60.0)
    for n in range(1001):
        throttle.attempt(f"10.0.{n // 256}.{n % 256}")
    now[0] = 120.0
    throttle.attempt("fresh")
    assert len(throttle._failures) == 1


def test_password_checks_are_limited() -> None:
    from googich_takeaway.web import auth

    stored = auth.hash_password("a long enough password")
    assert auth.verify_password_limited(stored, "a long enough password") is True
    for _ in range(auth.HASHING_AT_ONCE):
        auth._hashing.acquire()
    try:
        assert auth.verify_password_limited(stored, "x", wait=0.01) is None  # too busy
    finally:
        for _ in range(auth.HASHING_AT_ONCE):
            auth._hashing.release()


def test_large_or_unsized_bodies_are_refused_before_sign_in(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    big = client.post("/login", content=b"password=" + b"x" * 20_000, headers={
        **ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert big.status_code == 413
    chunked = client.post(
        "/login",
        content=iter([b"password=x"]),  # streamed: no Content-Length
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert chunked.status_code == 411


def test_only_exact_paths_are_open(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    client.cookies.clear()
    for path in ("/login-anything", "/setupx", "/healthz/extra"):
        assert client.get(path).status_code in (303, 404)
        assert client.get(path).headers.get("location", "/login") == "/login"


def test_dismiss_never_redirects_off_site(tmp_path: Path, clock: Clock) -> None:
    client = make(tmp_path, clock)
    set_up(client)
    csrf = csrf_of(client)
    for referer in ("http://testserver//evil.example/x", "http://testserver/\\evil.example"):
        response = client.post(
            "/updates/dismiss",
            data={"csrf_token": csrf},
            headers={**ORIGIN, "Referer": referer},
        )
        assert response.headers["location"] == "/"
