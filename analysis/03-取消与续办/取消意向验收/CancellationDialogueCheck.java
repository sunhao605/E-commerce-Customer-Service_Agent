package com.teachdemo.ecommerce.customer_service;

import static com.teachdemo.ecommerce.customer_service.CandidateDialogueCheck.*;
import com.fasterxml.jackson.databind.JsonNode;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.teachdemo.ecommerce.account.*;
import com.teachdemo.ecommerce.aftersale.*;
import com.teachdemo.ecommerce.order.*;
import java.net.URI;
import java.net.http.*;
import java.nio.file.*;
import java.time.LocalDateTime;
import java.util.*;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.annotation.*;

/** 复用M06隔离HTTP/H2样本；--offline只替换Agent，默认使用真实Python/模型。 */
public class CancellationDialogueCheck {
    static final List<Object> RESULTS = new ArrayList<>();
    @Profile("m07-offline")
    @Configuration(proxyBeanMethods = false)
    static class AgentConfig {
        @Bean @Primary CustomerServiceAgentClient isolatedAgent() {
            return new CustomerServiceAgentClient() {
                public JsonNode chat(AgentChatRequest request) {
                    var response = JSON.createObjectNode().put("answer", "隔离模型咨询回复。");
                    String message = (String) request.getRuntimeContext().get("userMessage");
                    if (message.contains("七天无理由退货")) {
                        response.putObject("session_state").put("intent", "return_request").putObject("workflow")
                            .put("workflow_type", "received_return").put("order_id", "ORD9803").put("return_reason", "七天无理由");
                    } else if (message.contains("退款") && !message.contains("不")) {
                        response.putObject("session_state").put("intent", "refund_request");
                        if (message.contains("ORD9801")) {
                            response.withObject("/session_state").putObject("workflow")
                                .put("workflow_id", "mock-workflow").put("workflow_type", "unshipped_refund")
                                .put("order_id", "ORD9801").put("status", "paused")
                                .put("pending_action", "require_approval").put("resume_token", "mock-token");
                        } else response.putObject("clarification").put("clarification_field", "order_id");
                    }
                    return response;
                }
                public JsonNode resume(AgentResumeRequest request) { throw new AssertionError("不能调用模型审批"); }
            };
        }
    }
    static void verify(boolean ok, String label) {
        RESULTS.add(Map.of("check", label, "passed", ok));
        System.out.println((ok ? "PASS " : "FAIL ") + label);
    }
    static int decision(JsonNode card, String decision) throws Exception {
        var body = Map.of("sessionId", card.path("sessionId").asText(), "resumeToken", card.path("resumeToken").asText(),
            "relatedOrderNo", card.path("relatedOrderNo").asText(), "decision", decision);
        var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:8081/api/customer-service/resume"))
            .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build();
        var response = CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
        TURNS.add(Map.of("request", body, "status", response.statusCode(), "response", JSON.readTree(response.body())));
        Files.writeString(Path.of("/check/dialogue.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(TURNS));
        return response.statusCode();
    }
    public static void main(String[] args) throws Exception {
        boolean offline = Arrays.asList(args).contains("--offline");
        var app = new SpringApplicationBuilder(EcommerceBackendApplication.class);
        if (offline) app.profiles("m07-offline");
        try (var ctx = (ServletWebServerApplicationContext) app.run(
            "--server.port=8081", "--spring.datasource.url=jdbc:h2:mem:m07;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa", "--spring.datasource.password=",
            "--spring.jpa.hibernate.ddl-auto=create-drop", "--spring.jpa.show-sql=false", "--logging.level.root=WARN",
            "--spring.main.banner-mode=off", "--agent.service.base-url=http://host.docker.internal:8013",
            "--agent.service.auth-token=course-debug-agent-service", "--customer-service.runtime-context.default-order-limit=2")) {
            fixture(ctx, "ORD9801", 2); fixture(ctx, "ORD9802", 1);
            fixture(ctx, "ORD9803", -1);
            var returnOrder = ctx.getBean(OrderRepository.class).findByOrderNo("ORD9803").orElseThrow();
            returnOrder.updateAdminFields("DELIVERED", "DELIVERED", "M07-TEST", "合成签收订单", LocalDateTime.now().minusDays(1));
            ctx.getBean(OrderRepository.class).save(returnOrder);
            var sales = ctx.getBean(AfterSaleRequestRepository.class);
            var transactions = ctx.getBean(BalanceTransactionRepository.class);
            var balances = ctx.getBean(BalanceAccountRepository.class);
            long initialSales = sales.count(), initialTransactions = transactions.count();
            var balance = balances.findByUserId("U1001").orElseThrow().getAvailableBalance();
            call("/api/auth/login", Map.of("username", "zhangsan", "password", "123456"));
            String run = "m07-" + System.currentTimeMillis();
            for (String phrase : List.of("取消本次申请。", "算了，不退了。", "先不退了。")) {
                String session = run + "-" + RESULTS.size();
                var card = chat(session, "ORD9801 我要退款，原因：买错了。", null);
                check(card.path("confirmRequired").asBoolean(), "前置确认卡必须存在");
                var cancelled = chat(session, phrase, null);
                boolean ok = "CANCELLED".equals(cancelled.path("pendingAction").asText())
                    && !cancelled.path("confirmRequired").asBoolean() && cancelled.path("answer").asText().contains("未提交");
                verify(ok, "确认卡后聊天取消：" + phrase);
                if (ok) {
                    verify(ctx.getBean(CustomerServiceSessionStore.class).find(session).orElseThrow().getResumeToken() == null,
                        "取消清除服务端确认凭证：" + phrase);
                    verify(decision(card, "CONFIRM_SUBMIT") == 409, "取消后旧卡提交被拒绝：" + phrase);
                    var logistics = chat(session, "查 ORD9802 的物流。", null);
                    verify(!logistics.path("confirmRequired").asBoolean(), "取消后查物流不恢复确认卡：" + phrase);
                    var next = chat(session, "ORD9801 我要退款，原因：买错了。", null);
                    verify(next.path("confirmRequired").asBoolean() && !next.path("resumeToken").equals(card.path("resumeToken")),
                        "重新申请生成新凭证：" + phrase);
                    decision(next, "CANCEL_SUBMIT");
                }
            }
            var button = chat(run + "-button", "ORD9801 我要退款，原因：买错了。", null);
            verify(decision(button, "CANCEL_SUBMIT") == 200 && decision(button, "CONFIRM_SUBMIT") == 409, "按钮取消及旧卡失效");
            var waiting = chat(run + "-waiting", "帮我退款。", null);
            verify(!waiting.path("confirmRequired").asBoolean(), "选单前尚未生成确认卡");
            var stopped = chat(run + "-waiting", "取消本次申请。", null);
            verify("CANCELLED".equals(stopped.path("pendingAction").asText()), "等待选单时取消");
            var ordinal = chat(run + "-waiting", "第一笔。", null);
            verify(!ordinal.path("confirmRequired").asBoolean() && !"REFUND_REASON_REQUIRED".equals(ordinal.path("pendingAction").asText())
                && !"REFUND_ORDER_REQUIRED".equals(ordinal.path("pendingAction").asText()), "取消后第一笔不复活旧待办");
            chat(run + "-waiting", "帮我退款。", null);
            verify("CANCELLED".equals(chat(run + "-waiting", "不申请了。", null).path("pendingAction").asText()),
                "取消后新建选单待办仍可再次取消");
            var reason = chat(run + "-reason", "ORD9801 我要退款。", null);
            verify("REFUND_REASON_REQUIRED".equals(reason.path("pendingAction").asText()), "等待补原因的前置状态");
            verify("CANCELLED".equals(chat(run + "-reason", "算了，不退了。", null).path("pendingAction").asText()), "补原因前自然取消");
            verify(!"CANCELLED".equals(chat(run + "-fresh", "取消本次申请。", null).path("pendingAction").asText()), "无待办不虚报取消成功");
            var questionCard = chat(run + "-question", "ORD9801 我要退款，原因：买错了。", null);
            var question = chat(run + "-question", "怎么取消本次申请？", null);
            verify(question.path("confirmRequired").asBoolean() && question.path("resumeToken").equals(questionCard.path("resumeToken")),
                "询问怎么取消不直接取消");
            var negation = chat(run + "-question", "不要取消本次申请，继续退款。", null);
            verify(negation.path("confirmRequired").asBoolean(), "否定取消不触发取消动作");
            decision(negation, "CANCEL_SUBMIT");
            var returnCard = chat(run + "-return", "ORD9803 我要申请七天无理由退货。", null);
            check(returnCard.path("confirmRequired").asBoolean(), "退货取消需要真实确认卡前置");
            verify("CANCELLED".equals(chat(run + "-return", "不退货了。", null).path("pendingAction").asText())
                && decision(returnCard, "CONFIRM_SUBMIT") == 409, "退货草稿复用取消并拒绝旧卡");
            verify(sales.count() == initialSales && transactions.count() == initialTransactions
                && balances.findByUserId("U1001").orElseThrow().getAvailableBalance().compareTo(balance) == 0,
                "取消对话无新增售后、资金流水或余额变化");
            // 单独准备一笔H2已提交样本，只验证取消不会撤销；不审批、不退款。
            var submittedCard = chat(run + "-submitted", "ORD9801 我要退款，原因：买错了。", null);
            check(decision(submittedCard, "CONFIRM_SUBMIT") == 200, "隔离已提交样本准备");
            var submitted = ctx.getBean(CustomerServiceSessionStore.class).find(run + "-submitted").orElseThrow();
            String requestNo = submitted.getRelatedAfterSaleNo();
            var refused = chat(run + "-submitted", "取消本次申请。", null);
            verify("NO_PENDING_APPLICATION".equals(refused.path("pendingAction").asText())
                && sales.findByRequestId(requestNo).orElseThrow().getStatus().equals("PENDING_REVIEW")
                && sales.count() == initialSales + 1 && transactions.count() == initialTransactions,
                "已提交申请保持待审核、不被聊天撤销或退款");
            boolean passed = RESULTS.stream().allMatch(item -> Boolean.TRUE.equals(((Map<?,?>)item).get("passed")));
            Files.writeString(Path.of("/check/result.json"), JSON.writerWithDefaultPrettyPrinter().writeValueAsString(
                Map.of("passed", passed, "mode", offline ? "synthetic_agent" : "real_python_model", "checks", RESULTS,
                    "turns", TURNS.size(), "submitted_fixture", "仅H2一笔PENDING_REVIEW，未审批或退款")));
            if (!passed) throw new AssertionError("M07失败项见result.json");
        }
    }
}
