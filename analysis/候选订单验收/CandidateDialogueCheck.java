package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.teachdemo.ecommerce.account.*;
import com.teachdemo.ecommerce.aftersale.*;
import com.teachdemo.ecommerce.order.*;
import java.math.BigDecimal;
import java.net.CookieManager;
import java.net.URI;
import java.net.http.*;
import java.nio.file.*;
import java.time.LocalDateTime;
import java.util.*;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;

/** 隔离 H2 + 真实 Python/模型；停在确认卡，不提交申请。 */
public class CandidateDialogueCheck {
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient CLIENT = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
    static final List<Object> TURNS = new ArrayList<>();
    static JsonNode call(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:8081" + path)).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        var res = CLIENT.send(req, HttpResponse.BodyHandlers.ofString());
        if (res.statusCode() != 200) throw new AssertionError(res.statusCode() + ": " + res.body());
        return JSON.readTree(res.body()).path("data");
    }
    static JsonNode chat(String session, String message, String pageOrder) throws Exception {
        var body = new HashMap<String,Object>();
        body.put("sessionId", session); body.put("message", message);
        body.put("pageContext", pageOrder == null ? Map.of("type", "HOME") : Map.of("type", "ORDER_DETAIL", "orderNo", pageOrder));
        var response = call("/api/customer-service/chat", body);
        TURNS.add(Map.of("request", body, "response", response));
        Files.writeString(Path.of("/check/dialogue.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(TURNS));
        System.out.println("TURN " + JSON.writeValueAsString(TURNS.get(TURNS.size()-1)));
        return response;
    }
    static void check(boolean ok, String message) { if (!ok) throw new AssertionError(message); }
    static void fixture(ServletWebServerApplicationContext ctx, String no, int minute) {
        var now = LocalDateTime.now().plusMinutes(minute);
        var amount = new BigDecimal("200.00");
        var order = new OrderEntity(no, "隔离验收", "PAID_PENDING_SHIPMENT", "PAID", amount, now, "U1001", false, true, "M06隔离");
        order.markPaid(now); ctx.getBean(OrderRepository.class).save(order);
        ctx.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "M06露营椅", 1, amount, true));
        ctx.getBean(BalanceTransactionRepository.class).save(new BalanceTransaction("M06-PAY-"+no, "U1001", no, null,
            "PAYMENT", amount.negate(), new BigDecimal("1000"), new BigDecimal("800"), "隔离合成支付", now));
    }
    public static void main(String[] args) throws Exception {
        try (var ctx = (ServletWebServerApplicationContext) new SpringApplicationBuilder(EcommerceBackendApplication.class).run(
            "--server.port=8081", "--spring.datasource.url=jdbc:h2:mem:m06;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa", "--spring.datasource.password=",
            "--spring.jpa.hibernate.ddl-auto=create-drop", "--spring.jpa.show-sql=false", "--logging.level.root=WARN",
            "--spring.main.banner-mode=off", "--agent.service.base-url=http://host.docker.internal:8012",
            "--agent.service.auth-token=course-debug-agent-service", "--customer-service.runtime-context.default-order-limit=2")) {
            fixture(ctx, "ORD9801", 2); fixture(ctx, "ORD9802", 1);
            long sales = ctx.getBean(AfterSaleRequestRepository.class).count();
            long transactions = ctx.getBean(BalanceTransactionRepository.class).count();
            var balance = ctx.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance();
            call("/api/auth/login", Map.of("username", "zhangsan", "password", "123456"));
            String run = "m06-mall-" + System.currentTimeMillis();
            var shown = chat(run, "帮我退款。", null);
            String answer = shown.path("answer").asText();
            check(answer.contains("ORD9801") && answer.indexOf("ORD9801") < answer.indexOf("ORD9802"), "按序展示两笔本人订单");
            var selected = chat(run, "第一笔。", null);
            check(selected.path("relatedOrderNo").asText().equals("ORD9801"), "第一笔须选择ORD9801");
            var card = chat(run, "原因：买错了。", null);
            check(card.path("confirmRequired").asBoolean() && card.path("relatedOrderNo").asText().equals("ORD9801"), "补原因后生成ORD9801确认卡");
            chat(run+"-second", "帮我退款，原因：买错了。", null);
            var second = chat(run+"-second", "第二笔。", null);
            check(second.path("confirmRequired").asBoolean() && second.path("relatedOrderNo").asText().equals("ORD9802"), "第二笔确认卡对象正确");
            chat(run+"-page", "帮我退款，原因：买错了。", null);
            var page = chat(run+"-page", "第一笔。", "ORD9802");
            check(page.path("confirmRequired").asBoolean() && page.path("relatedOrderNo").asText().equals("ORD9801"), "旧页面不能覆盖候选选择");
            chat(run+"-invalid", "帮我退款。", null);
            var invalid = chat(run+"-invalid", "第三笔。", null);
            check(!invalid.path("confirmRequired").asBoolean() && invalid.path("relatedOrderNo").asText("").isEmpty(), "越界不能准备确认卡");
            var missing = chat(run+"-fresh", "第一笔。", null);
            check(!missing.path("confirmRequired").asBoolean(), "新会话无候选不得准备申请");
            chat(run+"-switch", "帮我退款。", null);
            var switched = chat(run+"-switch", "先不要退款，查 ORD9802 的物流。", null);
            check(!switched.path("confirmRequired").asBoolean() && switched.path("answer").asText().contains("ORD9802"), "改问物流不准备退款确认卡");
            check(ctx.getBean(AfterSaleRequestRepository.class).count() == sales, "未确认无新增售后");
            check(ctx.getBean(BalanceTransactionRepository.class).count() == transactions, "资金流水不变");
            check(ctx.getBean(BalanceAccountRepository.class).findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(balance) == 0, "余额不变");
            Files.writeString(Path.of("/check/result.json"), JSON.writeValueAsString(Map.of("passed", true, "turns", TURNS.size(), "after_sales_unchanged", true, "transactions_unchanged", true, "balance_unchanged", true)));
            System.out.println("M06_MALL_PASSED");
        }
    }
}
