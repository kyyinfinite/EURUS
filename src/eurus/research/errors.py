from __future__ import annotations

from collections.abc import Sequence

from eurus.errors import ToolError


class ResearchError(ToolError):
    pass


class ProviderUnavailableError(ResearchError):
    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider


class SearchTimeoutError(ResearchError):
    def __init__(self, message: str, *, provider: str = "", timeout_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.provider = provider
        self.timeout_seconds = timeout_seconds


class SearchUnavailableError(ResearchError):
    def __init__(self, message: str, *, failures: Sequence[tuple[str, BaseException]] = ()) -> None:
        super().__init__(message)
        self.failures = tuple(failures)


class FetchTimeoutError(ResearchError):
    pass


class ExtractionError(ResearchError):
    pass


class SecurityRejectionError(ResearchError):
    pass


class UnsupportedContentError(ResearchError):
    pass


class BudgetExceededError(ResearchError):
    pass


class DeadlineExceededError(ResearchError):
    pass


class CitationValidationError(ResearchError):
    pass


class StorageError(ResearchError):
    pass


class BrowserFallbackError(ResearchError):
    pass


class PlanValidationError(ResearchError):
    pass
