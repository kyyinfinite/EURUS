"""Native catalog of Google Workspace MCP services.

All nine services ship with Eurus. They are disabled until the user enables
them, and only the scopes of enabled services are requested during OAuth
login. Google Workspace MCP is a Developer Preview: availability varies by
account, project enrollment and per-service activation; each service records
the caveats reported by diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass

from eurus.mcp.providers.google_workspace.endpoints import ENDPOINTS
from eurus.mcp.providers.google_workspace.scopes import SERVICE_SCOPES


@dataclass(frozen=True, slots=True)
class GoogleService:
    service_id: str
    label: str
    endpoint: str
    scopes: tuple[str, ...]
    requires_workspace_account: bool = False
    notes: str = ""

    @property
    def server_name(self) -> str:
        return f"google-{self.service_id}"


GMAIL = GoogleService(
    service_id="gmail",
    label="Gmail",
    endpoint=ENDPOINTS["gmail"],
    scopes=SERVICE_SCOPES["gmail"],
)
DRIVE = GoogleService(
    service_id="drive",
    label="Google Drive",
    endpoint=ENDPOINTS["drive"],
    scopes=SERVICE_SCOPES["drive"],
)
DOCS = GoogleService(
    service_id="docs",
    label="Google Docs",
    endpoint=ENDPOINTS["docs"],
    scopes=SERVICE_SCOPES["docs"],
    notes="Requires the docsmcp service enabled in your Google Cloud project.",
)
SHEETS = GoogleService(
    service_id="sheets",
    label="Google Sheets",
    endpoint=ENDPOINTS["sheets"],
    scopes=SERVICE_SCOPES["sheets"],
    notes="Requires the sheetsmcp service enabled in your Google Cloud project.",
)
SLIDES = GoogleService(
    service_id="slides",
    label="Google Slides",
    endpoint=ENDPOINTS["slides"],
    scopes=SERVICE_SCOPES["slides"],
    notes="Requires the slidesmcp service enabled in your Google Cloud project.",
)
CALENDAR = GoogleService(
    service_id="calendar",
    label="Google Calendar",
    endpoint=ENDPOINTS["calendar"],
    scopes=SERVICE_SCOPES["calendar"],
)
CHAT = GoogleService(
    service_id="chat",
    label="Google Chat",
    endpoint=ENDPOINTS["chat"],
    scopes=SERVICE_SCOPES["chat"],
    requires_workspace_account=True,
    notes=(
        "Requires a Chat app configured in the Google Cloud project and is "
        "generally unavailable for consumer Google accounts."
    ),
)
PEOPLE = GoogleService(
    service_id="people",
    label="Google People",
    endpoint=ENDPOINTS["people"],
    scopes=SERVICE_SCOPES["people"],
)
CLASSROOM = GoogleService(
    service_id="classroom",
    label="Google Classroom",
    endpoint=ENDPOINTS["classroom"],
    scopes=SERVICE_SCOPES["classroom"],
    requires_workspace_account=True,
    notes="Requires a Google Workspace for Education account.",
)

SERVICES: tuple[GoogleService, ...] = (
    GMAIL,
    DRIVE,
    DOCS,
    SHEETS,
    SLIDES,
    CALENDAR,
    CHAT,
    PEOPLE,
    CLASSROOM,
)

SERVICE_BY_ID: dict[str, GoogleService] = {item.service_id: item for item in SERVICES}


def service_by_id(service_id: str) -> GoogleService:
    service = SERVICE_BY_ID.get(service_id)
    if service is None:
        known = ", ".join(sorted(SERVICE_BY_ID))
        raise ValueError(f"unknown Google Workspace service '{service_id}'; known: {known}")
    return service


__all__ = [
    "CALENDAR",
    "CHAT",
    "CLASSROOM",
    "DOCS",
    "DRIVE",
    "GMAIL",
    "GoogleService",
    "PEOPLE",
    "SERVICES",
    "SHEETS",
    "SLIDES",
    "SERVICE_BY_ID",
    "service_by_id",
]
