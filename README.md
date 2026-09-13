# ocx-import

把 **ChatGPT Web Session JSON** 和 **sub2api OpenAI OAuth JSON** 直接导入 OpenCodex 账号池。导入过程不发起网络请求。

## 使用

需要 Python 3.13+ 和 uv。在项目目录直接运行：

```bash
uv run ocx-import session.json sub2api.json
uv run ocx-import --dry-run *.json
```

安装为命令后，使用你需要的简写：

```bash
uv tool install --editable .
export PATH="$(uv tool dir --bin):$PATH"
hash -r
ocx-import file1.json file2.json
```

安装后可直接使用 `ocx-import file1.json file2.json`；项目内也可用 `uv run ocx-import`。

```bash
ocx-import --dry-run session.json         # 仅预览，完全不写入
ocx-import --no-activate session.json     # 保留当前激活账号
ocx-import --home /tmp/ocx-test a.json    # 指定目标配置目录
OPENCODEX_HOME=/path/to/home ocx-import a.json b.json
ocx-import --help
```

配置目录优先级：`--home` > `OPENCODEX_HOME` > `~/.opencodex`。默认激活最后导入的账号；`--no-activate` 保留当前选择，目标尚无激活账号时选择首个导入账号。

## 输入格式

可混合传入多个文件，支持单对象、根数组，以及 sub2api 的 `accounts` 包装。接受 UTF-8 和带 BOM 的 UTF-8 JSON。

Session JSON 字段示例（占位值不能用来登录）：

```json
{
  "user": {"id": "user-example", "email": "user@example.com"},
  "account": {"id": "workspace-example", "planType": "plus"},
  "accessToken": "<真实的 access token JWT>",
  "expires": "2030-01-01T00:00:00Z"
}
```

sub2api 示例：

```json
{
  "exported_at": "2026-09-13T00:00:00Z",
  "proxies": [],
  "accounts": [
    {
      "name": "OpenAI account",
      "platform": "openai",
      "type": "oauth",
      "credentials": {
        "access_token": "<真实的 access token>",
        "refresh_token": "<可选的真实 refresh token>",
        "chatgpt_account_id": "workspace-example",
        "chatgpt_user_id": "user-example",
        "email": "user@example.com",
        "plan_type": "plus"
      }
    }
  ]
}
```

- 缺失的邮箱、workspace ID、用户 ID、套餐可从 access / ID token 的 JWT claims 中补充。显式身份字段与 token claims 冲突时拒绝导入。
- access token 必填；`accessToken` / `access_token`、`refreshToken` / `refresh_token` 均可读取。OpenCodex 账号池不需要存储或伪造 ID token。
- **没有 refresh token 时，要求 access token 的数值型 JWT `exp` 距现在超过 60 秒。** Session 的 `expires` 可能是网页会话有效期，不能覆盖 access token 的实际到期时间。解析 JWT 只提取未验证的元数据，并不验证签名或账号权限。
- 有 refresh token 时优先使用 JWT `exp`，再读取 `expires_at` / `expiresAt` / `expires` / `expired`（秒、毫秒或 ISO 时间）。未知到期时间写入 `0`，交由 OpenCodex 首次使用时刷新；不虚构 token 有效期。
- Web Session 通常不含 refresh token，`sessionToken` 也不能作为 refresh token。短期账号到期后需要重新导出并导入 Session；导入成功只表示本地格式与写入成功，实际调用取决于 token 和账号权限。
- 本工具支持 sub2api 的 **OpenAI OAuth** 账号；其他平台、API key 和 Agent Identity 会报错。`proxies` 等 sub2api 服务配置不迁移。

## 写入与备份

写入 `config.json`、`codex-accounts.json`，必要时维护与旧导入器兼容的 `ocx-import-identities.json`。使用 OpenCodex 2.42 的 `config-mutation.sqlite` 事务锁协调写入，并更新配置 generation。

同一 workspace 下按用户 ID 去重；没有用户 ID 时按规范化邮箱回退。重复账号使用最后出现的 access token，保留已有非空 refresh token。旧版导入器创建的账号会尽量原位更新，身份有歧义时拒绝猜测。已有的其他账号和无关配置字段会保留；OpenAI provider 会配置为 Codex 账号池。

所有输入文件均通过校验后才开始写入，任一输入错误时整批不导入。写入前备份至目标目录的 `backups/ocx-import-*`，文件使用 `0600`，备份目录使用 `0700`。每个 JSON 文件以临时文件原子替换；遇到写入错误会尝试恢复原始文件。突然断电或进程被强制终止时，多个文件之间无法保证整体原子性，可使用备份恢复。

标准输出为 JSON 摘要（包含账号邮箱、毫秒级 `expires_at`、是否可刷新和备份路径，不包含 token）。错误输出到 stderr；退出码 `0` 为成功，`1` 为读取、校验或写入失败，`2` 为命令行参数错误。`--dry-run` 不创建配置、备份或 SQLite 文件。

## 开发与验证

```bash
uv run python -m unittest discover -s tests -v
uv build
```

格式参考：[GPTSession2CPAandSub2API](https://github.com/gtxx3600/GPTSession2CPAandSub2API)。OpenCodex 存储结构对照本机 `@bitkyc08/opencodex` 2.42.0，账号 ID 和旧记录兼容逻辑参考 `opencodex-account-importer` 0.4.10；此处导入的是 OpenCodex 账号池，不会改写原生 Codex 的 `auth.json`。
