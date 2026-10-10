package com.teachdemo.ecommerce.aftersale;

import com.teachdemo.ecommerce.aftersale.ApprovalRequest;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

public interface ApprovalRequestRepository extends JpaRepository<ApprovalRequest, Long> {

    Optional<ApprovalRequest> findByApprovalId(String approvalId);
}
