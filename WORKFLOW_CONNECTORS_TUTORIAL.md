# Build a Google Calendar Daily Schedule and Slack Assistant Workflow

**Title:** Build a Google Calendar Daily Schedule and Slack Assistant Workflow
**Time to complete:** 30–45 minutes
**Audience:** Intermediate Python developers interested in workflow automation with Mistral Workflows and Connectors
**Product/feature:** Mistral Workflows, Connectors (Google Calendar, Slack)
**Next steps:**
- [Mistral Workflows documentation](https://docs.mistral.ai/workflows/)
- [Connector reference](https://docs.mistral.ai/workflows/connectors/)
- [Slack API docs](https://api.slack.com/)
- [Google Calendar API docs](https://developers.google.com/calendar/api)

---

# Build a Google Calendar Daily Schedule and Slack Assistant Workflow

Welcome! In this tutorial, you'll build two powerful, real-world workflows using Mistral Workflows and Connectors:

- **Google Calendar Daily Schedule:** Automatically sends your day's agenda to you on Slack every morning.
- **Slack Calendar Assistant:** Lets you search and create Google Calendar events by chatting with a Slack bot.

We'll walk through every step, from project setup to writing and understanding each activity and workflow. By the end, you'll have a working automation and a solid grasp of how Connectors supercharge your workflow projects.

---

## What Are Connectors?

Connectors in Mistral Workflows are prebuilt integrations that handle authentication, API calls, and error handling for popular services like Google Calendar and Slack. They let you focus on your business logic, not on OAuth flows or HTTP plumbing. With Connectors, you can:

- Securely manage API credentials and tokens
- Call external APIs with a single line of code
- Chain together services in robust, fault-tolerant workflows

---

## What You'll Build

- **Google Calendar Daily Schedule:** A scheduled workflow that fetches today's events from your Google Calendar and sends a formatted agenda to your Slack DM every weekday morning.
- **Slack Calendar Assistant:** A conversational webhook workflow that listens for Slack messages, parses user intent, and either searches your calendar or creates new events—all from Slack.

---

## Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (for fast dependency management)
- A Mistral Workflows account and API key
- Google Calendar and Slack accounts (with API access)
- Basic familiarity with Python async/await

---

## 1. Project Setup

Let's start by creating a new Workflows project using the official quickstart command.

```sh
uv pip install mistralai[workflows]  # Installs Mistral Workflows and CLI
mistral workflows quickstart your-first-workflow
cd your-first-workflow
```

This scaffolds a new project with the recommended structure and dependencies.

---

## 2. Add Required Dependencies

If you haven't already, add the following to your `pyproject.toml`:

```toml
[project.optional-dependencies]
workflows = [
    "mistralai[workflows]",
    "pydantic",
    "zoneinfo; python_version<'3.9'"
]
```

Then install with:

```sh
uv pip install -r requirements.txt
```

---

## 3. Create the Google Calendar Daily Schedule Workflow

Let's build the workflow that sends your daily agenda to Slack.

### 3.1. Create the Workflow File

Create a new file at `src/workflows/gcal_daily_schedule.py`.

### 3.2. Add Imports

Paste in the following imports at the top of your file:

```python
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
```

**What this does:**
These imports bring in all the tools you'll need: datetime handling, type hints, the Mistral Workflows framework, connectors for Google Calendar and Slack, and Pydantic for input validation.

### 3.3. Define Connectors and Schedule

Add the following code:

```python
GOOGLE_CALENDAR_CONNECTOR = connector("google_calendar")
SLACK_CONNECTOR = connector("slack")
PARIS_TZ = ZoneInfo("Europe/Paris")

DAILY_WEEKDAY_SCHEDULE = ScheduleDefinition(
    input={"calendar": "primary", "query": None},
    cron_expressions=["0 5 * * 1-5"],
    policy=SchedulePolicy(
        catchup_window_seconds=86_400,
        overlap=ScheduleOverlapPolicy.SKIP,
    ),
)
```

**What this does:**
- Registers the Google Calendar and Slack connectors for use in activities.
- Sets the Paris timezone for localizing event times.
- Defines a schedule to run the workflow every weekday at 07:00 Paris time (05:00 UTC during DST).

### 3.4. Define the Input Model

```python
class DailyScheduleInput(BaseModel):
    calendar: str = Field(default="primary")
    query: str | None = Field(default=None)
```

**What this does:**
Defines the expected input for the workflow—by default, it uses your primary calendar and no search query.

### 3.5. Add Activities

Activities are the building blocks of your workflow. Add these one by one, reading the explanations as you go.

#### Build the Date Window

```python
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
```

**What this does:**
Calculates the UTC start and end times for "today" in Paris, so your agenda always covers the correct local day.

#### Search Google Calendar

```python
@workflows.activity(name="google-calendar-search")
async def google_calendar_search(
    calendar: str,
    start: str,
    end: str,
    query: str | None,
    google_calendar: ToolCallClient = Depends(GOOGLE_CALENDAR_CONNECTOR),
) -> dict[str, Any]:
    """Search Google Calendar for events in the target date window."""
    return await google_calendar.call_tool(
        tool_name="google_calendar_search",
        arguments={
            "calendar": calendar,
            "start": start,
            "end": end,
            "query": query,
        },
    )
```

**What this does:**
Calls the Google Calendar connector to fetch events for the specified window and query.

#### Resolve Slack User ID

```python
@workflows.activity(name="resolve-slack-user-id")
async def resolve_slack_user_id() -> str:
    """Resolve Slack DM recipient from environment at activity runtime."""
    user_id = os.environ.get("SLACK_SELF_USER_ID", "").strip()
    if not user_id:
        raise ValueError(
            "Missing required environment variable SLACK_SELF_USER_ID"
        )
    return user_id
```

**What this does:**
Looks up your Slack user ID from an environment variable so the workflow knows where to send the DM.

#### Format the Slack Message

```python
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
```

**What this does:**
Formats the list of events into a readable Slack message, including times and locations.

#### Send the Slack DM

```python
@workflows.activity(name="send-slack-dm")
async def send_slack_dm(
    channel_id: str,
    message: str,
    slack: ToolCallClient = Depends(SLACK_CONNECTOR),
) -> dict[str, Any]:
    """Send the agenda message as a DM and return Slack API response."""
    return await slack.call_tool(
        tool_name="slack_send_message",
        arguments={
            "channel_id": channel_id,
            "message": message,
        },
    )
```

**What this does:**
Uses the Slack connector to send your formatted agenda as a DM.

### 3.6. Define the Workflow

Now, tie it all together:

```python
@workflows.workflow.define(
    name="daily-gcal-to-slack-dm",
    workflow_display_name="Daily GCal to Slack DM",
    workflow_description=(
        "Search today's Google Calendar events and DM the schedule in Slack."
    ),
    on_behalf_of=True,
    schedules=[DAILY_WEEKDAY_SCHEDULE],
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
```

**What this does:**
Defines the main workflow class, wires up all the activities, and sets the schedule. The workflow fetches events, formats the message, and sends it to Slack.

### 3.7. Add Helper Functions

Paste these at the bottom of your file:

```python
def _extract_events(calendar_search_response: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("events", "items", "results", "data"):
        value = calendar_search_response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []

def _extract_time(event: dict[str, Any], key: str) -> str | None:
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
    if not start_raw:
        return "Time TBD"
    start_label = _format_time_value(start_raw)
    if not end_raw:
        return start_label
    end_label = _format_time_value(end_raw)
    return f"{start_label} to {end_label}"

def _format_time_value(raw: str) -> str:
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.strftime("%H:%M")
        return dt.astimezone(PARIS_TZ).strftime("%H:%M")
    except ValueError:
        return raw

def _extract_message_link(slack_response: dict[str, Any]) -> str | None:
    for key in ("message_link", "permalink", "link", "url"):
        value = slack_response.get(key)
        if isinstance(value, str) and value:
            return value
    return None
```

**What this does:**
These helpers extract events, times, and links from connector responses and format them for Slack.

---

## 4. Create the Slack Calendar Assistant Workflow

Now, let's build a conversational Slack bot that can search and create calendar events.

### 4.1. Create the Workflow File

Create a new file at `src/workflows/slack_calendar_assistant.py`.

### 4.2. Add Imports

Paste in the following imports:

```python
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
```

**What this does:**
Brings in everything you need for webhook handling, connector calls, and intent parsing.

### 4.3. Define Connectors and Intent Model

```python
GOOGLE_CALENDAR_CONNECTOR = connector("google_calendar")
SLACK_CONNECTOR = connector("slack")

class ParsedCalendarIntent(BaseModel):
    intent: Literal["search", "create", "unknown"]
    calendar: str = Field(default="primary")
    query: str | None = Field(default=None)
    start: str | None = Field(default=None)
    end: str | None = Field(default=None)
    title: str | None = Field(default=None)
    description: str | None = Field(default=None)
    attendees: list[str] = Field(default_factory=list)
    location: str | None = Field(default=None)
    clarification: str | None = Field(default=None)
```

**What this does:**
- Registers the connectors.
- Defines a Pydantic model for the parsed user intent, so you can handle both search and create requests.

### 4.4. Add Activities

#### Parse Calendar Intent from Slack Message

```python
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
                    '{'  # ...schema omitted for brevity...
                    '}. '
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
```

**What this does:**
Uses a Mistral LLM to parse the user's Slack message into a structured intent object.

#### Search Google Calendar by Intent

```python
@workflows.activity(name="search-google-calendar-by-intent")
async def search_google_calendar_by_intent(
    parsed_intent: ParsedCalendarIntent,
    google_calendar: ToolCallClient = Depends(GOOGLE_CALENDAR_CONNECTOR),
) -> dict[str, Any]:
    """Call Google Calendar search connector with parsed intent parameters."""
    return await google_calendar.call_tool(
        tool_name="google_calendar_search",
        arguments={
            "calendar": parsed_intent.calendar,
            "query": parsed_intent.query,
            "start": parsed_intent.start,
            "end": parsed_intent.end,
        },
    )
```

**What this does:**
Searches Google Calendar using the parameters extracted from the user's message.

#### Create Google Calendar Event by Intent

```python
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

    arguments = {k: v for k, v in arguments.items() if v is not None}

    return await google_calendar.call_tool(
        tool_name="google_calendar_create_event",
        arguments=arguments,
    )
```

**What this does:**
Creates a new calendar event using the parsed details from the user's message.

#### Send Slack Reply

```python
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

    return await slack.call_tool(
        tool_name="slack_send_message",
        arguments=arguments,
    )
```

**What this does:**
Sends a message back to Slack, optionally as a threaded reply.

#### Format Search and Create Replies

```python
@workflows.activity(name="format-search-reply")
async def format_search_reply(
    parsed_intent: ParsedCalendarIntent,
    search_response: dict[str, Any],
) -> str:
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
    link = _extract_link(create_response)
    title = parsed_intent.title or "your event"
    if link:
        return f"Created *{title}* successfully.\n{link}"
    return f"Created *{title}* successfully."
```

**What this does:**
Formats the search results or creation confirmation into a Slack-friendly message.

### 4.5. Define the Webhook Workflow

```python
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
    @post("/slack/events")
    async def slack_events(self, request: HTTPRequest) -> HTTPResponse:
        payload = _extract_request_json(request)
        if payload.get("type") == "url_verification":
            return HTTPResponse(status_code=200, body={"challenge": payload.get("challenge")})
        if payload.get("type") != "event_callback":
            return HTTPResponse(status_code=200, body={"ok": True, "ignored": "unsupported_type"})
        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return HTTPResponse(status_code=200, body={"ok": True, "ignored": "invalid_event"})
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
```

**What this does:**
Defines the webhook handler for Slack events, parses the user's message, and routes to the right activity.

### 4.6. Add Helper Functions

Paste these at the bottom of your file:

```python
def _extract_request_json(request: HTTPRequest) -> dict[str, Any]:
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
    for attr_name in ("json", "data", "payload"):
        value = getattr(request, attr_name, None)
        if isinstance(value, dict):
            return value
    return {}

def _extract_first_text_content(chat_result: Any) -> str:
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
    for key in ("events", "items", "results", "data"):
        value = search_response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []

def _extract_time(event: dict[str, Any], key: str) -> str | None:
    value = event.get(key)
    if isinstance(value, dict):
        dt = value.get("dateTime") or value.get("datetime") or value.get("date")
        if isinstance(dt, str):
            return dt
    if isinstance(value, str):
        return value
    return None

def _format_time_range(start_raw: str | None, end_raw: str | None) -> str:
    if not start_raw:
        return "Time TBD"
    start_label = _format_time_value(start_raw)
    if not end_raw:
        return start_label
    return f"{start_label} to {_format_time_value(end_raw)}"

def _format_time_value(raw: str) -> str:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.strftime("%H:%M")
        return dt.astimezone(UTC).strftime("%H:%M UTC")
    except ValueError:
        return raw

def _extract_link(payload: dict[str, Any]) -> str | None:
    for key in ("htmlLink", "permalink", "message_link", "link", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None
```

**What this does:**
These helpers extract and format data from Slack and Google Calendar responses.

---

## Success!

You just built two robust, production-ready workflows using Mistral Workflows and Connectors. You:
- Set up a new project
- Added and explained each activity and workflow
- Leveraged Connectors for secure, reliable API calls
- Built both a scheduled and a conversational workflow

**Next steps:**
- Explore the [Mistral Workflows documentation](https://docs.mistral.ai/workflows/)
- Check out the [Connector reference](https://docs.mistral.ai/workflows/connectors/)
- Try adding more connectors or custom activities to your workflows!
