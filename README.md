# Lin Router

Lin Router 是本地 OpenAI 兼容中转站：它接收 Codex、Hermes 和通用 OpenAI 客户端的请求，按选定的连接组和模型转发到上游，同时保留健康、熔断、日志、测速和路由能力。

现已支持 **Windows** 与 **macOS** 跨平台运行，核心代理逻辑完全复用，仅托盘、开机自启、路径等系统能力做平台适配。

## 快速启动

### 桌面端

```bash
# 方式 1：直接运行源码
python desktop.py

# 方式 2：以模块方式启动（与 desktop.py 等价）
python -m linrouter

# 启动后仅驻留托盘/状态栏，不自动打开浏览器
python desktop.py --tray
python -m linrouter --tray
```

### Windows 产物

```text
dist\LinRouter_windows.exe
dist\LinRouter-Setup-v0.6.0-win-x64.exe
```

推荐分发安装包 `LinRouter-Setup-v0.6.0-win-x64.exe`；单文件免安装使用时可直接运行 `LinRouter_windows.exe`。

### macOS 产物

```text
dist/LinRouter.app
```

将 `LinRouter.app` 拖入 `/Applications`，首次启动请使用 **右键 → 打开**（未签名 App 会被 Gatekeeper 拦截）。

### 命令行模式

```bash
python app.py
```

默认地址：

```text
http://127.0.0.1:18400
http://127.0.0.1:18400/v1
```

客户端填写页面里生成的连接组 Key（`lr-...`），服务端会按 Key 绑定到对应连接组。v0.5.0 起旧全局 Key `lin-router` 已退役，如需跨多连接组 fallback 调度，请创建**聚合模型**并使用其专属的聚合模型 Key（`lr-ag-...`）。

## 桌面端行为

启动后程序会驻留在系统托盘（Windows）或菜单栏（macOS）：

- 左键/单击图标：打开管理面板
- 右键图标：打开主页 / 查看日志 / 编辑配置 / 复制地址 / 开机自启 / 启动最小化 / 退出（Key 请从管理面板的连接组或聚合模型处复制）
- 开机自启：
  - Windows：写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
  - macOS：写入 `~/Library/LaunchAgents/com.linrouter.launcher.plist` 并通过 `launchctl` 加载
- 启动最小化：启动后不自动打开浏览器，仅显示托盘/状态栏图标
- 单实例保护：重复启动会自动打开已有实例的管理面板
- macOS 下不显示 Dock 图标，体验与 Windows "最小化到托盘" 等价

## 平台数据路径

| 文件 | Windows | macOS |
|------|---------|-------|
| 配置文件 | 项目根目录 / 可执行文件同级父目录 `lin-router-config.json` | `~/Library/Application Support/LinRouter/lin-router-config.json` |
| 设置文件 | 配置文件同级目录 `lin-router-settings.json` | 配置文件同级目录 `lin-router-settings.json` |
| 请求日志 | 配置文件同级目录 `lin-router-logs.jsonl` | `~/.lin-router/lin-router-logs.jsonl` |

## 主要能力

- 连接组管理：火山方舟 / 中转站 / 通用 OpenAI 代理
- 聚合模型：多中转站 fallback 调度，按手动优先级顺序尝试
- 连接组级自动冷却：仅中转站启用
- 自动获取上游模型
- 最近请求日志、详情展开、筛选与 CSV 导出
- 代理测试
- 客户端接入信息快捷复制
- 复制连接组
- 跨平台系统托盘 / 状态栏、开机自启、启动最小化

## 模式说明

### 火山方舟

- 连接组填写：组名、Base URL、Ark API Key
- 模型填写：显示名称、EP ID

### 中转站

- 连接组填写：组名、Base URL
- 模型填写：显示名称、上游模型、价格组对应 API Key、价格组
- 仅中转站可开启 WAF 兼容
- 可设置自动冷却分钟数

### 通用 OpenAI 代理

- 连接组填写：组名、Base URL、上游 API Key
- 模型可映射到上游模型名，客户端未显式指定模型时按本地配置透传

## 聚合模型（v0.5.0+）

聚合模型用于替代旧全局 Key `lin-router` 与旧自动模型 `all-router-auto`，实现跨多个中转站的受控 fallback 调度。

- 每个聚合模型拥有独立的 `lr-ag-...` 路由 Key，可访问自身内部名及已配置的客户端公开别名。
- 客户端公开别名：在聚合配置中可按行填写 Codex 已识别的模型名（如 `gpt-5.5`）。别名仅在该聚合 Key 下命中同一候选链，方便客户端识别能力；不固定上游，也不会由 Lin Router 自动补写推理强度。
- 成员只能选自 `relay` 模式的连接组。
- 调度策略：
  - `priority`（手动优先级）：按成员在列表中的顺序依次尝试。
- 失败处理：成员失败且聚合模型配置了冷却分钟数时，该成员会进入冷却状态并在冷却期间被排除；所有成员均失败时返回 `503 all_aggregate_members_failed`，不会回退到全局 Key、其他聚合模型或非成员模型。
- 流式首包保护：流式响应一旦开始向客户端输出，即使后续上游失败也不会无感切换到其他成员，避免客户端收到混合内容。

## 自助接入 Codex / Hermes

管理台首页的“接入 Codex / Hermes”区域只会为**已经验证可用的中转站连接组**提供接入信息。它只帮助你准备本地入口，不会修改客户端配置、监听客户端请求，或判断客户端是否已经接入成功。

### 两类 Key 不可混用

| 名称 | 用途 | 填写位置 |
| --- | --- | --- |
| 上游 API Key | Lin Router 连接中转站 / 模型服务 | 连接组配置 |
| route key | Codex/Hermes 访问本机 Lin Router | 客户端接入信息 |

route key 是客户端到本机 Lin Router 的认证信息，**不是上游 API Key**；不要把它填回连接组，也不要把上游 API Key 填入 Codex 或 Hermes 的 Lin Router 接入配置。

### 快捷复制

首页会根据当前选择的已验证连接组、模型和目标客户端生成以下四项。切换连接组或模型后，请重新复制，避免混用不同连接组的 route key。

```text
Base URL: http://127.0.0.1:18400/v1
route key: 当前连接组的本地 route key（lr-...）
Model: 当前选择的已验证模型
客户端: Codex 或 Hermes
```

- Base URL 指向本机 Lin Router，不是上游地址。
- route key 填入目标客户端的 API Key 位置，用于客户端到 Lin Router 的认证。
- Model 是当前选择的已验证上游模型。

页面提供单项复制和“一键复制接入信息”。复制下方接入信息，在目标客户端中按其已有方式填写即可；客户端是否可用由你在客户端中自行验证。

## 预览 / 调试

前端和正式服务共用同一份配置文件，便于调试：

```bash
python app.py --port 18409 --config lin-router-config.json
```

也可以直接双击 `start-preview-18409.bat`（Windows）。

## 跨平台构建

使用统一构建脚本产出 Windows `.exe` 或 macOS `.app`/`.dmg`：

```bash
# Windows
scripts/build.sh --target win32
# -> dist/LinRouter_windows.exe

# Windows + 安装包（默认使用内置自举安装器；装了 Inno Setup 6 会优先使用 ISCC）
scripts/build.sh --target win32 --installer
# -> dist/LinRouter_windows.exe + dist/LinRouter-v0.6.3-win-x64.zip + dist/LinRouter-Setup-v0.6.3-win-x64.exe

# 指定安装包版本号
scripts/build.sh --target win32 --installer --version 0.6.3
# -> dist/LinRouter-Setup-v0.6.3-win-x64.exe

# macOS
scripts/build.sh --target darwin
# -> dist/LinRouter.app

# macOS + DMG
scripts/build.sh --target darwin --dmg
# -> dist/LinRouter.app + dist/LinRouter.dmg
```

构建前脚本会自动生成对应平台的应用图标（`.ico` / `.icns`）。若直接调用 PyInstaller，spec 文件也会尝试自动生成图标；macOS 上需要 `iconutil` 工具。

Windows 安装包默认通过 `scripts/installer/build_self_installer.py` 生成，未安装 Inno Setup 也可出包；如本机存在 Inno Setup 6 / `ISCC`，则优先使用 `scripts/installer/LinRouter.iss`。安装包默认安装到当前用户的 `%LOCALAPPDATA%\Programs\LinRouter`，不需要管理员权限，默认创建桌面快捷方式；配置和日志写入 `%APPDATA%\LinRouter`。支持静默安装参数 `--silent --desktop --no-desktop --autostart --no-run`。

v0.6.0 发布前检查清单见 `scripts/release-checklist-v0.6.0.md`；构建脚本会自动对 zip / setup 产物执行脱敏扫描。

### Windows 可选代码签名（发布机前置条件）

默认构建**不签名**，因此不会改变没有签名工具或证书时的既有构建行为。需要签名时必须显式增加 `--sign`；该模式会在生成任何 Windows 发布产物前校验以下条件，缺一项就明确失败，不会继续产出“看似已签名”的包：

- Windows SDK 中可用的 `signtool.exe`；也可通过 `LINROUTER_SIGNTOOL` 指定完整路径。
- 包含私钥的代码签名证书文件（通常为 `.pfx`/`.p12`），通过 `LINROUTER_SIGN_CERT_PATH` 指定。
- RFC 3161 时间戳服务 URL，通过 `LINROUTER_SIGN_TIMESTAMP_URL` 指定；项目不硬编码供应商或 URL。
- PFX 密码通过 `LINROUTER_SIGN_CERT_PASSWORD` 注入当前构建进程。不要把密码写入源码、命令行、`setx` 持久环境变量或日志；建议由 CI secret / Windows Credential Manager 等安全机制在启动构建进程时注入。

签名构建示例（以下命令本身不包含密码）：

```powershell
$env:LINROUTER_SIGNTOOL = 'C:\\Program Files (x86)\\Windows Kits\\10\\bin\\<sdk-version>\\x64\\signtool.exe'
$env:LINROUTER_SIGN_CERT_PATH = 'D:\\secure\\linrouter-release.pfx'
$env:LINROUTER_SIGN_TIMESTAMP_URL = 'https://<approved-timestamp-service>/timestamp'
# LINROUTER_SIGN_CERT_PASSWORD 由 CI secret / Credential Manager 注入，不要粘贴到 shell 历史
bash scripts/build.sh --target win32 --installer --sign --version 0.6.0
```

签名顺序是固定的：PyInstaller 生成 `dist\\LinRouter_windows.exe` 后先签 payload；再用已签名 payload 生成 ZIP 和安装包；最后再签 `LinRouter-Setup-...exe` 安装包本身。自举安装器因此嵌入已签名 payload；Inno Setup 也从同一个已签名 payload 生成外层安装包。ZIP 本身不是 PE 文件，不执行 Authenticode 签名。

签名后在 Windows PowerShell 验证签名者、状态和时间戳：

```powershell
Get-AuthenticodeSignature .\\dist\\LinRouter_windows.exe | Format-List Status,SignerCertificate,TimeStamperCertificate,Path
Get-AuthenticodeSignature .\\dist\\LinRouter-Setup-v0.6.0-win-x64.exe | Format-List Status,SignerCertificate,TimeStamperCertificate,Path
```

必须看到 `Status : Valid`，并检查证书主体、用途和时间戳符合发布要求；`SignerCertificate : null` 或 `SignatureType : 0` 表示未签名。当前开发环境没有可用代码签名证书和已定位的 `signtool.exe`，因此本地只能完成静态校验，不能声称 Smart App Control 已解决。自签名证书仅适合开发机或受控内网测试；它不等于面向公众的正规发布签名，不能据此承诺绕过 Windows Smart App Control。

```bash
python -m PyInstaller --noconfirm LinRouter.spec
```

## 新手安装说明（Windows）

1. 下载 `LinRouter-Setup-v0.6.0-win-x64.exe`，双击安装；如果 Windows Defender 提示未知发布者，确认来源是本项目发布包后选择“仍要运行”。
2. 安装完成后桌面会出现 `Lin Router` 图标；双击启动后会自动打开管理页面。
3. 首次启动会自动生成空配置，你需要在“配置”页添加连接组并填写上游 Base URL / API Key。
4. 客户端 Base URL 填 `http://127.0.0.1:18400/v1`，API Key 填页面中连接组 Key（`lr-...`）或聚合模型 Key（`lr-ag-...`）。
5. 卸载可运行安装目录下的 `uninstall.cmd`；用户配置与日志保存在 `%APPDATA%\LinRouter`，如需彻底清理可手动删除该目录。

## 配置文件

- 正式配置：`lin-router-config.json`
- 模板配置：`lin-router-config.example.json`

真实配置已加入 `.gitignore`，不要提交真实 API Key。

## 推理字段与 WAF 验证

- `/v1/responses` 使用 `reasoning.effort`；`/v1/chat/completions` 使用 `reasoning_effort`。Lin Router 只按对应协议读取和透传，不会同时注入两种字段。
- WAF 兼容只调整 Header；请求体仅在路由到真实上游模型时补丁替换 `model`，推理字段保持不变。
- 对真实渠道执行 low/high A/B（先关闭 WAF 运行一次，再开启 WAF 运行一次）：

```powershell
python scripts\reasoning_ab_check.py --api-key <route-key> --model <model> --waf-state off
python scripts\reasoning_ab_check.py --api-key <route-key> --model <model> --waf-state on
```

脚本不会打印 route key 或请求正文。若日志显示 `reasoning_preserved=true`，但 low/high 的上游用量与行为始终相同，应优先判断为渠道未支持或忽略推理强度，而不是 Lin Router 删除字段。
## WAF 客户端策略

中转站连接组开启 WAF 兼容后，可在“高级配置 → WAF 客户端策略”选择：

- **始终使用 WAF 兼容**：所有客户端使用浏览器化 Header。
- **智能兼容（Codex 直连 Header）**：识别 `Codex` User-Agent 或 `x-codex-*` Header 后，保留其原始 Header；其他客户端（例如 Hermes）仍使用 WAF 兼容。两种策略都不会改写请求 JSON。

WAF 兼容只控制 Header，不会限制请求并发。连接组默认允许同一上游模型同时处理多个请求；只有在“高级配置 → 请求并发”中显式选择“串行保护”时，才会对该连接组的同一上游候选串行等待。请求日志详情会分别显示 WAF 策略和请求并发策略。
