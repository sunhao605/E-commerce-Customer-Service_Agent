# 荒野户外服务助手

面向虚构户外品牌店的客服与售后演示原型。用户可以查询商品价格库存、订单物流及售后进度，了解优惠和会员规则，并核验售后资格。商城支持本人已支付、未发货订单的整单退款，以及已签收且符合七天无理由的整单退货退款。客户确认后保存待审核；后台同意退货只提供退回地址，客户填写物流后，后台实际收货验收通过才退款。运输中联系人工处理拦截；工作台保持模拟审核。2026-10-09 已更新本地服务，验证范围见 [七天退货验收](analysis/七天退货验证/README.md)。

本项目基于课程提供的电商客服代码改编。本次改造主要完成户外业务场景迁移、商品及政策材料更新、商城/管理后台/评测工作台的品牌与交互调整，以及对应验证。工具、检索、流程、身份与权限机制沿用原实现，不声称从零实现；没有真实用户或企业上线指标。来源许可证尚未核实。2026-10-07 清理旧品牌时移除了原源码备份与差异文件，保留课程来源说明、历史哈希清单及非品牌迁移契约。

当前界面采用蓝黄橙的卡通露营风格：粗描边卡片、贴纸标签和本地 SVG 插画。新截图与样式检查在 `analysis/卡通风格验证/`；页面业务 API 的验证限制见下文。

## 目录与维护入口

```text
backend/                  Python 服务
  api/                    HTTP 入口和请求响应结构
  agent/                  对话编排、上下文、状态、工具与售后流程
  retrieval/              知识加载、混合检索与规则答复
  resources/              提示词、政策材料、能力清单和评测用例
  evals/                  回归评测与反馈归因
  tests/                  单元检查
  main.py                 启动入口
  settings.py             运行配置
frontend/                 评测工作台（独立 React 18 工程）
ecommerce-backend/         Java 业务服务与 Docker 构建
  src/main/java/.../      按账号、商品、订单、售后、客服等业务分组
  src/main/resources/    Spring 配置
  frontend/              商城和管理后台（共享 React 19 工程）
    src/shop/            商城源码
    src/admin/           管理后台源码
    src/shared/          共用 API 请求与类型
    public/              当前 SVG 商品资源
    admin/index.html     后台 HTML 入口，保留 /admin/ 地址
  docker/                Maven 配置与实际构建使用的依赖缓存
docs/                     产品学习、交接、评测、BadCase 与手测用例
analysis/                 源码分析、流程图及各阶段验证证据
  课程参考截图/           随包课程参考材料
  结构整理验证/           本轮清理检查与结果
```

本轮改动及后续优化优先级见 [结构审查与整理](docs/结构审查与整理.md)。Python 后端已从 20 个顶层目录收拢为 6 个，文档链接及导入已同步迁移。命令统一在项目根目录执行；各前端命令按下文先进入对应目录。

## 三个入口

| 入口 | 源码 | 作用 |
|---|---|---|
| 荒野户外商城 | `ecommerce-backend/frontend/src/shop/` | 商品、购物车、余额支付、订单及商城客服 |
| 荒野户外管理后台 | `ecommerce-backend/frontend/src/admin/` | 商品、活动、订单、用户与售后管理 |
| 荒野户外 · 评测工作台 | `frontend/src/` | 演示用户对话、依据、模拟审批、执行记录、回归评测和反馈 |

商城客服经过 Java 网关，由登录身份建立上下文；评测工作台直接调用 Python Agent，使用演示用户。两个入口用途不同，工作台切换用户不等于真实登录或管理员审批。

## 保留的处理链路

```text
需求识别 → 代码建立工具白名单与上下文
  → 商品/订单只读工具，或政策检索，或固定售后资格流程
  → 有依据的答复 / 追问 / 模拟待审批 / 安全兜底
  → 会话状态、Trace 与成本估算
```

价格库存来自业务工具，通用优惠来自政策引用；一般满减规则不等于某商品最终成交价。退货资格满足只代表可以准备申请。退款审批与资金到账必须区分。

2026-10-08 已按 [缺口一计划](缺口一提交申请与审核分开的产品设计计划.md) 分开商城提交与工作台模拟审批。确认卡显示可信实付金额和明确原因，提交前重读事实；取消、旧卡失效与重复提交由后端处理。提交成功才返回业务申请号与状态。金额必须有本人 PAYMENT 流水与订单金额核对依据，缺可信记录的历史样例不会放行。隔离验证与复跑见 [GAP-001](docs/项目评测记录.md#gap-0012026-10-08商城确认提交待审核)。

2026-10-09 补齐管理员批准后的订单终态：未发货整单全额退款在同一事务同步订单/支付已退款、履约取消；列表和详情切回窗口会重读，并支持已退款筛选。用户指出的已入账测试订单已单笔修正，不重复入账。验证范围见 [GAP-002](docs/项目评测记录.md#gap-0022026-10-09管理员退款通过后同步订单状态)。

## 本地运行

以下命令在项目根目录执行。Node 两套前端分别使用 React 18/Vite 8 与 React 19/Vite 7，不合并依赖。Java 后端需要 JDK 17、Maven 与独立 MySQL 演示库。

Python Agent：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PYTHONPATH=backend AGENT_COURSE_DISABLE_LLM=0 AGENT_COURSE_OFFLINE_RAG=1 AGENT_COURSE_OFFLINE_FACTS=0 AGENT_COURSE_ENV="$PWD/.env.model.local" AGENT_ECOMMERCE_SERVICE_TOKEN=course-debug-agent-service LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/uvicorn api.routes:app --host 127.0.0.1 --port 8000
```

当前已按用户授权启用 DeepSeek 真实聊天模型，密钥保存在被 Git 忽略、权限为 600 的 `.env.model.local`。该文件配置 `AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_BASE_URL=https://api.deepseek.com`、`AGENT_OPENAI_MODEL=deepseek-flash`，不要将实际 Key 写进文档或提交仓库。模型列表认证、Agent 路由/工具/最终回答及商城客服物流查询已实际通过；新配置重启后需要新建对话。仅启用聊天模型，政策检索仍采用本地简化向量。模型请求会向 DeepSeek 发送对话与相关上下文并消耗 API 额度；禁用聊天模型时把 `AGENT_COURSE_DISABLE_LLM` 改为 `1`。

评测工作台（另开终端）：

```bash
cd frontend
npm ci
npm run dev
```

默认地址为 `http://localhost:5173`；`VITE_AGENT_BASE_URL` 默认 `http://localhost:8000`，`VITE_ECOMMERCE_BASE_URL` 默认 `http://localhost:8081`。

商城与管理后台（另开终端）：

```bash
cd ecommerce-backend/frontend
npm ci
npm run dev
```

默认地址为 `http://localhost:5174`，管理后台为 `/admin/`；Vite 把 `/api` 转发到业务服务 8081。

Java 业务服务优先使用已验证的 Docker 方式，容器内提供 Java 17、Maven 3.9.6 与 MySQL 8；宿主机无需安装 Java/Maven。当前机器已生成私有 `.env.local` 保存随机数据库密码，不提交仓库。更换机器时先设置 HUANGYE_DB_PASSWORD 和 HUANGYE_DB_ROOT_PASSWORD。

```bash
docker compose --env-file .env.local -f compose.local.yml up -d --build
docker compose --env-file .env.local -f compose.local.yml ps
```

后端健康检查：`http://127.0.0.1:8081/actuator/health`。新卷 `huangye-outdoor_mysql-data` 独立于旧课程数据卷；MySQL 不开放宿主机端口，后端仅开放本机 8081。启用已有 course-debug Profile，Java 与本机 Agent 使用相同的本地演示服务 token。业务接口启用后应设置 `AGENT_COURSE_OFFLINE_FACTS=0`，避免回退订单镜像。

商城：`http://127.0.0.1:5174/`，账号 `zhangsan / 123456`；管理后台：`http://127.0.0.1:5174/admin/`，账号 `admin / admin123456`。这些是已有源码定义的演示账号。

停止本项目容器但保留数据：`docker compose --env-file .env.local -f compose.local.yml stop`。重启：同一配置执行 `up -d`；源码变化时加 `--build`。保留 `.env.local` 与数据卷配套使用，不能重新生成密码后直接复用旧卷。

若选择宿主机方式，需要另装 JDK 17/Maven，并手动配置独立 MySQL、数据源和 Agent 地址。服务身份 token 必须一致，工作台订单摘要依赖 course-debug Profile。

**商品迁移不要清空已有数据库。** 首次在空的演示库启动会生成户外商品；已有商品的图片 URL 改为本次 SVG 后，会触发原有更新逻辑。但已有订单商品快照、偏好、优惠券不保证重建，需要在隔离演示库核验或通过已有管理入口更新。2026-10-07 已初始化本项目新建的独立演示数据库，未连接或修改旧课程数据库。

## 离线模式与模型配置

原配置名保留以兼容课程代码：

- `AGENT_COURSE_DISABLE_LLM=1`：不调用聊天模型。
- `AGENT_COURSE_OFFLINE_RAG=1`：使用本地字符向量，不能当成真实语义模型。
- `AGENT_COURSE_OFFLINE_FACTS=1`：业务接口不可用时允许显式种子镜像；商品镜像仅覆盖露营折叠椅，不能当成完整商城。
- `AGENT_COURSE_TODAY`：覆盖默认固定演示日期 `2026-07-15`；改日期须同步签收样例。
- `AGENT_COURSE_ENV`：指定配置文件，避免误加载其他项目的共享 `course.env`。

真实模型沿用 `AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_MODEL` 和 `AGENT_EMBEDDING_MODEL`，不在源码中存放密钥。不要把禁聊天模型当成禁 Embedding；需要完整离线时显式开启三个离线开关。历史离线回归没有真实模型请求；2026-10-07 的 MODEL-001 单独记录两条真实聊天验证，不能混入原离线通过率。

## 验证与已知边界

```bash
PYTHONPATH=backend AGENT_COURSE_DISABLE_LLM=1 AGENT_COURSE_OFFLINE_RAG=1 AGENT_COURSE_OFFLINE_FACTS=1 AGENT_COURSE_ENV=/tmp/huangye-no-model.env .venv/bin/python -m unittest discover -s backend/tests
.venv/bin/python analysis/改造验证/check_migration.py
.venv/bin/python analysis/结构整理验证/check_structure.py
.venv/bin/python analysis/结构整理验证/check_backend_api.py
.venv/bin/python analysis/结构整理验证/check_ecommerce_layout.py
.venv/bin/python analysis/改造验证/run_offline_eval.py backend /tmp/huangye-eval.json
```

两套前端各自执行 `npm run build`。原版和改版在显式离线镜像、无网络/无模型条件下均为 10/13，通过数不能代表真实业务成功率；同三条回答语义检查仍失败，未放宽断言。详细结果见 `docs/项目评测记录.md` 和 `analysis/改造验证/`。

- 工作台批准仅记录模拟结果；商城客户只登记待审核，不能自行批准。已有管理员批准可能改变本地演示余额，本次未修改或执行该动作；不接外部资金退款。转人工仍是状态与建议。
- 商品和规则是合成演示材料；SVG 为本地商品示意图，不是商品实拍；保留原版的 SKU、价格库存、商品可退标志、政策 ID、状态及审批逻辑。
- Agent 状态及检查点在内存中，重启不保证恢复；MCP 模块是本地目录，不能视为真实 MCP 服务接入。
- Python 模拟恢复的 BC-001 旧快照回退和 BC-004 角色/终态问题仍未修改；商城提交已绕开该入口，最新事实读取失败会阻断。2026-10-09 最新服务已重新构建启动，真实 MySQL/在线模型的确认卡与取消通过；正式提交和管理员审批留给用户手测。见 [当前测试路径](docs/本地测试用例.md) 与 [RUN-001](docs/项目评测记录.md#run-0012026-10-09最新本地服务启动与手测准备)。
- `analysis/课程参考截图/` 为随包课程材料；卡通改造阶段截图使用前端业务 API fixture。2026-10-07 新的 `analysis/商城启动验证/` 使用实际 Java/MySQL 与浏览器，登录、商品及客服查询已通过，未验证购买、支付或商城退款确认。

项目导读见 `analysis/dianshang-agent.md`，场景改造和取舍见 `docs/产品设计学习.md`，当前停点以 `docs/项目交接.md` 为准。文档和验证目录的编号、日期、对应关系见 `docs/文件索引.md`。
