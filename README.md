# galgame-patcher

一个零第三方强制依赖的轻量级 Python 库与桌面可视化工具，用于**自动扫描本机 Galgame、在线下载补丁、支持 `.zip` / `.7z` / `.rar` / 密码解压并一键安装或还原游戏补丁**。

## 核心特性 (v0.2.0)

- **自动扫描本机 Galgame & 在线下载补丁**：
  - 读取 Windows 注册表与 Steam `libraryfolders.vdf` / `appmanifest_<appid>.acf`，自动扫描本机所有硬盘 Steam 库中已安装的游戏及补丁状态。
  - 支持加载自定义本地/远程 JSON 补丁源清单（`PatchCatalog`），一键从 URL 流式下载补丁、自动本地缓存复用并校验 SHA256。
- **桌面图形界面 (GUI) & 单文件 `.exe`**：
  - 内置基于标准库 `tkinter` 的桌面窗口（执行 `galpatch gui` 或直接双击 Release 中提供的 `galgame-patcher-windows-x64.exe`）。
  - 无需安装 Python 或命令行操作，点选扫描到的游戏即可一键打补丁或还原备份。
- **全格式与密码自动尝试解压**：
  - 支持单文件补丁（`.xp3`、`.arc`、`.pck`、`.pac`、`.ypf`、`.rpa` 等）、补丁文件夹以及 `.zip`、`.7z`、`.rar`、`.tar` / `.tar.gz` / `.tgz` / `.tar.bz2` / `.tar.xz` 压缩包。
  - 自动剥离单层顶层套壳目录，内置 Zip Slip 路径穿越防护。
  - 支持显式传入解压密码，或未指定时自动轮询尝试常见 Galgame 补丁解压密码（如 `galgame`、`patch`、`chs`、`123456` 等）。
- **安全备份与一键回滚**：
  - 打补丁前自动将即将被覆盖的原文件备份至 `<游戏目录>/.galpatch_backup/` 并记录新增文件清单，支持随时一键还原至未打补丁前的纯净状态。

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
# 1) 扫描本机已安装的清单 Galgame 及当前补丁状态
galpatch scan --only-installed

# 2) 使用自定义清单自动定位游戏、下载并安装补丁
galpatch auto senren_banka --catalog ./my_catalog.json

# 3) 直接传入远程 URL 或加密 .7z / .zip 安装补丁（支持自动尝试常见密码）
galpatch apply https://example.com/patch.7z --app-id 1144400 --password galgame

# 4) 安装本地补丁并附带额外候选密码
galpatch apply ./patch.zip --game-name "MyGalgame" --try-password mypass

# 5) 仅查找并打印游戏安装路径
galpatch locate --app-id 1144400

# 6) 计算补丁文件的 SHA256 值
galpatch hash ./patch.zip

# 7) 一键卸载补丁并恢复游戏目录
galpatch rollback "D:\SteamLibrary\steamapps\common\MyGalgame"
```

### 3. Python 库用法

```python
from galgame_patcher import GalgamePatcher, PatchCatalog, apply_patch, rollback_patch

# 1) 使用清单自动扫描本机已安装的 Galgame
catalog = PatchCatalog.load("./my_catalog.json")  # 或不传参数使用内置清单模板
for status in catalog.scan_games(only_installed=True):
    print(status.entry.name, status.game_dir, "已打补丁:" if status.patched else "未打补丁")

# 2) 按清单条目自动下载、校验并打补丁
patcher = GalgamePatcher()
result = patcher.apply_from_catalog("senren_banka", catalog=catalog)
print(result.message)

# 3) 直接传入本地加密压缩包或远程 URL 打补丁
result = apply_patch(
    patch_source="./sample_patch.7z",
    app_id=1144400,
    password="custom_password",  # 不传时也会自动尝试常见密码
)

# 4) 如需卸载补丁并还原原文件：
rollback_result = rollback_patch(result.game_dir)
print(rollback_result.message)
```

### 4. 自定义清单 JSON 格式示例

```json
{
  "version": 1,
  "games": [
    {
      "id": "my_galgame",
      "name": "示例 Galgame",
      "steam_app_id": 123456,
      "install_dir_name": "MyGalgame",
      "executable_name": "Game.exe",
      "patch_url": "https://example.com/patches/my_galgame_chs.7z",
      "patch_version": "1.2",
      "expected_sha256": null,
      "archive_password": "galgame",
      "target_subdir": "",
      "description": "中文汉化补丁 v1.2"
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
