package com.teachdemo.ecommerce.aftersale;

import com.teachdemo.ecommerce.aftersale.RefundRequest;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

public interface RefundRequestRepository extends JpaRepository<RefundRequest, Long> {

    Optional<RefundRequest> findByRequestId(String requestId);
}
