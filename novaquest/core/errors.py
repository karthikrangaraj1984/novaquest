"""Domain errors raised by services and mapped to HTTP status codes by the web layer."""


class DomainError(Exception):
    """Base class for expected, user-facing errors."""

    status = 400

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class ValidationError(DomainError):
    """Input is missing or invalid."""

    status = 422


class NotFound(DomainError):
    """The record does not exist in the caller's institution."""

    status = 404


class Forbidden(DomainError):
    """The caller is not allowed to do this."""

    status = 403


class Conflict(DomainError):
    """The action clashes with the current state."""

    status = 409


class RateLimited(DomainError):
    """Too many attempts; try again later."""

    status = 429
