"""Official Google Workspace MCP server endpoints.

Source: Google Workspace developer documentation,
"Configure the Google Workspace MCP servers"
(https://developers.google.com/workspace/guides/configure-mcp-servers).
The servers are part of the Google Workspace Developer Preview program.
"""

from __future__ import annotations

GMAIL_ENDPOINT = "https://gmailmcp.googleapis.com/mcp/v1"
DRIVE_ENDPOINT = "https://drivemcp.googleapis.com/mcp/v1"
DOCS_ENDPOINT = "https://docsmcp.googleapis.com/mcp/v1"
SHEETS_ENDPOINT = "https://sheetsmcp.googleapis.com/mcp/v1"
SLIDES_ENDPOINT = "https://slidesmcp.googleapis.com/mcp/v1"
CALENDAR_ENDPOINT = "https://calendarmcp.googleapis.com/mcp/v1"
CHAT_ENDPOINT = "https://chatmcp.googleapis.com/mcp/v1"
PEOPLE_ENDPOINT = "https://people.googleapis.com/mcp/v1"
CLASSROOM_ENDPOINT = "https://classroommcp.googleapis.com/mcp/v1"

ENDPOINTS: dict[str, str] = {
    "gmail": GMAIL_ENDPOINT,
    "drive": DRIVE_ENDPOINT,
    "docs": DOCS_ENDPOINT,
    "sheets": SHEETS_ENDPOINT,
    "slides": SLIDES_ENDPOINT,
    "calendar": CALENDAR_ENDPOINT,
    "chat": CHAT_ENDPOINT,
    "people": PEOPLE_ENDPOINT,
    "classroom": CLASSROOM_ENDPOINT,
}

__all__ = [
    "CALENDAR_ENDPOINT",
    "CHAT_ENDPOINT",
    "CLASSROOM_ENDPOINT",
    "DOCS_ENDPOINT",
    "DRIVE_ENDPOINT",
    "ENDPOINTS",
    "GMAIL_ENDPOINT",
    "PEOPLE_ENDPOINT",
    "SHEETS_ENDPOINT",
    "SLIDES_ENDPOINT",
]
