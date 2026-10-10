package com.teachdemo.ecommerce.account;

import com.teachdemo.ecommerce.account.UserCoupon;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;

public interface UserCouponRepository extends JpaRepository<UserCoupon, Long> {

    List<UserCoupon> findByUserIdAndStatus(String userId, String status);
}
