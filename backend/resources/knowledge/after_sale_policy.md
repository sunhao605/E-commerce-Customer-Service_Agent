---
policy_id: refund_before_shipping
scene_key: after_sale_refund
title: 荒野户外未发货退款 SOP
score: 0.92
retrieval_stage: pre_retrieval
version: "2026-10-17.1"
content_status: demo_knowledge
publication_status: published
effective_from: "2025-01-01T00:00:00+08:00"
effective_to: "2028-01-01T00:00:00+08:00"
---
<!-- rule {"id":"refund_conditions","topics":["refund_conditions"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"退款条件"} -->
已支付且未出库、未发货的订单，可以发起退款申请；资金类动作必须进入售后审批。

# 荒野户外未发货退款与申请状态
<!-- /rule -->

<!-- rule {"id":"refund_scope","topics":["refund_conditions"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"退款适用范围"} -->
## 1. 适用范围

当前自助退款支持本人已支付、尚未出库且未发货订单的整单退款。签收后的七天无理由退货、已发货或运输中的订单、部分商品退款，适用不同的条件和处理范围。
<!-- /rule -->

<!-- rule {"id":"refund_arrival","topics":["refund_arrival"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"退款到账"} -->
当前退款使用演示余额和资金流水，不提供真实银行卡、支付宝或微信退款的到账时限保证。
<!-- /rule -->

<!-- rule {"id":"refund_application","topics":["refund_process"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"咨询与申请"} -->
## 2. 咨询与申请

了解退款条件、审核流程、到账情况或查询具体订单资格，不会自动创建退款申请。申请需要明确办理意愿，并在核对订单和确认信息后由本人确认提交。

准备申请、停止未提交的办理、撤销已提交申请和取消购物订单是不同操作。当前停止功能只停止尚未提交的售后准备，不会撤销已提交申请或取消购物订单。
<!-- /rule -->

<!-- rule {"id":"refund_eligibility","topics":["refund_conditions"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"订单资格与金额依据"} -->
## 3. 订单资格与金额依据

申请资格以本人订单的当前支付、出库、发货状态及可信资金记录为准。物流尚未更新不等于尚未出库或发货；资格事实缺失时暂不能确认。

申请金额以该笔订单实际支付与已有退款记录为依据，与今天的商品标价或后来变化的活动价不同。当前支持整单；部分退款、仅退运费和差价补偿不在这条自助流程范围内。

定制商品的无理由退货限制，不直接决定未发货退款资格；仍取决于该订单状态、制作情况与适用规则。
<!-- /rule -->

<!-- rule {"id":"refund_submit","topics":["refund_process"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"确认与提交"} -->
## 4. 确认与提交

确认信息包括订单、商品、整单范围、可验证实付金额和下一步。未发货退款申请需要填写问题或原因；签收后七天无理由无需另填原因的规定不自动适用于其他售后。

顾客确认前申请尚未提交。提交时会重新核验资格与金额；准备确认后订单已发货、金额变化或出现重复申请，可能导致原确认信息不能继续使用。
<!-- /rule -->

<!-- rule {"id":"refund_stages","topics":["refund_process"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"审核与处理状态"} -->
## 5. 审核与处理状态

- 咨询或资格查询：解释规则或读取订单，没有创建申请。
- 准备确认：申请信息已准备，等待顾客决定是否提交。
- 待审核：业务系统已保存申请，仍需商家审核。
- 需补材料：需按该申请记录补充指定信息。
- 驳回：申请未获批准，原因以审核记录为准。
- 批准及资金处理：审批和实际资金结果属于不同记录，批准文字不单独证明到账。
- 已退款：该笔申请与可信资金结果已确认完成。

管理员审核与客户确认属于不同角色。商家口头同意与业务系统完成审批也不同；顾客确认不代表已批准或已退款。
<!-- /rule -->

<!-- rule {"id":"refund_progress","topics":["refund_progress"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"退款进度与重复申请"} -->
## 6. 退款进度与重复申请

待审核、需补材料或驳回时，余额未变化不能直接解释为退款成功。当前退款结果以申请、资金流水及订单状态为准，旧聊天回复不能证明当前状态。

同一笔已完成退款不会再次退回整笔款项；已有待处理申请时沿用现有记录。订单、申请和资金状态不一致时，需要进一步核对，不代表应重新退款。
<!-- /rule -->

<!-- rule {"id":"refund_services","topics":["refund_services"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"其他售后与服务边界"} -->
## 7. 其他售后与服务边界

部分退款、仅退运费、差价补偿、真实支付渠道撤销、运输拦截和定制制作暂停，需要各自的规则及办理能力，不能由当前整单退款流程代办。

当前人工入口是开发中的占位页面。确认打开入口不代表已连接真人、创建工单或安排回电。
<!-- /rule -->

<!-- rule {"id":"refund_discounts","topics":["refund_discounts"],"domain":"after_sale_policy","scope":"mall_demo","requires":[],"label":"优惠与退款的关系"} -->
## 9. 优惠与退款的关系

购买后活动价变化不改变本笔实际支付记录。返券、价保与退款属于不同规则；退款完成不自动表示优惠券已退回。
<!-- /rule -->
