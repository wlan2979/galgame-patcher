# galgame-patcher

一个零第三方依赖的轻量级 Python 库与命令行工具，用于**自动定位本地 Galgame 安装目录并一键安装 / 还原游戏补丁**（如汉化补丁、去和谐补丁、语音/CG 扩展包等）。

## 核心特性

- **自动定位游戏目录**：读取 Windows 注册表与 Steam `libraryfolders.vdf` / `appmanifest_<appid>.acf`，支持通过 Steam `AppID` 或游戏目录名自动发现多磁盘 Steam 库中的游戏路径，也支持手动指定路径或额外搜索目录。
- **多形态补丁支持**：
  - 单个补丁文件（直接复制覆盖至游戏根目录或指定子目录）
  - 解压后的补丁文件夹（保持相对目录结构递归写入）
  - 压缩包格式（`.zip`、`.tar`、`.tar.gz`、`.tgz`、`.tar.bz2`、`.tar.xz`），支持自动剥离单层顶层套壳目录，并内置 Zip Slip 路径穿越防护
- **安全备份与一键回滚**：打补丁前自动将即将被覆盖的原文件备份至 `<游戏目录>/.galpatch_backup/` 并记录新增文件清单，支持随时一键还原至未打补丁前的纯净状态。
- **SHA256 完整性校验**：支持在安装前校验补丁文件或压缩包的 SHA256 摘要，防止下载损坏或误装错误补丁。
- **开箱即用**：仅依赖 Python 3.9+ 标准库，既可作为 Python 库集成到你的专属一键补丁器中，也可直接作为命令行工具 (`galgame-patcher` / `galpatch`) 使用。

## 支持的常见 Galgame 引擎补丁格式

内置常量 `COMMON_PATCH_EXTENSIONS` 涵盖常见视觉小说与 Galgame 引擎资源/补丁后缀：

- Kirikiri / 吉里吉里：`.xp3`
- BGI / Ethornell、Artemis、Qlie 等：`.arc`、`.pck`、`.pac`、`.int`
- YU-RIS：`.ypf`
- Ren'Py：`.rpa`
- 通用资源、脚本与补丁动态库：`.bin`、`.dat`、`.afs`、`.cpk`、`.ks`、`.scn`、`.psb`、`.dll`、`.exe`、`.ini`、`.cfg`、`.json`、`.txt`

## 安装方式

目前仓库为私有状态时，可通过克隆后本地安装或通过 SSH/PAT 从 GitHub 安装：

```bash
# 本地开发/可编辑安装
git clone https://github.com/wlan2979/galgame-patcher.git
cd galgame-patcher
pip install -e .
```

待仓库切换为公开或发布到 PyPI 后，可直接通过 pip 安装：

```bash
pip install galgame-patcher
```

## 快速上手

### 1. Python 库用法

```python
from galgame_patcher import GameProfile, apply_patch, rollback_patch

# 定义游戏信息（推荐填写 Steam AppID 与可执行文件名做校验）
profile = GameProfile(
    name="Sample Galgame",
    steam_app_id=123456,
    install_dir_name="Sample Galgame",
    executable_name="Game.exe",
)

# 一键定位游戏目录并安装补丁（自动备份原文件并剥离压缩包顶层套壳目录）
result = apply_patch(
    patch_source="./sample_patch.zip",
    profile=profile,
    expected_sha256="可选的预期sha256校验值",
)
print(result.message)
print("游戏目录:", result.game_dir)

# 如需卸载补丁并还原原文件：
rollback_result = rollback_patch(result.game_dir)
print(rollback_result.message)
```

如果已经有游戏本地路径，也可以直接传入 `game_dir`：

```python
from galgame_patcher import apply_patch

result = apply_patch(
    patch_source="./patch.xp3",
    game_dir=r"D:\Games\MyGalgame",
)
```

### 2. 命令行用法 (`galgame-patcher` / `galpatch`)

```bash
# 1) 根据 Steam AppID 自动定位游戏并安装补丁
galpatch apply ./patch.zip --app-id 123456

# 2) 按游戏名定位并带 SHA256 校验安装到游戏下的特定子目录
galpatch apply ./patch.xp3 --game-name "MyGalgame" --sha256 <64位哈希>

# 3) 仅查找并打印游戏安装路径
galpatch locate --app-id 123456

# 4) 计算补丁文件的 SHA256 值
galpatch hash ./patch.zip

# 5) 一键卸载补丁并恢复游戏目录
galpatch rollback "D:\SteamLibrary\steamapps\common\MyGalgame"
```

## 运行测试

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests -v
```

## 后续公开仓库与发布到 PyPI 指引

1. **将私有仓库切换为公开 (Public)**：
   ```bash
   gh repo edit wlan2979/galgame-patcher --visibility public --accept-visibility-change-consequences
   ```
2. **发布到 PyPI**（可选）：
   ```bash
   python -m pip install --upgrade build twine
   python -m build
   python -m twine upload dist/*
   ```

## License

[MIT License](LICENSE)
