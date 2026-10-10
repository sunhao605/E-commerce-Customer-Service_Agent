# 七天无理由退货退款：实现与验收

2026-10-09（Asia/Shanghai）。完整闭环已实现，Java 本地容器与 Python Agent 已更新。用户与后台分开认证和操作；用户确认只提交申请，后台同意只提供地址，实际收货验收通过才退回模拟余额。

## 续接时发现的停点

截图列出的是早期 11 个文件。接手时，接口、两套页面和 Java 检查已进一步接入，但 `java-check.log` 停在并发最终退款断言，交接与 README 仍描述旧范围。保留原失败日志 `previous-failed-check.log`。

复现得到并发 HTTP 200 / 400：锁前普通读取售后实体，使第二个事务沿用旧状态。改成仅查询归属字段，再按订单、售后、余额顺序锁定并读取实体；后台订单修改也直接加锁读取。最终并发检查为 200 / 409，仅一次入账。

补齐真实 Python 默认无理由申请类型和退货进度读取；其他售后原因不改成无理由，缺订单号时也不建立无理由草稿。增加旧草稿入口审批保护，不能绕过退货验收直接批准退款。移除启动时删除并重建七笔历史样例明细/物流的逻辑，已有购买快照保留，缺失记录仍由原初始化逻辑补齐。

## 流程与接口

`PENDING_REVIEW → WAITING_RETURN → RETURN_IN_TRANSIT → REFUNDED`。

初审可 `REJECTED`；验收可 `RETURN_INSPECTION_FAILED`，人工核验后重新确认验收通过。上海日期以首次有效提交为准，签收当日至次日起第七天结束前可提交；之后审核不重新使用当前日期判定过期。运输中、支付或购买属性缺失转人工核验。只做整单实付全额退款。

- 客户：`POST /api/customer-service/resume`，`CONFIRM_SUBMIT` / `CANCEL_SUBMIT`。确认凭证绑定本人、会话、订单及内容；不调用 Python 模拟审批恢复。
- 客户查询：`GET /api/shop/after-sales`、`GET /api/shop/after-sales/{requestNo}`。
- 客户寄回：`POST /api/shop/after-sales/{requestNo}/return-shipment`，`carrier`、`trackingNo`。只能本人在待寄回状态填写，同物流重试幂等。
- 后台初审：`POST /api/admin/after-sales/{requestNo}/approve`，退货必须填写 `returnAddress`、`returnContact`、`returnPhone`；无资金变化。
- 后台验收：`POST /api/admin/after-sales/{requestNo}/return-complete`，必须 `receivedAndInspected=true`；退款金额不能被修改。
- 后台异常：`POST /api/admin/after-sales/{requestNo}/return-inspection-failed`，`reviewNote` 必填，不退款。

最终退款将订单/支付置为 `REFUNDED`，履约置为 `RETURNED`，保留原发货、签收和物流历史。未发货退款仍为履约 `CANCELED`。余额、流水、售后、审批及订单同一事务，异常整体回滚。历史退货缺资格快照不会自动进入新流程或入账。

## 实际验证

| 证据 | 结果与范围 |
|---|---|
| `java-result.json` | 155 个 Java 源文件编译；98 项隔离 H2/HTTP/认证/事务检查通过，含原 49 项提交/审批回归和 49 项退货及快照检查 |
| `python-check.log` | 15 项 Python 检查通过，禁用模型及业务网络；默认申请类型、其他售后、真实进度均覆盖 |
| `ui-result.json`、四张 PNG | 7 组双角色浏览器验收通过，API 转发到真实隔离 Java/H2，仅模型为替身；正常申请、零入账初审、物流、验收失败、人工恢复、终态、手机宽度 |
| `docker-build.log` | 标准前端及 Docker/Maven package 成功；原 Dockerfile 的 `-DskipTests` 未更改，行为断言另行运行 |
| `live-before.json` / `live-after.json` | 本地更新前后 8 张业务表原字段逐行哈希完全一致；已有 15 订单、16 明细、3 余额、4 流水、5 售后、5 审批、10 物流、13 事件均未重写 |
| `health-result.json`、`live-api-result.json` | 更新后的 Java/Python 健康；当前 MySQL 用户/管理员读取与 3 个新增路由核验通过，未创建申请或执行审批/退款 |

本轮在线模型调用 0；现有 MySQL 新申请、审批和资金写入 0。Java 启动新增 9 个可空退货字段；旧记录未回填。新资金行为只在隔离 H2 验证，不能称为真实仓库或真实支付验证。浏览器首次因页面仍在加载而未定位到详情，增加状态等待后通过，没有修改业务响应或放宽业务断言。

既有检查中的“已签收七天无理由不纳入首版提交”保留了旧断言文案；该用例的模型替身仍返回 `unshipped_refund`，验证错误路由不会绕过服务端状态门槛。新 RETURN 用例独立验证已签收的完整闭环。

本地运行镜像：`sha256:8c991998680f4df1624ae631d6348c9ff7f6c6bcffc9276b2d9fafb0b564dc00`；候选标签 `huangye-outdoor-ecommerce:seven-day-20261009`，旧镜像保留为 `huangye-outdoor-ecommerce:before-seven-day-20261009`。Python PID 30692 为本轮快照，后续重启会变化。临时 H2 容器已停止并移除。

## 复跑

在项目根目录运行：

```bash
python3 analysis/缺口一验证/run_java_check.py --return
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -v
```

浏览器检查需要当前商城 Vite 5174，并先启动独立临时 H2：

```bash
docker run --rm --name huangye-seven-day-ui-check -p 127.0.0.1:18191:8081 \
  -v "$PWD/analysis/七天退货验证/.runtime:/check:ro" -w /check \
  maven:3.9.6-eclipse-temurin-17 java -cp '/check/classes:/check/lib/*' \
  com.teachdemo.ecommerce.customer_service.CustomerRefundCheck --ui
```

另一终端执行 `.venv/bin/python analysis/七天退货验证/check_ui.py`，结束后 `docker stop huangye-seven-day-ui-check`。复跑 Java 检查会重新生成 `.runtime`；不要在 H2 浏览器服务使用该目录时同时重新编译。

用户实际本地手测见 `docs/本地测试用例.md` 的七天退货段落。请新建并支付可退商品订单，在后台模拟发货和签收；不要修改旧样例日期、支付流水或商品标记来强行满足资格。

未扩展部分退货、运费结算、图片凭证、真实物流、库存恢复或 Python 工作台模拟审批 BC-001/004。本地服务更新不代表生产部署或法律合规验收。
