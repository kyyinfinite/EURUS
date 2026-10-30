"""Native Google Workspace MCP provider."""

from eurus.mcp.providers.google_workspace.catalog import (
    SERVICES,
    GoogleService,
    service_by_id,
)
from eurus.mcp.providers.google_workspace.provider import (
    GoogleBearerAuth,
    GoogleWorkspaceProvider,
    accounts_path,
    build_auth,
    build_provider,
    settings_path,
    tokens_dir,
)
from eurus.mcp.providers.google_workspace.scopes import scopes_for_services
from eurus.mcp.providers.google_workspace.services import (
    GoogleWorkspaceSettings,
    ServiceSettings,
)

__all__ = [
    "GoogleBearerAuth",
    "GoogleService",
    "GoogleWorkspaceProvider",
    "GoogleWorkspaceSettings",
    "SERVICES",
    "ServiceSettings",
    "accounts_path",
    "build_auth",
    "build_provider",
    "scopes_for_services",
    "service_by_id",
    "settings_path",
    "tokens_dir",
]
