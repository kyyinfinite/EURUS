from __future__ import annotations

from eurus.errors import EurusError


class OfficeError(EurusError):
    pass


class OfficeValidationError(OfficeError):
    pass


class OfficeBudgetError(OfficeError):
    pass


class OfficeWorkflowError(OfficeError):
    pass


class OfficeRenderError(OfficeError):
    pass


class OfficeCapabilityError(OfficeError):
    pass
