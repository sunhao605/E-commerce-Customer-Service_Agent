package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.teachdemo.ecommerce.account.BalanceTransaction;
import com.teachdemo.ecommerce.account.BalanceTransactionRepository;
import com.teachdemo.ecommerce.order.OrderEntity;
import com.teachdemo.ecommerce.order.OrderItem;
import com.teachdemo.ecommerce.order.OrderItemRepository;
import com.teachdemo.ecommerce.order.OrderRepository;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

/** 仅分析目录编译的隔离入口。内存 H2、合成支付；只准备草稿，不提交申请。 */
public class CheckServer {
    static ServletWebServerApplicationContext context;
    static final ObjectMapper JSON = new ObjectMapper();

    public static void main(String[] args) throws Exception {
        context = (ServletWebServerApplicationContext) new SpringApplicationBuilder(
            EcommerceBackendApplication.class, Probe.class).run(
            "--server.address=0.0.0.0", "--server.port=18102",
            "--spring.datasource.url=jdbc:h2:mem:bc032-live;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa",
            "--spring.datasource.password=", "--spring.jpa.hibernate.ddl-auto=create-drop",
            "--spring.jpa.show-sql=false", "--logging.level.root=WARN", "--spring.main.banner-mode=off",
            "--agent.service.base-url=http://host.docker.internal:18101",
            "--agent.service.read-timeout-ms=180000",
            "--agent.service.auth-token=course-debug-agent-service",
            "--customer-service.runtime-context.default-order-limit=2");
        refundable("ORD9801", 2);
        refundable("ORD9802", 1);
        refundable("ORD9803", -5);
        refundable("ORD9804", -10);
        returnable("ORD9805", -20);
        System.out.println("BC032_CHECK_READY");
    }

    static void refundable(String no, int minute) {
        LocalDateTime created = LocalDateTime.now().plusMinutes(minute);
        BigDecimal amount = new BigDecimal("200.00");
        OrderEntity order = new OrderEntity(no, "隔离验收", "PAID_PENDING_SHIPMENT", "PAID", amount, created,
            "U1001", false, true, "BC032隔离");
        order.markPaid(created);
        context.getBean(OrderRepository.class).save(order);
        context.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "隔离露营椅", 1, amount, true));
        context.getBean(BalanceTransactionRepository.class).save(new BalanceTransaction("BC032-PAY-" + no, "U1001", no,
            null, "PAYMENT", amount.negate(), new BigDecimal("1000"), new BigDecimal("800"), "隔离合成支付", created));
    }

    static void returnable(String no, int minute) {
        LocalDateTime created = LocalDateTime.now().plusMinutes(minute);
        BigDecimal amount = new BigDecimal("200.00");
        OrderEntity order = new OrderEntity(no, "隔离验收", "PAID", "PAID", amount, created,
            "U1001", false, true, "BC032隔离");
        order.markPaid(created);
        order.updateAdminFields("DELIVERED", "DELIVERED", "BC032-RETURN", "隔离已签收样本",
            LocalDateTime.now(ZoneId.of("Asia/Shanghai")).minusMinutes(1));
        context.getBean(OrderRepository.class).save(order);
        context.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "隔离露营椅", 1, amount, true));
        context.getBean(BalanceTransactionRepository.class).save(new BalanceTransaction("BC032-PAY-" + no, "U1001", no,
            null, "PAYMENT", amount.negate(), new BigDecimal("1000"), new BigDecimal("800"), "隔离合成支付", created));
    }

    static String sha(String value) throws Exception {
        return value == null ? null : HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
            .digest(value.getBytes(StandardCharsets.UTF_8)));
    }

    @RestController
    static class Probe {
        @GetMapping("/api/check/ready")
        public Map<String, Object> ready() {
            return Map.of("ready", true);
        }

        @GetMapping("/api/check/state")
        public Map<String, Object> state(@RequestParam(name = "session", defaultValue = "") String id) throws Exception {
            Map<String, Object> result = new LinkedHashMap<>();
            var jdbc = context.getBean(JdbcTemplate.class);
            Map<String, Object> tables = new LinkedHashMap<>();
            for (String table : List.of("order_header", "order_item", "balance_account", "balance_transaction",
                "after_sale_request", "approval_record", "logistics_info", "logistics_event")) {
                var rows = jdbc.queryForList("select * from " + table + " order by id");
                String raw = JSON.writeValueAsString(rows);
                tables.put(table, Map.of("rows", rows.size(), "sha256", sha(raw)));
            }
            result.put("tables", tables);
            if (!id.isEmpty()) {
                var session = context.getBean(CustomerServiceSessionStore.class).find(id).orElse(null);
                if (session == null) {
                    result.put("session", null);
                } else {
                    Map<String, Object> values = new LinkedHashMap<>();
                    values.put("pending", session.getPendingAction());
                    values.put("handled", session.isHandled());
                    values.put("actionType", session.getActionType());
                    values.put("order", session.getRelatedOrderNo());
                    values.put("reason", session.getRefundReason());
                    values.put("amount", session.getRefundAmount() == null ? null : session.getRefundAmount().toPlainString());
                    values.put("item", session.getRefundItemSummary());
                    values.put("expiresAt", session.getExpiresAt() == null ? null : session.getExpiresAt().toString());
                    values.put("revision", session.getDraftRevision());
                    values.put("tokenSha256", sha(session.getResumeToken()));
                    values.put("workflowSha256", sha(session.getWorkflowId()));
                    values.put("afterSaleQuery", session.getAfterSaleQuery());
                    result.put("session", values);
                }
            }
            return result;
        }

        /** 仅隔离夹具：构造未提交草稿和近期资格查询，不写业务库。 */
        @PostMapping("/api/check/seed")
        public Map<String, Object> seed(@RequestBody Map<String, Object> body) throws Exception {
            String id = String.valueOf(body.get("session"));
            var store = context.getBean(CustomerServiceSessionStore.class);
            var session = store.getOrCreate(id, "U1001");
            session.setPendingAction(text(body, "pending"));
            session.setActionType(text(body, "actionType"));
            session.setRelatedOrderNo(text(body, "order"));
            session.setRefundReason(text(body, "reason"));
            session.setRefundAmount(body.get("amount") == null ? null : new BigDecimal(String.valueOf(body.get("amount"))));
            session.setRefundItemSummary(text(body, "item"));
            int revision = body.get("revision") == null ? 0 : Integer.parseInt(String.valueOf(body.get("revision")));
            while (session.getDraftRevision() < revision) session.bumpDraftRevision();
            int minutes = body.get("expiresMinutes") == null ? 30 : Integer.parseInt(String.valueOf(body.get("expiresMinutes")));
            session.setExpiresAt(session.getPendingAction() == null ? null : LocalDateTime.now().plusMinutes(minutes));
            session.setHandled(false);
            session.setResumeToken("bc032-seed-" + id);
            session.setWorkflowId("bc032-seed-workflow-" + id);
            if (body.get("afterSaleQuery") != null) {
                @SuppressWarnings("unchecked")
                Map<String, Object> query = (Map<String, Object>) body.get("afterSaleQuery");
                session.setAfterSaleQuery(query);
            } else {
                session.setAfterSaleQuery(null);
            }
            store.save(session);
            return Map.of("seeded", id, "revision", session.getDraftRevision());
        }

        static String text(Map<String, Object> body, String key) {
            Object value = body.get(key);
            return value == null ? null : String.valueOf(value);
        }
    }
}
