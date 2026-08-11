"""Internal CDP errors."""


class CDPError(Exception):
    """Base error for CDP transport and protocol failures."""


class CDPConnectionError(CDPError):
    """The browser connection failed or was closed."""


class CDPProtocolError(CDPError):
    """Chrome rejected a CDP command."""

    def __init__(self, method: str, code: int | None, message: str) -> None:
        self.method = method
        self.code = code
        super().__init__(f"{method} failed ({code}): {message}")
