package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.teachdemo.ecommerce.account.BalanceTransaction;
import com.teachdemo.ecommerce.account.BalanceTransactionRepository;
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

/** 决定分不足时的回复。只重跑两句，下限仍是启动环境里的 0.9。 */
public class RealReplyCheck {
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient CLIENT = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
    static final List<Object> RESULTS = new ArrayList<>();

    static JsonNode post(String path, Object body) throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18085" + path))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        var response = CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) throw new AssertionError(response.statusCode() + " " + response.body());
        return JSON.readTree(response.body()).path("data");
    }

    static JsonNode chat(String session, String message) throws Exception {
        var body = new HashMap<String, Object>();
        body.put("sessionId", session);
        body.put("message", message);
        body.put("pageContext", Map.of("type", "HOME"));
        return post("/api/customer-service/chat", body);
    }

    static void note(String status, String label, String detail) {
        var row = new LinkedHashMap<String, Object>();
        row.put("check", label);
        row.put("status", status);
        row.put("detail", detail);
        RESULTS.add(row);
        System.out.println(status + " " + label + " " + detail);
    }

    static boolean hasCancelButton(JsonNode reply) {
        for (JsonNode action : reply.path("actions")) {
            if ("CANCEL_SUBMIT".equals(action.path("code").asText()) && "取消本次申请".equals(action.path("label").asText())) {
                return true;
            }
        }
        return false;
    }

    static void fixture(ServletWebServerApplicationContext ctx, String no, int minute) {
        var now = LocalDateTime.now().plusMinutes(minute);
        var amount = new BigDecimal("200.00");
        var order = new OrderEntity(no, "隔离验收", "PAID_PENDING_SHIPMENT", "PAID", amount, now, "U1001", false, true, "回复隔离");
        order.markPaid(now);
        ctx.getBean(OrderRepository.class).save(order);
        ctx.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "隔离露营椅", 1, amount, true));
        ctx.getBean(BalanceTransactionRepository.class).save(new BalanceTransaction("REPLY-PAY-" + no, "U1001", no, null,
            "PAYMENT", amount.negate(), new BigDecimal("1000"), new BigDecimal("800"), "隔离合成支付", now));
    }

    public static void main(String[] args) throws Exception {
        Path out = Path.of(args[0]);
        try (var ctx = (ServletWebServerApplicationContext) new SpringApplicationBuilder(EcommerceBackendApplication.class).run(
            "--server.port=18085", "--spring.datasource.url=jdbc:h2:mem:cancel-reply;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa", "--spring.datasource.password=",
            "--spring.jpa.hibernate.ddl-auto=create-drop", "--spring.jpa.show-sql=false", "--logging.level.root=WARN",
            "--spring.main.banner-mode=off", "--agent.service.base-url=http://127.0.0.1:18020",
            "--agent.service.read-timeout-ms=120000", "--agent.service.auth-token=course-debug-agent-service",
            "--customer-service.runtime-context.default-order-limit=2")) {
            fixture(ctx, "ORD9801", 2);
            post("/api/auth/login", Map.of("username", "zhangsan", "password", "123456"));
            String run = "reply-" + System.currentTimeMillis();
            var store = ctx.getBean(CustomerServiceSessionStore.class);

            String natural = run + "-natural";
            var card = chat(natural, "ORD9801 我要退款，原因：买错了。");
            if (!card.path("confirmRequired").asBoolean()) throw new AssertionError("没有确认卡");
            String token = card.path("resumeToken").asText();
            var reply = chat(natural, "我想了想，还是不退了");
            var session = store.find(natural).orElseThrow();
            boolean kept = "CONFIRM_SUBMIT".equals(session.getPendingAction()) && token.equals(session.getResumeToken());
            boolean pointed = reply.path("answer").asText().contains("取消本次申请") && reply.path("answer").asText().contains("仍保留");
            boolean notConflict = !reply.path("answer").asText().contains("请选择是否保留办理");
            note(kept && pointed && notConflict && hasCancelButton(reply) ? "pass" : "fail",
                "我想了想，还是不退了", "pending=" + session.getPendingAction() + " answer=" + reply.path("answer").asText());

            String keep = run + "-keep";
            var keepCard = chat(keep, "ORD9801 我要退款，原因：买错了。");
            String keepToken = keepCard.path("resumeToken").asText();
            var keepReply = chat(keep, "假如商品降价我可能留下，不过现在继续退。");
            var keepSession = store.find(keep).orElseThrow();
            boolean keepOk = "CONFIRM_SUBMIT".equals(keepSession.getPendingAction()) && keepToken.equals(keepSession.getResumeToken())
                && !keepReply.path("answer").asText().contains("这句还不够确定")
                && !keepReply.path("answer").asText().contains("请选择是否保留办理");
            note(keepOk ? "pass" : "fail", "假如商品降价我可能留下，不过现在继续退。",
                "pending=" + keepSession.getPendingAction() + " answer=" + keepReply.path("answer").asText());

            var result = new LinkedHashMap<String, Object>();
            result.put("score_min", "0.9");
            result.put("mode", "isolated_h2_real_models_low_score_reply");
            result.put("checks", RESULTS);
            Files.writeString(out.resolve("reply-result.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(result));
            if (RESULTS.stream().anyMatch(item -> "fail".equals(((Map<?, ?>) item).get("status")))) {
                throw new AssertionError("两句重跑未通过，见 reply-result.json");
            }
        }
    }
}
