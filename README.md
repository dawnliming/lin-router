# lin-router

本地 OpenAI 兼容路由器，为 Hermes、Codex++ 和通用 OpenAI 客户端提供统一入口。

## 启动

桌面端：

```text
dist\LinRouter.exe
```

命令行：

```bash
python app.py
```

默认地址：

```text
http://127.0.0.1:18400
http://127.0.0.1:18400/v1
```

客户端填写页面里生成的 `lr-...` Key，服务端会按 Key 绑定到对应连接组；也可以使用全局 Key `lin-router`，Lin Router 会在所有连接组中按顺序挑选第一个可用模型。

## 桌面端

双击 `dist\LinRouter.exe` 启动后，程序会驻留在系统托盘：

- 左键单击托盘图标：打开管理面板
- 右键托盘图标：打开面板 / 复制地址 / 复制全局 Key / 开机自启 / 启动最小化 / 退出
- 开机自启：写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
- 启动最小化：启动后不自动打开浏览器，仅显示托盘图标
- 单实例保护：重复启动会自动打开已有实例的管理面板

## 主要能力

- 连接组管理：火山方舟 / 中转站 / 通用 OpenAI 代理
- 自动调度模型 `lin-router-auto`
- 全局 Key `lin-router`：跨所有连接组自动挑选第一个可用模型
- 连接组级自动冷却：仅中转站启用
- 自动获取上游模型
- 最近请求日志、详情展开、筛选与 CSV 导出
- 代理测试
- 复制 Hermes 配置
- 复制连接组
- Windows 系统托盘、开机自启、启动最小化

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

## Hermes / Codex++

Hermes 推荐配置：

```text
Base URL: http://127.0.0.1:18400/v1
API Key: 对应连接组的 lr-... key
Model: lin-router-auto
```

Codex++ 也走同样的本地入口，建议单独建连接组，并保持请求语义尽量原样透传。

## 预览 / 调试

前端和正式服务共用同一份配置文件，便于调试：

```bash
python app.py --port 18409 --config lin-router-config.json
```

也可以直接双击 `start-preview-18409.bat`。

## 打包

```bash
python -m PyInstaller --noconfirm LinRouter.spec
```

产物：

```text
dist\LinRouter.exe
```

## 配置文件

- 正式配置：`lin-router-config.json`
- 模板配置：`lin-router-config.example.json`

真实配置已加入 `.gitignore`，不要提交真实 API Key。
