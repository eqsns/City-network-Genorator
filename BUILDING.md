# 构建说明

## Windows 10/11 x64

1. 安装 Python 3.12 x64，在项目目录建立 `.venv-build`：

   ```powershell
   py -3.12 -m venv .venv-build
   .venv-build\Scripts\python.exe -m pip install -r requirements-lock.txt
   ```

2. 准备原生资源。源码仓库不提交这些大型二进制：
   - `native/whitebox_tools.zip`：pywbt 兼容的官方 Windows WhiteboxTools ZIP，保留原 ZIP 结构和许可证。
   - `native/WebView2/`：解压后的 Microsoft WebView2 Fixed Version x64 运行时；程序文件应直接位于此目录，而不是再嵌套一层文件夹。已测版本和官方来源见 `THIRD-PARTY-NOTICES.md`。发布新版时应检查运行时更新。
3. 在允许运行本地构建脚本的 PowerShell 中执行 `./build.ps1`。
4. 执行 `.venv-build\Scripts\python.exe packaging/release.py` 创建 `dist/SWMManywhereStudio-Windows-x64.zip` 和 SHA256。压缩的是 `.cache/release-stage` 中的干净构建，不是含有用户 `data` 的使用目录。

发布前需在干净 Windows 环境验证启动、真实联网生成及导出。项目中的既有测试报告仅对应报告所列构建和环境，不自动覆盖未来重建结果。

## Apple 芯片 Mac

阅读 [README_MAC.md](README_MAC.md)。不能把 Windows EXE 改后缀当作 Mac 应用，也不能在 Windows 上完成所需的 macOS 原生编译和验收。

Mac 脚本不会使用 Windows WebView2、pythonnet 或 Windows WhiteboxTools。应用使用系统 WebKit，数据存入用户的 Application Support 目录。

## 源码交接包

执行 `python packaging/source_archive.py` 生成 `dist/CNG-macOS-AppleSilicon-Source.zip` 和校验文件。仅包含版本控制文件清单中的源码、测试、说明、界面截图和许可证，排除本地数据、缓存、Git 元数据、虚拟环境和原生运行时。
