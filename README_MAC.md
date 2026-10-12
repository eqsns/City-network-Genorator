# CNG：Apple 芯片 Mac 构建与接续

**这是源码构建包，不是已经完成验证的 Mac 安装包。** Windows 便携版不能在 macOS 上运行。当前在 Windows 上完成了平台适配及可运行于 Windows 的回归测试，macOS 窗口、原生计算和 `.app` 打包仍需在你的 Mac 上验证。

## 在 Mac 上继续

将 `CNG-macOS-AppleSilicon-Source.zip` 复制到 Mac，解压到可写目录，用 Codex 打开解压后的 `CNG` 文件夹。可直接发送：

> 请阅读 README_MAC.md，在这台 Apple 芯片 Mac 上继续构建 CNG。运行构建和测试，修复实际遇到的兼容问题；验证地图、一个新区域的真实联网生成、暂停恢复、取消、缓存清理、导出和重启恢复。不要把未经测试的包当作正式可用版本，保留 SWMManywhere 署名及所有许可证。

构建需要原生 **arm64 Python 3.12**、联网安装依赖，以及 Apple 命令行开发工具。避免使用 Rosetta 的 x86_64 Python。终端进入项目目录后运行：

```bash
bash build-macos.command
```

可通过 `CNG_PYTHON=/完整路径/python3.12 bash build-macos.command` 指定解释器；`bash build-macos.command --prepare-only` 只准备依赖并测试。

脚本创建独立 `.venv-macos`，从 Windows 已锁定版本中排除 Windows 专用包，添加 Cocoa/WebKit 依赖。**这份派生依赖不是已验证的 Mac 锁文件**；成功安装、检查和测试后，才会在 `.cache/requirements-macos-arm64.lock.txt` 保存 Mac 实际解析版本。依赖安装失败时应先处理具体兼容问题，不要直接忽略错误。

WhiteboxTools 使用官方 Apple 芯片压缩包，校验 ZIP、可执行文件 SHA256 和 Mach-O arm64 架构，并实际执行 `--version`。校验不一致时停止。PyInstaller 在 Mac 上构建 `.app`，使用系统 WKWebView，不携带 Windows WebView2。

## 产物与数据位置

- 构建输出：`.cache/macos-release-stage/CNG 城市排水管网生成.app`。
- 待验收包：`dist/CNG-macOS-AppleSilicon-UNVERIFIED.zip` 及 SHA256，使用 `ditto` 保留应用包链接结构。
- 默认数据：`~/Library/Application Support/SWMManywhereStudio/data`。数据不写入 `.app`，移动应用不改变成果位置。
- WebKit、数据目录偏好和启动日志放在同一个 `SWMManywhereStudio` 用户支持目录。
- 如需迁移 Windows 成果，先备份，复制 Windows 的整个 `data` 内容到上述 Mac 数据目录，再启动并验证。不要复制 Windows 的 `_internal`、虚拟环境或 WebView2。

默认构建只有本机临时签名，没有 Apple 公证。公开分发前需在 Mac 上选择合适的 Developer ID 签名、公证流程；构建脚本不移除隔离属性，也不关闭系统保护。可选 `CNG_CODESIGN_IDENTITY` 指定已有签名身份，公证应在最终验收后处理。

## 必须完成的 Mac 验收

1. 从 Finder 打开 `.app`，窗口、中文、主地图、框选、手动坐标和缩放正常。
2. 用全新数据目录，在新的小范围完成真实联网生成；检查所有数据源、管线位置和导出模型。
3. 暂停后进度不虚增，恢复使用同一个任务；取消时 Python 和 WhiteboxTools 子进程全部结束。
4. 重命名、删除时缓存提示、清除缓存、关闭窗口确认及再次启动正常。
5. 放到含中文和空格的路径，在没有开发 Python 环境的 Mac 账户或另一台 Apple 芯片 Mac 验证运行。
6. 检查 SWMM 模型及降雨相对引用；在 Mac 上运行前端测试 `node --test tests/frontend-state.test.cjs`。

完成后记录 macOS 版本、芯片、依赖锁文件、真实生成范围/耗时、测试结果，再移除包名中的 `UNVERIFIED`。构建成功和自动测试通过本身不能代替上述验收。

## 来源

计算核心来自 [ImperialCollegeLondon/SWMManywhere](https://github.com/ImperialCollegeLondon/SWMManywhere) 0.2.2（BSD-3-Clause）。本项目增加中文桌面界面、任务控制、下载恢复和缓存管理；原始版权、许可证见 `vendor/LICENSE-SWMManywhere` 和 `THIRD-PARTY-NOTICES.md`。

构建参考：[PyInstaller 平台说明](https://pyinstaller.org/en/stable/index.html)、[macOS 架构与签名](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)、[pywebview 安装要求](https://pywebview.flowrl.com/guide/installation.html)。
