package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * Runtime Context 使用的订单商品安全摘要。
 */
public record RuntimeOrderItemSummaryResponse(
    String productName,
    Integer quantity,
    @JsonInclude(JsonInclude.Include.ALWAYS) Boolean returnableAtPurchase,
    @Deprecated Boolean returnable
) {
}
