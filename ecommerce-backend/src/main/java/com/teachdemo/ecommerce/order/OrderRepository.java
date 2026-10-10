package com.teachdemo.ecommerce.order;

import com.teachdemo.ecommerce.order.OrderEntity;

import java.util.List;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import jakarta.persistence.LockModeType;

public interface OrderRepository extends JpaRepository<OrderEntity, Long> {

    Optional<OrderEntity> findByOrderNo(String orderNo);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select o from OrderEntity o where o.orderNo = :orderNo")
    Optional<OrderEntity> findByOrderNoForUpdate(@Param("orderNo") String orderNo);

    Optional<OrderEntity> findByOrderNoAndUserId(String orderNo, String userId);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select o from OrderEntity o where o.orderNo = :orderNo and o.userId = :userId")
    Optional<OrderEntity> findOwnOrderForUpdate(@Param("orderNo") String orderNo, @Param("userId") String userId);

    List<OrderEntity> findByUserIdOrderByCreatedAtDesc(String userId);

    List<OrderEntity> findByUserIdAndStatusOrderByCreatedAtDesc(String userId, String status);
}
