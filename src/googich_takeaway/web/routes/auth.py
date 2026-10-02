"""Signing in: first-run setup, login, logout and the health check."""

from typing import Annotated

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from googich_takeaway import __version__
from googich_takeaway.web import auth
from googich_takeaway.web.common import (
    COOKIE,
    _client,
    _start_session,
    _throttled,
    log,
)
from googich_takeaway.web.shared import Shared, StateDep


def register(app: FastAPI, web: Shared) -> None:
    templates = web.templates
    clock = web.clock
    login_throttle = web.login_throttle

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request, state: StateDep) -> Response:
        if state.password_hash() is not None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "setup.html", {"error": None})

    @app.post("/setup", response_class=HTMLResponse)
    def setup(
        request: Request,
        state: StateDep,
        token: Annotated[str, Form()],
        password: Annotated[str, Form()],
        confirm: Annotated[str, Form()],
    ) -> Response:
        if state.password_hash() is not None or not app.state.setup_token:
            return RedirectResponse("/login", status_code=303)
        address = _client(request)
        wait = login_throttle.attempt(address)
        if wait:
            return _throttled(request, templates, "setup.html", wait)
        if not auth.same(token.strip(), app.state.setup_token):
            return templates.TemplateResponse(
                request, "setup.html", {"error": "That setup token is not right."}, status_code=400
            )
        problem = auth.password_problem(password, confirm)
        if problem:
            return templates.TemplateResponse(
                request, "setup.html", {"error": problem}, status_code=400
            )
        state.set_password_hash(auth.hash_password(password), clock())
        app.state.setup_token = None
        login_throttle.succeeded(address)
        log.warning("First-run setup complete; password set")
        return _start_session(request, state, clock())

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, state: StateDep) -> Response:
        if state.password_hash() is None:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, state: StateDep, password: Annotated[str, Form()]) -> Response:
        stored = state.password_hash()
        if stored is None:
            return RedirectResponse("/setup", status_code=303)
        address = _client(request)
        wait = login_throttle.attempt(address)
        if wait:
            return _throttled(request, templates, "login.html", wait)
        verified = auth.verify_password_limited(stored, password)
        if verified is None:
            return _throttled(request, templates, "login.html", 5.0)
        if not verified:
            log.warning("Failed login from %s", address)
            return templates.TemplateResponse(
                request, "login.html", {"error": "Wrong password."}, status_code=401
            )
        login_throttle.succeeded(address)
        old = request.cookies.get(COOKIE)
        if old:
            state.delete_session(auth.token_hash(old))  # never reuse a pre-login session
        return _start_session(request, state, clock())

    @app.post("/logout")
    def logout(request: Request, state: StateDep) -> Response:
        token = request.cookies.get(COOKIE)
        if token:
            state.delete_session(auth.token_hash(token))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response
