# SPDX-License-Identifier: Apache-2.0

"""Email signature settings as the integration and agent pages show them."""

from typing import Literal

from pydantic import BaseModel


class SignatureIntegrationOption(BaseModel):
    """A signature integration the viewer may choose."""

    id: str
    name: str
    provider: str


class ResolvedSignature(BaseModel):
    """What a sending integration's tools sign with, as resolved for the viewer."""

    enabled: bool
    integration_id: str | None = None
    integration_name: str | None = None
    #: ``binding`` or ``integration`` when ``enabled`` is false.
    off_by: str | None = None
    #: Why no signature integration resolves, and how to set one.
    missing: str | None = None


class IntegrationEmailSignatureResponse(BaseModel):
    """The Email signature card of an integration whose tools send email."""

    enabled: bool
    #: The signature integration set on it; None = picked automatically.
    integration_id: str | None = None
    integration_name: str | None = None
    #: What the setting resolves to for the viewer, with no agent involved.
    resolved: ResolvedSignature
    signature_integrations: list[SignatureIntegrationOption] = []
    can_manage: bool = False


class AgentBindingSignature(BaseModel):
    """One email-sending binding of an agent: its override, what it inherits, and the result."""

    integration_id: str
    integration_name: str
    provider: str
    signature_mode: Literal["inherit", "on", "off"] = "inherit"
    signature_integration_id: str | None = None
    #: The integration's own setting, the binding inheriting everything.
    inherited: ResolvedSignature
    #: With the binding's override applied.
    effective: ResolvedSignature


class AgentEmailSignaturesResponse(BaseModel):
    """The Email signatures card of an agent: one row per bound email-sending integration."""

    bindings: list[AgentBindingSignature] = []
    signature_integrations: list[SignatureIntegrationOption] = []
