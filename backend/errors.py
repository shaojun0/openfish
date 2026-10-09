"""Exception hierarchy for cpypiserver."""

from __future__ import annotations


class PypiError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class PackageNotFoundError(PypiError):
    def __init__(self, package_name: str) -> None:
        super().__init__("Package not found", status_code=404)


class UploadConflictError(PypiError):
    def __init__(self, filename: str) -> None:
        super().__init__(
            "Package already exists (set overwrite=1 to allow)",
            status_code=409,
        )


class PublishConflictError(PypiError):
    """409 for an npm re-publish of an immutable version.

    Separate from :class:`UploadConflictError` because that message talks about
    twine's ``overwrite=1`` form field, which no npm client sends.
    """

    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            "You cannot publish over a previously published version",
            status_code=409,
        )


class BadRequestError(PypiError):
    def __init__(self, message: str = "Bad request") -> None:
        super().__init__(message=message, status_code=400)


class RevisionConflictError(PypiError):
    """409 for a documentation save whose base revision is stale.

    The documentation editor loads a revision and sends it back with the save;
    when someone else saved in between, refusing the write is the only outcome
    that does not silently discard one of the two edits.  Both numbers are in
    the message on purpose: a caller cannot resolve a conflict it cannot see,
    and the SPA shows the operator what it lost to.  The body is the ordinary
    ``PypiError`` envelope (``{"error": "<message>"}``), so the client's
    existing error path renders it without a new branch.
    """

    def __init__(self, doc_id: str, *, expected: int, current: int) -> None:
        super().__init__(
            f"文档 {doc_id} 已被他人修改：你基于 revision {expected}，当前是 "
            f"revision {current}。请刷新后重新应用你的修改。",
            status_code=409,
        )
        self.doc_id = doc_id
        self.expected = expected
        self.current = current


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
