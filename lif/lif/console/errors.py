"""Human errors for the console API (spec §81): every non-2xx /api body is {"error": HumanError}.

Routes raise `HumanHTTPError` (or call `human()` to build one); app.py installs `install(app)` so
that HTTPException, validation errors and unhandled exceptions on /api paths come out the same way.
The UI renders these with HumanErrorCard and never shows a bare status code.

Owner: ARCH (complete, shared by every route module).
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from lif.common import log
from lif.console.contracts import ErrorBody, HumanError, HumanErrorAction, TechDetail

LOG = log.get("lif.console")


class HumanHTTPError(Exception):
    """An API failure with a human explanation. `status` is for HTTP semantics only, never shown."""

    def __init__(self, status: int, error: HumanError, headers: dict[str, str] | None = None):
        super().__init__(error.title)
        self.status, self.error, self.headers = status, error, headers


def human(status: int, title: str, impact: str = "", next_step: str = "",
          actions: list[tuple[str, str]] | None = None, tech: dict[str, Any] | None = None) -> HumanHTTPError:
    """Build (not raise) a HumanHTTPError: `raise human(409, "This job can't be paused", …)`.

    actions: (label, action) pairs; action is a client verb ("retry", "details", "login", "reload")
    or an app path starting with "/".
    """
    return HumanHTTPError(status, HumanError(
        title=title, impact=impact, next_step=next_step,
        actions=[HumanErrorAction(label=lbl, action=act) for lbl, act in actions or []],
        tech=[TechDetail(label=k, value=str(v)) for k, v in (tech or {}).items()]))


def storage(exc: Exception) -> HumanHTTPError:
    """The console's own SQLite is locked, full or unwritable: a 503 that says so (never a generic 500)."""
    return human(503, "Labzilla can't reach its own storage", "Nothing was changed.", "Retry in a moment.",
                 [("Retry", "retry")], {"error": type(exc).__name__})


def body(error: HumanError) -> dict[str, Any]:
    return ErrorBody(error=error).model_dump()


def response(exc: HumanHTTPError) -> JSONResponse:
    return JSONResponse(body(exc.error), status_code=exc.status, headers=exc.headers)


# Defaults for status codes raised without a human explanation (framework 404/405, bare HTTPException).
_GENERIC: dict[int, tuple[str, str, str]] = {
    400: ("That request wasn't understood", "Nothing was changed.", "Check the input and try again."),
    401: ("Sign in to continue", "Your session has ended or this device isn't paired.", "Sign in again."),
    403: ("Not allowed from this session", "Nothing was changed.",
          "Use an admin session on a desktop for this action."),
    404: ("Not found", "This item doesn't exist or is no longer available.", "Go back and refresh the list."),
    405: ("That action isn't available here", "Nothing was changed.", "Refresh the page and try again."),
    409: ("That can't be done right now", "Nothing was changed.", "Refresh to see the current state."),
    422: ("Some details are missing or invalid", "Nothing was changed.", "Check the form and try again."),
    429: ("Too many attempts", "Labzilla is slowing requests from this device to keep it safe.",
          "Wait a minute and try again."),
    502: ("A Labzilla service didn't answer", "Some information may be out of date.", "Retry in a moment."),
    503: ("Labzilla is temporarily unavailable", "Some features may not work right now.", "Retry in a moment."),
}


def generic(status: int, detail: Any = None) -> HumanHTTPError:
    title, impact, nxt = _GENERIC.get(status, _GENERIC[503] if status >= 500 else _GENERIC[400])
    tech = {"status": status}
    if isinstance(detail, str) and detail and detail not in ("Not Found", "Method Not Allowed"):
        tech["detail"] = detail[:300]
    actions = [("Sign in", "login")] if status == 401 else [("Retry", "retry")] if status >= 500 else []
    return human(status, title, impact, nxt, actions, tech)


def install(app: FastAPI) -> None:
    """Register handlers so /api errors are always ErrorBody. HTTPException (and the SPA fallback for
    non-/api 404s) is handled in app.py, which needs to know about the UI directory."""

    @app.exception_handler(HumanHTTPError)
    async def _human(_: Request, exc: HumanHTTPError) -> JSONResponse:
        return response(exc)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()[:5])
        return response(human(422, "Some details are missing or invalid", "Nothing was changed.",
                              "Check the form and try again.", tech={"fields": fields}))

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> JSONResponse:
        log.event(LOG, "unhandled", path=request.url.path, error=type(exc).__name__)
        return response(human(500, "Something went wrong in Labzilla", "This action didn't complete.",
                              "Retry; if it keeps happening, check System → Logs.", [("Retry", "retry")],
                              {"error": type(exc).__name__}))
