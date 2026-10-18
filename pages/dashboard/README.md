# StarHeart 面板（pages/dashboard）

AstrBot 插件 Pages：运行在 WebUI 的受限 iframe 里，只通过 `window.AstrBotPluginPage` bridge
访问后端（路由见 `core/page_api.py`，全部挂在 `/{plugin}/page/...` 下）。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `index.html` | 外壳 + 设计令牌（CSS 变量、双主题）+ 顶栏/侧栏/抽屉/弹窗容器 |
| `app.js` | 哈希路由（`#page/sub`）、壳层交互、全局动作绑定（`window.hf*`，供内联 onclick） |
| `store.js` | 数据层（bridge 封装 / 超时 / 容错 GET）、格式化、主题、toast/抽屉/弹窗、页面骨架构件 |
| `charts.js` | 纯 SVG 图表库：sparkline / bars / waterfall / heatstrip / heatgrid / area / timeline / miniNetwork / sankey / scatter / matrix / legend（环类保留定义但**页面已不再使用**：面板全面改线性表达） |
| `graph.js` | 记忆图谱引擎（canvas 2D 手写力导向，零依赖）：单点拖拽、卡片化、四视图、关系链粒子流 |
| `views-memory.js` | 记忆图谱 / 记忆库（管理表）/ 人物画像 / 记忆设置（含维护动作） |
| `views-events.js` | 消息事件（指挥台 + 门控 / 主动 / 撤回 / 好感四域页，货架式折叠卡） |
| `views-system.js` | 总览 / 语言学习 / 人格管理 / 设置（导入、名单、模型分配、Token 消耗） |
| `views-conf.js` | 通用「配置域」渲染器（每个域的键、类型、范围、说明写在一张表里） |

## 每个界面用什么图形化表达（以及为什么）

| 界面 | 图形化选择 | 理由 |
| --- | --- | --- |
| 消息事件 · 指挥台 | 模块状态灯 + 事件密度矩阵（模块 × 24 小时）+ 统一事件流 | 先看「现在能不能动作」，再看「最近发生了什么」；密度用格子写数字，不靠悬停 |
| 总览 | 运作图谱（节点卡片 + 流动光点）+ KPI + 开口转化分级条形 + 记忆构成条 | 模块之间是**依赖关系**，网络图比平铺卡片更能说明结构；线性条取代圆环 |
| 记忆图谱 | 力导向关系链（3D WebGL2 为主，回退 canvas 2D）+ L0/L1/L2/L3 语义分层 | 记忆的本质是「谁和谁有关系」，力导向是唯一能同时表达聚类与链路的布局 |
| 记忆库 | 记忆管理表（筛选 / 排序 / 分页 / 行内展开）+ 召回测试 | 记忆条目多、字段多，表格能排序与批量看；展开放到行内，不跳页 |
| 人物画像 | 头像 + 关系数值 + 分档标签 + 画像正文 | 「人」用头像最好认，分档用标签比圆环更好读 |
| 门控决策 | 因子瀑布 + 分数分布 + 因子命中率 + **交互式散点**（点一下右侧抽屉出因子明细） | 门控的核心问题是「为什么没回」；瀑布把因子逐项加减画出来最直接，散点看分布与离群 |
| 撤回 | 事件时间轴 + 降权系数条 + 「模型决定 + 它当时回了什么」 | 事件型数据，时间轴够用；把模型决定写在流水里，才看得出撤回反应是否生效 |
| 主动消息 | 额度计数格 + 静默 24h 时段条 + 审计表 | 主动的护栏本质是「额度与时钟」，计数格比圆环省空间也更精确 |
| 好感 | 区间色带 + 档位标签 + 结算列表 | 关系是连续量且在 6 个档位间移动，色带能一眼看出分布 |
| 语言学习 | 情景 → 表达 二分图（桑基）+ 使用次数条形 | 学习结果是「映射关系」，二分图天生适合 |
| 人格管理 | 头像卡 + 提案左右对比 + 状态与进化 | 提案的本质是「改前 / 改后」的 diff，并排看最快 |
| 模型分配 | 任务 → 提供商 绑定图 + 卡片 | 分配是「谁绑谁」的关系，网络图能看出耦合 |
| Token 消耗 | 任务条形 + 模型统计 + 明细表 | 只有一个聚合口径（按任务 / 模型），不编按天趋势 |
| 设置 | 模块状态灯 + 数据治理（危险操作集中卡） | 开关类用灯与开关，破坏性操作用红色隔离区 |

## 图谱引擎 API（`graph.js`）

```js
import { MemoryGraph, GRAPH_PALETTE } from './graph.js';

const g = new MemoryGraph(containerEl, {});
g.setData({ nodes, links });          // nodes 见 core/storage.py graph()；links: {s,t,type,w}
g.setView('chain'|'cluster'|'timeline'|'entity');
g.setSearch(q); g.setFilter({groups, kinds, minStrength});
g.setPhysics({repulsion, linkForce, centerForce, linkDistance});
g.setCards(bool); g.setMotion(bool);
g.fitView(); g.reshuffle(); g.resize(); g.exportPNG(); g.destroy();
g.on('select'|'hover'|'viewchange'|'stats', cb);
g.selectNode(id); g.focusNode(id);    // 程序化选中 / 带相机缓动定位
```

**物理参数单位**（滑条与 `setPhysics` 必须一致，否则会出现「整片乱飞」）：
`repulsion≈1400`、`linkForce≈0.055`、`centerForce≈0.008`、`linkDistance≈92`。
拖动时引擎只给「被拖节点 + 一跳邻居」全强度物理，其余节点 12%，因此拖一个点不会带动整片。

## 关系链从哪来

后端 `core/storage.py`：

- `link_memory()`：运行时每写入一条记忆就补边（实体边 + 同会话时序边 + 共享实体/线索的语义边）；
- `rebuild_links()`：对存量记忆重建关系链（幂等），面板右上角「重建关系链」与 `hf link` 指令都会调用；
- `graph()` / `graph_stats()`：图谱数据与「关系链体检」（边数 / 平均度数 / 孤立点 / 连通分量 / 枢纽）。

## 开发约定

1. 页面里**不放任何假数据**：拿不到就显示空状态或错误，不编造数字。
2. 后端调用一律走 `bridge`（受限 iframe 没有 cookie，直连必然 401）；endpoint 必须带 `page/` 前缀。
3. 新增子页：在 `app.js` 的 `PAGES` 表里加 `render`，视图函数返回 `{ html, mount }`，
   `mount(host)` 里画图表（数据在这一步才拉，保证不会画在空容器上）。
4. 内联事件参数一律用 `jsStr()`（它就是「JSON 字符串 + HTML 属性转义」），
   直接拼引号会把 handler 截断成 `Unexpected end of input`。
5. 新图表：先往 `charts.js` 加组件（内部处理空数据与容器自适应），不要在页面里手写 svg。
