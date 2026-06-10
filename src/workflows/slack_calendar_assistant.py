"""Slack webhook conversational assistant for Google Calendar search/create."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

import mistralai.workflows as workflows
import mistralai.workflows.plugins.mistralai as workflows_mistralai
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)
from mistralai.workflows.plugins.webhook import (
    HTTPRequest,
    HTTPResponse,
    HTTPRouterWorkflow,
    post,
)
from pydantic import BaseModel, Field, ValidationError

GOOGLE_CALENDAR_CONNECTOR = connector("google_calendar")
SLACK_CONNECTOR = connector("slack")


class ParsedCalendarIntent(BaseModel):
    """Structured interpretation of a free-form Slack user request."""

    intent: Literal["search", "create", "unknown"]
    calendar: str = Field(default="primary")
    query: str | None = Field(default=None)
    # RFC3339 with timezone offset.
    start: str | None = Field(default=None)
    end: str | None = Field(default=None)

    # Event creation fields.
    title: str | None = Field(default=None)
    description: str | None = Field(default=None)
    attendees: list[str] = Field(default_factory=list)
    location: str | None = Field(default=None)

    # Optional message when the model thinks required data is missing.
    clarification: str | None = Field(default=None)


@workflows.activity(name="parse-calendar-intent-from-slack-message")
async def parse_calendar_intent_from_slack_message(
    user_message: str,
) -> ParsedCalendarIntent:
    """Use a Mistral model to parse user text into a calendar intent object."""
    now_utc = datetime.now(UTC).isoformat()

    request = workflows_mistralai.ChatCompletionRequest(
        model="mistral-medium-latest",
        messages=[
            workflows_mistralai.SystemMessage(
                content=(
                    "You are a strict parser for a Slack Google Calendar assistant. "
                    "Return ONLY valid JSON, no markdown. "
                    "Use this schema exactly: "
                    "{"
                    '"intent":"search|create|unknown",'
                    '"calendar":"string",'
                    '"query":"string|null",'
                    '"start":"RFC3339 datetime with timezone or null",'
                    '"end":"RFC3339 datetime with timezone or null",'
                    '"title":"string|null",'
                    '"description":"string|null",'
                    '"attendees":["email"],'
                    '"location":"string|null",'
                    '"clarification":"string|null"'
                    "}. "
                    "For search intent, extract query/start/end when possible. "
                    "For create intent, extract title/start/end and attendees when present. "
                    "If data is missing, keep missing fields null and set clarification. "
                    f"Current UTC timestamp: {now_utc}."
                )
            ),
            workflows_mistralai.UserMessage(content=user_message),
        ],
        response_format={"type": "json_object"},
    )

    result = await workflows_mistralai.mistralai_chat_complete(request)
    content = _extract_first_text_content(result)

    try:
        return ParsedCalendarIntent.model_validate_json(content)
    except ValidationError as exc:
        raise ValueError(f"Failed to parse model output as intent JSON: {exc}") from exc


@workflows.activity(name="search-google-calendar-by-intent")
async def search_google_calendar_by_intent(
    parsed_intent: ParsedCalendarIntent,
    google_calendar: ToolCallClient = Depends(GOOGLE_CALENDAR_CONNECTOR),
) -> dict[str, Any]:
    """Call Google Calendar search connector with parsed intent parameters."""
    return await google_calendar.call_tool(  # type: ignore[no-any-return]
        tool_name="google_calendar_search",
        arguments={
            "calendar": parsed_intent.calendar,
            "query": parsed_intent.query,
            "start": parsed_intent.start,
            "end": parsed_intent.end,
        },
    )


@workflows.activity(name="create-google-calendar-event-by-intent")
async def create_google_calendar_event_by_intent(
    parsed_intent: ParsedCalendarIntent,
    google_calendar: ToolCallClient = Depends(GOOGLE_CALENDAR_CONNECTOR),
) -> dict[str, Any]:
    """Create a calendar event from parsed data."""
    if not parsed_intent.title or not parsed_intent.start:
        raise ValueError("Missing required fields for event creation: title and start")

    arguments: dict[str, Any] = {
        "calendar": parsed_intent.calendar,
        "title": parsed_intent.title,
        "start": parsed_intent.start,
        "end": parsed_intent.end,
        "description": parsed_intent.description,
        "attendees": parsed_intent.attendees,
        "location": parsed_intent.location,
    }

    # Remove null values to avoid connector validation errors.
    arguments = {k: v for k, v in arguments.items() if v is not None}

    return await google_calendar.call_tool(  # type: ignore[no-any-return]
        tool_name="google_calendar_create_event",
        arguments=arguments,
    )


@workflows.activity(name="send-slack-reply")
async def send_slack_reply(
    channel_id: str,
    message: str,
    thread_ts: str | None,
    slack: ToolCallClient = Depends(SLACK_CONNECTOR),
) -> dict[str, Any]:
    """Send a reply in Slack, optionally threaded."""
    arguments: dict[str, Any] = {
        "channel_id": channel_id,
        "message": message,
    }
    if thread_ts:
        arguments["thread_ts"] = thread_ts

    return await slack.call_tool(  # type: ignore[no-any-return]
        tool_name="slack_send_message",
        arguments=arguments,
    )


@workflows.activity(name="format-search-reply")
async def format_search_reply(
    parsed_intent: ParsedCalendarIntent,
    search_response: dict[str, Any],
) -> str:
    """Format search results into a concise Slack response."""
    events = _extract_events(search_response)

    if not events:
        return "I could not find matching events for that request."

    header = "Here is what I found:"
    if parsed_intent.query:
        header = f"Here is what I found for *{parsed_intent.query}*:"

    lines: list[str] = [header, ""]
    for event in events[:10]:
        title = str(event.get("summary") or event.get("title") or "Untitled event")
        when = _format_time_range(_extract_time(event, "start"), _extract_time(event, "end"))
        lines.append(f"- *{when}* - *{title}*")

    return "\n".join(lines)


@workflows.activity(name="format-create-reply")
async def format_create_reply(
    parsed_intent: ParsedCalendarIntent,
    create_response: dict[str, Any],
) -> str:
    """Format calendar creation response into a user-facing Slack message."""
    link = _extract_link(create_response)
    title = parsed_intent.title or "your event"

    if link:
        return f"Created *{title}* successfully.\n{link}"
    return f"Created *{title}* successfully."


@workflows.workflow.define(
    name="slack-calendar-assistant-webhook",
    workflow_display_name="Slack Calendar Assistant Webhook",
    workflow_description=(
        "Slack webhook assistant that can search Google Calendar and create events "
        "from natural language."
    ),
    on_behalf_of=True,
)
@uses_connectors(GOOGLE_CALENDAR_CONNECTOR, SLACK_CONNECTOR)
class SlackCalendarAssistantWebhookWorkflow(HTTPRouterWorkflow):
    """Webhook router workflow for Slack message events."""

    @post("/slack/events")
    async def slack_events(self, request: HTTPRequest) -> HTTPResponse:
        payload = _extract_request_json(request)

        # Slack URL verification handshake.
        if payload.get("type") == "url_verification":
            return HTTPResponse(status_code=200, body={"challenge": payload.get("challenge")})

        if payload.get("type") != "event_callback":
            return HTTPResponse(status_code=200, body={"ok": True, "ignored": "unsupported_type"})

        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return HTTPResponse(status_code=200, body={"ok": True, "ignored": "invalid_event"})

        # Ignore bot and non-message events.
        if event.get("type") != "message" or event.get("subtype") is not None:
            return HTTPResponse(status_code=200, body={"ok": True, "ignored": "non_user_message"})

        user_text = str(event.get("text") or "").strip()
        channel_id = str(event.get("channel") or "").strip()
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip() or None

        if not user_text or not channel_id:
            return HTTPResponse(status_code=200, body={"ok": True, "ignored": "missing_text_or_channel"})

        parsed_intent = await parse_calendar_intent_from_slack_message(user_text)

        if parsed_intent.clarification:
            reply = parsed_intent.clarification
        elif parsed_intent.intent == "search":
            search_response = await search_google_calendar_by_intent(parsed_intent)
            reply = await format_search_reply(parsed_intent, search_response)
        elif parsed_intent.intent == "create":
            create_response = await create_google_calendar_event_by_intent(parsed_intent)
            reply = await format_create_reply(parsed_intent, create_response)
        else:
            reply = (
                "I can help you search or create Google Calendar events. "
                "Try: 'What are my afternoon meetings tomorrow?' or "
                "'Create a meeting for alice@example.com and me Friday at 4:30pm'."
            )

        await send_slack_reply(channel_id=channel_id, message=reply, thread_ts=thread_ts)
        return HTTPResponse(status_code=200, body={"ok": True})


def _extract_request_json(request: HTTPRequest) -> dict[str, Any]:
    """Extract JSON payload from webhook request in a tolerant way."""
    body = getattr(request, "body", None)
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}

    # Some webhook request models expose parsed JSON on alternate attributes.
    for attr_name in ("json", "data", "payload"):
        value = getattr(request, attr_name, None)
        if isinstance(value, dict):
            return value
    return {}


def _extract_first_text_content(chat_result: Any) -> str:
    """Extract first text chunk from chat completion response."""
    choices = getattr(chat_result, "choices", None)
    if not choices:
        raise ValueError("Model response has no choices")

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None:
        raise ValueError("Model response choice has no message")

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        for part in content:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return text

    raise ValueError("Model response message has no text content")


def _extract_events(search_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Support common event list keys across connector response variants."""
    for key in ("events", "items", "results", "data"):
        value = search_response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _extract_time(event: dict[str, Any], key: str) -> str | None:
    """Extract datetime-like value from nested or flat event fields."""
    value = event.get(key)
    if isinstance(value, dict):
        dt = value.get("dateTime") or value.get("datetime") or value.get("date")
        if isinstance(dt, str):
            return dt
    if isinstance(value, str):
        return value
    return None


def _format_time_range(start_raw: str | None, end_raw: str | None) -> str:
    """Format a readable start/end range for Slack markdown."""
    if not start_raw:
        return "Time TBD"

    start_label = _format_time_value(start_raw)
    if not end_raw:
        return start_label

    return f"{start_label} to {_format_time_value(end_raw)}"


def _format_time_value(raw: str) -> str:
    """Format an ISO datetime string into UTC HH:MM when parsable."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.strftime("%H:%M")
        return dt.astimezone(UTC).strftime("%H:%M UTC")
    except ValueError:
        return raw


def _extract_link(payload: dict[str, Any]) -> str | None:
    """Extract link/permalink fields from connector responses."""
    for key in ("htmlLink", "permalink", "message_link", "link", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None
