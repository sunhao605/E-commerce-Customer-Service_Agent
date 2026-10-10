package com.teachdemo.ecommerce.order;

import com.teachdemo.ecommerce.order.LogisticsEvent;

import com.teachdemo.ecommerce.order.LogisticsInfo;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;

public interface LogisticsEventRepository extends JpaRepository<LogisticsEvent, Long> {

    List<LogisticsEvent> findByLogisticsInfoOrderByOccurredAtDesc(LogisticsInfo logisticsInfo);
}
