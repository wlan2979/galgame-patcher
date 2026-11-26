# 补丁清单

清单把补丁文件与安装目录、补丁版本及预期 SHA256 关联起来。先在发布页核对游戏的商店、语言、版本和安装方式，再填写清单。Steam App ID 用于定位游戏目录。同一游戏如有多个发行版，可为已确认兼容的版本各建一个条目，并在 `description` 写明适用条件。

## 新建与预览

将以下模板保存为 `my_catalog.json`。占位 URL、应用编号和哈希需要替换为实际值。用于远程安装的 `expected_sha256` 应从可信发布页取得，长度为 64 个十六进制字符。

```json
{
  "version": 1,
  "games": [
    {
      "id": "my_game",
      "name": "示例游戏",
      "steam_app_id": 123456,
      "install_dir_name": "MyGame",
      "executable_name": "Game.exe",
      "patch_url": "https://example.com/official/my_game_update_1.2.zip",
      "patch_version": "1.2",
      "expected_sha256": "REPLACE_WITH_PUBLISHER_SHA256",
      "archive_password": null,
      "target_subdir": "",
      "description": "填写适用的商店、游戏版本、语言和安装方式"
    }
  ]
}
```

```bash
galpatch catalog validate --catalog ./my_catalog.json
galpatch catalog list --catalog ./my_catalog.json
galpatch catalog verify --catalog ./my_catalog.json
galpatch catalog export ./shared_catalog.json --catalog ./my_catalog.json
galpatch auto my_game --catalog ./my_catalog.json --game-dir ./MyGame --dry-run
galpatch auto my_game --catalog ./my_catalog.json --game-dir ./MyGame
```

出于安全考虑，保存和导出清单时默认**不写入** `archive_password` 明文密码（包括 `catalog add`、`catalog update` 与默认导出）。安装加密补丁时请通过 `--password` 临时提供密码；仅在确实需要备份含密码副本时，显式使用 `galpatch catalog export <输出文件> --include-passwords`。

也可以用命令逐条维护清单，不必手写 JSON。`catalog add` 按 `id` 新增或覆盖条目；省略的可选字段会保留原值：

```bash
galpatch catalog add --catalog ./my_catalog.json \
  --id my_game --name "示例游戏" --app-id 123456 \
  --url "https://example.com/official/my_game_update_1.2.zip" \
  --sha256 $publisherSha256 --patch-version 1.2 \
  --description "Steam 版 / 简体中文 / 覆盖安装"
galpatch catalog remove my_game --catalog ./my_catalog.json
```

`catalog validate` 检查空或重复的 id、缺少名称、非 http(s) 链接、无效 SHA256、越界的 `target_subdir` 以及含路径分隔符的安装目录名或主程序名；发现错误时返回非零状态码。远程链接缺少 `expected_sha256` 只给出警告。`catalog add` 保存前执行同一套校验，有错误时不会写入文件。`add` 与 `remove` 只编辑本地清单，不接受远程 URL 作为 `--catalog`。

`catalog verify` 检查链接能否响应 HEAD 请求；不支持 HEAD 的站点会自动改用只取首字节的 GET 复检。站点可能限制自动请求，状态异常时可在浏览器中核对发布页。`auto` 会比对补丁文件的 SHA256。`patch_version` 是补丁的版本说明，游戏构建版本需由使用者核对。安装器类补丁请依照发布者的安装说明。

可用 `galpatch catalog update` 拉取远程清单，已有文件会保留 `.bak` 副本。远程清单能同时替换补丁链接和预期 SHA256，因此必须锁定清单文件本身：

```bash
# 从可信发布页取得清单文件的 SHA256 后锁定
galpatch catalog update <清单URL> --catalog ./my_catalog.json \
  --trust-catalog-sha256 <清单文件SHA256>
```

未提供 `--trust-catalog-sha256` 时会拒绝更新；明确知晓风险时才使用 `--allow-untrusted-catalog`。更新前会校验远程清单条目，合并后再次校验，并逐条打印新增/变更的 URL 与 SHA256 供核对。省略 `--catalog` 时使用默认用户清单。Windows 默认为 `%APPDATA%\galgame-patcher\catalog.json`，未设置 `APPDATA` 时为 `~/.config/galgame-patcher/catalog.json`。

## Python API

```python
from galgame_patcher import GalgamePatcher, PatchCatalog

catalog = PatchCatalog.load("./my_catalog.json")
patcher = GalgamePatcher()
preview = patcher.apply_from_catalog(
    "my_game", catalog=catalog, game_dir="./MyGame", dry_run=True
)
print(preview.message)
```

直接安装本地文件可调用 `apply_patch("./patch.zip", game_dir="./MyGame")`。安装前可传入 `expected_sha256`，也可对加密包传入 `password`。

调用 `rollback_patch("./MyGame")` 完整回滚，或传入 `mode="keep-patches"` 撤回最近一次安装。用 `GalgamePatcher().rollback_history("./MyGame")` 查询记录 ID 和 UTC 时间，再用 `rollback_patch("./MyGame", mode="keep-patches", to=record_id)` 恢复到某次安装完成后的状态。扫描结果的 `patched` 为 `None` 时，备份清单需要检查。

查询状态可调用 `GalgamePatcher().install_status("./MyGame")`，返回 `InstallStatus`，其 `state` 为 `not_installed`、`installed`、`installed_no_history`、`incomplete`、`rolled_back` 或 `invalid`。若上次安装中断（`incomplete`），调用 `recover_incomplete_install("./MyGame")` 完整回滚残留改动；它在加锁后再次确认清单仍为 `in_progress`，状态已变化时不会执行回滚。
