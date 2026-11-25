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
galpatch catalog list --catalog ./my_catalog.json
galpatch catalog verify --catalog ./my_catalog.json
galpatch auto my_game --catalog ./my_catalog.json --game-dir ./MyGame --dry-run
galpatch auto my_game --catalog ./my_catalog.json --game-dir ./MyGame
```

`catalog verify` 检查链接能否响应 HEAD 请求；不支持 HEAD 的站点会自动改用只取首字节的 GET 复检。站点可能限制自动请求，状态异常时可在浏览器中核对发布页。`auto` 会比对补丁文件的 SHA256。`patch_version` 是补丁的版本说明，游戏构建版本需由使用者核对。安装器类补丁请依照发布者的安装说明。

可用 `galpatch catalog update <清单URL> --catalog ./my_catalog.json` 拉取受信任的远程清单，已有文件会保留 `.bak` 副本。省略 `--catalog` 时使用默认用户清单。Windows 默认为 `%APPDATA%\galgame-patcher\catalog.json`，未设置 `APPDATA` 时为 `~/.config/galgame-patcher/catalog.json`。

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
