package com.teachdemo.ecommerce.order;

import com.teachdemo.ecommerce.order.LogisticsInfo;

import com.teachdemo.ecommerce.order.OrderEntity;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

public interface LogisticsInfoRepository extends JpaRepository<LogisticsInfo, Long> {

    Optional<LogisticsInfo> findByOrderEntity(OrderEntity orderEntity);
}
