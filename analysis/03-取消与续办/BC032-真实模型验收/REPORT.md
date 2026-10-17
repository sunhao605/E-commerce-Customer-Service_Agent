# BC032-LIVE-001：真实模型＋隔离 Python→Java→页面验收

执行日期：2026-10-17 18:12–18:14（上海）。执行编号：`BC032-LIVE-001`。

## 结论

29 个自然轮次：**21 PASS／8 FAIL／0 BLOCKED／0 NOT_RUN**。业务表前后不变；未发生取消误执行或资金动作；56 次模型 HTTP、160,862 tokens，全部 HTTP 200，未触发预算闸门。

8 个 FAIL 分为两类，不能合并成“真实模型通过率”：

- **产品／展示缺口（3 轮）**：退款待选订单时，Python 记忆里保留了 ORD9801／ORD9802 候选，但用户可见答复只有“请选择订单号”，没有列出候选。`G02-T1`、`G03B-T1`、`G03B-T2`。
- **验收器／用例口径问题（5 轮，非产品失败）**：
  - `G02-T3`、`G02B-T2`：选单成功后 `clarification_focus=null`，用例仍要求 `order_query`；实际 `order_query` 与绑定订单都已写入，焦点已解决后清空。
  - `G03B-T3`、`G03C-T2`：验收器优先读了 Python 内部占位答复，未读用户可见的 Java 答复；后者包含 `ORD9802`。
  - `G05-T4`：runner 在 `reset` 轮后没有登记该会话，下一轮又执行了一次 seed，把 T3 已取消的会话重新写回 `REFUND_ORDER_REQUIRED`；T3 本身已 PASS，取消实际成功。

因此本轮**不能**把 BC-032 标为整类通过；也**没有**证据说 29 条自然表达里 8 条是产品失败率。

## 冻结与配置

- `frozen.sha256`：`4f2bfdf88349e9bc529b6fd3019c4e9e575cd17ca1e208ea879292c79d361259`
- 冻结时间：2026-10-17T18:12:56+0800
- 模型：`deepseek-flash` @ `api.deepseek.com`；route 0℃／512 tokens，generation 0.2℃／1024，judge 0℃／1536；自动重试 0。
- `ANSWER_JUDGE_MODE=enforce`，timeout 5s；`CANCEL_INTENT_EXECUTE=0`；无自动取消。
- 预算：120 次／300,000 tokens；实际 56 次／160,862 tokens，单例预算未触发。
- 运行前后源码指纹一致；`frozen.json` 校验通过，执行期间无源码改动。
- 隔离环境：Python `127.0.0.1:18101`，Java 容器 `127.0.0.1:18112`（原 18102 被残留 `limactl` 端口转发占用，未动该进程），Vite `127.0.0.1:18103`。
- Java 为内存 H2＋合成订单 A–E（`ORD9801`–`ORD9805`），未连接日常 8081／MySQL；只准备草稿，不确认提交。
- G02、G03B 通过浏览器自然建立初态；其余组用隔离 `/check/seed` 构造初态并在 `cases.json` 标明。

## 逐轮结果

| 组 | 轮次 | 判定 | Python intent | 绑定订单 | 失败检查 |
|---|---|---|---|---|---|
| G01 | G01-T1 | PASS | promotion_query | — | — |
| G01 | G01-T2 | PASS | general_chat | — | — |
| G02 | G02-T1 | FAIL | refund_request | — | 页面候选顺序 |
| G02 | G02-T2 | PASS | order_query | — | — |
| G02 | G02-T3 | FAIL | order_query | ORD9801 | Python澄清焦点 |
| G02B | G02B-T1 | PASS | order_query | — | — |
| G02B | G02B-T2 | FAIL | order_query | ORD9802 | Python澄清焦点 |
| G03A | G03A-T1 | PASS | refund_request | ORD9801 | — |
| G03B | G03B-T1 | FAIL | refund_request | — | 页面候选顺序 |
| G03B | G03B-T2 | FAIL | refund_request | — | 页面候选顺序 |
| G03B | G03B-T3 | FAIL | refund_request | ORD9802 | 答复包含 ORD9802 |
| G03C | G03C-T1 | PASS | refund_request | — | — |
| G03C | G03C-T2 | FAIL | refund_request | ORD9802 | 答复包含 ORD9802 |
| G04A | G04A-T1 | PASS | product_query | — | — |
| G04A | G04A-T2 | PASS | invoice_query | — | — |
| G04A | G04A-T3 | PASS | product_query | — | — |
| G04B | G04B-T1 | PASS | promotion_query | — | — |
| G04B | G04B-T2 | PASS | general_chat | — | — |
| G04B | G04B-T3 | PASS | promotion_query | — | — |
| G05 | G05-T1 | PASS | invoice_query | — | — |
| G05 | G05-T2 | PASS | general_chat | — | — |
| G05 | G05-T3 | PASS | —（Java 明确取消） | — | — |
| G05 | G05-T4 | FAIL | general_chat | — | Python原待办=None；Java取消未保持 |
| G05 | G05-T5 | PASS | general_chat | — | — |
| G06A | G06A-T1 | PASS | invoice_query | — | — |
| G06A | G06A-T2 | PASS | return_request | ORD9805 | — |
| G06B | G06B-T1 | PASS | —（路线注入） | — | — |
| G06B | G06B-T2 | PASS | —（工具注入） | — | — |
| G06B | G06B-T3 | PASS | order_query | ORD9801 | — |

## 关键通过项

- **插问保留**：G01 优惠券咨询与客套话后，原退款待办、A／B 候选、版本不变；未重发确认卡。
- **查询与办理分开**：“退款先放着”后补 `ORD9801` 只绑定物流查询，Java 退款草稿字段不变（G02-T2／T3、G02B）。
- **明确恢复原任务**：G03A 从页面 C、最近资格查询 D 中恢复原草稿 A，重发 A 的退款确认卡，未提交。
- **未选单恢复**：G03B-T3 选择“第二笔”绑定 `ORD9802`，未被页面或最近订单代选；只进入“补退款原因”，未出确认卡。
- **越界序号**：G03C-T1 “第三笔”未改候选／版本，也未回落其他订单；随后“第二笔”仍正确绑定 B。
- **商品与优惠状态**：G04A 商品候选跨发票插问保留并正确选中“轻量露营折叠椅”；G04B 优惠待补条件跨客套保留，补充落到原优惠问题。
- **关系判断不等于执行**：G05-T1／T2 的否定与犹豫没有取消或提交；G05-T3 “不退了”经 Java 原明确取消路径成功，返回“已取消、未提交”；G05-T5 只说明未执行，不宣称已停止或已办新退货。
- **退货恢复**：G06A 从发票插问恢复 `ORD9805` 的退货确认卡，未改成退款、未提交。
- **故障边界**：G06B 路线／工具注入使该轮降级为“客服繁忙”，原待办和 Java 字段不变；生成注入被消费，该轮仍以工具事实答复，未破坏状态。

## 失败证据与判断

### 1. 候选未展示（产品／展示）

`G02-T1` 的用户可见答复：

> 你要退款哪一个订单？请选择订单号，或直接补充订单号。

同轮 Python 记忆：

```json
"open_task": {"intent": "refund_request", "pending_slot": "order_id",
  "candidates": [{"value": "ORD9801"}, {"value": "ORD9802"}]}
```

Java 响应没有 `clarification` 字段，前端也没有候选块；因此顾客看不到 A／B。对照 `G02-T2` 物流查询会列出“当前账号下可选订单：ORD9801…ORD9802…”，说明状态在、展示分支漏了。G03B 同样。

这是本轮唯一可复现的产品缺口；不影响对象绑定和状态保留，但让用户无法知道“第二笔”指哪一笔。

### 2. 焦点期望过严（用例口径）

`G02-T3`／`G02B-T2` 的 `clarification_focus` 实际为 `null`，但 `order_query` 已写入并带 `order_id`，绑定订单正确。焦点用于“待澄清事项”，选单完成后清空是合理行为；原用例要求保持 `order_query` 与实现语义不符。

### 3. 答复检查读错对象（验收器）

`G03B-T3`／`G03C-T2` 的 Python 答复是内部占位“已识别申请诉求，待商城核验…”，用户可见 Java 答复是“订单 ORD9802 的整单实付金额为 200.00 元。请补充退款原因…”。验收器优先取 Python 答复，造成假失败。

### 4. 重置会话被二次 seed（验收器）

`G05-T3` 使用 `reset` 新建会话并成功取消（`pending=CANCELLED`）。`G05-T4` 开始前 runner 因该会话未登记又 seed 一次，恢复成 `REFUND_ORDER_REQUIRED`，导致 T4 的两个检查失败。产品侧的取消结果以 T3 为准；T4 的“取消保持”证据无效，不能据此判定产品复活草稿。

## 证据文件

- `frozen.json`／`frozen.sha256`：冻结配置、用例、源码与知识指纹。
- `turns-BC032-LIVE-001.jsonl`：29 轮原始请求、响应、Python 状态、Java 前后字段、页面信息与逐项检查。
- `models-real.jsonl`：56 次模型 HTTP 的模型、阶段、usage、原始输出与状态。
- `python-turns-real.jsonl`：隔离 Python 每轮请求／响应（含页面上下文拼接后的用户消息）。
- `results-BC032-LIVE-001.json`／`summary-BC032-LIVE-001.json`：汇总。
- `business-before-BC032-LIVE-001.json`／`business-after-BC032-LIVE-001.json`：业务表行数与 SHA256。
- `selfcheck.json`：同源码基线的离线全链路自检（模型调用 0）。

复跑命令（换新执行编号，先归档本目录的冻结与结果，再重新冻结并确认新预算）：

```bash
.venv/bin/python analysis/03-取消与续办/BC032-真实模型验收/run.py --prepare --run-id BC032-LIVE-002
.venv/bin/python analysis/03-取消与续办/BC032-真实模型验收/run.py --self-check
.venv/bin/python analysis/03-取消与续办/BC032-真实模型验收/run.py --run --run-id BC032-LIVE-002
```

本报告生成后仅修正了 runner 的 `reset` 会话登记与“用户可见答复”取值，供后续执行使用；`frozen.json` 仍保存当次 runner 指纹，本次 29 轮结果未被重跑或改写。

## 边界

- 一次真实模型运行、一个模型版本、一组合成订单；不是通过率，也不是跨模型／跨用户结论。
- judge 为 `enforce`，但正文 pass 不替代状态断言；本轮状态检查以 Python 记忆与 Java 业务字段为准。
- 页面检查只覆盖答复文本与确认卡数量，不覆盖移动端、响应式、可访问性或视觉质量。
- 未确认提交任何售后、未推进资金节点、未使用真实订单或余额；未启用自动取消。
- 未修本轮发现的候选展示缺口；未重跑首次结果。
