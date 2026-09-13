"""The ocx-import command line entry point."""

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .models import Account, ImportError
from .parsers import parse_document
from .target import apply_accounts


def _read_file(path: Path) -> list[Account]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise ImportError(f"JSON 格式错误（第 {error.lineno} 行，第 {error.colno} 列）") from None
    except UnicodeError:
        raise ImportError("文件必须使用 UTF-8 编码") from None
    except OSError as error:
        raise ImportError(f"无法读取文件（{error.strerror or '文件系统错误'}）") from None
    except (ValueError, RecursionError):
        raise ImportError("JSON 数值过大或嵌套过深") from None
    return parse_document(value, str(path))


def _deduplicate(accounts: list[Account]) -> list[Account]:
    result: dict[str, Account] = {}
    for account in accounts:
        previous = result.pop(account.identity_id, None)
        if previous and not account.refresh_token:
            account = replace(account, refresh_token=previous.refresh_token)
        result[account.identity_id] = account
    return list(result.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocx-import",
        description="将 ChatGPT Web Session / sub2api OpenAI OAuth JSON 导入 OpenCodex。",
        epilog="默认写入 OPENCODEX_HOME 或 ~/.opencodex；所有文件通过校验后才执行导入。",
    )
    parser.add_argument("files", nargs="+", type=Path, metavar="FILE.json", help="可混用两种格式")
    parser.add_argument("--home", type=Path, help="OpenCodex 配置目录")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入配置或备份")
    parser.add_argument("--no-activate", action="store_true", help="保留当前激活账号；无激活账号时选择首个")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    root = (args.home or Path(os.environ.get("OPENCODEX_HOME") or "~/.opencodex")).expanduser()
    accounts: list[Account] = []
    errors: list[dict] = []
    for file in args.files:
        path = file.expanduser()
        try:
            accounts.extend(_read_file(path))
        except ImportError as error:
            errors.append({"file": str(path), "error": str(error)})
    if errors:
        print(json.dumps({"imported": [], "errors": errors}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    unique_accounts = _deduplicate(accounts)
    try:
        result = apply_accounts(root, unique_accounts, activate=not args.no_activate, dry_run=args.dry_run)
    except ImportError as error:
        message = str(error)
    except OSError as error:
        message = f"无法写入 OpenCodex 配置（{error.strerror or '文件系统错误'}）"
    else:
        result.update({"dry_run": args.dry_run, "input_accounts": len(accounts), "home": str(root)})
        short_lived = sum(not account["refreshable"] for account in result["imported"])
        if short_lived:
            result["warnings"] = [
                f"{short_lived} 个账号没有 refresh token；到期后需重新导入 Session。"
            ]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"imported": [], "errors": [{"error": message}]}, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1
