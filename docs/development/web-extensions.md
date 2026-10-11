# Web 扩展（配置表单 / API / 自定义页面）

子插件通过 `lumen.web`（`WebBridge`）把自己的配置表单、REST API 与自定义页面挂进 LumenBridge Web 管理面板，无需自行处理鉴权与静态服务。

三个注册入口：

| 方法 | 用途 |
|------|------|
| `createConfig()` | 声明配置表单（自动出现在 WebUI「子插件」页） |
| `registerApi(method, path, handler, need_auth)` | 注册 REST API（`/api/plugin/...` 前缀） |
| `registerPage(title, relative_path, tab, icon)` | 注册自定义页面（iframe 加载） |

## 配置表单

`createConfig()` 返回链式构建器（Fluent API），声明 Schema 后 `register()` 上线：

```python
def on_load(lumen):
    def on_change(key, value):
        lumen.logger.info(f"配置变更 {key} = {value}")

    form = (
        lumen.web.createConfig()
        .section("基础设置")
        .text("nickname", "小明", desc="机器人昵称", label="昵称")
        .number("level", 10, desc="最低等级", minimum=0, maximum=100)
        .switch("enabled", True, desc="开启功能")
        .select("mode", "normal", ["normal", "strict"], desc="工作模式")
        .multiselect("channels", ["a"], ["a", "b", "c"], desc="生效频道")
        .array("blacklist", [], desc="黑名单列表")
        .textarea("template", "hello {player}", desc="消息模板")
        .text("api_key", "", secret=True, desc="密钥（密码框）")
        .register()
    )
    lumen.on("config.update." + form.name, on_change)
```

### 字段类型

| 方法 | 前端渲染 | 值类型 |
|------|----------|--------|
| `text(key, val, desc, secret=)` | 文本框 / 密码框（secret=True） | str |
| `number(key, val, minimum, maximum, step)` | 数字框，min/max 齐备时滑块联动 | float |
| `switch(key, val)` | 开关 | bool |
| `select(key, val, options)` | 下拉框 | 选项值 |
| `multiselect(key, val, options)` | 复选框组 | list |
| `array(key, val)` | chips 列表编辑器（增删 + 批量导入） | list |
| `textarea(key, val)` | 多行文本（支持全屏编辑） | str |
| `file(key, val, accept, upload_url)` | 带预览上传组件（走独立端点，不参与 JSON 保存） | — |
| `section(title, desc)` | 分组卡片（仅布局，不参与读写） | — |

所有字段支持 `label`（显示名，默认取 key）与 `default`（恢复默认按钮数据源，缺省为注册时的初始值）。

### 保存与更新事件

表单保存（WebUI POST）时：

1. 按 Schema 校验每个提交键的值类型（select 限定 options、multiselect/array 必须 list 等），非法值跳过；
2. 写入 Schema 内存态（供前端回显）；
3. 对每个**真正写入**的键触发事件：

```python
lumen.on("config.update.表单名", lambda key, value: ...)
```

> Schema 保存在 WebUI 进程内存（`plugins_config_schema`），**不落盘**——需要持久化的子插件请在更新回调里用 `lumen.storage` 自行保存，加载时读回并作为表单初始值。

## REST API

```python
def on_load(lumen):
    def handler(req):
        return {"code": 200, "data": {"hello": "world"}}

    lumen.web.registerApi("GET", "/myplugin/status", handler, need_auth=True)
    # 实际路径：/api/plugin/myplugin/status
```

- 路径自动挂到 `/api/plugin` 前缀下；
- `need_auth=True`（默认）要求面板 token；`False` 任何人可访问，**注册时即告警**，仅用于确需公开的端点；
- handler 返回 dict 直接 JSON 化；异常由框架兜底 500；
- 卸载/热重载时自动注销本插件注册的全部 API。

## 自定义页面

```python
def on_load(lumen):
    lumen.web.registerPage("排行榜", "rank.html", tab=True, icon="trophy")
```

| 参数 | 说明 |
|------|------|
| `title` | 页面显示名 |
| `relative_path` | 相对子插件目录的文件路径，页面实际 URL 为 `/plugin-views/<子插件目录>/<relative_path>` |
| `tab` | `True` 时额外注册为移动端底栏 tab；默认仅进「其它」面板与桌面侧栏 |
| `icon` | 内置图标名（`trophy` `coin` `fire` `chart` `gear` 等 30+）或单个 emoji |

页面约束（iframe 沙箱）：

- 页面经**带 token 的 URL** 加载，相对资源不携带 token——页面必须**自包含**（内联 CSS/JS，不引外部文件）；
- 主面板注入融合基础样式并按内容高度自适应 iframe；需保留背景色时覆盖：`<body style="background:#fff !important">`。

## 加载顺序与暂存机制

WebUI 可能晚于子插件启动（配置里关闭后开启、初始化顺序），`WebBridge` 内置**暂存队列**：

- `webui` 未就绪时注册项进入 pending 队列；
- WebUI 启动后由主插件统一 flush 补注册；
- 卸载时已 flush 的逐键撤销，未 flush 的直接丢弃。

因此子插件**无需关心 WebUI 与自身的加载先后**，随时调用注册接口即可。

## 卸载清理

框架在 `context._cleanup()` 中自动执行（见 [生命周期](lifecycle.md)）：

- 撤销全部已注册 API（按 method+path 键）、自定义页面、配置表单；
- 清空未 flush 的暂存队列；
- 全程持 WebUI 扩展锁，防热重载后旧 handler 残留。

## 相关文档

- **下一步**：[清单与依赖](manifest-and-deps.md)——子插件发布前的依赖声明
- [Context API 参考](context-api.md)：`lumen.web` 完整签名
- [Web 管理面板](../guide/webui.md)：面板功能与安全
