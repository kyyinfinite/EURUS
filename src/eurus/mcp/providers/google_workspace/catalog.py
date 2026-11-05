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
    api_service: str = ""
    mcp_service: str = ""
    console_url: str = ""

    @property
    def server_name(self) -> str:
        return f"google-{self.service_id}"

    @property
    def gcloud_commands(self) -> list[str]:
        services = [name for name in (self.api_service, self.mcp_service) if name]
        return [f"gcloud services enable {name}" for name in services]


_API_CONSOLE = "https://console.cloud.google.com/apis/library/"


def _console(service_name: str) -> str:
    return f"{_API_CONSOLE}{service_name}"


GMAIL = GoogleService(
    service_id="gmail",
    label="Gmail",
    endpoint=ENDPOINTS["gmail"],
    scopes=SERVICE_SCOPES["gmail"],
    api_service="gmail.googleapis.com",
    mcp_service="gmailmcp.googleapis.com",
    console_url=_console("gmail.googleapis.com"),
)
DRIVE = GoogleService(
    service_id="drive",
    label="Google Drive",
    endpoint=ENDPOINTS["drive"],
    scopes=SERVICE_SCOPES["drive"],
    api_service="drive.googleapis.com",
    mcp_service="drivemcp.googleapis.com",
    console_url=_console("drive.googleapis.com"),
)
DOCS = GoogleService(
    service_id="docs",
    label="Google Docs",
    endpoint=ENDPOINTS["docs"],
    scopes=SERVICE_SCOPES["docs"],
    api_service="docs.googleapis.com",
    mcp_service="docsmcp.googleapis.com",
    console_url=_console("docs.googleapis.com"),
    notes="Requires the docsmcp service enabled in your Google Cloud project.",
)
SHEETS = GoogleService(
    service_id="sheets",
    label="Google Sheets",
    endpoint=ENDPOINTS["sheets"],
    scopes=SERVICE_SCOPES["sheets"],
    api_service="sheets.googleapis.com",
    mcp_service="sheetsmcp.googleapis.com",
    console_url=_console("sheets.googleapis.com"),
    notes="Requires the sheetsmcp service enabled in your Google Cloud project.",
)
SLIDES = GoogleService(
    service_id="slides",
    label="Google Slides",
    endpoint=ENDPOINTS["slides"],
    scopes=SERVICE_SCOPES["slides"],
    api_service="slides.googleapis.com",
    mcp_service="slidesmcp.googleapis.com",
    console_url=_console("slides.googleapis.com"),
    notes="Requires the slidesmcp service enabled in your Google Cloud project.",
)
CALENDAR = GoogleService(
    service_id="calendar",
    label="Google Calendar",
    endpoint=ENDPOINTS["calendar"],
    scopes=SERVICE_SCOPES["calendar"],
    api_service="calendar-json.googleapis.com",
    mcp_service="calendarmcp.googleapis.com",
    console_url=_console("calendar-json.googleapis.com"),
)
CHAT = GoogleService(
    service_id="chat",
    label="Google Chat",
    endpoint=ENDPOINTS["chat"],
    scopes=SERVICE_SCOPES["chat"],
    requires_workspace_account=True,
    notes=(
        "Read tools need the Chat API enabled plus the OAuth client; write tools "
        "additionally require a Chat app configured in the Google Cloud project. "
        "Generally unavailable for consumer Google accounts."
    ),
    api_service="chat.googleapis.com",
    mcp_service="chatmcp.googleapis.com",
    console_url=_console("chat.googleapis.com"),
)
PEOPLE = GoogleService(
    service_id="people",
    label="Google People",
    endpoint=ENDPOINTS["people"],
    scopes=SERVICE_SCOPES["people"],
    api_service="people.googleapis.com",
    mcp_service="people.googleapis.com",
    console_url=_console("people.googleapis.com"),
)
CLASSROOM = GoogleService(
    service_id="classroom",
    label="Google Classroom",
    endpoint=ENDPOINTS["classroom"],
    scopes=SERVICE_SCOPES["classroom"],
    requires_workspace_account=True,
    notes="Requires a Google Workspace for Education account.",
    api_service="classroom.googleapis.com",
    mcp_service="classroommcp.googleapis.com",
    console_url=_console("classroom.googleapis.com"),
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
