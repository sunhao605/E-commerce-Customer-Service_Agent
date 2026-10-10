package com.teachdemo.ecommerce.aftersale;

import com.teachdemo.ecommerce.aftersale.AfterSalePolicy;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;

public interface AfterSalePolicyRepository extends JpaRepository<AfterSalePolicy, Long> {

    List<AfterSalePolicy> findBySceneKeyContainingIgnoreCase(String sceneKey);
}
