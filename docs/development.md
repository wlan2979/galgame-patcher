# 测试与打包

在仓库根目录运行测试。

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

在 Windows 上构建独立版。

```powershell
python -m pip install -e ".[build,archive]"
python scripts/build_exe.py
```

产物路径为 `dist/galgame-patcher-windows-x64.exe`。发布前核对打包依赖的许可证及随包提供的版权声明，并在 Release 发布页注明各产物的 SHA256。
