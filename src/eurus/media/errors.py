from __future__ import annotations

from enum import StrEnum


class MediaErrorCode(StrEnum):
    EMPTY_FILE = "empty_file"
    TOO_LARGE = "too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    INVALID_SIGNATURE = "invalid_signature"
    SIGNATURE_MISMATCH = "signature_mismatch"
    INVALID_NAME = "invalid_name"
    INVALID_SESSION = "invalid_session"
    TRAVERSAL_BLOCKED = "traversal_blocked"
    NOT_FOUND = "not_found"
    OWNERSHIP_REQUIRED = "ownership_required"
    OWNER_MISMATCH = "owner_mismatch"
    DUPLICATE_ASSET = "duplicate_asset"
    PROCESSING_FAILED = "processing_failed"
    PROCESSING_TIMEOUT = "processing_timeout"
    LIMIT_EXCEEDED = "limit_exceeded"
    DECODER_UNAVAILABLE = "decoder_unavailable"
    CORRUPT_MEDIA = "corrupt_media"


class MediaError(ValueError):
    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code or MediaErrorCode.UNSUPPORTED_TYPE.value


class MediaValidationError(MediaError):
    def __init__(self, message: str, *, code: str = MediaErrorCode.INVALID_SIGNATURE.value) -> None:
        super().__init__(message, code=code)


class MediaLimitError(MediaError):
    def __init__(self, message: str, *, code: str, limit: int | float, actual: int | float) -> None:
        super().__init__(message, code=code)
        self.limit = limit
        self.actual = actual


class MediaOwnershipError(MediaError):
    def __init__(self, message: str, *, code: str = MediaErrorCode.OWNER_MISMATCH.value) -> None:
        super().__init__(message, code=code)


class MediaProcessingError(MediaError):
    def __init__(
        self,
        message: str,
        *,
        code: str = MediaErrorCode.PROCESSING_FAILED.value,
        stage: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code)
        self.stage = stage
        self.retryable = retryable
