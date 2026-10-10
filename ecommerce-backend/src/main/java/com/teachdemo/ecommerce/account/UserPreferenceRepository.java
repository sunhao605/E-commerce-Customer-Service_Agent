package com.teachdemo.ecommerce.account;

import com.teachdemo.ecommerce.account.UserPreference;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

public interface UserPreferenceRepository extends JpaRepository<UserPreference, Long> {

    Optional<UserPreference> findByUserId(String userId);
}
