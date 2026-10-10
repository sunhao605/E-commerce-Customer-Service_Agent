# 缺口一首版验证

本目录使用隔离 H2 合成订单，不连接既有 MySQL。Java 入口、登录权限、HTTP、事务和业务记录都实际运行；Agent 用合成返回代替模型。检查不执行管理员批准，余额、退款流水与审核记录不变。正式结论见 [GAP-001](../../docs/项目评测记录.md#gap-0012026-10-08商城确认提交待审核)。

接口与数据库检查（项目根目录执行）：

```bash
.venv/bin/python analysis/缺口一验证/run_java_check.py
```

脚本从本机已有 `huangye-outdoor-ecommerce:latest` 镜像提取依赖，从项目自带缓存取 H2；使用已有 `maven:3.9.6-eclipse-temurin-17` 的 JDK，在无网络容器中编译全部源码并执行检查，不拉取新镜像。这是完整源码编译及 Spring/HTTP/H2 运行，不能替代 Maven 打包。首次运行需要 Docker 权限；`.runtime` 是可删除的临时文件，下次运行会重建。

Python 回归：

```bash
PYTHONPATH=backend AGENT_COURSE_DISABLE_LLM=1 AGENT_COURSE_OFFLINE_RAG=1 AGENT_COURSE_OFFLINE_FACTS=1 AGENT_COURSE_ENV=/tmp/huangye-refund-test.env .venv/bin/python -m unittest discover -s backend/tests
```

浏览器检查：先完成两套前端构建和上面的 Java 检查，再执行：

```bash
.venv/bin/python /Users/sunhao/.codex/skills/webapp-testing/scripts/with_server.py \
  --server '.venv/bin/python -m http.server 6184 --bind 127.0.0.1 --directory ecommerce-backend/frontend/dist' --port 6184 \
  --server 'docker run --rm --name huangye-gap-one-ui -p 127.0.0.1:18191:8081 -v /Users/sunhao/Downloads/03_AIPMProject/0305_DianShang_Agent/analysis/缺口一验证/.runtime:/check:ro maven:3.9.6-eclipse-temurin-17 java -cp /check/classes:/check/lib/* com.teachdemo.ecommerce.customer_service.CustomerRefundCheck --ui' --port 18191 \
  --timeout 45 -- .venv/bin/python analysis/缺口一验证/check_ui.py
```

浏览器 /api 请求仅转发到 18191 的真实 Java 服务，登录和数据库结果没有被前端 fixture 替代。临时服务会关闭，H2 记录不进入原演示库。

源码版本见 before-manifest.json / after-manifest.json，修改差异见 changes.diff；接口结果、浏览器结果、日志和截图均保留。本目录结果不是线上指标；在线模型与既有 MySQL 的新链路尚未联调。
