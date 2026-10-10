package com.teachdemo.ecommerce.account;

import com.teachdemo.ecommerce.account.BalanceAccount;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

public interface BalanceAccountRepository extends JpaRepository<BalanceAccount, Long> {

    Optional<BalanceAccount> findByUserId(String userId);

    @org.springframework.data.jpa.repository.Lock(jakarta.persistence.LockModeType.PESSIMISTIC_WRITE)
    @org.springframework.data.jpa.repository.Query("select a from BalanceAccount a where a.userId = :userId")
    Optional<BalanceAccount> findByUserIdForUpdate(@org.springframework.data.repository.query.Param("userId") String userId);
}
