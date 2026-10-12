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

/** 隔离 H2。判断来自本机固定模型，验收草稿、旧卡和后续请求，不提交真实退款。 */
public class IsolatedCancelCheck {
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient CLIENT = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
    static final List<Object> RESULTS = new ArrayList<>();
    static final List<Object> TURNS = new ArrayList<>();
    static Path out;

    static JsonNode call(String path, Object body, boolean requireOk) throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18081" + path))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        var response = CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
        JsonNode parsed = response.body().isBlank() ? JSON.createObjectNode() : JSON.readTree(response.body());
        TURNS.add(Map.of("path", path, "status", response.statusCode(), "request", body, "response", parsed));
        if (requireOk && response.statusCode() != 200) throw new AssertionError(response.statusCode() + " " + response.body());
        return requireOk ? parsed.path("data") : parsed;
    }

    static JsonNode chat(String session, String message) throws Exception {
        var body = new HashMap<String, Object>();
        body.put("sessionId", session);
        body.put("message", message);
        body.put("pageContext", Map.of("type", "HOME"));
        return call("/api/customer-service/chat", body, true);
    }

    static int resume(JsonNode card, String decision) throws Exception {
        var body = new HashMap<String, Object>();
        body.put("sessionId", card.path("sessionId").asText());
        body.put("resumeToken", card.path("resumeToken").asText());
        body.put("relatedOrderNo", card.path("relatedOrderNo").asText());
        body.put("decision", decision);
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18081/api/customer-service/resume"))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        var response = CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
        TURNS.add(Map.of("path", "/api/customer-service/resume", "status", response.statusCode(), "request", body,
            "response", response.body().isBlank() ? "" : JSON.readTree(response.body())));
        return response.statusCode();
    }

    static void verify(boolean ok, String label) {
        RESULTS.add(Map.of("check", label, "passed", ok));
        System.out.println((ok ? "PASS " : "FAIL ") + label);
    }

    static CustomerServiceSession session(ServletWebServerApplicationContext ctx, String id) {
        return ctx.getBean(CustomerServiceSessionStore.class).find(id).orElseThrow();
    }

    static void fixture(ServletWebServerApplicationContext ctx, String no, int minute) {
        var now = LocalDateTime.now().plusMinutes(minute);
        var amount = new BigDecimal("200.00");
        var order = new OrderEntity(no, "隔离验收", "PAID_PENDING_SHIPMENT", "PAID", amount, now, "U1001", false, true, "取消意愿隔离");
        order.markPaid(now);
        ctx.getBean(OrderRepository.class).save(order);
        ctx.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "隔离露营椅", 1, amount, true));
        ctx.getBean(BalanceTransactionRepository.class).save(new BalanceTransaction("CANCEL-PAY-" + no, "U1001", no, null,
            "PAYMENT", amount.negate(), new BigDecimal("1000"), new BigDecimal("800"), "隔离合成支付", now));
    }

    public static void main(String[] args) throws Exception {
        out = Path.of(args.length > 0 ? args[0] : "result");
        Files.createDirectories(out);
        try (var ctx = (ServletWebServerApplicationContext) new SpringApplicationBuilder(EcommerceBackendApplication.class).run(
            "--server.port=18081", "--spring.datasource.url=jdbc:h2:mem:cancel-accept;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa", "--spring.datasource.password=",
            "--spring.jpa.hibernate.ddl-auto=create-drop", "--spring.jpa.show-sql=false", "--logging.level.root=WARN",
            "--spring.main.banner-mode=off", "--agent.service.base-url=http://127.0.0.1:18014",
            "--agent.service.auth-token=course-debug-agent-service", "--customer-service.runtime-context.default-order-limit=2")) {
            fixture(ctx, "ORD9801", 2);
            fixture(ctx, "ORD9802", 1);
            var sales = ctx.getBean(AfterSaleRequestRepository.class);
            var transactions = ctx.getBean(BalanceTransactionRepository.class);
            var balances = ctx.getBean(BalanceAccountRepository.class);
            long initialSales = sales.count();
            long initialTransactions = transactions.count();
            var balance = balances.findByUserId("U1001").orElseThrow().getAvailableBalance();
            call("/api/auth/login", Map.of("username", "zhangsan", "password", "123456"), true);
            String run = "cancel-" + System.currentTimeMillis();

            String natural = run + "-natural";
            var card = chat(natural, "ORD9801 我要退款，原因：买错了。");
            if (!card.path("confirmRequired").asBoolean()) throw new AssertionError("前置确认卡不存在: " + card);
            String oldToken = card.path("resumeToken").asText();
            var cancelled = chat(natural, "我想了想，还是不退了");
            var naturalSession = session(ctx, natural);
            verify("CANCELLED".equals(cancelled.path("pendingAction").asText()) && cancelled.path("answer").asText().contains("未提交")
                && naturalSession.getResumeToken() == null && "CANCELLED".equals(naturalSession.getPendingAction()),
                "自然改口取消未提交草稿并清掉凭证");
            verify(resume(card, "CONFIRM_SUBMIT") == 409, "自然改口后旧确认卡不能提交");
            var follow = chat(natural, "查一下物流");
            verify(!follow.path("confirmRequired").asBoolean() && !"CONFIRM_SUBMIT".equals(session(ctx, natural).getPendingAction()),
                "取消后下一句不恢复确认卡");

            String keep = run + "-keep";
            var keepCard = chat(keep, "ORD9801 我要退款，原因：买错了。");
            if (!keepCard.path("confirmRequired").asBoolean()) throw new AssertionError("继续用例没有确认卡");
            String keepToken = keepCard.path("resumeToken").asText();
            chat(keep, "这次退款我还要办，不能取消。");
            var keepSession = session(ctx, keep);
            verify("CONFIRM_SUBMIT".equals(keepSession.getPendingAction()) && !keepSession.isHandled()
                && keepToken.equals(keepSession.getResumeToken()), "明确继续不取消，原确认卡还在");

            String shipping = run + "-ship";
            var shipCard = chat(shipping, "ORD9801 我要退款，原因：买错了。");
            String shipToken = shipCard.path("resumeToken").asText();
            var shipReply = chat(shipping, "要是退货运费得自己承担，那就别退了。");
            var shipSession = session(ctx, shipping);
            verify(!"CANCELLED".equals(shipSession.getPendingAction()) && shipToken.equals(shipSession.getResumeToken())
                && shipReply.path("answer").asText().contains("保留"), "运费条件未核实不取消");

            String ordinal = run + "-ordinal";
            var choosing = chat(ordinal, "帮我退款。");
            verify("REFUND_ORDER_REQUIRED".equals(session(ctx, ordinal).getPendingAction()) && !choosing.path("confirmRequired").asBoolean(),
                "选单阶段先有待办、没有确认卡");
            var asked = chat(ordinal, "取消订单吧");
            verify(!"CANCELLED".equals(session(ctx, ordinal).getPendingAction()) && asked.path("answer").asText().contains("退款"),
                "对象不清时追问，不取消选单");
            chat(ordinal, "第一笔");
            verify("REFUND_ORDER_REQUIRED".equals(session(ctx, ordinal).getPendingAction()),
                "第一笔不把选单待办当成当前售后取消");

            String ended = run + "-ended";
            chat(ended, "帮我退款。");
            chat(ended, "取消订单吧");
            var endedReply = chat(ended, "结束这次");
            verify("CANCELLED".equals(endedReply.path("pendingAction").asText()) && session(ctx, ended).getResumeToken() == null,
                "补明是这次办理后才取消选单");
            var revived = chat(ended, "第一笔");
            verify(!revived.path("confirmRequired").asBoolean()
                && !"REFUND_ORDER_REQUIRED".equals(session(ctx, ended).getPendingAction()),
                "取消后第一笔不复活旧待办");

            String conflict = run + "-conflict";
            var conflictCard = chat(conflict, "ORD9801 我要退款，原因：买错了。");
            var conflictReply = chat(conflict, "我有点想取消这单退款");
            boolean labels = false;
            for (JsonNode action : conflictReply.path("actions")) {
                if ("KEEP".equals(action.path("code").asText()) && "保留当前办理".equals(action.path("label").asText())) labels = true;
            }
            boolean cancelLabel = false;
            for (JsonNode action : conflictReply.path("actions")) {
                if ("CANCEL_DRAFT".equals(action.path("code").asText()) && action.path("label").asText().contains("尚未提交")) cancelLabel = true;
            }
            verify(conflictReply.path("confirmRequired").asBoolean() && labels && cancelLabel
                && "CONFIRM_SUBMIT".equals(session(ctx, conflict).getPendingAction()),
                "冲突时展示保留和取消未提交办理，不直接取消");
            verify(resume(conflictReply, "KEEP") == 200 && "CONFIRM_SUBMIT".equals(session(ctx, conflict).getPendingAction())
                && !session(ctx, conflict).isHandled(), "选择保留不取消草稿");
            verify(resume(conflictReply, "KEEP") == 403, "保留后旧冲突卡不能再点");

            String submit = run + "-submit";
            chat(submit, "ORD9801 我要退款，原因：买错了。");
            var submitCard = chat(submit, "我有点想取消这单退款");
            verify(resume(submitCard, "CONFIRM_SUBMIT") == 400 && sales.count() == initialSales,
                "冲突卡不能提交申请，也不产生售后记录");

            String button = run + "-button";
            chat(button, "ORD9801 我要退款，原因：买错了。");
            var buttonCard = chat(button, "我有点想取消这单退款");
            verify(resume(buttonCard, "CANCEL_DRAFT") == 200 && "CANCELLED".equals(session(ctx, button).getPendingAction())
                && session(ctx, button).getResumeToken() == null, "冲突卡确认取消后清草稿和凭证");
            verify(resume(buttonCard, "CANCEL_DRAFT") == 409, "取消后重复点击旧卡被拒绝");

            verify(sales.count() == initialSales && transactions.count() == initialTransactions
                && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(balance) == 0,
                "整组对话没有新增售后、流水或余额变化");
            boolean passed = RESULTS.stream().allMatch(item -> Boolean.TRUE.equals(((Map<?, ?>) item).get("passed")));
            var result = new LinkedHashMap<String, Object>();
            result.put("passed", passed);
            result.put("mode", "isolated_h2_scripted_models");
            result.put("checks", RESULTS);
            Files.writeString(out.resolve("result.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(result));
            Files.writeString(out.resolve("dialogue.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(TURNS));
            if (!passed) throw new AssertionError("隔离验收失败，见 result.json");
        }
    }
}
