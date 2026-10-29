class AppError(Exception):
    """Base for errors that are safe to show to the user."""
    status = 400


class ValidationFailed(AppError):
    """Input is malformed. `errors` maps field -> message."""
    status = 422

    def __init__(self, errors: dict[str, str] | str):
        if isinstance(errors, str):
            errors = {"__all__": errors}
        self.errors = errors
        super().__init__("; ".join(errors.values()))


class BusinessRuleViolation(AppError):
    """Input is well-formed but the ERP rules forbid the action."""
    status = 409


class PermissionDenied(AppError):
    status = 403


class NotFound(AppError):
    status = 404
