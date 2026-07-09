# Lin Router v0.5.2 Release Checklist

## 构建前

- [ ] 确认 `lin-router-config.json`、`lin-router-settings.json`、`lin-router-logs.jsonl` 未进入发布包。
- [ ] 确认真实 API Key、`Bearer` token、`lr-...` / `lr-ag-...` route key 未进入发布包。
- [ ] 运行 Python 语法检查：`python -m py_compile app.py desktop.py settings_store.py upstream_client.py scripts/installer/build_self_installer.py scripts/release_guard.py`。
- [ ] 运行核心回归测试：`python -m pytest tests`。

## 功能回归

- [ ] 聚合成员行存在明确“停用 / 启用”按钮。
- [ ] 停用聚合成员只修改 `AggregateMember.enabled=false`，不影响底层真实模型。
- [ ] 启用聚合成员会清理成员自身 cooldown / last_error。
- [ ] timeout / 502 / 503 / 504 会触发上游健康 cooldown。
- [ ] `waf_lock_timeout` 不写 cooldown，并以本地锁忙语义展示。
- [ ] WAF blocked 403 中文提示仍正常。
- [ ] `priority` 与 `price_first` 策略行为不回退。

## 发布产物

- [ ] 构建 Windows 发布包：`bash scripts/build.sh --target win32 --installer`。
- [ ] 生成 `dist/LinRouter-v0.5.2-win-x64.zip`。
- [ ] 生成 `dist/LinRouter-Setup-v0.5.2-win-x64.exe`。
- [ ] 安装包 smoke test：`LinRouter-Setup-v0.5.2-win-x64.exe --silent --no-run --dir <temp-dir>`。
- [ ] 安装后存在 `<temp-dir>/dist/LinRouter.exe` 和 `<temp-dir>/uninstall.cmd`。
- [ ] 运行脱敏扫描：`python scripts/release_guard.py dist/LinRouter-v0.5.2-win-x64.zip dist/LinRouter-Setup-v0.5.2-win-x64.exe`。

## 文档

- [ ] README 已说明下载哪个文件、如何安装、首次配置、Base URL / API Key 填写方式。
- [ ] README 已说明 Defender 提示、卸载方式、配置与日志目录。
- [ ] 设置面板版本号显示 `v0.5.2`。

## v0.5.2 管理页体验专项

- [ ] 首页 Tab 可回访，展示运行状态、Base URL、聚合 Key 和快捷入口。
- [ ] 侧边栏折叠/展开后各页面布局正常。
- [ ] 请求日志主列表 token 仅展示输入、输出、命中、total。
- [ ] `member_disabled` 默认不作为异常主记录强提示，详情仍可查看候选过滤链。
- [ ] 聚合成员列表无启用 checkbox，使用停用/启用/恢复按钮。
- [ ] 添加聚合成员时底层模型价格可默认带入，且可手动覆盖。
- [ ] 批量导入预览展示行号、中文原因、重复/无效统计；确认导入与预览数量一致。
