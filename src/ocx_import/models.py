"""Shared, credential-safe account model."""

import hashlib
import json
from dataclasses import dataclass, field


class ImportError(ValueError):
    """An actionable import error whose message never includes credential values."""


@dataclass(frozen=True)
class Account:
    account_id: str
    email: str
    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    user_id: str = ""
    plan: str = ""
    expires_at: int = 0
    source_format: str = ""
    source: str = ""

    @property
    def identity_id(self) -> str:
        # Match opencodex-account-importer IDs so existing imports are updated.
        identity = [
            "openai", "codex-oauth",
            "workspace-user" if self.user_id else "workspace-email",
            self.account_id, self.user_id or self.email.strip().lower(),
        ]
        encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
        return f"import-openai-codex-oauth-{digest}"
