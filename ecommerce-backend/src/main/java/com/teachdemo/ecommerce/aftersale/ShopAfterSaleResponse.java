package com.teachdemo.ecommerce.aftersale;

import java.math.BigDecimal;

import java.time.LocalDateTime;

public record ShopAfterSaleResponse(
    String requestNo,
    String orderNo,
    String type,
    String status,
    BigDecimal amount,
    String reason,
    LocalDateTime createdAt,
    String handlingNote,
    ReturnInfoResponse returnInfo
) {
}
