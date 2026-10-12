package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.teachdemo.ecommerce.account.BalanceAccountRepository;
import com.teachdemo.ecommerce.account.BalanceTransaction;
import com.teachdemo.ecommerce.account.BalanceTransactionRepository;
import com.teachdemo.ecommerce.aftersale.AfterSaleRequestRepository;
import com.teachdemo.ecommerce.order.OrderEntity;
import com.teachdemo.ecommerce.order.OrderItem;
import com.teachdemo.ecommerce.order.OrderItemRepository;
import com.teachdemo.ecommerce.order.OrderRepository;
import java.math.BigDecimal;
import java.net.CookieManager;
import java.net.URI;
import java.net.http.*;
import java.nio.file.*;
import java.time.LocalDateTime;
import java.util.*;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;

/** 真实 Jev 与 DeepSeek。下限在启动参数里固定。未授权改草稿即停止。 */
public class RealModelCancelCheck {
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient CLIENT = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
    static final List<Object> RESULTS = new ArrayList<>();
    static Path out;
    static boolean stopped;

    static JsonNode post(String path, Object body) throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18082" + path))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        var response = CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
        JsonNode parsed = response.body().isBlank() ? JSON.createObjectNode() : JSON.readTree(response.body());
        if (response.statusCode() != 200) throw new AssertionError(response.statusCode() + " " + response.body());
        return parsed.path("data");
    }

    static JsonNode chat(String session, String message) throws Exception {
        var body = new HashMap<String, Object>();
        body.put("sessionId", session);
        body.put("message", message);
        body.put("pageContext", Map.of("type", "HOME"));
        return post("/api/customer-service/chat", body);
    }

    static int resume(JsonNode card, String decision) throws Exception {
        var body = new HashMap<String, Object>();
        body.put("sessionId", card.path("sessionId").asText());
        body.put("resumeToken", card.path("resumeToken").asText());
        body.put("relatedOrderNo", card.path("relatedOrderNo").asText());
        body.put("decision", decision);
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18082/api/customer-service/resume"))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        var response = CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
        return response.statusCode();
    }

    static void note(boolean passed, String label, String detail) {
        var row = new LinkedHashMap<String, Object>();
        row.put("check", label);
        row.put("passed", passed);
        row.put("detail", detail);
        RESULTS.add(row);
        System.out.println((passed ? "PASS " : "FAIL ") + label + " " + detail);
    }

    static CustomerServiceSession session(ServletWebServerApplicationContext ctx, String id) {
        return ctx.getBean(CustomerServiceSessionStore.class).find(id).orElseThrow();
    }

    static void fixture(ServletWebServerApplicationContext ctx, String no, int minute) {
        var now = LocalDateTime.now().plusMinutes(minute);
        var amount = new BigDecimal("200.00");
        var order = new OrderEntity(no, "隔离验收", "PAID_PENDING_SHIPMENT", "PAID", amount, now, "U1001", false, true, "真实模型隔离");
        order.markPaid(now);
        ctx.getBean(OrderRepository.class).save(order);
        ctx.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "隔离露营椅", 1, amount, true));
        ctx.getBean(BalanceTransactionRepository.class).save(new BalanceTransaction("REAL-PAY-" + no, "U1001", no, null,
            "PAYMENT", amount.negate(), new BigDecimal("1000"), new BigDecimal("800"), "隔离合成支付", now));
    }

    static JsonNode card(String session) throws Exception {
        var created = chat(session, "ORD9801 我要退款，原因：买错了。");
        if (!created.path("confirmRequired").asBoolean()) throw new AssertionError("没有确认卡: " + created);
        return created;
    }

    static void mustNotCancel(ServletWebServerApplicationContext ctx, String session, String message, boolean expectAsk) throws Exception {
        var before = card(session);
        String token = before.path("resumeToken").asText();
        var reply = chat(session, message);
        var state = session(ctx, session);
        boolean cancelled = "CANCELLED".equals(state.getPendingAction()) || state.isHandled();
        String detail = "pending=" + state.getPendingAction() + " answer=" + reply.path("answer").asText();
        if (cancelled) {
            note(false, message, "未授权却改变了草稿。" + detail);
            stopped = true;
            return;
        }
        boolean tokenKept = token.equals(state.getResumeToken());
        boolean asked = reply.path("answer").asText().contains("订单") || reply.path("answer").asText().contains("退款");
        note(tokenKept && (!expectAsk || asked), message, detail + " tokenKept=" + tokenKept + " asked=" + asked);
    }

    public static void main(String[] args) throws Exception {
        out = Path.of(args[0]);
        Files.createDirectories(out);
        try (var ctx = (ServletWebServerApplicationContext) new SpringApplicationBuilder(EcommerceBackendApplication.class).run(
            "--server.port=18082", "--spring.datasource.url=jdbc:h2:mem:cancel-real;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa", "--spring.datasource.password=",
            "--spring.jpa.hibernate.ddl-auto=create-drop", "--spring.jpa.show-sql=false", "--logging.level.root=WARN",
            "--spring.main.banner-mode=off", "--agent.service.base-url=http://127.0.0.1:18016",
            "--agent.service.read-timeout-ms=120000", "--agent.service.connect-timeout-ms=5000",
            "--agent.service.auth-token=course-debug-agent-service", "--customer-service.runtime-context.default-order-limit=2")) {
            fixture(ctx, "ORD9801", 2);
            fixture(ctx, "ORD9802", 1);
            var sales = ctx.getBean(AfterSaleRequestRepository.class);
            long initialSales = sales.count();
            post("/api/auth/login", Map.of("username", "zhangsan", "password", "123456"));
            String run = "real-" + System.currentTimeMillis();
            mustNotCancel(ctx, run + "-keep", "假如商品降价我可能留下，不过现在继续退。", false);
            if (!stopped) mustNotCancel(ctx, run + "-object", "取消订单吧。", true);
            if (!stopped) mustNotCancel(ctx, run + "-purchase", "把购买订单取消掉。", false);
            if (!stopped) mustNotCancel(ctx, run + "-other", "撤销另一笔售后，这笔退款继续办。", false);
            if (!stopped) {
                String natural = run + "-natural";
                var before = card(natural);
                var reply = chat(natural, "我想了想，还是不退了");
                var state = session(ctx, natural);
                if ("CANCELLED".equals(state.getPendingAction()) && state.getResumeToken() == null) {
                    int oldCard = resume(before, "CONFIRM_SUBMIT");
                    var follow = chat(natural, "查一下物流");
                    boolean ok = oldCard == 409 && !follow.path("confirmRequired").asBoolean();
                    note(ok, "我想了想，还是不退了", "已取消。旧卡=" + oldCard + " 下一句确认卡=" + follow.path("confirmRequired").asBoolean()
                        + " answer=" + reply.path("answer").asText());
                } else {
                    note(false, "我想了想，还是不退了", "没有取消。pending=" + state.getPendingAction() + " answer=" + reply.path("answer").asText());
                }
            }
            boolean passed = !stopped && RESULTS.stream().allMatch(item -> Boolean.TRUE.equals(((Map<?, ?>) item).get("passed")));
            var result = new LinkedHashMap<String, Object>();
            result.put("passed", passed);
            result.put("stopped_on_unauthorized_cancel", stopped);
            result.put("score_min", "0.9");
            result.put("mode", "isolated_h2_real_models");
            result.put("checks", RESULTS);
            result.put("sales_unchanged", sales.count() == initialSales);
            Files.writeString(out.resolve("real-result.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(result));
            if (stopped) throw new AssertionError("未授权取消，已停止");
            if (!passed) throw new AssertionError("真实模型验收未全部通过，见 real-result.json");
        }
    }
}
