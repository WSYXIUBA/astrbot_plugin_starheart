# 开发说明

## 结构

- `main.py`：入口，事件钩子、指令与面板 API 注册。
- `core/`：`service`（业务）、`storage`（SQLite）、`engine`（召回与文本处理）、`page_api`（面板接口）。
- `pages/dashboard/`：面板前端，原生 ES 模块，零构建。

## 配置与数据

- 配置由 AstrBot 托管；面板写入走 `core/page_api.py` 的 `config/save`。
- 数据目录：`data/plugin_data/astrbot_plugin_starheart/`，单文件 `starheart.db`。
- 主动消息写回走 `conversation_manager`：历史字段是 `history`（JSON 字符串），末尾为 assistant 时并入上一条。

## 发版

- 同步五处：`metadata.yaml`、`main.py` 的 `@register`、README 中英版本行、`pages/dashboard/` 所有 `?v=`、`CHANGELOG.md`。
- `?v=` 是手工缓存键，改前端必须同步更新。
- CHANGELOG 按 Added / Changed / Fixed 分类，只写用户可感知的变化。

## 兼容

- AstrBot >= 4.24.0
- 平台：`aiocqhttp`（OneBot v11）

## 已知坑

- 内联 `onclick` 的函数必须挂到 `app.js` 的 `Object.assign(window, {...})`，只在模块内 `window.x =` 静态检查认不出。
- 面板模块新增文件也要带同一个 `?v=`，否则会与主包版本错位。
