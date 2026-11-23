# galgame-patcher

一个零第三方强制依赖的轻量级 Python 库与桌面可视化工具，用于**自动扫描本机 Galgame、在线下载补丁、支持 `.zip` / `.7z` / `.rar` / 密码解压并一键安装或还原游戏补丁**。

> [!NOTE]
> 库与命令行只依赖 Python 标准库。桌面 GUI 额外需要 `tkinter`：Windows / macOS 官方安装包已自带；Debian/Ubuntu 需执行 `sudo apt install python3-tk`。缺少 tkinter 时仅 GUI 不可用，库与 CLI 照常工作。

> [!WARNING]
> **安全说明**
> - 本工具不验证第三方补丁的安全性；请只从官方或你信任的来源获取补丁；安装前请核对 SHA256。
> - **搜索只提供链接，不自动下载**：`galpatch search` 与 GUI 搜索窗格仅列出公开链接供参考，请自行在浏览器核验并下载到本地，再通过工具导入本地文件核对 SHA256 后安装。
> - **拒绝自动下载或直接安装裸可执行文件**：`.exe` / `.dll` / `.msi` / `.bat` / `.ps1` 等格式永远不作为直链包自动下载；GUI 侧彻底禁止从远程 URL 直接安装，CLI 侧拒绝无 `expected_sha256` 的远程 URL（除非显式传入 `--allow-untrusted-url`）。

> [!IMPORTANT]
> **使用声明**
> - 本项目只是一个通用的本地文件安装 / 备份 / 还原工具，**不托管、不提供、不索引任何游戏补丁或游戏资源**，内置清单仅包含一个格式示例条目。
> - 请只用于**官方发布或已获得权利人授权**的补丁（如官方更新、官方语言包、作者明确允许分发的 Mod）。
> - 请勿在清单中收录指向盗版资源或未经授权分发内容的链接，本仓库也不接受此类清单或 PR。
> - 修改游戏文件可能违反部分游戏的用户协议，后果由使用者自行承担。

## 核心特性 (v0.3.2)

- **事务化安装与安全回滚（零毁档保护）**：
  - 安装过程每备份/覆盖一个文件即原子写入 `manifest.json` (`in_progress` → `committed`)；中途遇到任何异常（权限不足、磁盘满、写入后 SHA256 校验失败等）会**自动回滚本轮全部变更**，并将残留进度恢复逻辑集成在 `rollback()` 中。
  - 安装前前置检测目标文件是否被运行中的游戏占用（抛出 `GameRunningError`，支持 `--force-locked` 跳过）；通过 `.galpatch_backup/.lock` 排他锁防止多实例并发破坏（自动清理已退出进程的陈旧锁）。
  - 回滚失败时保留 `.galpatch_backup` 副本与失败原因详情，不到全部确认成功绝不删除备份。
  - 支持 `--dry-run` 仅预览模式，不写盘即可查看将覆盖/新增的文件清单。
- **支持 Steam + 手工登记 + 非 Steam 深度扫描**：
  - 自动读取 Windows 注册表与 Steam `libraryfolders.vdf` / `appmanifest_<appid>.acf`。
  - 支持 `galpatch add-game <path>` 将 DMM / DLsite / 实体盘等非 Steam 游戏登记到 `%APPDATA%\galgame-patcher\games.json`；支持 `--deep` 深度扫描常见目录中的特征 Galgame 文件夹。
- **多站点在线补丁检索（只给链接，不自动下载）与本地文件核验**：
  - 提供 `galpatch search <游戏名或网页URL>`（及 GUI 内置搜索按钮），已内置对接多个安全合规的官方/开源数据源：**VNDB API v2**、**柚子社官方网站 (`yuzu-soft.com/support.html`)**、**NekoNyan 官方补丁站 (`nekonyansoft.com/shop/patches`)**、**PCGamingWiki API**、**07th-Mod (`07th-mod.com`)** 以及 **GitHub Releases**（最低 10 stars、跳过可执行资产并标注白名单/未审核状态）。
  - 提供 `galpatch sites`（与 GUI「官方补丁站导航...」）汇总 Shiravune、Kagura Games、Denpasoft、Johren、JAST USA、MangaGamer、Sekai Project 等官方补丁页面快捷入口，并支持在 `%APPDATA%\galgame-patcher\sources.json` 中自由添加自定义站点 URL 与信任域名/GitHub owner 白名单。
  - 搜索结果**只提供链接与可信度提示，不自动下载**；用户可通过浏览器下载后导入本地文件，使用 `galpatch verify-file <path>` 或 GUI 预览区核对文件大小、修改时间与 SHA256（导入裸可执行文件需强制二次确认）。
  - 远程 URL 安装必须配置 `expected_sha256` 锁定内容，否则默认拒绝安装（`UntrustedSourceError`）。
- **清单管理**：
  - 支持加载本地/远程 JSON 清单，提供 `galpatch catalog update / list / verify` 进行版本化增量合并（保留 `.bak`）与 `HEAD` 链接探活。
- **桌面图形界面 (GUI) & 单文件 `.exe`**：
  - 左右分栏布局（左侧库列表 + 右侧详情/操作/日志面板），带 200ms 防抖搜索、后台队列异步进度与速度显示、`Ctrl+V` 剪贴板粘贴路径、窗口与目录记忆、`%APPDATA%\galgame-patcher\logs\gui.log` 日志落盘及危险操作二次确认。
  - 执行 `galpatch gui` 或直接双击 Release 中提供的 `galgame-patcher-windows-x64.exe` 即可使用。
- **多格式解压**：
  - 支持单文件补丁（`.xp3`、`.arc`、`.pck`、`.pac`、`.ypf`、`.rpa` 等）、补丁文件夹以及 `.zip`、`.7z`、`.rar`、`.tar*` 压缩包，全格式在写盘前完成路径穿越校验，外部命令禁用 stdin 并带 300s 超时防假死。
  - 加密压缩包需由用户提供密码（`--password` / `--try-password` / GUI 输入框 / 清单 `archive_password`），**程序不内置任何密码字典，也不会自动猜测密码**。

## 支持的常见 Galgame 引擎补丁格式

内置常量 `COMMON_PATCH_EXTENSIONS` 涵盖常见视觉小说与 Galgame 引擎资源/补丁后缀：

- Kirikiri / 吉里吉里：`.xp3`
- BGI / Ethornell、Artemis、Qlie 等：`.arc`、`.pck`、`.pac`、`.int`
- YU-RIS：`.ypf`
- Ren'Py：`.rpa`
- 通用资源、脚本与补丁动态库：`.bin`、`.dat`、`.afs`、`.cpk`、`.ks`、`.scn`、`.psb`、`.dll`、`.exe`、`.ini`、`.cfg`、`.json`、`.txt`

## 安装与使用方式

### 方式一：直接下载桌面单文件 `.exe`（推荐普通玩家）

从 GitHub [Releases](https://github.com/wlan2979/galgame-patcher/releases) 页面下载 `galgame-patcher-windows-x64.exe`，无需安装 Python，直接双击打开图形界面即可使用。

### 方式二：从源码安装 Python 库与命令行工具

```bash
git clone https://github.com/wlan2979/galgame-patcher.git
cd galgame-patcher
pip install -e .

# 如需内嵌加密 .7z 与 AES-ZIP 原生解压支持（不依赖外部 7-Zip/WinRAR）：
pip install -e ".[archive]"
```

## 快速上手

### 1. 启动桌面 GUI

```bash
galpatch gui
# 或
galgame-patcher-gui
```

### 2. 命令行用法 (`galpatch` / `galgame-patcher`)

```bash
# 1) 扫描本机已安装的清单游戏（含手工登记目录与深度扫描非 Steam 目录）
galpatch scan --only-installed --include-manual --deep

# 2) 手工登记非 Steam 游戏目录（自动推断目录名和最大 .exe）
galpatch add-game "D:\Games\MyDlsiteGame"

# 3) 上网检索公开补丁链接（只列出链接与可信度，不下载任何文件）
galpatch search "Senren Banka"
galpatch search "Senren Banka" --open 1
galpatch search "https://www.yuzu-soft.com"
galpatch search "https://example.com/patch-release-page.html" --json

# 3.5) 查看官方/开源补丁站快捷入口或初始化自定义站点配置文件
galpatch sites
galpatch sites --init-config
galpatch sites --open 2

# 4) 安装前检查本地补丁文件信息并核对 SHA256
galpatch verify-file ./downloaded_patch.zip

# 5) 清单远程更新、列表查看与链接探活
galpatch catalog update https://example.com/catalog.json
galpatch catalog list
galpatch catalog verify

# 6) 仅预览将修改的文件（Dry-Run，不写盘）
galpatch apply ./patch.zip --game-name "MyGame" --dry-run

# 7) 使用自定义清单自动定位游戏、校验 SHA256 后安装补丁
galpatch auto my_game --catalog ./my_catalog.json

# 8) 直接传入远程 URL 时必须提供预期 SHA256（无 SHA256 需显式加 --allow-untrusted-url）
galpatch apply https://example.com/official_update.7z --app-id 123456 --sha256 <64位哈希> --password <密码>

# 9) 不确定是哪个密码时，可多次传入候选密码（仅尝试你提供的这些）
galpatch apply ./patch.zip --game-name "MyGame" --try-password pw1 --try-password pw2

# 10) 仅查找并打印游戏安装路径
galpatch locate --app-id 123456

# 11) 计算补丁文件的 SHA256 值
galpatch hash ./patch.zip

# 12) 一键卸载补丁并恢复游戏目录
galpatch rollback "D:\SteamLibrary\steamapps\common\MyGame"
```

### 3. Python 库用法

```python
from galgame_patcher import GalgamePatcher, PatchCatalog, apply_patch, rollback_patch

# 1) 使用清单自动扫描本机已安装的游戏
catalog = PatchCatalog.load("./my_catalog.json")
for status in catalog.scan_games(only_installed=True):
    print(status.entry.name, status.game_dir, "已打补丁" if status.patched else "未打补丁")

# 2) 按清单条目自动下载、校验并打补丁
patcher = GalgamePatcher()
result = patcher.apply_from_catalog("my_game", catalog=catalog)
print(result.message)

# 3) 直接传入本地加密压缩包或远程 URL 打补丁
result = apply_patch(
    patch_source="./official_update.7z",
    app_id=123456,
    password="<密码>",  # 加密包必须提供；未加密可省略
)

# 4) 如需卸载补丁并还原原文件：
rollback_result = rollback_patch(result.game_dir)
print(rollback_result.message)
```

### 4. 自定义清单 JSON 格式示例

> 请只收录官方发布或已获授权的补丁链接。

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
      "expected_sha256": null,
      "archive_password": null,
      "target_subdir": "",
      "description": "官方更新补丁 v1.2"
    }
  ]
}
```

## 本地打包 Windows 单文件 `.exe`

```powershell
pip install pyinstaller py7zr pyzipper
python scripts/build_exe.py
```

打包产物将生成在 `dist/galgame-patcher-windows-x64.exe`。

## 运行测试

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests -v
```

## 后续公开仓库

```powershell
gh repo edit wlan2979/galgame-patcher --visibility public --accept-visibility-change-consequences
```

## License

[MIT License](LICENSE)
