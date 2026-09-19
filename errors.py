"""领域错误。所有错误都携带稳定的机器可读 code。"""


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        if code:
            self.code = code
        self.details = details or {}


class NotFoundError(DomainError):
    code = "not_found"


class ValidationError(DomainError):
    code = "validation_error"


class LicenseError(DomainError):
    code = "license_incomplete"


class EditionError(DomainError):
    code = "edition_error"


class ConflictError(DomainError):
    code = "conflict"
