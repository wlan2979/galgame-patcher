# galgame-patcher

用于查找 Galgame 安装目录、检索补丁来源，以及预览、安装或还原本地补丁的 Python 库、命令行和桌面工具。

## 安装

- **Windows x64 桌面版**从 [Releases](https://github.com/wlan2979/galgame-patcher/releases) 下载 `galgame-patcher-windows-x64.exe`，双击启动。独立版无需安装 Python。
- **Python 库与命令行**可从 Releases 下载 wheel，也可克隆仓库后从源码安装。

新版 Release 附带 `SHA256SUMS.txt`，可用其中的 SHA256 核对下载文件。

```bash
git clone https://github.com/wlan2979/galgame-patcher.git
cd galgame-patcher
python -m pip install -e .
galpatch gui
```

使用 `galpatch --version` 查看已安装的版本。

`galpatch search --verbose "游戏名"` 会显示来源失败详情。使用 `--json` 时，失败提示写入标准错误流，候选列表仍按 JSON 输出。

| 平台 | 工具与测试 |
| --- | --- |
| Windows | 提供 x64 独立版。Python 版支持 Windows 注册表和 Steam 安装目录探测。CI 测试 Python 3.11–3.13。 |
| Linux | 从 wheel 或源码安装。Ubuntu 上的 CI 测试 Python 3.11–3.13。使用 GUI 需要 Tk，例如 Debian/Ubuntu 的 `python3-tk`。 |
| macOS | Python 版可尝试从 wheel 或源码安装，需要带 Tk 的 Python 才能打开 GUI。项目目前没有 macOS CI。 |

Python 包要求 **3.9 或更新**，CI 目前覆盖 3.11–3.13。普通 ZIP、Tar、补丁文件和文件夹只需标准库。源码安装时可用 `python -m pip install -e ".[archive]"` 加装 `py7zr` 和 `pyzipper`，用于 7z 和 AES 加密 ZIP。RAR 支持取决于环境里的解压工具，例如 7-Zip 或 UnRAR。加密包需要提供密码。

## 搜索与安装

1. 使用 `galpatch search "Senren Banka"` 或 GUI 的在线搜索。搜索只显示候选链接，`galpatch sites` 可打开[发行商或社区资源](docs/sources.md)。
2. 在发布页确认商店、语言、游戏版本和安装方式。标为「手动安装」的补丁按发布者的说明操作。查找结果命中白名单仅表示来源匹配。
3. 对可以覆盖安装的本地补丁，将以下示例的文件和游戏目录换成实际路径。用发布者提供的预期 SHA256 核对文件，再预览将写入游戏目录的内容。

```powershell
galpatch verify-file ./patch.zip --sha256 $publisherSha256
galpatch apply ./patch.zip --game-dir ./MyGame --sha256 $publisherSha256 --dry-run
galpatch apply ./patch.zip --game-dir ./MyGame --sha256 $publisherSha256
galpatch rollback ./MyGame
```

如需强制校验本地补丁，请在安装命令同时指定 `--require-sha256` 和 `--sha256`。手动运行 `rollback` 会还原备份中的原文件并清理记录中的新增文件。安装中途报错时，自动回滚尝试撤回本轮写入。

先把 `$publisherSha256` 设为从可信发布页取得的 64 位十六进制哈希。`verify-file` 也可单独用于查看本地文件信息。远程安装仅在 CLI 和 Python 库提供，默认需要预期 SHA256。下载入口拦截文件名可识别的裸可执行文件和脚本。预览可能使用临时解压目录，游戏文件保持原样。

使用 `galpatch scan --only-installed --include-manual --deep` 扫描游戏，或用 `galpatch add-game "D:\Games\MyGame"` 登记非 Steam 游戏。

### 游戏发行版示例

| 补丁 | 适用游戏发行版 | 安装方式 |
| --- | --- | --- |
| [《千恋＊万花》v1.12 修正](https://www.yuzu-soft.com/products/senren/download.html) | 柚子社 2016 年发布，Steam 版兼容性未确认 | 解压后按官网说明运行发行商安装器 |
| [NekoNyan 补丁](https://patches.nekonyansoft.com/) | Steam 恢复补丁仅适用于 Steam 版，部分其他商店有单独更新 | 按作品卡片确认发行版和安装方式 |
| [07th-Mod《寒蝉》](https://wiki.07th-mod.com/Higurashi/Higurashi-Getting-started/) | 项目说明列出新版 MangaGamer 的 Steam 和 DRM-free 版本，包括 GOG | 按社区指南选择对应章节和安装方式 |

## 清单、开发与授权

- [补丁清单与 Python API](docs/catalog.md)
- [站点来源、费用与再分发](docs/sources.md)
- [测试与 Windows 打包](docs/development.md)

从 v0.3.5 起，本项目的代码和文档采用 [PolyForm Noncommercial 1.0.0](LICENSE)，商业用途须另行取得许可。个人非商业使用及许可中列明的其他用途按正文执行。分发时请附上许可条款或其网址，并保留 [版权声明](NOTICE)。

v0.3.4 及更早版本按各自发布时的 MIT 许可分发。游戏、补丁和其他第三方内容各自适用原有条款，本仓库不托管游戏或第三方补丁文件。请从发行商或获授权的发布者获取补丁。
