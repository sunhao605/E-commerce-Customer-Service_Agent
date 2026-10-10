package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.teachdemo.ecommerce.account.*;
import com.teachdemo.ecommerce.aftersale.*;
import com.teachdemo.ecommerce.order.*;
import java.math.BigDecimal;
import java.net.CookieManager;
import java.net.URI;
import java.net.http.*;
import java.time.LocalDateTime;
import java.util.*;
import java.util.concurrent.*;
import java.util.regex.Pattern;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.annotation.*;
import org.springframework.jdbc.core.JdbcTemplate;

/** 隔离 H2 + 真实 HTTP/认证/事务；只替换模型入口，--approval 额外验证隔离审批。 */
public class CustomerRefundCheck {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final List<String> PASSED = new ArrayList<>();
    private static String base;
    private static ServletWebServerApplicationContext context;
    private static HttpClient customer;
    private static HttpClient admin;
    private static OrderRepository orders;
    private static AfterSaleRequestRepository applications;
    private static BalanceTransactionRepository transactions;
    private static CustomerServiceSessionStore sessions;

    @Configuration(proxyBeanMethods = false)
    static class AgentConfig {
        @Bean @Primary StubAgent isolatedAgent() { return new StubAgent(); }
        @Bean @Primary java.time.Clock returnBoundaryClock() {
            return java.time.Clock.fixed(java.time.Instant.parse("2026-10-09T15:59:59Z"), java.time.ZoneId.of("Asia/Shanghai"));
        }
    }

    static class StubAgent implements CustomerServiceAgentClient {
        int resumes;
        boolean failChat;
        public JsonNode chat(AgentChatRequest request) {
            if (failChat) throw new IllegalStateException("synthetic agent failure");
            ObjectNode response = JSON.createObjectNode().put("answer", "这是隔离模型返回的咨询回复。");
            var matcher = Pattern.compile("\\b(?:ORD\\d{4,}|SO[A-Za-z0-9_-]{6,})\\b").matcher(request.getUserMessage());
            String order = matcher.find() ? matcher.group() : (String) request.getRuntimeContext().get("relatedOrderNo");
            if (request.getUserMessage().contains("退货")) {
                response.putObject("session_state").put("intent", "return_request");
                if (order == null) response.putObject("clarification").put("clarification_field", "order_id");
                else response.withObject("/session_state").putObject("workflow")
                    .put("workflow_type", "received_return").put("order_id", order)
                    .put("return_reason", "七天无理由").put("pending_action", "prepare_return_application");
            } else if (request.getUserMessage().contains("退款")) {
                response.putObject("session_state").put("intent", "refund_request");
                if (order == null) {
                    response.putObject("clarification").put("clarification_field", "order_id");
                } else {
                    response.withObject("/session_state").putObject("workflow")
                        .put("workflow_id", "mock-workflow").put("workflow_type", "unshipped_refund")
                        .put("order_id", order).put("status", "paused").put("pending_action", "require_approval")
                        .put("resume_token", "mock-reviewer-token");
                }
            }
            return response;
        }
        public JsonNode resume(AgentResumeRequest request) {
            resumes++;
            throw new AssertionError("商城不能调用模拟审核入口");
        }
    }

    public static void main(String[] args) throws Exception {
        boolean ui = Arrays.asList(args).contains("--ui");
        boolean approval = Arrays.asList(args).contains("--approval") || Arrays.asList(args).contains("--return");
        boolean returns = Arrays.asList(args).contains("--return");
        context = (ServletWebServerApplicationContext) new SpringApplicationBuilder(EcommerceBackendApplication.class)
            .sources(AgentConfig.class).run(ui ? "--server.port=8081" : "--server.port=0",
                "--spring.datasource.url=jdbc:h2:mem:gap_one;MODE=MySQL;DB_CLOSE_DELAY=-1;LOCK_TIMEOUT=10000",
                "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa",
                "--spring.datasource.password=", "--spring.jpa.hibernate.ddl-auto=create-drop",
                "--spring.jpa.show-sql=false", "--logging.level.root=WARN", "--spring.main.banner-mode=off");
        try {
            base = "http://127.0.0.1:" + context.getWebServer().getPort();
            orders = context.getBean(OrderRepository.class);
            applications = context.getBean(AfterSaleRequestRepository.class);
            transactions = context.getBean(BalanceTransactionRepository.class);
            sessions = context.getBean(CustomerServiceSessionStore.class);
            if (ui) {
                fixture("ORD9200", "U1001"); fixture("ORD9201", "U1001");
                returnFixture("ORD9400", 1); returnFixture("ORD9401", 1);
                System.out.println("UI_READY isolated H2; real HTTP/auth; synthetic Agent");
                new CountDownLatch(1).await();
                return;
            }
            customer = login("zhangsan", "123456");
            admin = login("admin", "admin123456");
            BigDecimal balance = context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance();
            long refunds = transactions.findByUserIdAndTypeOrderByCreatedAtDesc("U1001", "REFUND").size();
            long approvals = context.getBean(ApprovalRecordRepository.class).count();

            fixture("ORD9100", "U1001");
            JsonNode missing = chat("normal", "ORD9100 我要退款");
            check(!missing.path("confirmRequired").asBoolean() && count("ORD9100") == 0, "缺少原因时只追问，不提交");
            JsonNode amount = chat("normal", "能退多少钱？");
            check(amount.path("answer").asText().contains("200.00") && count("ORD9100") == 0, "金额提问只回答可信实付金额");
            JsonNode card = chat("normal", "原因：买错了");
            check(card.path("confirmRequired").asBoolean() && card.path("confirmationSummary").asText().contains("整单退款")
                && !card.path("resumeToken").asText().equals("mock-reviewer-token"), "确认卡完整且不使用模拟审批凭证");
            JsonNode same = chat("normal", "审核是什么流程？");
            check(card.path("resumeToken").equals(same.path("resumeToken")) && count("ORD9100") == 0, "提问保留草稿和确认凭证");
            JsonNode submitted = decision(card, "CONFIRM_SUBMIT", 200);
            String requestNo = submitted.path("relatedAfterSaleNo").asText();
            check(count("ORD9100") == 1 && applications.findByRequestId(requestNo).orElseThrow().getStatus().equals("PENDING_REVIEW"), "确认后只保存一笔待审核记录");
            check(decision(card, "CONFIRM_SUBMIT", 200).path("relatedAfterSaleNo").asText().equals(requestNo), "重复点击返回同一笔申请");
            check(data(call(admin, "GET", "/api/admin/after-sales/" + requestNo, null), 200).path("requestNo").asText().equals(requestNo), "已有管理后台可见同一记录");
            sessions.clear();
            check(data(call(customer, "GET", "/api/shop/after-sales", null), 200).toString().contains(requestNo), "会话结束后本人业务记录仍可查");
            decision(card, "CONFIRM_SUBMIT", 404);
            JsonNode otherSession = chat("replay", "ORD9100 我要退款，原因：重复下单");
            check(decision(otherSession, "CONFIRM_SUBMIT", 200).path("relatedAfterSaleNo").asText().equals(requestNo)
                && count("ORD9100") == 1, "换会话不会新增待处理退款");

            fixture("ORD9101", "U1001");
            JsonNode cancelled = chat("cancel", "ORD9101 我要退款，原因：行程取消");
            check(decision(cancelled, "CANCEL_SUBMIT", 200).path("pendingAction").asText().equals("CANCELLED"), "取消写回后端");
            decision(cancelled, "CONFIRM_SUBMIT", 409);
            check(count("ORD9101") == 0, "取消后旧卡不能提交");

            fixture("ORD9102", "U1001");
            JsonNode before = chat("change", "ORD9102 我要退款，原因：买错了");
            JsonNode changed = chat("change", "原因：行程取消");
            check(!before.path("resumeToken").equals(changed.path("resumeToken")), "修改原因生成新确认凭证");
            decision(before, "CONFIRM_SUBMIT", 403);
            JsonNode changedResult = decision(changed, "CONFIRM_SUBMIT", 200);
            check(applications.findByRequestId(changedResult.path("relatedAfterSaleNo").asText()).orElseThrow().getReason().equals("行程取消"), "新卡保存客户明确修改的原因");

            fixture("ORD9103", "U1001"); fixture("ORD9104", "U1001");
            JsonNode oldOrder = chat("order-change", "ORD9103 我要退款，原因：买错了");
            JsonNode newOrder = chat("order-change", "ORD9104");
            check(newOrder.path("relatedOrderNo").asText().equals("ORD9104") && !newOrder.path("confirmRequired").asBoolean(), "更换订单不沿用旧原因");
            decision(oldOrder, "CONFIRM_SUBMIT", 403);
            decision(chat("order-change", "原因：重复下单"), "CONFIRM_SUBMIT", 200);
            check(count("ORD9103") == 0 && count("ORD9104") == 1, "只提交新选择的订单");

            fixture("ORD9105", "U1001");
            JsonNode drift = chat("shipped", "ORD9105 我要退款，原因：买错了");
            ship("ORD9105", "SHIPPED");
            decision(drift, "CONFIRM_SUBMIT", 400);
            check(count("ORD9105") == 0, "确认前发货会阻断提交");
            check(!chat("shipped-fresh", "ORD9105 我要退款，原因：买错了").path("confirmRequired").asBoolean(), "已发货订单不生成提交卡");
            fixture("ORD9106", "U1001"); ship("ORD9106", "DELIVERED");
            check(!chat("signed", "ORD9106 我要退款，原因：七天无理由").path("confirmRequired").asBoolean()
                && count("ORD9106") == 0, "已签收七天无理由不纳入首版提交");

            fixture("ORD9107", "U1001");
            JsonNode moneyDrift = chat("money-change", "ORD9107 我要退款，原因：买错了");
            JdbcTemplate jdbc = context.getBean(JdbcTemplate.class);
            jdbc.update("update order_header set total_amount=210 where order_no='ORD9107'");
            jdbc.update("update balance_transaction set amount=-210 where order_no='ORD9107' and type='PAYMENT'");
            decision(moneyDrift, "CONFIRM_SUBMIT", 409);
            check(count("ORD9107") == 0, "实付金额变化要求重新确认");
            check(chat("money-new", "ORD9107 我要退款，原因：买错了").path("confirmationSummary").asText().contains("210"), "新卡显示最新金额");

            fixture("ORD9108", "U1001");
            transactions.deleteAll(transactions.findByUserIdAndTypeOrderByCreatedAtDesc("U1001", "PAYMENT").stream()
                .filter(item -> "ORD9108".equals(item.getOrderNo())).toList());
            check(!chat("payment-missing", "ORD9108 我要退款，原因：买错了").path("confirmRequired").asBoolean()
                && count("ORD9108") == 0, "没有可信支付记录不能放行");
            fixture("ORD9109", "U1001");
            jdbc.update("update order_header set payment_status=null where order_no='ORD9109'");
            check(!chat("payment-state", "ORD9109 我要退款，原因：买错了").path("confirmRequired").asBoolean(), "支付状态缺失不能放行");

            fixture("ORD9110", "U1001");
            JsonNode expiry = chat("expired", "ORD9110 我要退款，原因：买错了");
            sessions.expire("expired"); decision(expiry, "CONFIRM_SUBMIT", 409);
            check(count("ORD9110") == 0, "过期卡不能提交");
            JsonNode agentFailure;
            context.getBean(StubAgent.class).failChat = true;
            try { agentFailure = chat("agent-fault", "ORD9110 我要退款，原因：买错了"); }
            finally { context.getBean(StubAgent.class).failChat = false; }
            check(agentFailure.path("fallback").asBoolean() && !agentFailure.path("confirmRequired").asBoolean(), "Agent 故障不虚报成功");

            fixture("ORD9111", "U1001");
            JsonNode databaseFault = chat("db-fault", "ORD9111 我要退款，原因：买错了");
            jdbc.execute("alter table after_sale_request add constraint gap_one_fault check (order_no <> 'ORD9111')");
            try {
                check(decision(databaseFault, "CONFIRM_SUBMIT", 200).path("fallback").asBoolean()
                    && count("ORD9111") == 0 && !Boolean.TRUE.equals(orders.findByOrderNo("ORD9111").orElseThrow().getHasAfterSaleRequest()),
                    "保存失败回滚申请和订单标记，不虚报成功");
            } finally { jdbc.execute("alter table after_sale_request drop constraint gap_one_fault"); }
            decision(databaseFault, "CONFIRM_SUBMIT", 200);
            check(count("ORD9111") == 1, "失败后重试只保存一笔申请");

            fixture("ORD9114", "U1001");
            JsonNode noOrder = chat("waiting-order", "我要退款");
            check(!noOrder.path("confirmRequired").asBoolean(), "缺订单号时不生成提交卡");
            chat("waiting-order", "原因：行程取消");
            JsonNode suppliedOrder = chat("waiting-order", "ORD9114");
            check(suppliedOrder.path("confirmRequired").asBoolean()
                && suppliedOrder.path("confirmationSummary").asText().contains("行程取消"), "补订单号续接退款，保留已明确的原因");

            fixture("ORD9115", "U1001");
            JsonNode readFault = chat("read-fault", "ORD9115 我要退款，原因：买错了");
            jdbc.execute("alter table balance_transaction rename to gap_one_payments_unavailable");
            try {
                check(decision(readFault, "CONFIRM_SUBMIT", 200).path("fallback").asBoolean()
                    && count("ORD9115") == 0, "最新支付事实查询失败不回旧快照放行");
            } finally { jdbc.execute("alter table gap_one_payments_unavailable rename to balance_transaction"); }
            decision(readFault, "CONFIRM_SUBMIT", 200);
            check(count("ORD9115") == 1, "事实查询恢复后仍需客户确认，重试只登记一次");

            fixture("ORD9112", "U1002");
            check(!chat("wrong-owner", "ORD9112 我要退款，原因：买错了").path("confirmRequired").asBoolean()
                && count("ORD9112") == 0, "不能提交别人订单");
            check(call(HttpClient.newHttpClient(), "POST", "/api/customer-service/chat", Map.of("message", "退款")).statusCode() == 401,
                "未登录客户不能访问提交链路");
            JsonNode noApproval = chat("no-approval", "ORD9110 我要退款，原因：买错了");
            decision(noApproval, "APPROVED", 400);
            check(call(customer, "POST", "/api/admin/after-sales/" + requestNo + "/approve", Map.of()).statusCode() == 403,
                "客户不能调用管理员批准入口");
            HttpClient otherCustomer = login("lisi", "123456");
            check(call(otherCustomer, "POST", "/api/customer-service/resume", decisionBody(noApproval, "CONFIRM_SUBMIT")).statusCode() == 403,
                "确认凭证不能跨客户使用");

            fixture("ORD9113", "U1001");
            JsonNode concurrentA = chat("parallel-a", "ORD9113 我要退款，原因：买错了");
            JsonNode concurrentB = chat("parallel-b", "ORD9113 我要退款，原因：买错了");
            ExecutorService executor = Executors.newFixedThreadPool(2);
            CountDownLatch start = new CountDownLatch(1);
            try {
                Future<JsonNode> a = executor.submit(() -> { start.await(); return decision(concurrentA, "CONFIRM_SUBMIT", 200); });
                Future<JsonNode> b = executor.submit(() -> { start.await(); return decision(concurrentB, "CONFIRM_SUBMIT", 200); });
                start.countDown();
                check(a.get(15, TimeUnit.SECONDS).path("relatedAfterSaleNo").equals(b.get(15, TimeUnit.SECONDS).path("relatedAfterSaleNo"))
                    && count("ORD9113") == 1, "跨会话并发提交由订单行锁防重");
            } finally { executor.shutdownNow(); }
            AfterSaleRequest needsInfo = applications.findByOrderNo("ORD9113").get(0);
            needsInfo.updateReview("NEED_MORE_INFO", null, "合成待补充状态", LocalDateTime.now()); applications.save(needsInfo);
            check(decision(chat("needs-info", "ORD9113 我要退款，原因：买错了"), "CONFIRM_SUBMIT", 200).path("answer").asText().contains("待补充材料")
                && count("ORD9113") == 1, "待补充材料仍复用现有申请");
            check(call(customer, "POST", "/api/shop/after-sales", Map.of("orderNo", "ORD9113", "type", "REFUND", "amount", 200, "reason", "重复申请")).statusCode() == 400
                && count("ORD9113") == 1, "商城其他创建入口共享防重规则");

            check(context.getBean(StubAgent.class).resumes == 0, "商城从未调用 Python 模拟审批恢复");
            check(context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(balance) == 0
                && transactions.findByUserIdAndTypeOrderByCreatedAtDesc("U1001", "REFUND").size() == refunds
                && context.getBean(ApprovalRecordRepository.class).count() == approvals, "全过程余额、退款流水和审核记录未变化");
            if (approval) approvalChecks();
            if (returns) returnChecks();
            System.out.println((returns ? "SEVEN_DAY_RETURN_RESULT " : approval ? "REFUND_COMPLETION_RESULT " : "GAP_ONE_RESULT ") + JSON.writeValueAsString(Map.of("passed", PASSED.size(), "checks", PASSED,
                "database", "isolated in-memory H2", "agent", "synthetic stub; no model calls", "actual_http_and_security", true)));
        } finally { context.close(); }
    }

    private static void approvalChecks() throws Exception {
        fixture("ORD9300", "U1001");
        String request = decision(chat("approve-full", "ORD9300 我要退款，原因：买错了"), "CONFIRM_SUBMIT", 200)
            .path("relatedAfterSaleNo").asText();
        BigDecimal before = context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance();
        data(call(admin, "POST", "/api/admin/after-sales/" + request + "/approve", Map.of("approvedAmount", 200)), 200);
        JsonNode order = data(call(customer, "GET", "/api/shop/orders/ORD9300", null), 200);
        check(order.path("orderStatus").asText().equals("REFUNDED") && order.path("paymentStatus").asText().equals("REFUNDED")
            && order.path("fulfillmentStatus").asText().equals("CANCELED") && !order.path("afterSaleAvailable").asBoolean(),
            "全额未发货退款通过后，订单/支付已退款、履约取消、不能再发起售后");
        check(context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance()
            .compareTo(before.add(new BigDecimal("200"))) == 0 && transactions.findByAfterSaleNo(request).orElseThrow().getAmount().intValue() == 200,
            "退款状态与余额/流水在同一次审批生效");
        JsonNode refunded = data(call(customer, "GET", "/api/shop/orders?status=REFUNDED", null), 200);
        check(refunded.toString().contains("ORD9300")
            && !data(call(customer, "GET", "/api/shop/orders?status=PENDING_SHIPMENT", null), 200).toString().contains("ORD9300"),
            "已退款筛选包含订单，待发货筛选不再包含订单");
        data(call(admin, "POST", "/api/admin/after-sales/" + request + "/approve", Map.of("approvedAmount", 200)), 409);
        check(context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance()
            .compareTo(before.add(new BigDecimal("200"))) == 0, "重复审批不能再次入账");

        fixture("ORD9301", "U1001");
        String partial = decision(chat("approve-partial", "ORD9301 我要退款，原因：买错了"), "CONFIRM_SUBMIT", 200)
            .path("relatedAfterSaleNo").asText();
        data(call(admin, "POST", "/api/admin/after-sales/" + partial + "/approve", Map.of("approvedAmount", 100)), 200);
        check(orders.findByOrderNo("ORD9301").orElseThrow().getPaymentStatus().equals("PAID"), "部分金额审批不能把整单标为已退款");

        fixture("ORD9302", "U1001");
        String compensation = data(call(customer, "POST", "/api/shop/after-sales", Map.of("orderNo", "ORD9302", "type", "COMPENSATION", "amount", 20,
            "reason", "隔离补偿检查")), 200).path("requestNo").asText();
        data(call(admin, "POST", "/api/admin/after-sales/" + compensation + "/approve", Map.of("approvedAmount", 20)), 200);
        check(orders.findByOrderNo("ORD9302").orElseThrow().getStatus().equals("PAID_PENDING_SHIPMENT"), "补偿审批不改变订单退款状态");

        fixture("ORD9303", "U1001");
        String reject = decision(chat("approve-reject", "ORD9303 我要退款，原因：买错了"), "CONFIRM_SUBMIT", 200)
            .path("relatedAfterSaleNo").asText();
        before = context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance();
        data(call(admin, "POST", "/api/admin/after-sales/" + reject + "/reject", Map.of("reviewNote", "隔离驳回")), 200);
        check(orders.findByOrderNo("ORD9303").orElseThrow().getStatus().equals("PAID_PENDING_SHIPMENT")
            && context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(before) == 0,
            "驳回保持订单和余额不变");
        fixture("ORD9304", "U1001");
        String needsInfo = decision(chat("approve-info", "ORD9304 我要退款，原因：买错了"), "CONFIRM_SUBMIT", 200)
            .path("relatedAfterSaleNo").asText();
        data(call(admin, "POST", "/api/admin/after-sales/" + needsInfo + "/need-more-info", Map.of("reviewNote", "隔离补充材料")), 200);
        check(orders.findByOrderNo("ORD9304").orElseThrow().getPaymentStatus().equals("PAID")
            && transactions.findByAfterSaleNo(needsInfo).isEmpty(), "待补充材料不改变订单、不退款");

        fixture("ORD9305", "U1001");
        String fault = decision(chat("approve-fault", "ORD9305 我要退款，原因：买错了"), "CONFIRM_SUBMIT", 200)
            .path("relatedAfterSaleNo").asText();
        before = context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance();
        long approvalCount = context.getBean(ApprovalRecordRepository.class).count();
        JdbcTemplate jdbc = context.getBean(JdbcTemplate.class);
        jdbc.execute("alter table order_header add constraint refund_status_fault check (order_no <> 'ORD9305' or status <> 'REFUNDED')");
        try { call(admin, "POST", "/api/admin/after-sales/" + fault + "/approve", Map.of("approvedAmount", 200)); }
        finally { jdbc.execute("alter table order_header drop constraint refund_status_fault"); }
        check(context.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(before) == 0
            && transactions.findByAfterSaleNo(fault).isEmpty() && applications.findByRequestId(fault).orElseThrow().getStatus().equals("PENDING_REVIEW")
            && context.getBean(ApprovalRecordRepository.class).count() == approvalCount, "订单状态保存失败时，退款、审批与余额全部回滚");

        fixture("ORD9306", "U1001");
        String concurrent = decision(chat("approve-parallel", "ORD9306 我要退款，原因：买错了"), "CONFIRM_SUBMIT", 200)
            .path("relatedAfterSaleNo").asText();
        ExecutorService executor = Executors.newFixedThreadPool(2);
        CountDownLatch start = new CountDownLatch(1);
        try {
            Callable<Integer> approve = () -> { start.await(); return call(admin, "POST", "/api/admin/after-sales/" + concurrent + "/approve", Map.of("approvedAmount", 200)).statusCode(); };
            Future<Integer> a = executor.submit(approve), b = executor.submit(approve);
            start.countDown();
            check(Set.of(a.get(15, TimeUnit.SECONDS), b.get(15, TimeUnit.SECONDS)).equals(Set.of(200, 409))
                && context.getBean(ApprovalRecordRepository.class).findByTargetNoOrderByCreatedAtDesc(concurrent).size() == 1,
                "并发批准只有一次成功，另一次返回已处理");
        } finally { executor.shutdownNow(); }
    }

    private static final Map<String, Object> DESTINATION = Map.of("returnAddress", "隔离测试地址，请勿寄送",
        "returnContact", "测试收货员", "returnPhone", "测试电话，仅隔离验证");
    private static final Map<String, Object> RECEIPT = Map.of("receivedAndInspected", true);
    private static final Map<String, Object> PARCEL = Map.of("carrier", "隔离测试快递", "trackingNo", "TEST-RETURN-ONLY");

    private static void returnFixture(String orderNo, int signedDays) {
        fixture(orderNo, "U1001");
        OrderEntity order = orders.findByOrderNo(orderNo).orElseThrow();
        order.updateAdminFields("DELIVERED", "DELIVERED", "TEST-ORIGINAL-LOGISTICS", "合成签收订单",
            LocalDateTime.now(context.getBean(java.time.Clock.class)).minusDays(signedDays));
        orders.save(order);
    }
    private static String submitReturn(String orderNo) throws Exception {
        return decision(chat("return-" + orderNo, orderNo + " 我要七天无理由退货"), "CONFIRM_SUBMIT", 200)
            .path("relatedAfterSaleNo").asText();
    }
    private static JsonNode returnAction(String requestNo, String action, Object body, int code) throws Exception {
        return data(call(admin, "POST", "/api/admin/after-sales/" + requestNo + "/" + action, body), code);
    }
    private static void returnChecks() throws Exception {
        JdbcTemplate jdbc = context.getBean(JdbcTemplate.class);
        BalanceAccountRepository balances = context.getBean(BalanceAccountRepository.class);
        BigDecimal before = balances.findByUserId("U1001").orElseThrow().getAvailableBalance();
        long oldRefunds = transactions.findByUserIdAndTypeOrderByCreatedAtDesc("U1001", "REFUND").size();
        returnFixture("ORD9402", 1);
        check(!chat("return-quality", "ORD9402 商品破损，我要退货").path("confirmRequired").asBoolean(),
            "其他售后原因不能因模型返回无理由而改写类型");
        JsonNode card = chat("return-card", "ORD9402 我要七天无理由退货");
        check(card.path("confirmRequired").asBoolean() && card.path("confirmationSummary").asText().contains("收到且验收通过")
            && count("ORD9402") == 0, "退货确认卡包含整单金额与退回要求，确认前不创建申请");
        check(chat("return-card", "审核流程是什么？").path("resumeToken").equals(card.path("resumeToken")), "退货追问保留同一凭证，不降级为未发货退款");
        String request = decision(card, "CONFIRM_SUBMIT", 200).path("relatedAfterSaleNo").asText();
        check(applications.findByRequestId(request).orElseThrow().getRequestType().equals("RETURN")
            && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(before) == 0,
            "客户提交只创建待审核RETURN，零入账");
        check(decision(card, "CONFIRM_SUBMIT", 200).path("relatedAfterSaleNo").asText().equals(request) && count("ORD9402") == 1,
            "退货重复确认复用同一申请");
        returnAction(request, "return-complete", RECEIPT, 409);
        returnAction(request, "approve", Map.of(), 400);
        check(applications.findByRequestId(request).orElseThrow().getStatus().equals("PENDING_REVIEW"), "无地址不能同意，未同意不能直接退款");
        returnAction(request, "approve", DESTINATION, 200);
        check(applications.findByRequestId(request).orElseThrow().getStatus().equals("WAITING_RETURN")
            && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(before) == 0
            && transactions.findByAfterSaleNo(request).isEmpty() && orders.findByOrderNo("ORD9402").orElseThrow().getPaymentStatus().equals("PAID"),
            "后台同意退货仅提供地址，订单仍已支付且零退款");
        check(data(call(customer, "GET", "/api/shop/after-sales/" + request, null), 200).path("returnInfo").path("address").asText().equals(DESTINATION.get("returnAddress")),
            "本人可以查看真实退回地址与意见");
        returnAction(request, "return-complete", RECEIPT, 409);
        check(call(customer, "POST", "/api/admin/after-sales/" + request + "/return-complete", RECEIPT).statusCode() == 403,
            "客户不能调用管理员最终退款接口");
        check(call(customer, "POST", "/api/admin/after-sales/" + request + "/approve", DESTINATION).statusCode() == 403,
            "客户不能调用管理员初审接口");
        HttpClient other = login("lisi", "123456");
        check(call(other, "POST", "/api/shop/after-sales/" + request + "/return-shipment", PARCEL).statusCode() == 404,
            "其他客户不能填写本人的退回物流");
        check(call(other, "GET", "/api/shop/after-sales/" + request, null).statusCode() == 404,
            "其他客户不能读取本人退货地址和申请");
        data(call(customer, "POST", "/api/shop/after-sales/" + request + "/return-shipment", Map.of("carrier", " ", "trackingNo", " ")), 400);
        data(call(customer, "POST", "/api/shop/after-sales/" + request + "/return-shipment", PARCEL), 200);
        data(call(customer, "POST", "/api/shop/after-sales/" + request + "/return-shipment", PARCEL), 200);
        data(call(customer, "POST", "/api/shop/after-sales/" + request + "/return-shipment", Map.of("carrier", "另一快递", "trackingNo", "CHANGED")), 409);
        check(applications.findByRequestId(request).orElseThrow().getStatus().equals("RETURN_IN_TRANSIT")
            && transactions.findByAfterSaleNo(request).isEmpty(), "有效物流推进待验收，重复提交幂等，仍不退款");
        returnAction(request, "return-complete", Map.of(), 400);
        returnAction(request, "return-complete", Map.of("receivedAndInspected", true, "approvedAmount", 999), 400);
        check(transactions.findByAfterSaleNo(request).isEmpty(), "未确认实际收货验收或篡改金额不能退款");
        check(call(customer, "POST", "/api/shop/after-sales", Map.of("orderNo", "ORD9402", "type", "REFUND", "amount", 200, "reason", "绕过退货")).statusCode() == 409,
            "退货处理中不能另开REFUND绕过两阶段审核");
        String legacyDraft = data(call(customer, "POST", "/api/after-sale/requests", Map.of("orderNo", "ORD9402", "requestType", "REFUND", "reason", "合成旧草稿入口")), 200)
            .path("requestId").asText();
        returnAction(legacyDraft, "approve", Map.of("approvedAmount", 200), 409);
        check(transactions.findByAfterSaleNo(legacyDraft).isEmpty(), "旧草稿REFUND入口也不能绕过正在处理的退货直接入账");
        returnAction(request, "return-inspection-failed", Map.of(), 400);
        returnAction(request, "return-inspection-failed", Map.of("reviewNote", "合成验收异常：配件不全，待人工核验"), 200);
        check(transactions.findByAfterSaleNo(request).isEmpty() && data(call(customer, "GET", "/api/shop/after-sales/" + request, null), 200)
            .path("handlingNote").asText().contains("配件不全"), "验收失败原因必填且本人可见，零入账");
        returnAction(request, "return-complete", RECEIPT, 200);
        JsonNode result = data(call(customer, "GET", "/api/shop/orders/ORD9402", null), 200);
        check(result.path("orderStatus").asText().equals("REFUNDED") && result.path("paymentStatus").asText().equals("REFUNDED")
            && result.path("fulfillmentStatus").asText().equals("RETURNED") && !result.path("afterSaleAvailable").asBoolean()
            && result.path("signedAt").isTextual() && result.path("logisticsNo").asText().equals("TEST-ORIGINAL-LOGISTICS"),
            "人工核验通过后订单/支付已退款，履约已退回，保留历史签收和物流");
        check(balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(before.add(new BigDecimal("200"))) == 0
            && transactions.findByUserIdAndTypeOrderByCreatedAtDesc("U1001", "REFUND").size() == oldRefunds + 1
            && context.getBean(ApprovalRecordRepository.class).findByTargetNoOrderByCreatedAtDesc(request).size() == 3,
            "最终仅一次实付200元入账，初审/异常/退款操作完整留痕");
        returnAction(request, "return-complete", RECEIPT, 409);
        returnAction(request, "approve", DESTINATION, 409);
        check(call(admin, "PATCH", "/api/admin/orders/ORD9402", Map.of("orderStatus", "DELIVERED", "fulfillmentStatus", "DELIVERED")).statusCode() == 409,
            "已退款订单不能后台改回已签收");

        for (int days : new int[]{0, 7, 8, -1}) {
            String number = "ORD94" + (days == -1 ? "13" : "0" + (days == 0 ? 4 : days == 7 ? 5 : 6));
            returnFixture(number, days);
            JsonNode draft = chat("boundary-" + days, number + " 我要七天无理由退货");
            check(draft.path("confirmRequired").asBoolean() == (days == 0 || days == 7), "签收日边界 " + days + " 天严格由业务后端判断");
        }
        String seventhDay = submitReturn("ORD9405");
        check(applications.findByRequestId(seventhDay).orElseThrow().getCreatedAt().toLocalTime().equals(java.time.LocalTime.of(23, 59, 59)),
            "上海第七天23:59:59仍允许提交，使用隔离固定业务时钟");
        returnFixture("ORD9407", 1);
        transactions.deleteAll(transactions.findByUserIdAndTypeOrderByCreatedAtDesc("U1001", "PAYMENT").stream().filter(t -> "ORD9407".equals(t.getOrderNo())).toList());
        check(!chat("return-no-pay", "ORD9407 我要七天无理由退货").path("confirmRequired").asBoolean(), "退货缺实际支付不能准备申请");
        returnFixture("ORD9408", 1);
        jdbc.update("update order_item set returnable_at_purchase=null where order_id=(select id from order_header where order_no='ORD9408')");
        check(!chat("return-unknown", "ORD9408 我要七天无理由退货").path("confirmRequired").asBoolean(), "退货缺购买时可退记录不能放行");
        returnFixture("ORD9409", 1);
        jdbc.update("update order_item set returnable_at_purchase=false where order_id=(select id from order_header where order_no='ORD9409')");
        check(!chat("return-false", "ORD9409 我要七天无理由退货").path("confirmRequired").asBoolean(), "购买时不支持退货不能放行");
        returnFixture("ORD9410", 1);
        check(call(customer, "POST", "/api/shop/after-sales", Map.of("orderNo", "ORD9410", "type", "RETURN", "amount", 1, "reason", "任意文本")).statusCode() == 409,
            "通用创建入口不能篡改RETURN实付金额");
        check(call(customer, "POST", "/api/shop/after-sales", Map.of("orderNo", "ORD9410", "type", "REFUND", "amount", 200, "reason", "绕过退货")).statusCode() == 400,
            "已签收不能通过通用REFUND直接申请退款");
        String rejected = submitReturn("ORD9410");
        returnAction(rejected, "reject", Map.of(), 400);
        returnAction(rejected, "reject", Map.of("reviewNote", "隔离驳回，请人工核验"), 200);
        check(transactions.findByAfterSaleNo(rejected).isEmpty(), "驳回理由必填且不退款");
        returnFixture("ORD9411", 1);
        String late = submitReturn("ORD9411");
        LocalDateTime oldSigned = LocalDateTime.now(java.time.ZoneId.of("Asia/Shanghai")).minusDays(9);
        jdbc.update("update order_header set delivered_at=? where order_no='ORD9411'", oldSigned);
        jdbc.update("update after_sale_request set return_signed_at=?, created_at=? where request_id=?", oldSigned, oldSigned.plusDays(6), late);
        returnAction(late, "approve", DESTINATION, 200);
        data(call(customer, "POST", "/api/shop/after-sales/" + late + "/return-shipment", PARCEL), 200);
        returnAction(late, "return-complete", RECEIPT, 200);
        check(applications.findByRequestId(late).orElseThrow().getStatus().equals("REFUNDED"), "合成及时提交晚审核仍可退货退款，七天以提交为准");

        returnFixture("ORD9412", 1);
        String fault = submitReturn("ORD9412");
        returnAction(fault, "approve", DESTINATION, 200);
        data(call(customer, "POST", "/api/shop/after-sales/" + fault + "/return-shipment", PARCEL), 200);
        BigDecimal faultBefore = balances.findByUserId("U1001").orElseThrow().getAvailableBalance();
        long recordsBefore = context.getBean(ApprovalRecordRepository.class).count();
        jdbc.execute("alter table order_header add constraint return_refund_fault check (order_no <> 'ORD9412' or status <> 'REFUNDED')");
        try { call(admin, "POST", "/api/admin/after-sales/" + fault + "/return-complete", RECEIPT); }
        finally { jdbc.execute("alter table order_header drop constraint return_refund_fault"); }
        check(balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(faultBefore) == 0
            && transactions.findByAfterSaleNo(fault).isEmpty() && applications.findByRequestId(fault).orElseThrow().getStatus().equals("RETURN_IN_TRANSIT")
            && context.getBean(ApprovalRecordRepository.class).count() == recordsBefore, "退货订单写入失败，余额/流水/审批/售后整体回滚");
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch start = new CountDownLatch(1);
        try {
            Callable<Integer> complete = () -> { start.await(); return call(admin, "POST", "/api/admin/after-sales/" + fault + "/return-complete", RECEIPT).statusCode(); };
            Future<Integer> a=pool.submit(complete), b=pool.submit(complete); start.countDown();
            int first = a.get(15, TimeUnit.SECONDS), second = b.get(15, TimeUnit.SECONDS);
            check(Set.of(first, second).equals(Set.of(200, 409))
                && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(faultBefore.add(new BigDecimal("200"))) == 0,
                "并发最终确认只一次成功与一次退款；HTTP=" + first + "," + second);
        } finally { pool.shutdownNow(); }
        returnFixture("ORD9414", 1); returnFixture("ORD9415", 1);
        String ra=submitReturn("ORD9414"), rb=submitReturn("ORD9415");
        for (String no : List.of(ra,rb)) { returnAction(no,"approve",DESTINATION,200); data(call(customer,"POST","/api/shop/after-sales/"+no+"/return-shipment",PARCEL),200); }
        BigDecimal parallelBefore=balances.findByUserId("U1001").orElseThrow().getAvailableBalance();
        pool=Executors.newFixedThreadPool(2);
        CountDownLatch parallel=new CountDownLatch(1);
        try {
            Future<Integer> a=pool.submit(() -> { parallel.await(); return call(admin,"POST","/api/admin/after-sales/"+ra+"/return-complete",RECEIPT).statusCode(); });
            Future<Integer> b=pool.submit(() -> { parallel.await(); return call(admin,"POST","/api/admin/after-sales/"+rb+"/return-complete",RECEIPT).statusCode(); });
            parallel.countDown();
            check(a.get(15,TimeUnit.SECONDS)==200 && b.get(15,TimeUnit.SECONDS)==200
                && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(parallelBefore.add(new BigDecimal("400")))==0,
                "两订单并发退款均正确累加，不覆盖账户余额");
        } finally { pool.shutdownNow(); }

        returnFixture("ORD9416", 1);
        JsonNode noOrderQuality = chat("return-quality-no-order", "质量问题，我要退货");
        check(!noOrderQuality.path("confirmRequired").asBoolean()
            && sessions.find("return-quality-no-order").orElseThrow().getActionType() == null,
            "其他售后缺订单号时也不登记为无理由草稿");
        JsonNode cancelledReturn = chat("return-cancel", "ORD9416 我要退货");
        decision(cancelledReturn, "CANCEL_SUBMIT", 200);
        decision(cancelledReturn, "CONFIRM_SUBMIT", 409);
        check(count("ORD9416") == 0, "退货取消后旧卡失效且没有申请");
        JsonNode changedType = chat("return-type-change", "ORD9416 我要退货");
        chat("return-type-change", "原因：商品破损");
        decision(changedType, "CONFIRM_SUBMIT", 403);
        check(count("ORD9416") == 0, "待确认无理由改为质量问题后旧卡失效，不代改售后类型");
        chat("return-order-required", "我要七天无理由退货");
        JsonNode suppliedOrder = chat("return-order-required", "ORD9416");
        check(suppliedOrder.path("confirmationSummary").asText().contains("整单退货退款"), "退货补充订单保持RETURN类型");
        returnFixture("ORD9417", 1);
        JsonNode switchedOrder = chat("return-order-required", "改成 ORD9417");
        decision(suppliedOrder, "CONFIRM_SUBMIT", 403);
        check(decision(switchedOrder, "CONFIRM_SUBMIT", 200).path("relatedOrderNo").asText().equals("ORD9417")
            && count("ORD9416") == 0, "退货换单使旧卡失效，新卡提交新订单");

        returnFixture("ORD9418", 1);
        JsonNode firstCard = chat("return-parallel-a", "ORD9418 我要退货"), secondCard = chat("return-parallel-b", "ORD9418 我要退货");
        pool = Executors.newFixedThreadPool(2);
        CountDownLatch submitStart = new CountDownLatch(1);
        try {
            Future<JsonNode> a = pool.submit(() -> { submitStart.await(); return decision(firstCard, "CONFIRM_SUBMIT", 200); });
            Future<JsonNode> b = pool.submit(() -> { submitStart.await(); return decision(secondCard, "CONFIRM_SUBMIT", 200); });
            submitStart.countDown();
            check(a.get(15, TimeUnit.SECONDS).path("relatedAfterSaleNo").equals(b.get(15, TimeUnit.SECONDS).path("relatedAfterSaleNo"))
                && count("ORD9418") == 1, "退货跨会话并发提交只有一笔申请");
        } finally { pool.shutdownNow(); }
        String parcelRequest = applications.findByOrderNo("ORD9418").get(0).getRequestId();
        returnAction(parcelRequest, "approve", DESTINATION, 200);
        pool = Executors.newFixedThreadPool(2);
        CountDownLatch parcelStart = new CountDownLatch(1);
        try {
            Future<Integer> a = pool.submit(() -> { parcelStart.await(); return call(customer, "POST", "/api/shop/after-sales/" + parcelRequest + "/return-shipment", PARCEL).statusCode(); });
            Future<Integer> b = pool.submit(() -> { parcelStart.await(); return call(customer, "POST", "/api/shop/after-sales/" + parcelRequest + "/return-shipment", Map.of("carrier", "另一快递", "trackingNo", "OTHER")).statusCode(); });
            parcelStart.countDown();
            check(Set.of(a.get(15, TimeUnit.SECONDS), b.get(15, TimeUnit.SECONDS)).equals(Set.of(200, 409)),
                "不同物流并发提交只有一份被采纳，不覆盖已提交物流");
        } finally { pool.shutdownNow(); }

        returnFixture("ORD9419", 1);
        jdbc.update("update order_header set delivered_at=null where order_no='ORD9419'");
        check(!chat("return-missing-signed", "ORD9419 我要退货").path("confirmRequired").asBoolean(), "缺签收时间不能准备退货");
        returnFixture("ORD9420", 1);
        ship("ORD9420", "SHIPPED");
        check(!chat("return-in-transit", "ORD9420 我要退货").path("confirmRequired").asBoolean(), "运输中仅引导人工拦截，不准备签收退货");
        returnFixture("ORD9421", 1);
        String shippedRefund = data(call(customer, "POST", "/api/after-sale/requests", Map.of("orderNo", "ORD9421", "requestType", "REFUND", "reason", "合成签收直退草稿")), 200).path("requestId").asText();
        returnAction(shippedRefund, "approve", Map.of("approvedAmount", 200), 409);
        check(transactions.findByAfterSaleNo(shippedRefund).isEmpty(), "签收旧REFUND草稿不能绕过收货验收直接退款");
        String oldReturn = data(call(customer, "POST", "/api/after-sale/requests", Map.of("orderNo", "ORD9421", "requestType", "RETURN", "reason", "合成旧退货")), 200).path("requestId").asText();
        jdbc.update("update after_sale_request set status='PENDING_REVIEW', amount=200 where request_id=?", oldReturn);
        returnAction(oldReturn, "approve", DESTINATION, 409);
        jdbc.update("update after_sale_request set status='APPROVED' where request_id=?", oldReturn);
        returnAction(oldReturn, "return-complete", RECEIPT, 409);
        check(transactions.findByAfterSaleNo(oldReturn).isEmpty(), "旧退货缺冻结资格不能初审，旧APPROVED不能直接退款");
        returnFixture("ORD9422", 1);
        String inconsistent = submitReturn("ORD9422");
        transactions.save(new BalanceTransaction("TEST-PRIOR-REFUND", "U1001", "ORD9422", "TEST-OTHER-AS", "REFUND", new BigDecimal("200"),
            BigDecimal.ZERO, new BigDecimal("200"), "合成矛盾流水，不改余额", LocalDateTime.now()));
        returnAction(inconsistent, "approve", DESTINATION, 409);
        check(applications.findByRequestId(inconsistent).orElseThrow().getStatus().equals("PENDING_REVIEW"), "已有退款流水与状态矛盾时拒绝推进，不补写资金");
        String historicalOrder = "SO20260712090000010-a1000010";
        long itemId = jdbc.queryForObject("select min(i.id) from order_item i join order_header o on i.order_id=o.id where o.order_no=?", Long.class, historicalOrder);
        jdbc.update("update order_item set returnable_at_purchase=false, product_name='合成历史购买快照' where id=?", itemId);
        BigDecimal seedBefore = balances.findByUserId("U1001").orElseThrow().getAvailableBalance();
        context.getBean(com.teachdemo.ecommerce.config.DataInitializer.class).run();
        check(jdbc.queryForObject("select count(*) from order_item where id=? and returnable_at_purchase=false and product_name='合成历史购买快照'", Long.class, itemId) == 1
            && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(seedBefore) == 0,
            "再次启动初始化保留购买时属性和商品快照，不重建历史订单明细或改余额");
        check(context.getBean(StubAgent.class).resumes == 0, "完整退货始终不调用Python模拟审批");
    }

    private static void fixture(String orderNo, String userId) {
        LocalDateTime now = LocalDateTime.now();
        BigDecimal amount = new BigDecimal("200.00");
        OrderEntity order = new OrderEntity(orderNo, "合成客户", "PAID_PENDING_SHIPMENT", "PAID", amount, now,
            userId, false, true, "隔离退款验收");
        order.markPaid(now); orders.save(order);
        context.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "合成露营椅", 1, amount, true));
        transactions.save(new BalanceTransaction("TEST-PAY-" + orderNo, userId, orderNo, null, "PAYMENT", amount.negate(),
            new BigDecimal("1000"), new BigDecimal("800"), "合成支付事实，不修改余额", now));
    }
    private static void ship(String orderNo, String status) {
        OrderEntity order = orders.findByOrderNo(orderNo).orElseThrow();
        order.updateAdminFields(status, status, "TEST-LOGISTICS", "合成状态变化", LocalDateTime.now()); orders.save(order);
    }
    private static long count(String orderNo) { return applications.findByOrderNo(orderNo).size(); }
    private static void check(boolean success, String name) {
        if (!success) throw new AssertionError(name);
        PASSED.add(name);
    }
    private static HttpClient login(String name, String password) throws Exception {
        HttpClient client = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
        data(call(client, "POST", "/api/auth/login", Map.of("username", name, "password", password)), 200);
        return client;
    }
    private static HttpResponse<String> call(HttpClient client, String method, String path, Object body) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(base + path)).header("Content-Type", "application/json");
        request.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
        return client.send(request.build(), HttpResponse.BodyHandlers.ofString());
    }
    private static JsonNode data(HttpResponse<String> response, int status) throws Exception {
        if (response.statusCode() != status) throw new AssertionError("HTTP " + status + " expected; got " + response.statusCode() + ": " + response.body());
        return JSON.readTree(response.body()).path("data");
    }
    private static JsonNode chat(String session, String message) throws Exception {
        return data(call(customer, "POST", "/api/customer-service/chat", Map.of("sessionId", session, "message", message)), 200);
    }
    private static Map<String, Object> decisionBody(JsonNode card, String decision) {
        return Map.of("sessionId", card.path("sessionId").asText(), "resumeToken", card.path("resumeToken").asText(),
            "relatedOrderNo", card.path("relatedOrderNo").asText(), "decision", decision);
    }
    private static JsonNode decision(JsonNode card, String decision, int status) throws Exception {
        return data(call(customer, "POST", "/api/customer-service/resume", decisionBody(card, decision)), status);
    }
}
