# lin-router

本地可视化中转代理，适合 Hermes 接入火山方舟模型。

## 桌面端启动

双击运行：

```text
dist\LinRouter.exe
```

启动后会自动打开管理页面：

```text
http://127.0.0.1:18400
```

Hermes 里填写 OpenAI 兼容地址：

```text
http://127.0.0.1:18400/v1
```

Hermes 的 API Key 必须填写页面里对应连接组生成的 `lr-...` key。不同连接组使用不同 key，服务端会按 key 判断要调用哪个连接组。

## 命令行启动

```bash
python app.py
```

默认固定端口：

```text
18400
```

## 配置方式

1. 先建连接组：填写 `Base URL` 和 `Ark API Key`。
2. 再建模型：填写模型名称、EP ID，并选择连接组。
3. 多个模型可以复用同一个连接组，不需要重复填写 key。
4. Hermes 中选择 `lin-router-auto` 时，会在对应连接组内自动调度仍有额度的模型。

## 重新打包

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name LinRouter desktop.py
```

打包结果会生成到：

```text
dist\LinRouter.exe
```
