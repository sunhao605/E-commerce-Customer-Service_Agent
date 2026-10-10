# 退款审批状态验证

2026-10-09。用户199元订单已由管理员全额退款，但仍待发货。生产源码修复审批事务、订单终态及商城focus刷新/已退款筛选；本目录不新增业务接口。

- `before-live.json` / `after-live.json`：实际MySQL订单、余额及资金流水对比。
- `java-before.log`：修复前断言失败；`java-result.json`：修复后49项通过（原39＋新增10），153Java文件编译，隔离H2和模型替身。
- `ui-result.json`：当前Vite页面经路由代理到真实隔离Java/H2，实际提交→管理员批准，窗口focus读取与筛选/手机宽度共4组通过；没有合成业务响应。
- `live-ui-result.json`：实际Java/MySQL订单的列表、筛选、详情3项只读检查通过，无模型调用或审批资金写入。
- `docker-build.log`：标准Docker/Maven package成功并部署；Dockerfile原有-DskipTests，断言检查另行运行。
- `repair_test_order.sql` / `repair-result.txt`：单笔已批准/已支付/已退款订单校正1行；不改钱、不重走审批。已执行，不要当作全库迁移。

## 复跑隔离检查

在项目根目录：

```bash
PATH="/Users/sunhao/.local/bin:$PATH" .venv/bin/python analysis/缺口一验证/run_java_check.py --approval
```

原提交检查脚本默认仍是39项、无审批；--approval复用同一测试类，额外执行临时H2审批。运行会生成可清理的 `.runtime`，仅取已有镜像依赖，不连接当前MySQL或模型。

浏览器检查需商城Vite5174保持运行，且先运行上面命令生成类：

```bash
docker run --rm --name huangye-refund-status-ui-check -p 127.0.0.1:18191:8081   -v "$PWD/analysis/退款状态验证/.runtime:/check:ro" -w /check   maven:3.9.6-eclipse-temurin-17 java -cp '/check/classes:/check/lib/*'   com.teachdemo.ecommerce.customer_service.CustomerRefundCheck --ui
```

另一个终端运行 `.venv/bin/python analysis/退款状态验证/check_ui.py`。结束后停止这个临时容器；本轮已停止并移除临时运行依赖，常用服务保持运行。

实际已修正订单的只读页面检查：`.venv/bin/python analysis/退款状态验证/check_live.py`。它不会发送客服问题、创建申请或批准退款。

本轮未扩展部分退款/已发货退货，不批量修正其他历史订单，不证明生产可用。
