"""App profiles and tenant configuration.

An **app profile** holds what is shared by every tenant of one vendor product: the login
sub-capability, detectors that apply on every page (session expired, error page, maintenance),
label synonyms used when onboarding a new tenant, and app-specific redaction patterns.

A **tenant** is one institution running that product: its base URL, product version,
credential reference, policy file, and (for the demo) who owns which members.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ruamel.yaml import YAML

from .artifact import Detector
from .semver import parse_requirement, parse_version
from .targets import FrameRef


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppProfile(_Strict):
    product: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str
    login: str
    content_frame: tuple[FrameRef, ...] = Field(
        default=(),
        description="Frame whose URL and title define 'the page' for routes and titles "
        "(framesets). Empty means the top-level document.",
    )
    detectors: tuple[Detector, ...] = ()
    label_synonyms: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    redaction_patterns: dict[str, str] = Field(default_factory=dict)

    @field_validator("login")
    @classmethod
    def _login_req(cls, v: str) -> str:
        parse_requirement(v)
        return v

    @field_validator("redaction_patterns")
    @classmethod
    def _patterns_compile(cls, v: dict[str, str]) -> dict[str, str]:
        for rx in v.values():
            re.compile(rx)
        return v


class TenantConfig(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    institution: str
    product: str
    product_version: str
    base_url_env: str
    default_base_url: str
    secrets_prefix: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    policy: str
    members_by_principal: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("product_version")
    @classmethod
    def _version(cls, v: str) -> str:
        parse_version(v)
        return v

    @property
    def base_url(self) -> str:
        return os.environ.get(self.base_url_env, self.default_base_url)


def _load(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return YAML(typ="safe").load(fh)


def load_profile(path: str | Path) -> AppProfile:
    return AppProfile.model_validate(_load(path))


def load_tenant(path: str | Path) -> TenantConfig:
    return TenantConfig.model_validate(_load(path))
