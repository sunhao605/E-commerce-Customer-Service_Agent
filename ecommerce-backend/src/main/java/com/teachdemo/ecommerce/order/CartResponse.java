package com.teachdemo.ecommerce.order;

import java.math.BigDecimal;

import java.util.List;

public record CartResponse(
    List<CartItemResponse> items,
    BigDecimal selectedTotalAmount,
    Integer selectedItemCount
) {
}
