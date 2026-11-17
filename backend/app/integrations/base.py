# SPDX-License-Identifier: Apache-2.0

"""Base integration class – contract for external service integrations.

OAuth credentials are stored per-integration in the database.
OAuthDefaults provides default URLs/scopes for pre-filling the UI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tools.base import CallableToolExecutor, ToolDefinition


@dataclass
class OAuthDefaults:
    """Default OAuth configuration for a provider (URLs and scopes)."""

    authorize_url: str
    token_url: str
    scopes: list[str]


class BaseIntegration(ABC):
    """Base class for all external service integrations."""

    provider_name: str
    display_name: str
    icon: str
    description: str
    auth_types: list[str] = ["oauth"]

    @abstractmethod
    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """Return default OAuth URLs/scopes, or None if non-OAuth."""
        raise NotImplementedError

    @abstractmethod
    async def validate_connection(self, token: str) -> bool:
        """Test if the provided credential is valid."""
        raise NotImplementedError

    @abstractmethod
    async def get_user_info(self, token: str) -> dict:
        """Retrieve basic user/account info for display."""
        raise NotImplementedError

    def build_tools(
        self, token: str, executor: CallableToolExecutor
    ) -> list[ToolDefinition]:
        """Build provider-specific tools. Override in subclasses."""
        return []
