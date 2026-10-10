package com.teachdemo.ecommerce.order;

import com.fasterxml.jackson.annotation.JsonInclude;

import io.swagger.v3.oas.annotations.media.Schema;

import java.math.BigDecimal;

@Schema(description = "Order line item")
public class OrderItemResponse {

    @Schema(description = "Product ID", example = "1")
    private final Long productId;
    @Schema(description = "Product name", example = "Noise Cancelling Headphones")
    private final String productName;
    @Schema(description = "Quantity", example = "1")
    private final Integer quantity;
    @Schema(description = "Unit price", example = "299.00")
    private final BigDecimal unitPrice;
    @JsonInclude(JsonInclude.Include.ALWAYS)
    @Schema(description = "Whether the item supported no-reason returns when purchased", nullable = true, example = "true")
    private final Boolean returnableAtPurchase;

    public OrderItemResponse(Long productId, String productName, Integer quantity, BigDecimal unitPrice) {
        this(productId, productName, quantity, unitPrice, null);
    }

    public OrderItemResponse(Long productId, String productName, Integer quantity, BigDecimal unitPrice,
                             Boolean returnableAtPurchase) {
        this.productId = productId;
        this.productName = productName;
        this.quantity = quantity;
        this.unitPrice = unitPrice;
        this.returnableAtPurchase = returnableAtPurchase;
    }

    public Long getProductId() {
        return productId;
    }

    public String getProductName() {
        return productName;
    }

    public Integer getQuantity() {
        return quantity;
    }

    public BigDecimal getUnitPrice() {
        return unitPrice;
    }

    public Boolean getReturnableAtPurchase() {
        return returnableAtPurchase;
    }
}
