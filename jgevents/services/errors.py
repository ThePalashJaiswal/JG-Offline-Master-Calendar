class ServiceError(Exception):
    """Base class for errors shown to the user."""


class PermissionDenied(ServiceError):
    pass


class NotFound(ServiceError):
    pass


class ValidationError(ServiceError):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


class ConflictError(ServiceError):
    """Someone else changed the record since it was loaded (optimistic lock)."""


class ScheduleConflict(ServiceError):
    def __init__(self, message: str, conflicts=None):
        self.conflicts = conflicts or []
        super().__init__(message)


class InvalidTransition(ServiceError):
    pass


class DuplicateSubmission(ServiceError):
    pass


class AuthError(ServiceError):
    pass
