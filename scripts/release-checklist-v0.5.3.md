# Lin Router v0.5.3 Release Checklist

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
- [ ] 生成 `dist/LinRouter-v0.5.3-win-x64.zip`。
- [ ] 生成 `dist/LinRouter-Setup-v0.5.3-win-x64.exe`。
- [ ] 安装包 smoke test：`LinRouter-Setup-v0.5.3-win-x64.exe --silent --no-run --dir <temp-dir>`。
- [ ] 安装后存在 `<temp-dir>/dist/LinRouter.exe` 和 `<temp-dir>/uninstall.cmd`。
- [ ] 运行脱敏扫描：`python scripts/release_guard.py dist/LinRouter-v0.5.3-win-x64.zip dist/LinRouter-Setup-v0.5.3-win-x64.exe`。

## 文档

- [ ] README 已说明下载哪个文件、如何安装、首次配置、Base URL / API Key 填写方式。
- [ ] README 已说明 Defender 提示、卸载方式、配置与日志目录。
- [ ] 设置面板版本号显示 `v0.5.3`。

## v0.5.3 管理页体验专项

- [ ] 首页 Tab 可回访，展示运行状态、Base URL、聚合 Key 和快捷入口。
- [ ] 侧边栏折叠/展开后各页面布局正常。
- [ ] 请求日志主列表 token 仅展示输入、输出、命中、total。
- [ ] `member_disabled` 默认不作为异常主记录强提示，详情仍可查看候选过滤链。
- [ ] 聚合成员列表无启用 checkbox，使用停用/启用/恢复按钮。
- [ ] 添加聚合成员时底层模型价格可默认带入，且可手动覆盖。
- [ ] 批量导入预览展示行号、中文原因、重复/无效统计；确认导入与预览数量一致。

## v0.5.3 调度收益与配置安全专项

- [ ] 聚合模型详情页存在调度收益看板，请求总数不含配置型 skip。
- [ ] fallback 成功、cooldown 跳过、候选忙切换、cache 命中率展示正确。
- [ ] `member_disabled` / `member_cooling` / `underlying_model_disabled` / `underlying_model_cooling` 不作为主请求记录展示。
- [ ] 配置页 3～5 秒内自动刷新 cooldown / last_error / derived_status，编辑中输入不被覆盖。
- [ ] 删除连接组前展示受影响模型和聚合成员。
- [ ] 删除模型前展示依赖它的聚合成员。
- [ ] 设置页调试模式可持久化，日志详情只展示脱敏诊断字段。
- [ ] `/api/runtime-state`、`/api/aggregates/{id}/stats`、delete-preview 接口回归通过。
