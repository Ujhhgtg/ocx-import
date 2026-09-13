"""OpenCodex pool persistence, compatible with the existing Node importer."""

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Account, ImportError
from .parsers import AUTH_CLAIM, decode_jwt

IDENTITY_ID = re.compile(r"import-openai-codex-oauth-[a-f0-9]{12}")
EMPTY_ALIASES = {"schemaVersion": 1, "codexAccounts": {}}


def _read(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError, RecursionError):
        raise ImportError(f"无法读取 JSON：{path.name}") from None
    if not isinstance(value, dict):
        raise ImportError(f"{path.name} 必须是 JSON 对象")
    return value


def _atomic_bytes(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write(path: Path, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _atomic_bytes(path, data.encode("utf-8"))


def _backup(root: Path, originals: dict[str, bytes | None]) -> Path:
    parent = root / "backups"
    parent.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = Path(tempfile.mkdtemp(prefix=f"ocx-import-{stamp}-", dir=parent))
    for name, data in originals.items():
        if data is not None:
            _atomic_bytes(directory / name, data)
    return directory


def _validate(config: dict, store: dict, aliases: dict) -> None:
    rows = config.get("codexAccounts", [])
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
        for row in rows
    ):
        raise ImportError("config.json 的 codexAccounts 格式无效")
    if len({row["id"] for row in rows}) != len(rows):
        raise ImportError("config.json 的账号 ID 重复")
    providers = config.get("providers", {})
    if not isinstance(providers, dict) or not isinstance(providers.get("openai", {}), dict):
        raise ImportError("config.json 的 providers 格式无效")
    if any(not isinstance(record, dict) for record in store.values()):
        raise ImportError("codex-accounts.json 包含无效记录")
    mapping = aliases.get("codexAccounts")
    if aliases.get("schemaVersion") != 1 or not isinstance(mapping, dict):
        raise ImportError("导入器身份别名文件格式无效")
    if any(not IDENTITY_ID.fullmatch(key) or not isinstance(value, str) or not value
           for key, value in mapping.items()):
        raise ImportError("导入器身份别名文件包含无效记录")


def _resolve_id(rows: list[dict], store: dict, aliases: dict, account: Account) -> str:
    identity = account.identity_id
    by_id = {row["id"]: row for row in rows}
    if identity in by_id:
        return identity
    if alias := aliases.get(identity):
        if alias in by_id:
            return alias
        record = store.get(alias, {})
        if record.get("deletedAt") is not None and record.get("credential") is None:
            if any(key != identity and value == alias for key, value in aliases.items()):
                raise ImportError("已删除账号的身份别名存在冲突")
            del aliases[identity]
        else:
            raise ImportError("导入器身份别名指向不存在的 OpenCodex 记录")
    if account.user_id:
        fallback = replace(account, user_id="").identity_id
        if fallback in by_id:
            return fallback
    workspace_rows = [
        row for row in rows
        if not row.get("isMain") and row.get("chatgptAccountId") == account.account_id
    ]
    if not account.user_id and any(IDENTITY_ID.fullmatch(row["id"]) for row in workspace_rows):
        raise ImportError("缺少 ChatGPT user ID，无法确定要更新的 Team/K12 用户")
    legacy = [
        row for row in workspace_rows
        if not IDENTITY_ID.fullmatch(row["id"])
        and isinstance(row.get("email"), str)
        and row["email"].strip().lower() == account.email.strip().lower()
    ]
    if len(legacy) > 1:
        raise ImportError("发现多个相同 workspace/email 的旧账号记录，拒绝猜测")
    return legacy[0]["id"] if legacy else identity


def _old_record(raw: dict, previous: dict | None, account: Account) -> dict:
    if previous and (previous.get("isMain") or previous.get("chatgptAccountId") != account.account_id):
        raise ImportError("已有记录的账号身份与待导入账号不一致")
    # Pre-generation stores contained credentials directly.
    record = {"credential": raw, "generation": 0} if "accessToken" in raw else raw
    credential = record.get("credential", {})
    if credential is None and record.get("deletedAt") is not None:
        record = {**record, "credential": {}}
        credential = {}
    if not isinstance(credential, dict):
        raise ImportError("已有账号的 credential 格式无效")
    if credential.get("authMode") == "agentIdentity":
        raise ImportError("已有账号使用 Agent Identity，不能用 OAuth 覆盖")
    if credential.get("chatgptAccountId") not in (None, account.account_id):
        raise ImportError("已有凭据的 workspace ID 与待导入账号不一致")
    if account.user_id:
        token = credential.get("accessToken", "")
        claims = decode_jwt(token) if isinstance(token, str) else {}
        auth = claims.get(AUTH_CLAIM, {})
        auth = auth if isinstance(auth, dict) else {}
        old_users = [claims.get("chatgpt_user_id"), auth.get("chatgpt_user_id"), auth.get("user_id")]
        if any(isinstance(user, str) and user and user != account.user_id for user in old_users):
            raise ImportError("已有凭据属于另一个 ChatGPT 用户")
    return record


def _plan(root: Path, accounts: list[Account], activate: bool) -> tuple[dict[str, dict], dict]:
    config = _read(root / "config.json", {})
    store = _read(root / "codex-accounts.json", {})
    aliases = _read(root / "ocx-import-identities.json", EMPTY_ALIASES)
    _validate(config, store, aliases)
    aliases_before = copy.deepcopy(aliases)
    rows = config.setdefault("codexAccounts", [])
    mapping = aliases["codexAccounts"]
    imported = []
    for account in accounts:
        record_id = _resolve_id(rows, store, mapping, account)
        previous = next((row for row in rows if row["id"] == record_id), None)
        old = _old_record(store.get(record_id, {}), previous, account)
        if account.user_id:
            if any(key != account.identity_id and value == record_id for key, value in mapping.items()):
                raise ImportError("同一个旧 OpenCodex 记录已绑定到另一个 Team/K12 用户")
            mapping[account.identity_id] = record_id
        row = {**(previous or {}), "id": record_id, "email": account.email,
               "chatgptAccountId": account.account_id, "isMain": False}
        if account.plan:
            row["plan"] = account.plan
        if previous is None:
            rows.append(row)
        else:
            rows[rows.index(previous)] = row
        retained = old.get("credential", {}).get("refreshToken", "")
        refresh = account.refresh_token or (retained if isinstance(retained, str) else "")
        generation = old.get("generation", -1)
        if type(generation) is not int or generation < -1:
            raise ImportError("已有账号的 generation 格式无效")
        fingerprint = f"codex-refresh-grant:{refresh}" if refresh else f"codex-access-only:{record_id}"
        record = {
            **old, "generation": generation + 1,
            "refreshGrantFingerprint": hashlib.sha256(fingerprint.encode()).hexdigest(),
            "credential": {
                "accessToken": account.access_token, "refreshToken": refresh,
                "expiresAt": account.expires_at, "chatgptAccountId": account.account_id,
            },
        }
        for key in ("deletedAt", "lastCodexValidatedAt", "lastCodexValidationStatus", "lastCodexValidationError"):
            record.pop(key, None)
        store[record_id] = record
        if activate or not config.get("activeCodexAccountId"):
            config["activeCodexAccountId"] = record_id
        imported.append({"id": record_id, "email": account.email, "account_id": account.account_id,
                         "refreshable": bool(refresh), "expires_at": account.expires_at,
                         "source_format": account.source_format})

    values = {}
    if imported:
        providers = config.setdefault("providers", {})
        providers["openai"] = {
            **providers.get("openai", {}), "adapter": "openai-responses",
            "baseUrl": "https://chatgpt.com/backend-api/codex", "authMode": "forward",
            "codexAccountMode": "pool",
        }
        if not config.get("defaultProvider"):
            config["defaultProvider"] = "openai"
        values = {"codex-accounts.json": store, "config.json": config}
    if aliases != aliases_before:
        values["ocx-import-identities.json"] = aliases
    try:
        for value in values.values():
            json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, RecursionError):
        raise ImportError("已有 OpenCodex 配置含无效 JSON 数值或嵌套过深") from None
    return values, {"imported": imported, "backup_path": None, "touched": list(values)}


def apply_accounts(root: Path, accounts: list[Account], *, activate: bool = True,
                   dry_run: bool = False) -> dict[str, Any]:
    root = Path(root)
    _, preview = _plan(root, accounts, activate)
    if dry_run or not accounts:
        return preview

    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    database_path = root / "config-mutation.sqlite"
    # Match OpenCodex 2.42's OS-backed mutation lock and generation counter.
    descriptor = os.open(database_path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    database_path.chmod(0o600)
    database = None
    try:
        database = sqlite3.connect(database_path, timeout=5, isolation_level=None)
        database.execute("BEGIN IMMEDIATE")
        database.execute("""CREATE TABLE IF NOT EXISTS config_generation (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            value INTEGER NOT NULL CHECK (value >= 0))""")
        database.execute("INSERT OR IGNORE INTO config_generation VALUES (1, 0)")
        generation = database.execute("SELECT value FROM config_generation WHERE singleton = 1").fetchone()
        if not generation or type(generation[0]) is not int or not 0 <= generation[0] < 2**53 - 1:
            raise ImportError("OpenCodex 配置 generation 无效")
        # The preflight snapshot can become stale; reread under the lock.
        values, result = _plan(root, accounts, activate)
        originals = {name: (root / name).read_bytes() if (root / name).exists() else None for name in values}
        backup = _backup(root, originals)
        attempted: list[str] = []
        try:
            for name, value in values.items():
                attempted.append(name)
                _write(root / name, value)
            database.execute("UPDATE config_generation SET value = value + 1 WHERE singleton = 1")
            database.execute("COMMIT")
        except BaseException:
            failed_restore = False
            for name in reversed(attempted):
                try:
                    if originals[name] is None:
                        (root / name).unlink(missing_ok=True)
                    else:
                        _atomic_bytes(root / name, originals[name])
                except OSError:
                    failed_restore = True
            if failed_restore:
                raise ImportError(f"写入失败且未能完全回滚；原始文件备份位于 {backup}") from None
            raise
        result["backup_path"] = str(backup)
        return result
    except sqlite3.Error:
        raise ImportError("无法取得或提交 OpenCodex 配置锁；请稍后重试或检查 config-mutation.sqlite") from None
    finally:
        if database is not None:
            try:
                if database.in_transaction:
                    database.rollback()
            finally:
                database.close()
