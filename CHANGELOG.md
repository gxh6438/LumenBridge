# LumenBridge 更新日志

## v1.0.1

**发布日期：** 2026-08-18

### 🐛 Bug 修复

- **修复插件市场封面图加载失败**
  市场卡片左侧封面图无法加载的问题，原因是封面 URL 校验函数导入失败导致所有封面请求抛 `ImportError`。已移除该校验，依赖项目现有内网部署策略。

- **修复 `MarketplaceError` 构造异常**
  路由回退信号 `_RouteNotFoundError` 继承 `MarketplaceError` 时未传递 `message` 参数，导致部分 404 场景抛出 `TypeError`。已为子类添加无参构造函数。

- **修复框架更新按钮样式异常**
  总览界面"框架更新"按钮多余的向上箭头图标已移除。

### ✨ 新功能

- **框架热重载**
  框架 wheel 更新安装后自动清除 Endstone 模块缓存并重载，无需手动重启服务端即可生效。基于 Endstone `_invalidate_caches` 机制实现。

- **点赞 / 举报完全匿名化**
  引入市场匿名会话管理，通过访问市场页面获取匿名 Cookie 与 CSRF Token 完成点赞和举报操作。`webui_report_api_key` 配置项已删除，不再需要填写任何密钥。

- **任务进度弹窗**
  安装 pip 依赖、安装 / 更新子插件、应用框架更新等长耗时操作现在会在 WebUI 弹出实时日志面板和进度条（仿 AstrBot 风格）。包含：
  - 实时滚动日志输出
  - 0–100% 进度条与状态标签
  - 完成 / 失败 / 热重载中状态徽章
  - 三语国际化文案（zh_CN / zh_TW / en）

### 🔧 改进

- **国际化文案完善**
  `task_log_modal` 与 `task_log` 相关键已补齐到全部三种语言包。

- **第三方子插件兼容性**
  修复 `picture_rank` 子插件 `NameError: name '_fmt_money' is not defined` —— 嵌套函数内静态方法调用需带 `self.` 前缀。

### ⚠️ 破坏性变更

- 配置项 `report_api_key` 已移除，升级后旧配置值会被自动忽略。建议在 WebUI 配置页保存一次以清理残留键。

### 📦 升级方法

1. 通过插件市场检查框架更新，下载 v1.0.1 wheel
2. 点击"应用更新"，框架会自动安装并热重载
3. 进入 WebUI 保存一次配置以清理已移除的 `report_api_key` 键
