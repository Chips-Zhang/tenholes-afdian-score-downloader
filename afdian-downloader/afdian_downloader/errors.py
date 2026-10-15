"""Domain-specific errors with messages safe to show to users."""


class AfdianDownloaderError(Exception):
    """Base error for expected downloader failures."""


class AuthenticationError(AfdianDownloaderError):
    """Raised when a usable auth token was not supplied."""


class NetworkError(AfdianDownloaderError):
    """Raised when a request cannot be completed after retries."""


class APIError(AfdianDownloaderError):
    """Raised when Afdian rejects a request."""


class ProtocolError(AfdianDownloaderError):
    """Raised when an API response has an unexpected structure."""

