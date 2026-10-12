package com.teachdemo.ecommerce.order;

import java.math.BigDecimal;

public record AfterSaleEligibilityResponse(
    String orderNo, String status, String applicationType, BigDecimal amount, String reason
) {
}
