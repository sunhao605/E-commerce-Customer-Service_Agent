package com.teachdemo.ecommerce.customer_service;

import com.teachdemo.ecommerce.account.UserProfileRepository;

import com.teachdemo.ecommerce.common.BusinessException;

import com.teachdemo.ecommerce.customer_service.RuntimeOrderContextResponse;

import com.teachdemo.ecommerce.customer_service.RuntimeOrderItemSummaryResponse;

import com.teachdemo.ecommerce.customer_service.RuntimeOrderSummaryResponse;

import com.teachdemo.ecommerce.order.OrderEntity;

import com.teachdemo.ecommerce.order.OrderItem;

import com.teachdemo.ecommerce.order.OrderItemRepository;

import com.teachdemo.ecommerce.order.OrderRepository;

import java.util.List;

import org.springframework.beans.factory.annotation.Value;

import org.springframework.stereotype.Service;

@Service
public class RuntimeOrderContextService {

    private final UserProfileRepository userProfileRepository;
    private final OrderRepository orderRepository;
    private final OrderItemRepository orderItemRepository;
    private final int defaultLimit;
    private final int maxLimit;

    public RuntimeOrderContextService(UserProfileRepository userProfileRepository,
                                      OrderRepository orderRepository,
                                      OrderItemRepository orderItemRepository,
                                      @Value("${customer-service.runtime-context.default-order-limit:10}") int defaultLimit,
                                      @Value("${customer-service.runtime-context.max-order-limit:50}") int maxLimit) {
        this.userProfileRepository = userProfileRepository;
        this.orderRepository = orderRepository;
        this.orderItemRepository = orderItemRepository;
        this.maxLimit = Math.max(1, maxLimit);
        this.defaultLimit = Math.max(1, Math.min(defaultLimit, this.maxLimit));
    }

    public RuntimeOrderContextResponse loadCurrentUserOrders(String userId, Integer month, Integer limit) {
        userProfileRepository.findByUserId(userId)
            .orElseThrow(() -> BusinessException.notFound("USER_NOT_FOUND", "用户不存在"));
        if (month != null && (month < 1 || month > 12)) {
            throw BusinessException.badRequest("ORDER_MONTH_INVALID", "订单月份必须在 1 到 12 之间");
        }
        int boundedLimit = limit == null ? defaultLimit : Math.max(1, Math.min(limit, maxLimit));
        List<OrderEntity> matchedOrders = orderRepository.findByUserIdOrderByCreatedAtDesc(userId).stream()
            .filter(order -> month == null
                || order.getCreatedAt() != null && order.getCreatedAt().getMonthValue() == month)
            .toList();
        List<RuntimeOrderSummaryResponse> orders = matchedOrders.stream()
            .limit(boundedLimit)
            .map(order -> {
                List<OrderItem> orderItems = orderItemRepository.findByOrderEntity(order);
                List<RuntimeOrderItemSummaryResponse> items = orderItems.stream()
                    .map(this::toRuntimeItem)
                    .toList();
                return new RuntimeOrderSummaryResponse(
                    order.getOrderNo(),
                    order.getUserId(),
                    order.getStatus(),
                    order.getPaymentStatus(),
                    order.getFulfillmentStatus(),
                    order.getTotalAmount(),
                    order.getCreatedAt(),
                    order.getPaidAt(),
                    order.getShippedAt(),
                    order.getDeliveredAt(),
                    order.getLogisticsNo(),
                    orderItems.stream().map(OrderItem::getProductName).toList(),
                    items,
                    aggregateReturnableAtPurchase(items),
                    aggregateReturnabilityStatus(items),
                    aggregateReturnableAtPurchase(items)
                );
            })
            .toList();
        return new RuntimeOrderContextResponse(orders, matchedOrders.size() > boundedLimit, boundedLimit);
    }

    private RuntimeOrderItemSummaryResponse toRuntimeItem(OrderItem item) {
        Boolean snapshot = item.getReturnableAtPurchase();
        return new RuntimeOrderItemSummaryResponse(item.getProductName(), item.getQuantity(), snapshot, snapshot);
    }

    private Boolean aggregateReturnableAtPurchase(List<RuntimeOrderItemSummaryResponse> items) {
        if (items.isEmpty() || items.stream().anyMatch(item -> item.returnableAtPurchase() == null)) {
            return null;
        }
        return items.stream().allMatch(item -> Boolean.TRUE.equals(item.returnableAtPurchase()));
    }

    private String aggregateReturnabilityStatus(List<RuntimeOrderItemSummaryResponse> items) {
        if (items.isEmpty() || items.stream().anyMatch(item -> item.returnableAtPurchase() == null)) {
            return "unknown";
        }
        boolean anyReturnable = items.stream().anyMatch(item -> Boolean.TRUE.equals(item.returnableAtPurchase()));
        boolean anyNotReturnable = items.stream().anyMatch(item -> Boolean.FALSE.equals(item.returnableAtPurchase()));
        if (anyReturnable && anyNotReturnable) {
            return "mixed";
        }
        return anyReturnable ? "returnable" : "not_returnable";
    }
}
