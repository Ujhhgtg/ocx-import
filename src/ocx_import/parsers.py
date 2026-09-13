"""Normalize ChatGPT sessions and sub2api OpenAI OAuth exports locally."""

import base64
import binascii
import json
import math
import time
from datetime import datetime, timezone
from typing import Any

from .models import Account, ImportError

AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first(*values: Any) -> str:
    return next((result for value in values if (result := _text(value))), "")


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def decode_jwt(token: str) -> dict:
    """Read unverified metadata only; this does not authenticate a token."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return {}
    try:
        raw = base64.b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
        )
        return _object(json.loads(raw))
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        return {}


def _milliseconds(value: Any, *, jwt: bool = False) -> int:
    # JWT exp is always seconds, whereas exported timestamps may be milliseconds.
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        limit = 253402300799 if jwt else 253402300799000
        if value <= 0 or value > limit or not math.isfinite(value):
            return 0
        return int(value * 1000 if jwt or value < 100_000_000_000 else value)
    if jwt or not isinstance(value, str) or not value.strip():
        return 0
    try:
        numeric = float(value)
    except ValueError:
        try:
            date = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return _milliseconds(date.timestamp(), jwt=True)
        except (ValueError, OverflowError, OSError):
            return 0
    return _milliseconds(numeric)


def _identity(label: str, *values: Any) -> str:
    ids = {_text(value) for value in values if _text(value)}
    if len(ids) > 1:
        raise ImportError(f"{label} 字段与令牌 claim 不一致")
    return next(iter(ids), "")


def _normalize(row: dict, source: str, now: float) -> Account:
    is_sub2api = "credentials" in row or "platform" in row
    if is_sub2api:
        if "credentials" in row and not isinstance(row["credentials"], dict):
            raise ImportError("sub2api 的 credentials 必须是 JSON 对象")
        credentials = row.get("credentials", row)
        platform = _first(row.get("platform"), credentials.get("provider")).lower()
        if platform not in {"", "openai", "codex", "chatgpt", "gpt"}:
            raise ImportError("只支持 sub2api 的 OpenAI OAuth 账号")
        if _text(row.get("type")).lower() not in {"", "oauth"}:
            raise ImportError("只支持 sub2api 的 OpenAI OAuth 账号")
        if _text(credentials.get("auth_mode")).lower() not in {"", "oauth", "chatgpt"}:
            raise ImportError("不支持此 sub2api 认证类型；需要 OpenAI OAuth 凭据")
        source_format = "sub2api"
    else:
        if not any(key in row for key in ("accessToken", "access_token")):
            raise ImportError("无法识别 JSON；需要 ChatGPT Session 或 sub2api 账号")
        credentials = row
        source_format = "session"

    access = _first(credentials.get("accessToken"), credentials.get("access_token"))
    if not access:
        raise ImportError("缺少非空 accessToken / access_token")
    refresh = _first(credentials.get("refreshToken"), credentials.get("refresh_token"))
    id_token = _first(credentials.get("idToken"), credentials.get("id_token"))
    access_claims, id_claims = decode_jwt(access), decode_jwt(id_token)
    access_auth = _object(access_claims.get(AUTH_CLAIM))
    id_auth = _object(id_claims.get(AUTH_CLAIM))
    user, account = _object(row.get("user")), _object(row.get("account"))

    account_id = _identity(
        "ChatGPT workspace ID",
        account.get("id"), row.get("account_id"), row.get("chatgpt_account_id"),
        row.get("chatgptAccountId"), credentials.get("chatgpt_account_id"),
        credentials.get("account_id"), credentials.get("chatgptAccountId"),
        credentials.get("accountId"), row.get("accountId"),
        access_claims.get("chatgpt_account_id"), access_auth.get("chatgpt_account_id"),
        id_claims.get("chatgpt_account_id"), id_auth.get("chatgpt_account_id"),
    )
    if not account_id:
        raise ImportError("缺少 ChatGPT workspace ID（account.id / chatgpt_account_id）")
    user_id = _identity(
        "ChatGPT user ID",
        user.get("id"), row.get("user_id"), row.get("chatgpt_user_id"),
        row.get("chatgptUserId"), credentials.get("chatgpt_user_id"),
        credentials.get("chatgptUserId"), credentials.get("user_id"),
        access_claims.get("chatgpt_user_id"), access_auth.get("chatgpt_user_id"),
        access_auth.get("user_id"), id_claims.get("chatgpt_user_id"),
        id_auth.get("chatgpt_user_id"), id_auth.get("user_id"),
    )
    email = _first(
        user.get("email"), row.get("email"), credentials.get("email"),
        _object(row.get("extra")).get("email"),
        _object(access_claims.get(PROFILE_CLAIM)).get("email"),
        id_claims.get("email"), access_claims.get("email"),
        _object(id_claims.get(PROFILE_CLAIM)).get("email"),
    ).lower()
    if not email:
        raise ImportError("缺少账号 email（可来自 user.email、credentials.email 或 JWT）")
    plan = _first(
        account.get("planType"), account.get("plan_type"),
        credentials.get("chatgpt_plan_type"), credentials.get("plan_type"),
        credentials.get("planType"), row.get("plan_type"), row.get("planType"),
        access_auth.get("chatgpt_plan_type"), access_claims.get("chatgpt_plan_type"),
        id_auth.get("chatgpt_plan_type"), id_claims.get("chatgpt_plan_type"),
    )
    jwt_expiry = _milliseconds(access_claims.get("exp"), jwt=True)
    if not refresh:
        if not jwt_expiry:
            raise ImportError("缺少 refresh token，且 access token 没有有效的数值型 JWT exp")
        if jwt_expiry <= (now + 60) * 1000:
            raise ImportError("access token 已过期或将在 60 秒内过期；请重新导出 Session")
    expires_at = jwt_expiry or next(
        (parsed for obj in (credentials, row)
         for key in ("expires_at", "expiresAt", "expires", "expired")
         if (parsed := _milliseconds(obj.get(key)))),
        0,
    )
    return Account(
        account_id=account_id, email=email, access_token=access, refresh_token=refresh,
        user_id=user_id, plan=plan, expires_at=expires_at,
        source_format=source_format, source=source,
    )


def parse_document(value: Any, source: str = "unknown", *, now: float | None = None) -> list[Account]:
    """Accept an individual session/account, a root array, or sub2api envelope."""
    timestamp = time.time() if now is None else now
    if isinstance(value, dict) and "accounts" in value:
        rows = value["accounts"]
        if not isinstance(rows, list):
            raise ImportError("accounts 必须是 JSON 数组")
    elif isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = [value]
    else:
        raise ImportError("JSON 顶层必须是对象或账号数组")
    if not rows:
        raise ImportError("JSON 没有可导入的账号")
    result = []
    for index, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict):
                raise ImportError("账号必须是 JSON 对象")
            result.append(_normalize(row, source, timestamp))
        except ImportError as error:
            raise ImportError(f"第 {index} 个账号：{error}") from None
    return result
