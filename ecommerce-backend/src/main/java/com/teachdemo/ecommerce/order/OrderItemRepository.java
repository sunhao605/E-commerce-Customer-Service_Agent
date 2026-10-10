package com.teachdemo.ecommerce.order;

import com.teachdemo.ecommerce.order.OrderEntity;

import com.teachdemo.ecommerce.order.OrderItem;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;

public interface OrderItemRepository extends JpaRepository<OrderItem, Long> {

    List<OrderItem> findByOrderEntity(OrderEntity orderEntity);
}
