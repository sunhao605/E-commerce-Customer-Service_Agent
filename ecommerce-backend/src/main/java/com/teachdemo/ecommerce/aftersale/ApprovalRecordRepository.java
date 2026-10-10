package com.teachdemo.ecommerce.aftersale;

import com.teachdemo.ecommerce.aftersale.ApprovalRecord;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;

public interface ApprovalRecordRepository extends JpaRepository<ApprovalRecord, Long> {

    List<ApprovalRecord> findByTargetNoOrderByCreatedAtDesc(String targetNo);
}
