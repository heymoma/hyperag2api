from src.core.interfaces import CookieProvider
from src.core.logging_config import get_logger

logger = get_logger("auth")

class AuthService:
    """Manages the use case of re-authenticating or clearing active browser sessions."""

    def __init__(self, cookie_provider: CookieProvider):
        self.cookie_provider = cookie_provider

    def reset_authentication(self) -> bool:
        """Clear existing cookies via browser CDP context to force re-authentication."""
        logger.info("Triggered cookie reset via AuthService.")
        return self.cookie_provider.clear_cookies()
