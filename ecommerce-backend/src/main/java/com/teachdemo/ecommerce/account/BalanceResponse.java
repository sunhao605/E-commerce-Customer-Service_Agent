package com.teachdemo.ecommerce.account;

import java.math.BigDecimal;

import java.time.LocalDateTime;

public record BalanceResponse(
    String userId,
    BigDecimal availableBalance,
    LocalDateTime updatedAt
) {
}
