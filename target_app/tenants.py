"""Tenant variants of the same vendor product.

Tenant B mirrors what real credit unions do to a shared core product: different branding,
relabeled fields, credit-union wording for account types, and one extra compliance step.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    key: str
    institution: str
    product_version: str
    brand_color: str
    accent_color: str
    member_id_label: str
    savings_label: str
    require_privacy_ack: bool


TENANTS: dict[str, Tenant] = {
    "a": Tenant(
        key="a",
        institution="Harbor Federal Credit Union",
        product_version="4.6.2",
        brand_color="#1f3a5f",
        accent_color="#d9e3ef",
        member_id_label="Member ID",
        savings_label="Savings",
        require_privacy_ack=False,
    ),
    "b": Tenant(
        key="b",
        institution="Riverbend Community Credit Union",
        product_version="4.7.0",
        brand_color="#4a2c0f",
        accent_color="#efe4d4",
        member_id_label="Member #",
        savings_label="Share Savings",
        require_privacy_ack=True,
    ),
}


def display_kind(tenant: Tenant, kind: str) -> str:
    """How an account kind is labeled on screen for this tenant."""
    if kind == "Savings":
        return tenant.savings_label
    return kind
