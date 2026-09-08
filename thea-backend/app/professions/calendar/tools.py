"""
`calendar` profession — list, create, and (rarely) delete calendar events.

Same provider-swappable pattern as the `email` profession. No calendar
provider is wired up yet (`UnconfiguredCalendarClient`) — every call fails
the same explicit, non-retryable way (error_type="not_configured") until
one is chosen and implemented in `_get_client()`.

`calendar.delete_event` mirrors `email.delete_email`'s narrowly-typed
confirmation pattern — any destructive tool in any profession should look
like this, not just the handoff doc's original example.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import get_settings
from app.models import ToolExecutionResult
from app.tools.registry import register_tool


class CalendarClient(Protocol):
    def list_events(self, start: datetime, end: datetime, calendar_id: str) -> list[dict]: ...

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        description: str | None,
        calendar_id: str,
    ) -> str:
        """Returns the provider's event id."""
        ...

    def delete_event(self, event_id: str, calendar_id: str) -> None: ...


class _NotConfiguredError(Exception):
    pass


class UnconfiguredCalendarClient:
    """Placeholder until a real provider (Google Calendar / Graph / CalDAV)
    is chosen. Fails loudly and specifically rather than pretending to
    succeed."""

    def _err(self) -> _NotConfiguredError:
        provider = get_settings().calendar_provider or "<unset>"
        return _NotConfiguredError(
            f"No calendar provider is wired up (CALENDAR_PROVIDER={provider}). "
            "Implement a CalendarClient for the chosen provider and return it "
            "from _get_client()."
        )

    def list_events(self, start, end, calendar_id):
        raise self._err()

    def create_event(self, title, start, end, attendees, description, calendar_id):
        raise self._err()

    def delete_event(self, event_id, calendar_id):
        raise self._err()


def _get_client() -> CalendarClient:
    return UnconfiguredCalendarClient()


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


class ListEventsArgs(BaseModel):
    start: datetime
    end: datetime
    calendar_id: str = "primary"

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, end: datetime, info) -> datetime:
        start = info.data.get("start")
        if start is not None and end <= start:
            raise ValueError("end must be after start")
        return end


async def _list_events(args: ListEventsArgs) -> ToolExecutionResult:
    try:
        events = _get_client().list_events(args.start, args.end, args.calendar_id)
    except _NotConfiguredError as exc:
        return ToolExecutionResult(success=False, error=str(exc), error_type="not_configured")
    return ToolExecutionResult(success=True, result={"events": events})


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


class CreateEventArgs(BaseModel):
    title: str
    start: datetime
    end: datetime
    attendees: list[EmailStr] = Field(default_factory=list)
    description: str | None = None
    calendar_id: str = "primary"

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, end: datetime, info) -> datetime:
        start = info.data.get("start")
        if start is not None and end <= start:
            raise ValueError("end must be after start")
        return end


async def _create_event(args: CreateEventArgs) -> ToolExecutionResult:
    try:
        event_id = _get_client().create_event(
            title=args.title,
            start=args.start,
            end=args.end,
            attendees=[str(a) for a in args.attendees],
            description=args.description,
            calendar_id=args.calendar_id,
        )
    except _NotConfiguredError as exc:
        return ToolExecutionResult(success=False, error=str(exc), error_type="not_configured")
    return ToolExecutionResult(success=True, result={"event_id": event_id, "title": args.title})


# ---------------------------------------------------------------------------
# delete_event
# ---------------------------------------------------------------------------


class DeleteEventArgs(BaseModel):
    event_id: str
    calendar_id: str = "primary"
    confirm_delete: Literal[True] = Field(
        ...,
        description="Must be the literal boolean true. No default — PLAN must "
        "explicitly assert intent to delete for this step to validate at all.",
    )


async def _delete_event(args: DeleteEventArgs) -> ToolExecutionResult:
    try:
        _get_client().delete_event(args.event_id, args.calendar_id)
    except _NotConfiguredError as exc:
        return ToolExecutionResult(success=False, error=str(exc), error_type="not_configured")
    return ToolExecutionResult(success=True, result={"deleted_event_id": args.event_id})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_tool(
    name="calendar.list_events",
    args_schema=ListEventsArgs,
    handler=_list_events,
    description="List calendar events within a start/end window.",
    skill_doc=(
        "calendar.list_events(start, end, calendar_id='primary'). No "
        "provider configured yet — fails with error_type=not_configured "
        "until CALENDAR_PROVIDER is wired up."
    ),
)

register_tool(
    name="calendar.create_event",
    args_schema=CreateEventArgs,
    handler=_create_event,
    description="Create a new calendar event, optionally inviting attendees.",
    skill_doc=(
        "calendar.create_event(title, start, end, attendees=[], "
        "description=None, calendar_id='primary'). end must be after "
        "start (schema-enforced). No provider configured yet."
    ),
)

register_tool(
    name="calendar.delete_event",
    args_schema=DeleteEventArgs,
    handler=_delete_event,
    description="Permanently delete a single calendar event. Destructive — requires explicit confirmation.",
    skill_doc=(
        "calendar.delete_event(event_id, calendar_id='primary', "
        "confirm_delete: true). confirm_delete has no default and only "
        "accepts literal true. No provider configured yet."
    ),
)
