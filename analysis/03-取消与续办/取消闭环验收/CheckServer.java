package com.teachdemo.ecommerce.customer_service;

import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

/** 仅分析目录编译的隔离入口。内存 H2、合成支付，不连接日常 MySQL。 */
public class CheckServer {
    static ServletWebServerApplicationContext context;
    static final ObjectMapper JSON = new ObjectMapper();

    public static void main(String[] args) throws Exception {
        context = (ServletWebServerApplicationContext) new SpringApplicationBuilder(
            EcommerceBackendApplication.class, Probe.class).run(
            "--server.address=127.0.0.1", "--server.port=18102",
            "--spring.datasource.url=jdbc:h2:mem:cancel-closure;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa",
            "--spring.datasource.password=", "--spring.jpa.hibernate.ddl-auto=create-drop",
            "--spring.jpa.show-sql=false", "--logging.level.root=WARN", "--spring.main.banner-mode=off",
            "--agent.service.base-url=http://127.0.0.1:18101", "--agent.service.read-timeout-ms=120000",
            "--agent.service.auth-token=course-debug-agent-service",
            "--customer-service.runtime-context.default-order-limit=2");
        IsolatedCancelCheck.fixture(context, "ORD9801", 2);
        IsolatedCancelCheck.fixture(context, "ORD9802", 1);
        if ("scripted".equals(System.getenv("CANCEL_CHECK_MODE"))) {
            IsolatedCancelCheck.fixture(context, "ORD9803", -1);
            var orders = context.getBean(com.teachdemo.ecommerce.order.OrderRepository.class);
            var delivered = orders.findByOrderNo("ORD9803").orElseThrow();
            delivered.updateAdminFields("DELIVERED", "DELIVERED", "ISOLATED-RETURN", "隔离已签收样本",
                java.time.LocalDateTime.now(java.time.ZoneId.of("Asia/Shanghai")).minusMinutes(1));
            orders.save(delivered);
        }
        System.out.println("CANCEL_CHECK_READY");
    }

    @RestController
    static class Probe {
        @GetMapping("/api/check/state")
        public Map<String, Object> state(@RequestParam(name="session", defaultValue="") String id) throws Exception {
            Map<String, Object> result = new LinkedHashMap<>();
            var jdbc = context.getBean(JdbcTemplate.class);
            Map<String, Object> tables = new LinkedHashMap<>();
            for (String table : List.of("order_header", "order_item", "balance_account", "balance_transaction",
                "after_sale_request", "approval_record", "logistics_info", "logistics_event")) {
                var rows = jdbc.queryForList("select * from " + table + " order by id");
                String raw = JSON.writeValueAsString(rows);
                String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(raw.getBytes(StandardCharsets.UTF_8)));
                tables.put(table, Map.of("rows", rows.size(), "sha256", hash));
            }
            result.put("tables", tables);
            if (!id.isEmpty()) {
                var session = context.getBean(CustomerServiceSessionStore.class).find(id).orElseThrow();
                Map<String, Object> values = new LinkedHashMap<>();
                values.put("pending", session.getPendingAction());
                values.put("handled", session.isHandled());
                values.put("token", session.getResumeToken());
                values.put("revision", session.getDraftRevision());
                values.put("objectWait", session.getObjectWait());
                values.put("order", session.getRelatedOrderNo());
                result.put("session", values);
            }
            return result;
        }
    }
}
