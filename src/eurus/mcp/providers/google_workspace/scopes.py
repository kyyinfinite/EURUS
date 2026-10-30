"""OAuth scopes required by each Google Workspace MCP server.

The scope lists mirror Google's official configuration guide; enabling a
service requests exactly the scopes Google documents for it (plus the base
identity scopes), never more.
"""

from __future__ import annotations

BASE_IDENTITY_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

GMAIL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)
DRIVE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
)
DOCS_SCOPES: tuple[str, ...] = DRIVE_SCOPES + (
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/documents",
)
SHEETS_SCOPES: tuple[str, ...] = DRIVE_SCOPES + (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
)
SLIDES_SCOPES: tuple[str, ...] = DRIVE_SCOPES + (
    "https://www.googleapis.com/auth/presentations.readonly",
    "https://www.googleapis.com/auth/presentations",
)
CALENDAR_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
CHAT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.users.readstate",
    "https://www.googleapis.com/auth/chat.users.spacesettings",
)
PEOPLE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/directory.readonly",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/contacts.readonly",
)
CLASSROOM_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/classroom.announcements",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.students.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
)

SERVICE_SCOPES: dict[str, tuple[str, ...]] = {
    "gmail": GMAIL_SCOPES,
    "drive": DRIVE_SCOPES,
    "docs": DOCS_SCOPES,
    "sheets": SHEETS_SCOPES,
    "slides": SLIDES_SCOPES,
    "calendar": CALENDAR_SCOPES,
    "chat": CHAT_SCOPES,
    "people": PEOPLE_SCOPES,
    "classroom": CLASSROOM_SCOPES,
}


def scopes_for_services(service_ids: list[str] | tuple[str, ...]) -> list[str]:
    """Union of identity and service scopes, order-preserving, deduplicated."""
    ordered: list[str] = list(BASE_IDENTITY_SCOPES)
    for service_id in service_ids:
        for scope in SERVICE_SCOPES.get(service_id, ()):
            if scope not in ordered:
                ordered.append(scope)
    return ordered


__all__ = [
    "BASE_IDENTITY_SCOPES",
    "CALENDAR_SCOPES",
    "CHAT_SCOPES",
    "CLASSROOM_SCOPES",
    "DOCS_SCOPES",
    "DRIVE_SCOPES",
    "GMAIL_SCOPES",
    "PEOPLE_SCOPES",
    "SERVICE_SCOPES",
    "SHEETS_SCOPES",
    "SLIDES_SCOPES",
    "scopes_for_services",
]
