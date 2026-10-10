package com.teachdemo.ecommerce.order;

import jakarta.persistence.Column;

import jakarta.persistence.ConstraintMode;

import jakarta.persistence.Entity;

import jakarta.persistence.FetchType;

import jakarta.persistence.ForeignKey;

import jakarta.persistence.GeneratedValue;

import jakarta.persistence.GenerationType;

import jakarta.persistence.Id;

import jakarta.persistence.JoinColumn;

import jakarta.persistence.ManyToOne;

import java.math.BigDecimal;

@Entity
public class OrderItem {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "order_id", nullable = false, foreignKey = @ForeignKey(ConstraintMode.NO_CONSTRAINT))
    private OrderEntity orderEntity;

    private Long productId;

    private String productName;

    private Integer quantity;

    private BigDecimal unitPrice;

    @Column(name = "returnable_at_purchase")
    private Boolean returnableAtPurchase;

    protected OrderItem() {
    }

    public OrderItem(OrderEntity orderEntity, Long productId, String productName, Integer quantity, BigDecimal unitPrice) {
        this(orderEntity, productId, productName, quantity, unitPrice, null);
    }

    public OrderItem(OrderEntity orderEntity, Long productId, String productName, Integer quantity,
                     BigDecimal unitPrice, Boolean returnableAtPurchase) {
        this.orderEntity = orderEntity;
        this.productId = productId;
        this.productName = productName;
        this.quantity = quantity;
        this.unitPrice = unitPrice;
        this.returnableAtPurchase = returnableAtPurchase;
    }

    public Long getId() {
        return id;
    }

    public OrderEntity getOrderEntity() {
        return orderEntity;
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
