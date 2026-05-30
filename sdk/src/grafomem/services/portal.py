"""Portal authentication service.

Provides signup and login for the Cloud Portal.

Usage::

    tenant = client.portal.signup(
        email="dev@example.com", password="...", organization="Acme",
    )
    session = client.portal.login(email="dev@example.com", password="...")
    print(session.token)
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import Session, Tenant

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class PortalService:
    """Cloud Portal authentication."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    def signup(
        self,
        name: str,
        email: str,
        password: str,
        plan: str = "starter",
    ) -> Tenant:
        """Create a new tenant account.

        Args:
            name: Account display name.
            email: Account email address.
            password: Account password.
            plan: Subscription plan (default ``"starter"``).

        Returns:
            A :class:`Tenant` with the assigned ``api_key``.
        """
        data = self._http.post("/v1/portal/signup", json={
            "name": name,
            "email": email,
            "password": password,
            "plan": plan,
        })
        return Tenant.model_validate(data)

    def login(self, email: str, password: str) -> Session:
        """Log in to an existing account.

        Args:
            email: Account email address.
            password: Account password.

        Returns:
            A :class:`Session` with a JWT ``token``.
        """
        data = self._http.post("/v1/portal/login", json={
            "email": email,
            "password": password,
        })
        return Session.model_validate(data)
