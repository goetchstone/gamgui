"""The Classroom screen (/classroom), read only (plan 2026-11-11 C1.3).

One search box takes a class's name or a teacher's address. An address reads that teacher's classes live (`gam print
courses teacher <address>`, archived ones too); anything else searches the cached index of active and not-yet-accepted
classes (`AppState.courses`). Both are paged server-side. A class opens as a card with its roster.

GamGUI never writes to Classroom, so nothing here posts: every route is a GET. Classroom support hasn't run on a real
school domain (the operator has none), and the page says so once.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Sequence, Union

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import clock
from ...core.classroom import Course, Roster
from ...core.gam.commands import is_course_id, is_one_address
from ...core.gam.errors import GAMError, GAMErrorKind
from ...core.phishing import Incomplete
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, app_state, as_of, friendly

PAGE_SIZE = 25          # classes per page of results; the list scrolls inside the fixed window
ROSTER_SHOWN = 200      # people listed per side of a class card, with a count past it (#9)
TEACHER_STATES = ("active", "provisioned", "archived")   # a teacher's classes: last year's too
# A failed index read is said again, not re-run, for this long: on a domain without Classroom each typing pause once ran
# a domain-wide read that could only fail. Refresh tries at once.
INDEX_RETRY = 60.0

_PAGE = "classroom.html"
_RESULTS = "_classroom_results.html"
_CARD = "_classroom_card.html"

router = APIRouter(prefix="/classroom")


def _why(exc: Exception, about: str = "") -> str:
    """A failed Classroom read in words. A missing scope points at Setup; an address Classroom doesn't know is named."""
    if isinstance(exc, Incomplete):
        return str(exc)
    if isinstance(exc, GAMError) and exc.kind is GAMErrorKind.SCOPE_MISSING:
        return ("The admin token can't read Classroom. On Setup, the delegation step names the Classroom scopes to tick in "
                "`gam oauth create`. Classroom is only on school (Workspace for Education) domains.")
    if isinstance(exc, GAMError) and exc.kind is GAMErrorKind.NOT_FOUND and about:
        return f"Classroom doesn't know {about}."
    return "Couldn't read Classroom. " + friendly(exc)


def _owners(st) -> Dict[str, str]:
    """Owner ID → address, from the directory as already cached: the index is read without `owneremail`. Never a fetch (a
    cold cache would cost a full `print users` to label one column); empty when nothing is cached."""
    users = st.user_cache.cached or []
    return {str(u.raw.get("id")): u.primary_email for u in users if u.raw.get("id")}


def _page(rows: List[Course], page: int) -> tuple:
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(max(1, page), pages)
    start = (page - 1) * PAGE_SIZE
    return rows[start:start + PAGE_SIZE], page, pages, start


async def _results(st, q: str, page: int = 1, refresh: bool = False, live: bool = True) -> dict:
    """A page of classes. An address is a live read of that teacher's classes, run only when asked (``live``: Enter, a
    pager click, a link), never on each pause while it's typed."""
    q = q.strip()
    ctx: dict = {"q": q, "courses": [], "total": 0, "page": 1, "pages": 1, "start": 0, "error": "", "by_teacher": False,
                 "waiting": False}
    if "@" in q:
        if not live:
            ctx["waiting"] = True
            return ctx
        if not is_one_address(q):
            ctx["error"] = f"“{q}” isn't one address. Type one teacher's full address, or part of a class name."
            return ctx
        try:
            rows = await st.connector.list_courses(teacher=q, states=TEACHER_STATES)
        except Exception as exc:  # noqa: BLE001 — any failure is said in words, not a 500
            ctx["error"] = _why(exc, q)
            return ctx
        ctx["by_teacher"] = True
    else:
        failed = st.course_index_failed
        if failed and not refresh and clock.now() - failed[0] < INDEX_RETRY:
            ctx["error"], ctx["retry"] = failed[1], True   # said again without another domain-wide read per keystroke
            return ctx
        try:
            index = await st.courses(force=refresh, stale_ok=True)
        except Exception as exc:  # noqa: BLE001
            st.course_index_failed = (clock.now(), _why(exc))
            ctx["error"], ctx["retry"] = _why(exc), True
            return ctx
        st.course_index_failed = None
        ql = q.lower()
        rows = [c for c in index if not ql or ql in c.name.lower() or ql in c.section.lower() or ql in c.room.lower()]
        ctx.update(as_of(st.course_cache), index_total=len(index))
    shown, page, pages, start = _page(rows, page)
    owners = _owners(st)
    ctx.update(courses=[(c, c.owner_email or owners.get(c.owner_id, "")) for c in shown], total=len(rows),
               page=page, pages=pages, start=start)
    return ctx


async def _card(st, course_id: str) -> dict:
    ctx: dict = {"course_id": course_id, "course": None, "owner": "", "roster": None, "error": "", "roster_error": "",
                 "shown": ROSTER_SHOWN}
    if not is_course_id(course_id):
        ctx["error"] = "That isn't a Classroom class ID."
        return ctx
    reads: Sequence[Any] = await asyncio.gather(st.connector.get_course(course_id), st.connector.course_roster(course_id),
                                            return_exceptions=True)
    read_course, read_roster = reads
    if isinstance(read_course, BaseException):
        if not isinstance(read_course, Exception):
            raise read_course            # a cancellation, not a failed read
        ctx["error"] = _why(read_course, f"class {course_id}")
        return ctx
    course: Course = read_course
    roster: Union[Roster, BaseException] = read_roster
    ctx["course"] = course
    ctx["owner"] = course.owner_email or _owners(st).get(course.owner_id, "")
    if isinstance(roster, BaseException):
        if not isinstance(roster, Exception):
            raise roster
        ctx["roster_error"] = _why(roster, f"class {course_id}")
    else:
        ctx["roster"] = roster
    return ctx


@router.get("", response_class=HTMLResponse)
async def page(request: Request, q: str = "", course: str = "") -> HTMLResponse:
    """The screen. Its class list loads after the page (`/search`, on load): a cold index is a domain-wide read."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _PAGE, {"connected": False})
    course = course.strip()
    ctx: dict = {"connected": True, "selected": course, "q": q.strip()}
    if course:
        ctx["card"] = await _card(st, course)
    return TEMPLATES.TemplateResponse(request, _PAGE, ctx)


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", page: str = "1", refresh: str = "0", live: str = "1",
                 selected: str = "") -> HTMLResponse:
    """One page of classes matching a name, or a teacher's classes. Typing sends ``live=0``: an address waits for Enter.
    ``selected`` is the open class, kept marked in the new list."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _RESULTS, {"q": q, "courses": [], "error": NOT_CONNECTED})
    number = int(page) if page.isdigit() else 1
    ctx = await _results(st, q, number, refresh == "1", live != "0")
    return TEMPLATES.TemplateResponse(request, _RESULTS, {**ctx, "selected": selected.strip()})


@router.get("/class", response_class=HTMLResponse)
async def card(request: Request, id: str = "") -> HTMLResponse:
    """A class's card: its details and roster, read live."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _CARD, {"card": {"error": NOT_CONNECTED}})
    return TEMPLATES.TemplateResponse(request, _CARD, {"card": await _card(st, id.strip())})
