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

/** 剧本里的 17 项用真实模型重跑。没走到的按钮步骤记为跳过，不拿有效卡去提交。 */
public class RealScriptCheck {
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient CLIENT = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
    static final List<Object> RESULTS = new ArrayList<>();

    static JsonNode post(String path, Object body) throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18083" + path))
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

    static int resume(JsonNode card, String decision) throws Exception {
        var body = new HashMap<String, Object>();
        body.put("sessionId", card.path("sessionId").asText());
        body.put("resumeToken", card.path("resumeToken").asText());
        body.put("relatedOrderNo", card.path("relatedOrderNo").asText());
        body.put("decision", decision);
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:18083/api/customer-service/resume"))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        return CLIENT.send(request, HttpResponse.BodyHandlers.ofString()).statusCode();
    }

    static void note(String status, String label, String detail) {
        var row = new LinkedHashMap<String, Object>();
        row.put("check", label);
        row.put("status", status);
        row.put("detail", detail);
        RESULTS.add(row);
        System.out.println(status + " " + label + " " + detail);
    }

    static CustomerServiceSession session(ServletWebServerApplicationContext ctx, String id) {
        return ctx.getBean(CustomerServiceSessionStore.class).find(id).orElseThrow();
    }

    static boolean cancelled(CustomerServiceSession session) {
        return "CANCELLED".equals(session.getPendingAction()) || session.isHandled();
    }

    static boolean conflictCard(JsonNode reply) {
        for (JsonNode action : reply.path("actions")) {
            if ("KEEP".equals(action.path("code").asText())) return true;
        }
        return false;
    }

    static JsonNode card(String session) throws Exception {
        var created = chat(session, "ORD9801 我要退款，原因：买错了。");
        if (!created.path("confirmRequired").asBoolean()) throw new AssertionError("没有确认卡: " + created);
        return created;
    }

    static void fixture(ServletWebServerApplicationContext ctx, String no, int minute) {
        var now = LocalDateTime.now().plusMinutes(minute);
        var amount = new BigDecimal("200.00");
        var order = new OrderEntity(no, "隔离验收", "PAID_PENDING_SHIPMENT", "PAID", amount, now, "U1001", false, true, "剧本真实模型");
        order.markPaid(now);
        ctx.getBean(OrderRepository.class).save(order);
        ctx.getBean(OrderItemRepository.class).save(new OrderItem(order, 1L, "隔离露营椅", 1, amount, true));
        ctx.getBean(BalanceTransactionRepository.class).save(new BalanceTransaction("SCRIPT-PAY-" + no, "U1001", no, null,
            "PAYMENT", amount.negate(), new BigDecimal("1000"), new BigDecimal("800"), "隔离合成支付", now));
    }

    public static void main(String[] args) throws Exception {
        Path out = Path.of(args[0]);
        try (var ctx = (ServletWebServerApplicationContext) new SpringApplicationBuilder(EcommerceBackendApplication.class).run(
            "--server.port=18083", "--spring.datasource.url=jdbc:h2:mem:cancel-script-real;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa", "--spring.datasource.password=",
            "--spring.jpa.hibernate.ddl-auto=create-drop", "--spring.jpa.show-sql=false", "--logging.level.root=WARN",
            "--spring.main.banner-mode=off", "--agent.service.base-url=http://127.0.0.1:18018",
            "--agent.service.read-timeout-ms=120000", "--agent.service.connect-timeout-ms=5000",
            "--agent.service.auth-token=course-debug-agent-service", "--customer-service.runtime-context.default-order-limit=2")) {
            fixture(ctx, "ORD9801", 2);
            fixture(ctx, "ORD9802", 1);
            var sales = ctx.getBean(AfterSaleRequestRepository.class);
            var transactions = ctx.getBean(BalanceTransactionRepository.class);
            var balances = ctx.getBean(BalanceAccountRepository.class);
            long initialSales = sales.count();
            long initialTransactions = transactions.count();
            var balance = balances.findByUserId("U1001").orElseThrow().getAvailableBalance();
            post("/api/auth/login", Map.of("username", "zhangsan", "password", "123456"));
            String run = "script-real-" + System.currentTimeMillis();

            String natural = run + "-natural";
            var card = card(natural);
            var cancelled = chat(natural, "我想了想，还是不退了");
            var naturalSession = session(ctx, natural);
            boolean naturalCancelled = "CANCELLED".equals(naturalSession.getPendingAction()) && naturalSession.getResumeToken() == null;
            note(naturalCancelled ? "pass" : "fail", "自然改口取消未提交草稿并清掉凭证",
                "pending=" + naturalSession.getPendingAction() + " answer=" + cancelled.path("answer").asText());
            if (naturalCancelled) {
                note(resume(card, "CONFIRM_SUBMIT") == 409 ? "pass" : "fail", "自然改口后旧确认卡不能提交", "已点旧卡");
                var follow = chat(natural, "查一下物流");
                note(!follow.path("confirmRequired").asBoolean() ? "pass" : "fail", "取消后下一句不恢复确认卡",
                    "confirmRequired=" + follow.path("confirmRequired").asBoolean());
            } else {
                note("skipped", "自然改口后旧确认卡不能提交", "草稿还在，没有点提交");
                note("skipped", "取消后下一句不恢复确认卡", "这一轮没有取消");
            }

            String keep = run + "-keep";
            var keepCard = card(keep);
            String keepToken = keepCard.path("resumeToken").asText();
            var keepReply = chat(keep, "这次退款我还要办，不能取消。");
            var keepSession = session(ctx, keep);
            boolean keepOk = "CONFIRM_SUBMIT".equals(keepSession.getPendingAction()) && !keepSession.isHandled()
                && keepToken.equals(keepSession.getResumeToken());
            note(keepOk ? "pass" : "fail", "明确继续不取消，原确认卡还在",
                "pending=" + keepSession.getPendingAction() + " answer=" + keepReply.path("answer").asText());

            String shipping = run + "-ship";
            var shipCard = card(shipping);
            String shipToken = shipCard.path("resumeToken").asText();
            var shipReply = chat(shipping, "要是退货运费得自己承担，那就别退了。");
            var shipSession = session(ctx, shipping);
            boolean shipOk = !cancelled(shipSession) && shipToken.equals(shipSession.getResumeToken())
                && shipReply.path("answer").asText().contains("保留");
            note(shipOk ? "pass" : "fail", "运费条件未核实不取消",
                "pending=" + shipSession.getPendingAction() + " answer=" + shipReply.path("answer").asText());

            String ordinal = run + "-ordinal";
            var choosing = chat(ordinal, "帮我退款。");
            note("REFUND_ORDER_REQUIRED".equals(session(ctx, ordinal).getPendingAction()) && !choosing.path("confirmRequired").asBoolean() ? "pass" : "fail",
                "选单阶段先有待办、没有确认卡", "pending=" + session(ctx, ordinal).getPendingAction());
            var asked = chat(ordinal, "取消订单吧");
            boolean askedOk = !cancelled(session(ctx, ordinal)) && asked.path("answer").asText().contains("退款");
            note(askedOk ? "pass" : "fail", "对象不清时追问，不取消选单",
                "pending=" + session(ctx, ordinal).getPendingAction() + " answer=" + asked.path("answer").asText());
            var first = chat(ordinal, "第一笔");
            note("REFUND_ORDER_REQUIRED".equals(session(ctx, ordinal).getPendingAction()) ? "pass" : "fail",
                "第一笔不把选单待办当成当前售后取消",
                "pending=" + session(ctx, ordinal).getPendingAction() + " answer=" + first.path("answer").asText());

            String ended = run + "-ended";
            chat(ended, "帮我退款。");
            chat(ended, "取消订单吧");
            var endedReply = chat(ended, "结束这次");
            boolean endedCancelled = "CANCELLED".equals(session(ctx, ended).getPendingAction()) && session(ctx, ended).getResumeToken() == null;
            note(endedCancelled ? "pass" : "fail", "补明是这次办理后才取消选单",
                "pending=" + session(ctx, ended).getPendingAction() + " answer=" + endedReply.path("answer").asText());
            if (endedCancelled) {
                var revived = chat(ended, "第一笔");
                note(!revived.path("confirmRequired").asBoolean() && !"REFUND_ORDER_REQUIRED".equals(session(ctx, ended).getPendingAction()) ? "pass" : "fail",
                    "取消后第一笔不复活旧待办", "pending=" + session(ctx, ended).getPendingAction());
            } else {
                note("skipped", "取消后第一笔不复活旧待办", "选单没有被取消");
            }

            String conflict = run + "-conflict";
            card(conflict);
            var conflictReply = chat(conflict, "我有点想取消这单退款");
            boolean labels = conflictCard(conflictReply);
            note(labels && !cancelled(session(ctx, conflict)) ? "pass" : "fail",
                "冲突时展示保留和取消未提交办理，不直接取消",
                "pending=" + session(ctx, conflict).getPendingAction() + " answer=" + conflictReply.path("answer").asText());
            if (labels) {
                note(resume(conflictReply, "KEEP") == 200 && !cancelled(session(ctx, conflict)) ? "pass" : "fail",
                    "选择保留不取消草稿", "pending=" + session(ctx, conflict).getPendingAction());
                note(resume(conflictReply, "KEEP") == 403 ? "pass" : "fail", "保留后旧冲突卡不能再点", "已再点一次");
            } else {
                note("skipped", "选择保留不取消草稿", "没有出现冲突卡");
                note("skipped", "保留后旧冲突卡不能再点", "没有出现冲突卡");
            }

            String submit = run + "-submit";
            card(submit);
            var submitCard = chat(submit, "我有点想取消这单退款");
            if (conflictCard(submitCard)) {
                int code = resume(submitCard, "CONFIRM_SUBMIT");
                note(code == 400 && sales.count() == initialSales ? "pass" : "fail",
                    "冲突卡不能提交申请，也不产生售后记录", "status=" + code);
            } else {
                note("skipped", "冲突卡不能提交申请，也不产生售后记录", "没有出现冲突卡，没有点提交");
            }

            String button = run + "-button";
            card(button);
            var buttonCard = chat(button, "我有点想取消这单退款");
            if (conflictCard(buttonCard)) {
                int code = resume(buttonCard, "CANCEL_DRAFT");
                note(code == 200 && cancelled(session(ctx, button)) && session(ctx, button).getResumeToken() == null ? "pass" : "fail",
                    "冲突卡确认取消后清草稿和凭证", "status=" + code + " pending=" + session(ctx, button).getPendingAction());
                note(resume(buttonCard, "CANCEL_DRAFT") == 409 ? "pass" : "fail", "取消后重复点击旧卡被拒绝", "已再点一次");
            } else {
                note("skipped", "冲突卡确认取消后清草稿和凭证", "没有出现冲突卡");
                note("skipped", "取消后重复点击旧卡被拒绝", "没有出现冲突卡");
            }

            boolean moneyOk = sales.count() == initialSales && transactions.count() == initialTransactions
                && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(balance) == 0;
            note(moneyOk ? "pass" : "fail", "整组对话没有新增售后、流水或余额变化",
                "sales=" + sales.count() + " transactions=" + transactions.count());
            long fails = RESULTS.stream().filter(item -> "fail".equals(((Map<?, ?>) item).get("status"))).count();
            var result = new LinkedHashMap<String, Object>();
            result.put("fail_count", fails);
            result.put("score_min", "0.9");
            result.put("mode", "isolated_h2_real_models_script_cases");
            result.put("checks", RESULTS);
            Files.writeString(out.resolve("script-real-result.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(result));
        }
    }
}
