# 测试与打包

在仓库根目录运行单元测试：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

安装开发工具并运行代码检查、类型检查与覆盖率检测：

```powershell
python -m pip install -e ".[dev]"
ruff check src tests scripts
mypy src scripts
$env:PYTHONPATH = "src"
coverage run -m unittest discover -s tests -v
coverage report
```

在 Windows 上构建独立版：

```powershell
python -m pip install -e ".[build,archive]"
python scripts/build_exe.py
```

`scripts/build_exe.py` 会按顺序探测并打包 `7za.exe` / `7z.exe`（及同目录的 `7z.dll` / `7zxa.dll`）到独立包的 `bin/` 目录，使独立版在未预装 7-Zip 的 Windows 机器上也能解压 `.rar` 与 `.7z` 补丁：

1. 命令行 `--seven-zip <路径>` 或环境变量 `GALGAME_PATCHER_7Z_PATH`
2. 仓库内 `vendor/7zip/7za.exe` 或 `vendor/7zip/7z.exe`
3. `PATH` 中的 `7za` / `7z` 或 `C:\Program Files\7-Zip\` 下的可执行文件

若构建环境中找不到任何 7-Zip 二进制，脚本会向标准错误流打印醒目警告而不会静默跳过；在正式发布构建中可加 `--require-7z` 强制要求内置 7-Zip。

产物路径为 `dist/galgame-patcher-windows-x64.exe`。发布前核对打包依赖（含 7-Zip 的 LGPL/BSD 声明）的许可证及随包提供的版权声明，并在 Release 发布页注明各产物的 SHA256。
