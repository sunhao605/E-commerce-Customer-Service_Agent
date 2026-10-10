package com.teachdemo.ecommerce.aftersale;

import com.teachdemo.ecommerce.aftersale.AfterSaleRequest;

import java.util.List;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import jakarta.persistence.LockModeType;

public interface AfterSaleRequestRepository extends JpaRepository<AfterSaleRequest, Long> {

    Optional<AfterSaleRequest> findByRequestId(String requestId);

    interface RequestOwner {
        String getOrderNo();
        String getUserId();
    }

    @Query("select r.orderNo as orderNo, r.userId as userId from AfterSaleRequest r where r.requestId = :requestId")
    Optional<RequestOwner> findOwnerByRequestId(@Param("requestId") String requestId);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select r from AfterSaleRequest r where r.requestId = :requestId")
    Optional<AfterSaleRequest> findByRequestIdForUpdate(@Param("requestId") String requestId);

    List<AfterSaleRequest> findByOrderNo(String orderNo);

    Optional<AfterSaleRequest> findByRequestIdAndUserId(String requestId, String userId);

    List<AfterSaleRequest> findByUserId(String userId);

    boolean existsByOrderNoAndUserIdAndRequestTypeAndStatus(String orderNo, String userId,
                                                            String requestType, String status);
}
