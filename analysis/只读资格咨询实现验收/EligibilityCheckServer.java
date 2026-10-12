package com.teachdemo.ecommerce.customer_service;

import com.teachdemo.ecommerce.order.*;
import com.teachdemo.ecommerce.account.*;
import com.teachdemo.ecommerce.common.BusinessException;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.util.*;
import org.springframework.web.bind.annotation.*;

/** 复用既有隔离H2启动器，只补资格样本；不连接日常商城或外部模型。 */
public class EligibilityCheckServer {
    public static void main(String[] args) throws Exception {
        CheckServer.main(args);
        var ctx = CheckServer.context;
        var orders = ctx.getBean(OrderRepository.class);
        var now = LocalDateTime.now(ZoneId.of("Asia/Shanghai"));
        IsolatedCancelCheck.fixture(ctx, "ORD9812", 3);
        var expired = orders.findByOrderNo("ORD9812").orElseThrow();
        expired.updateAdminFields("DELIVERED", "DELIVERED", "EXPIRED-TEST", "七天窗口外", now.minusDays(9));
        orders.save(expired);
        IsolatedCancelCheck.fixture(ctx, "ORD9813", -2);
        var transactions = ctx.getBean(BalanceTransactionRepository.class);
        transactions.deleteAll(transactions.findByUserIdAndTypeOrderByCreatedAtDesc("U1001", "PAYMENT").stream()
            .filter(item -> "ORD9813".equals(item.getOrderNo())).toList());
        orders.save(new OrderEntity("ORD9899", "不能泄露的另一客户", "PAID_PENDING_SHIPMENT", "PAID",
            new BigDecimal("999"), now, "U1002", false, true, "他人订单"));
        var shop = ctx.getBean(ShopService.class);
        List<Object> checks = new ArrayList<>();
        verify(checks, shop.getAfterSaleEligibility("U1001", "ORD9801").status().equals("ELIGIBLE"), "已支付未发货可申请退款");
        var returned = shop.getAfterSaleEligibility("U1001", "ORD9803");
        verify(checks, returned.status().equals("ELIGIBLE") && returned.applicationType().equals("RETURN"), "当前七天窗口内可申请退货");
        verify(checks, shop.getAfterSaleEligibility("U1001", "ORD9812").status().equals("INELIGIBLE"), "过期退货不符合条件");
        verify(checks, shop.getAfterSaleEligibility("U1001", "ORD9813").status().equals("UNKNOWN"), "支付事实缺失保留未知");
        boolean rejected = false;
        try { shop.getAfterSaleEligibility("U1001", "ORD9899"); }
        catch (BusinessException ex) { rejected = ex.getCode().equals("ORDER_NOT_FOUND"); }
        verify(checks, rejected, "他人订单不可读取");
        System.out.println("ELIGIBILITY_DIRECT_CHECKS " + CheckServer.JSON.writeValueAsString(checks));
        System.out.println("ELIGIBILITY_CHECK_READY");
    }

    static void verify(List<Object> checks, boolean passed, String label) {
        checks.add(Map.of("check", label, "passed", passed));
        if (!passed) throw new AssertionError(label);
    }

    @RestController
    static class ChangeFixture {
        @PostMapping("/api/check/ship")
        public Map<String, Boolean> ship() {
            var repository = CheckServer.context.getBean(OrderRepository.class);
            var order = repository.findByOrderNo("ORD9802").orElseThrow();
            order.updateAdminFields("SHIPPED", "SHIPPED", "CHANGED-TEST", "测试中发货", LocalDateTime.now());
            repository.save(order);
            return Map.of("changed", true);
        }
    }
}
