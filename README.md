# Lin Router

本地优先的 OpenAI 兼容中转站。Lin Router 在本机提供统一的 `/v1` 接口，将 Codex、Hermes 和其他 OpenAI 兼容客户端的请求转发到你配置的上游服务。

- 当前版本：`v0.6.4`
- 支持平台：Windows、macOS
- 管理台：`http://127.0.0.1:18400`
- API 地址：`http://127.0.0.1:18400/v1`

## 下载

从 GitHub Release 下载对应平台的文件。

### Windows

| 文件 | 用途 |
| --- | --- |
| `LinRouter-Setup-v0.6.4-win-x64.exe` | 推荐。安装版，支持快捷方式和卸载 |
| `LinRouter_windows.exe` | 便携版，直接运行，不安装 |
| `LinRouter-v0.6.4-win-x64.zip` | 便携版压缩包 |

### macOS

下载 `LinRouter.dmg`，将 `LinRouter.app` 拖入 `/Applications`。

当前 v0.6.4 为自动构建的候选版本。Windows 产物尚未完成 Authenticode 签名，首次运行可能出现未知发布者提示；请确认下载来源后，在受控环境中测试。

## 快速开始

### 1. 启动

安装版启动后会打开管理台。便携版直接运行 `LinRouter_windows.exe`。

源码运行：

```bash
python desktop.py
# 或
python -m linrouter
```

仅启动托盘/菜单栏、不自动打开浏览器：

```bash
python desktop.py --tray
```

### 2. 配置上游

在管理台的“配置”页创建连接组，然后添加模型。

| 类型 | 连接组 | 模型 |
| --- | --- | --- |
| 火山方舟 | 组名、Base URL、Ark API Key | 显示名称、EP ID |
| 中转站 | 组名、Base URL | 显示名称、上游模型、API Key、价格组 |
| 通用 OpenAI 代理 | 组名、Base URL、上游 API Key | 显示名称、可选的上游模型名 |

保存后可使用“代理测试”或健康检查确认配置是否可用。

### 3. 接入客户端

在管理台首页选择已验证的连接组和模型，复制接入信息：

```text
Base URL: http://127.0.0.1:18400/v1
API Key: 连接组 Key（lr-...）
Model: 已验证的模型名
```

客户端中的 Base URL 指向本机 Lin Router，不是上游地址。

### Key 的区别

| Key | 用途 | 配置位置 |
| --- | --- | --- |
| 上游 API Key | Lin Router 访问上游服务 | 连接组或模型配置 |
| route key（`lr-...`） | 客户端访问本机 Lin Router | 客户端 API Key |
| 聚合模型 Key（`lr-ag-...`） | 客户端访问指定聚合模型 | 客户端 API Key |

三类 Key 不要混用。

## 聚合模型

聚合模型可以把多个中转站模型组成一条受控候选链，使用独立的 `lr-ag-...` Key 接入客户端。

- 成员只能来自中转站连接组。
- 按列表顺序，即手动优先级，依次尝试。
- 成员失败后可进入冷却，冷却期间不会参与调度。
- 所有成员不可用时返回 `503 all_aggregate_members_failed`。
- 不会回退到其他聚合模型或未加入的模型。
- 流式响应开始输出后不再切换成员，避免返回混合内容。

### 成员批量管理

聚合成员页支持：

- 按连接组、状态和关键词筛选；
- 勾选成员，支持三态全选；
- 批量添加、启用、停用和删除；
- 删除前预览候选链和优先级变化；
- 仅拖动列表最前方的六点柄 `⠿` 调整顺序。

批量管理只影响当前聚合中的成员引用，不会修改底层模型的 API Key、价格、价格组、可用状态或调度策略，也不支持跨聚合移动成员。

## 日志与状态

- 实时请求面板只显示当前仍在处理的请求。
- 请求日志用于查看历史请求，可筛选、查看详情和导出 CSV。
- 服务重启时，未正常结束的历史流会记录为“服务重启后中断”，不会继续显示为实时进行中。

## 桌面端

程序启动后驻留在 Windows 系统托盘或 macOS 菜单栏：

- 单击图标打开管理台；
- 右键菜单可打开主页、查看日志、编辑配置、复制地址、设置开机自启、设置启动最小化和退出；
- 重复启动会打开已有实例，不会启动第二个服务。

## 配置与日志位置

| 内容 | Windows | macOS |
| --- | --- | --- |
| 配置 | 可执行文件同级目录的 `lin-router-config.json` | `~/Library/Application Support/LinRouter/lin-router-config.json` |
| 设置 | 配置文件同级的 `lin-router-settings.json` | 配置文件同级的 `lin-router-settings.json` |
| 请求日志 | 配置文件同级的 `lin-router-logs.jsonl` | `~/.lin-router/lin-router-logs.jsonl` |

真实配置已被 `.gitignore` 排除。不要提交或分享 API Key、route key、请求正文和认证 Header。

## WAF 与推理参数

WAF 兼容只调整请求 Header；路由到真实上游时会将 `model` 替换为上游模型标识。

推理字段按协议透传：

- `/v1/responses`：`reasoning.effort`
- `/v1/chat/completions`：`reasoning_effort`

中转站可在高级配置中选择 WAF 客户端策略。WAF 兼容不限制并发；需要串行保护时，单独启用请求并发设置。

## 开发与构建

### 本地预览

```bash
# 默认端口 18400
python app.py

# 指定端口和配置文件
python app.py --port 18409 --config lin-router-config.json
```

Windows 也可以运行 `start-preview-18409.bat`。

### 构建

```bash
# Windows 便携 EXE
bash scripts/build.sh --target win32

# Windows EXE、ZIP 和安装包
bash scripts/build.sh --target win32 --installer --version 0.6.4

# macOS App 和 DMG
bash scripts/build.sh --target darwin --dmg
```

Windows 安装包默认安装到当前用户目录，不需要管理员权限。默认构建不签名；代码签名需要显式使用 `--sign`，并由发布环境安全提供签名工具、证书、时间戳服务和证书密码。

### GitHub Actions

- Push 或 Pull Request 到 `main`：执行静态检查和全量测试。
- 手动运行 CI：另外生成 Windows 预览包，Artifact 保留 14 天。
- 推送 `vX.Y.Z` Tag：验证版本、执行全量测试、构建 Windows/macOS 产物并创建 Draft Release。

Actions 中按平台显示两个 Artifact：Windows Artifact 内含 EXE、ZIP 和安装包；macOS Artifact 内含 App 和 DMG。Draft Release 中会将最终文件分别列出。

Tag 发布不会自动公开 Release。正式公开 Windows 版本前，还需要完成代码签名、隔离安装首启和已安装 EXE 验收。

## 配置模板

- 正式配置：`lin-router-config.json`
- 配置模板：`lin-router-config.example.json`

提交代码前请执行相关测试和构建检查，并确认没有把真实配置或凭据加入仓库。
