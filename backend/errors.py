"""Exception hierarchy for cpypiserver."""

from __future__ import annotations


class PypiError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class PackageNotFoundError(PypiError):
    def __init__(self, package_name: str) -> None:
        super().__init__(f"Package '{package_name}' not found", status_code=404)


class UploadConflictError(PypiError):
    def __init__(self, filename: str) -> None:
        super().__init__(
            f"Package '{filename}' already exists (set overwrite=1 to allow)",
            status_code=409,
        )


class PublishConflictError(PypiError):
    """409 for an npm re-publish of an immutable version.

    Separate from :class:`UploadConflictError` because that message talks about
    twine's ``overwrite=1`` form field, which no npm client sends.
    """

    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            f"You cannot publish over the previously published versions: "
            f"{name}@{version} already exists",
            status_code=409,
        )


class BadRequestError(PypiError):
    def __init__(self, message: str = "Bad request") -> None:
        super().__init__(message=message, status_code=400)


class UnauthorizedError(PypiError):
    def __init__(
        self,
        message: str = "Authentication required",
        www_authenticate: str | None = None,
    ) -> None:
        super().__init__(message=message, status_code=401)
        self.www_authenticate = www_authenticate


class ForbiddenError(PypiError):
    def __init__(self, message: str = "Forbidden") -> None:
        super().__init__(message=message, status_code=403)
