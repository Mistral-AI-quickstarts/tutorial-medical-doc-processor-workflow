"""Scheduled workflow: send today's Google Calendar agenda to Slack DM."""

from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.models import (
    ScheduleDefinition,
    ScheduleOverlapPolicy,
    SchedulePolicy,
)
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)
from pydantic import BaseModel, Field

GOOGLE_CALENDAR_CONNECTOR = connector("google_calendar")
SLACK_CONNECTOR = connector("slack")

PARIS_TZ = ZoneInfo("Europe/Paris")

# Docs state cron is UTC-based. 05:00 UTC maps to 07:00 Paris during DST.
# Adjust to 06:00 UTC outside DST if you need strict all-year 07:00 local delivery.
DAILY_WEEKDAY_SCHEDULE = ScheduleDefinition(
    input={"calendar": "primary", "query": None},
    cron_expressions=["0 5 * * 1-5"],
    policy=SchedulePolicy(
        catchup_window_seconds=86_400,
        overlap=ScheduleOverlapPolicy.SKIP,
    ),
)


class DailyScheduleInput(BaseModel):
    calendar: str = Field(default="primary")
    query: str | None = Field(default=None)


@workflows.activity(name="build-today-window")
async def build_today_window() -> dict[str, str]:
    """Build RFC3339 UTC bounds for the current Paris calendar day."""
    now_paris = datetime.now(PARIS_TZ)
    day_start_paris = datetime.combine(
        now_paris.date(),
        time.min,
        tzinfo=PARIS_TZ,
    )
    day_end_paris = day_start_paris + timedelta(days=1)

    return {
        "start": day_start_paris.astimezone(UTC).isoformat(),
        "end": day_end_paris.astimezone(UTC).isoformat(),
    }


@workflows.activity(name="google-calendar-search")
async def google_calendar_search(
    calendar: str,
    start: str,
    end: str,
    query: str | None,
    google_calendar: ToolCallClient = Depends(GOOGLE_CALENDAR_CONNECTOR),
) -> dict[str, Any]:
    """Search Google Calendar for events in the target date window."""
    return await google_calendar.call_tool(  # type: ignore[no-any-return]
        tool_name="google_calendar_search",
        arguments={
            "calendar": calendar,
            "start": start,
            "end": end,
            "query": query,
        },
    )


@workflows.activity(name="resolve-slack-user-id")
async def resolve_slack_user_id() -> str:
    """Resolve Slack DM recipient from environment at activity runtime."""
    user_id = os.environ.get("SLACK_SELF_USER_ID", "").strip()
    if not user_id:
        raise ValueError(
            "Missing required environment variable SLACK_SELF_USER_ID"
        )
    return user_id


@workflows.activity(name="format-daily-schedule-message")
async def format_daily_schedule_message(
    calendar_search_response: dict[str, Any],
) -> str:
    """Format a Slack markdown agenda from the connector response."""
    events = _extract_events(calendar_search_response)

    today_paris = datetime.now(PARIS_TZ).strftime("%A, %d %B %Y")
    header = f"*Daily schedule*\n_{today_paris} (Europe/Paris)_"

    if not events:
        return f"{header}\n\nNo events found for today."

    lines: list[str] = [header, ""]
    for event in events:
        title = str(event.get("summary") or event.get("title") or "Untitled event")
        start_raw = _extract_time(event, "start")
        end_raw = _extract_time(event, "end")
        when = _format_time_range(start_raw, end_raw)
        location = str(event.get("location") or "").strip()

        if location:
            lines.append(f"- *{when}* - *{title}* ({location})")
        else:
            lines.append(f"- *{when}* - *{title}*")

    return "\n".join(lines)


@workflows.activity(name="send-slack-dm")
async def send_slack_dm(
    channel_id: str,
    message: str,
    slack: ToolCallClient = Depends(SLACK_CONNECTOR),
) -> dict[str, Any]:
    """Send the agenda message as a DM and return Slack API response."""
    return await slack.call_tool(  # type: ignore[no-any-return]
        tool_name="slack_send_message",
        arguments={
            "channel_id": channel_id,
            "message": message,
        },
    )


@workflows.workflow.define(
    name="daily-gcal-to-slack-dm",
    workflow_display_name="Daily GCal to Slack DM",
    workflow_description=(
        "Search today's Google Calendar events and DM the schedule in Slack."
    ),
    on_behalf_of=True,
)
@uses_connectors(GOOGLE_CALENDAR_CONNECTOR, SLACK_CONNECTOR)
class DailyGoogleCalendarToSlackWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: DailyScheduleInput) -> dict[str, Any]:
        window = await build_today_window()
        calendar_result = await google_calendar_search(
            calendar=input.calendar,
            start=window["start"],
            end=window["end"],
            query=input.query,
        )
        slack_user_id = await resolve_slack_user_id()
        message = await format_daily_schedule_message(calendar_result)
        slack_result = await send_slack_dm(slack_user_id, message)

        return {
            "window": window,
            "calendar": calendar_result,
            "slack": slack_result,
            "message": message,
            "message_link": _extract_message_link(slack_result),
            "slack_user_id": slack_user_id,
        }


def _extract_events(calendar_search_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Support common event list field names from connector payloads."""
    for key in ("events", "items", "results", "data"):
        value = calendar_search_response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _extract_time(event: dict[str, Any], key: str) -> str | None:
    """Extract a start/end value from common Google Calendar event shapes."""
    value = event.get(key)
    if isinstance(value, dict):
        dt = value.get("dateTime") or value.get("datetime")
        if isinstance(dt, str):
            return dt
        d = value.get("date")
        if isinstance(d, str):
            return d
    if isinstance(value, str):
        return value
    return None


def _format_time_range(start_raw: str | None, end_raw: str | None) -> str:
    """Render a human-friendly time range for Slack output."""
    if not start_raw:
        return "Time TBD"

    start_label = _format_time_value(start_raw)
    if not end_raw:
        return start_label

    end_label = _format_time_value(end_raw)
    return f"{start_label} to {end_label}"


def _format_time_value(raw: str) -> str:
    """Format an ISO-like timestamp into Paris local HH:MM when possible."""
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.strftime("%H:%M")
        return dt.astimezone(PARIS_TZ).strftime("%H:%M")
    except ValueError:
        return raw


def _extract_message_link(slack_response: dict[str, Any]) -> str | None:
    """Find a permalink-like field from a Slack send-message response."""
    for key in ("message_link", "permalink", "link", "url"):
        value = slack_response.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@workflows.workflow.define(
    name="daily-gcal-to-slack-dm-scheduler",
    workflow_display_name="Daily GCal to Slack DM Scheduler",
    workflow_description=(
        "Scheduler that triggers the daily Google Calendar to Slack DM workflow."
    ),
    schedules=[DAILY_WEEKDAY_SCHEDULE],
)
class DailyGoogleCalendarToSlackSchedulerWorkflow:
    """Scheduler workflow that runs on cron and calls the main workflow."""

    @workflows.workflow.entrypoint
    async def run(self, input: DailyScheduleInput) -> dict[str, Any]:
        """Execute the main workflow as a child with user identity context."""
        return await workflows.workflow.execute_workflow(
            DailyGoogleCalendarToSlackWorkflow,
            input,
        )
