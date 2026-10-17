---
policy_id: promotion_camping_season_stack_rule
scene_key: promotion
title: 荒野户外 露营季活动规则
score: 0.91
retrieval_stage: pre_retrieval
version: "2026-10-17.1"
content_status: demo_knowledge
publication_status: published
effective_from: "2025-01-01T00:00:00+08:00"
effective_to: "2028-01-01T00:00:00+08:00"
---
<!-- rule {"id":"promotion_scope","topics":["promotion_scope"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":[],"label":"适用范围"} -->
## 1. 适用范围

活动是否正在进行、具体参与商品、活动价、会员门槛、优惠券状态与最终金额，以对应活动、商品及结算信息为准。

露营季不代表固定月份，也没有覆盖全部商品的永久活动周期。文档更新时间不代表活动开始时间，以上规则不包含未发布的其他活动或隐藏券。
<!-- /rule -->

<!-- rule {"id":"promotion_eligibility","topics":["promotion_eligibility"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":[],"label":"活动与商品条件"} -->
## 2. 活动与商品条件

不同会场、商品和账号可能适用不同优惠。活动宣传中的品类名称不表示该品类全部商品参加，商品详情页出现活动信息也不代表每位顾客都符合条件。

具体SKU的活动价与通用满减属于不同依据，通用规则不自动证明当前商品可以叠加，最终以商品条件与结算支持的组合为准。
<!-- /rule -->

<!-- rule {"id":"promotion_types","topics":["promotion_types"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":[],"label":"优惠类型"} -->
## 3. 优惠类型

会员专享价、活动直降、品类券和满减是不同条件类型。商品活动价与会员券面额不能未经具体组合规则确认就全部相减。
<!-- /rule -->

<!-- rule {"id":"promotion_stacking","topics":["promotion_stacking"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":["coupon_conditions","promotion_scope"],"label":"满减与会员券叠加"} -->
## 4. 满 300 减 40 与叠加边界

符合露营季活动条件时，满 300 减 40 可以与平台会员券叠加；同类型满减券不能重复叠加。该规则没有定义“每满 300 减 40”、所有订单无限次参加或其他券种一律兼容。

是否达到300元取决于活动计入的金额和商品范围。运费、未选中商品或其他活动商品是否计入，当前通用规则没有统一保证。

满600元不自动表示减80元；不同券种能否组合，仍以对应券及活动规则为准。该规则不自动覆盖品类券、定向券或未发布的其他活动。
<!-- /rule -->

<!-- rule {"id":"promotion_price","topics":["promotion_price"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":[],"label":"页面价格与成交价格"} -->
## 5. 页面价格与成交价格

商品页面的基础价与有条件活动价可能不同，实际成交价还受会员条件、数量、选择的商品及下单时状态影响。

旧截图反映当时看到的价格，不自动证明现在仍应恢复该活动价。当前没有统一的保价条款、返差价或先付款后补偿承诺。
<!-- /rule -->

<!-- rule {"id":"promotion_status","topics":["promotion_status"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":[],"label":"活动与查询状态"} -->
## 6. 活动与查询状态

活动过期、商品没有对应活动、商品无库存和本次查询失败是不同情况。未取得当前数据时，对应价格、参与范围或优惠状态暂不能确认。

商品库存为零不代表一定会补货。没有对应已发布规则的活动，不能直接套用露营季优惠。
<!-- /rule -->

<!-- rule {"id":"promotion_refund","topics":["promotion_refund"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":[],"label":"退款、退货与优惠"} -->
## 7. 退款、退货与优惠

活动参与不改变本人订单、支付状态、发货状态或购买时的七天无理由资格。特价不单独排除所有售后，参加活动也不自动表示商品符合无理由退货条件。

退款金额以该笔订单实际支付及已退款记录为准，与今天的标价或宣传满减不同。部分退货后的优惠分摊、返券和赠品处理没有统一自动办理保证。

咨询优惠不会自动触发领券、改价、创建订单或退款。
<!-- /rule -->

<!-- rule {"id":"promotion_evidence","topics":["promotion_evidence"],"domain":"promotion_and_member_policy","scope":"mall_demo","requires":[],"label":"规则适用与证据边界"} -->
## 9. 规则适用与证据边界

平台通用政策与具体商品、本人持券和订单条件需要同时满足。规则冲突、时间或对象不明确时，适用资格暂不能确认。
<!-- /rule -->
