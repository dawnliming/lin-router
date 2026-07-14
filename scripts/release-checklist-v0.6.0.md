# Lin Router v0.6.0 发布检查清单

## 构建门禁

- [ ] `python -m pytest tests -q` 通过。
- [ ] `python -m py_compile app.py` 通过。
- [ ] 修改过的前端文件均通过 `node --check`。
- [ ] `git diff --check` 通过。
- [ ] 执行 `bash scripts/build.sh --target win32 --installer` 构建发布包。
- [ ] 执行 `python scripts/release_guard.py dist/LinRouter-v0.6.0-win-x64.zip dist/LinRouter-Setup-v0.6.0-win-x64.exe`。
- [ ] 静默安装后确认存在 `LinRouter.exe` 与 `uninstall.cmd`。

## Windows Authenticode 签名（可选，但面向公开发布时建议使用）

未配置签名时不要勾选以下项目；普通构建行为保持不变。若显式启用 `bash scripts/build.sh --target win32 --installer --sign`，构建脚本会先校验全部条件，缺失 `signtool.exe`、证书路径、时间戳 URL 或 PFX 密码时必须失败，不会继续构建签名发布链：

- [ ] 发布机已安装 Windows SDK `signtool.exe`，或已设置 `LINROUTER_SIGNTOOL`。
- [ ] 已准备带私钥的代码签名 `.pfx`/`.p12`，并通过 `LINROUTER_SIGN_CERT_PATH` 提供。
- [ ] 已由发布方确认时间戳服务 URL，并通过 `LINROUTER_SIGN_TIMESTAMP_URL` 提供；不要使用项目硬编码的供应商地址（项目不提供）。
- [ ] PFX 密码由 CI secret / Windows Credential Manager 注入 `LINROUTER_SIGN_CERT_PASSWORD`；不写入源码、命令行、持久环境变量或日志。
- [ ] 签名顺序已留证：payload `LinRouter_windows.exe` → ZIP/安装包生成 → 外层 `LinRouter-Setup-...exe`。
- [ ] 在 Windows PowerShell 执行并保存脱敏结果：

```powershell
Get-AuthenticodeSignature .\dist\LinRouter_windows.exe | Format-List Status,SignerCertificate,TimeStamperCertificate,Path
Get-AuthenticodeSignature .\dist\LinRouter-Setup-v0.6.0-win-x64.exe | Format-List Status,SignerCertificate,TimeStamperCertificate,Path
```

- [ ] 两个 PE 文件均显示 `Status : Valid`，证书主体/代码签名用途/时间戳符合发布要求；`SignerCertificate : null` 或 `SignatureType : 0` 不得视为签名成功。
- [ ] 明确记录：自签名只适合开发机或受控内网测试，不等于面向公众的正规发布，也不能据此声称已解决 Windows Smart App Control。
- [ ] 当前无证书或无法定位 `signtool.exe` 时，只报告“签名未验证/待发布机条件满足”，不得报告 Smart App Control 已解决。

## 首次使用流程

- [ ] 空配置时，Dashboard 显示“还没有连接组”，并提供添加和导入入口。
- [ ] 新建连接组默认选择中转站；Base URL 与 API Key 校验能指出具体缺失字段。
- [ ] 已保存的连接组提供获取模型和手动添加模型，不会自动请求上游。
- [ ] 只有真实测试或请求成功后，才展示客户端 Base URL、路由 Key 和已验证模型。
- [ ] 路由 Key 被明确说明为 Lin Router 客户端 Key，而不是上游 API Key。

## P0 并发流与终态收口

- [ ] WAF Header 兼容不会启用串行保护；同一中转站候选的两个流可并发执行，不出现 `waf_lock_timeout` 或候选忙 fallback。
- [ ] 连接组高级配置默认“允许并发”；只有显式选择“串行保护”时，才会出现 `serial_protection_timeout`，且不写入 cooldown。
- [ ] Dashboard 同时展示两个独立的流式请求，并在各自收口后独立移除。
- [ ] `response.completed`、`response.failed`、`response.incomplete`、`[DONE]` 和 EOF 均记录可验证的流生命周期，包括 `stream_finalized=true`、`lifecycle` 与 `completion_signal`。
- [ ] 收到协议终态后在上游 TCP 连接关闭前完成收口；终态遗漏或异常不能阻塞另一个并发请求。
