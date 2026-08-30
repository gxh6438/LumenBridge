"""QQ 官方机器人适配器子包。

模块划分（qqofficial_adapter.py 组合入口，见各模块 docstring）：
- constants   协议常量（对照官方文档 https://bot.q.qq.com/wiki/develop/api-v2/）
- utils       HTTP 错误封装 / 业务码提取 / 消息内容解析
- credentials 被动凭据池 / 入群 event_id / 主动补发栈
- translate   官方事件 → OneBot v11 事件包翻译
- sender      发送队列 / 富媒体上传 / 重试矩阵 / 补发
"""
