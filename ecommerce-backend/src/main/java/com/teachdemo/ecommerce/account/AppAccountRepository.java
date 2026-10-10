package com.teachdemo.ecommerce.account;

import com.teachdemo.ecommerce.account.AppAccount;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

public interface AppAccountRepository extends JpaRepository<AppAccount, Long> {

    Optional<AppAccount> findByUsername(String username);
}
