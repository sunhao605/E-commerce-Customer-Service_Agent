package verification;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import com.teachdemo.ecommerce.customer_service.*;
import java.net.CookieManager;
import java.net.URI;
import java.net.http.*;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.annotation.*;
import org.springframework.jdbc.core.JdbcTemplate;

/** 隔离 H2 与真实 Java HTTP；Agent 为固定替身，无外部模型或业务系统。 */
public class CapabilityIsolationCheck {
    static final ObjectMapper JSON = new ObjectMapper();
    static final String TOKEN = UUID.randomUUID().toString();
    static final List<Object> CHECKS = new ArrayList<>();
    static final List<Object> REQUESTS = new ArrayList<>();
    static String base;
    static int agentCalls;

    static void verify(boolean passed, String label) {
        CHECKS.add(Map.of("check", label, "passed", passed));
        System.out.println((passed ? "PASS " : "FAIL ") + label);
        if (!passed) throw new AssertionError(label);
    }

    static HttpResponse<String> call(HttpClient client, String method, String path, Object body, String token) throws Exception {
        var request = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(15));
        if (token != null) request.header("X-Agent-Service-Token", token);
        if (body == null) request.method(method, HttpRequest.BodyPublishers.noBody());
        else request.header("Content-Type", "application/json").method(method,
            HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
        var response = client.send(request.build(), HttpResponse.BodyHandlers.ofString());
        // 登录材料和 Cookie 不写入证据。
        if (!path.equals("/api/auth/login")) REQUESTS.add(Map.of("method", method, "path", path,
            "status", response.statusCode(), "response", response.body()));
        return response;
    }

    static JsonNode data(HttpResponse<String> response) throws Exception {
        verify(response.statusCode() == 200, "HTTP 200: " + response.request().uri().getPath());
        JsonNode parsed = JSON.readTree(response.body());
        verify(parsed.path("success").asBoolean(), "业务响应成功");
        return parsed.path("data");
    }

    static JsonNode tables(ServletWebServerApplicationContext context) throws Exception {
        var jdbc = context.getBean(JdbcTemplate.class);
        Map<String, Object> result = new TreeMap<>();
        for (String table : List.of("order_header", "order_item", "balance_account", "balance_transaction",
                "after_sale_request", "approval_record", "logistics_info", "logistics_event")) {
            var rows = jdbc.queryForList("select * from " + table + " order by id");
            result.put(table, Map.of("rows", rows.size(), "sha256", HexFormat.of().formatHex(
                MessageDigest.getInstance("SHA-256").digest(JSON.writeValueAsBytes(rows)))));
        }
        return JSON.valueToTree(result);
    }

    static JsonNode draft(CustomerServiceSession session) {
        Map<String, Object> result = new TreeMap<>();
        result.put("pendingAction", session.getPendingAction());
        result.put("workflowId", session.getWorkflowId());
        result.put("resumeToken", session.getResumeToken());
        result.put("revision", session.getDraftRevision());
        result.put("actionType", session.getActionType());
        result.put("order", session.getRelatedOrderNo());
        result.put("afterSale", session.getRelatedAfterSaleNo());
        result.put("reason", session.getRefundReason());
        result.put("amount", session.getRefundAmount());
        result.put("handled", session.isHandled());
        result.put("objectWait", session.getObjectWait());
        result.put("afterSaleQuery", session.getAfterSaleQuery());
        return JSON.valueToTree(result);
    }

    static JsonNode chat(HttpClient user, String id, String message) throws Exception {
        return data(call(user, "POST", "/api/customer-service/chat",
            Map.of("sessionId", id, "message", message, "pageContext", Map.of("type", "HOME")), null));
    }

    public static void main(String[] args) throws Exception {
        var out = Path.of(args[0]);
        boolean passed = false;
        try (var context = (ServletWebServerApplicationContext) new SpringApplicationBuilder(
                EcommerceBackendApplication.class, FixedAgent.class).run(
                "--server.address=127.0.0.1", "--server.port=0",
                "--spring.datasource.url=jdbc:h2:mem:capabilities;MODE=MySQL;DB_CLOSE_DELAY=-1",
                "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa",
                "--spring.datasource.password=", "--spring.jpa.hibernate.ddl-auto=create-drop",
                "--spring.jpa.show-sql=false", "--logging.level.root=WARN", "--spring.main.banner-mode=off",
                "--agent.service.base-url=http://127.0.0.1:1", "--agent.service.auth-token=" + TOKEN)) {
            base = "http://127.0.0.1:" + context.getWebServer().getPort();
            var before = tables(context);
            JSON.writerWithDefaultPrettyPrinter().writeValue(out.resolve("business-before.json").toFile(), before);
            var anonymous = HttpClient.newHttpClient();
            String path = "/api/customer-service/capabilities/";
            verify(call(anonymous, "GET", path + "inventory_query", null, null).statusCode() == 401, "匿名读取被拒绝");
            verify(call(anonymous, "GET", path + "inventory_query", null, "invalid").statusCode() == 401, "错误服务令牌被拒绝");

            JsonNode catalog;
            try (var input = CapabilityIsolationCheck.class.getResourceAsStream("/service-capabilities.json")) {
                catalog = JSON.readTree(input);
            }
            verify(catalog.path("capabilities").size() == 6, "目录加载六项能力");
            for (var item : catalog.path("capabilities")) {
                var found = data(call(anonymous, "GET", path + item.path("capabilityId").asText(), null, TOKEN));
                verify(found.path("supportStatus").equals(item.path("supportStatus"))
                    && found.path("scope").equals(item.path("scope"))
                    && found.path("explanation").equals(item.path("explanation"))
                    && found.path("alternatives").equals(item.path("alternatives"))
                    && found.path("version").equals(catalog.path("version"))
                    && found.path("channel").equals(catalog.path("channel")), "真实接口与单一目录一致: " + item.path("capabilityId").asText());
            }
            verify(data(call(anonymous, "GET", path + "not_registered", null, TOKEN)).path("supportStatus").asText().equals("unknown"), "未登记返回 unknown");
            verify(call(anonymous, "GET", path + "Bad-ID", null, TOKEN).statusCode() == 400, "非法标识返回 400");
            verify(call(anonymous, "POST", path + "inventory_query", Map.of(), TOKEN).statusCode() == 403, "服务身份没有能力写入口权限");

            var user = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
            verify(call(user, "POST", "/api/auth/login", Map.of("username", "zhangsan", "password", "123456"), null).statusCode() == 200, "隔离客户登录成功");
            verify(data(call(user, "GET", path + "stock_notification", null, null)).path("supportStatus").asText().equals("unsupported"), "登录客户可读取能力");
            var admin = HttpClient.newBuilder().cookieHandler(new CookieManager()).build();
            verify(call(admin, "POST", "/api/auth/login", Map.of("username", "admin", "password", "admin123456"), null).statusCode() == 200, "隔离管理员登录成功");
            verify(call(admin, "GET", path + "inventory_query", null, null).statusCode() == 403, "管理员角色不会越过 USER/AGENT_SERVICE 限制");

            var store = context.getBean(CustomerServiceSessionStore.class);
            String[][] cases = {
                {"支持到货提醒吗", "CAPABILITY_QUERY", "stock_notification"},
                {"轻量露营折叠椅现在多少钱，有货后能提醒我吗？", "PRODUCT_QUERY", "stock_notification"},
                {"支持退款吗", "CAPABILITY_QUERY", "refund_consultation"},
                {"支持退货吗", "CAPABILITY_QUERY", "return_consultation"},
                {"支持换货吗", "CAPABILITY_QUERY", "exchange_handling"},
                {"能不能转人工", "CAPABILITY_QUERY", "human_takeover"},
                {"介绍一下你们能提供哪些服务？", "CAPABILITY_QUERY", "refund_consultation"}
            };
            for (int i = 0; i < cases.length; i++) {
                String id = "cap-isolation-" + i;
                var answer = chat(user, id, cases[i][0]);
                verify(answer.path("pendingAction").asText().equals(cases[i][1])
                    && !answer.path("confirmRequired").asBoolean()
                    && answer.path("resumeToken").isMissingNode()
                    && answer.path("answer").asText().contains(ServiceCapabilityCatalog.get(cases[i][2]).explanation()),
                    "咨询直出且无确认卡: " + cases[i][0]);
                var session = store.find(id).orElseThrow();
                verify(session.getPendingAction() == null && session.getResumeToken() == null
                    && session.getWorkflowId() == null && session.getRelatedAfterSaleNo() == null,
                    "服务端未准备申请: " + cases[i][0]);
            }
            var existing = store.getOrCreate("cap-existing-draft", "U1001");
            existing.setPendingAction("CONFIRM_SUBMIT");
            existing.setResumeToken("synthetic-existing-token");
            existing.setWorkflowId("synthetic-existing-workflow");
            existing.setActionType("REFUND");
            existing.setRelatedOrderNo("SYNTHETIC-ORDER");
            existing.setRefundReason("合成草稿，只检查咨询保留");
            existing.bumpDraftRevision();
            var oldDraft = draft(existing);
            var answer = chat(user, existing.getSessionId(), "支持退款吗");
            verify(!answer.path("confirmRequired").asBoolean() && oldDraft.equals(draft(existing)), "能力咨询保留既有草稿、版本和凭证");
            chat(user, existing.getSessionId(), "介绍一下你们能提供哪些服务？");
            verify(oldDraft.equals(draft(existing)), "新服务范围意图保留既有草稿和凭证");
            verify(agentCalls == 9, "九轮咨询均经过固定 Agent，无额外恢复或外发");
            var after = tables(context);
            JSON.writerWithDefaultPrettyPrinter().writeValue(out.resolve("business-after.json").toFile(), after);
            verify(before.equals(after), "验收前后八张业务表行数及内容哈希一致");
            passed = true;
        } finally {
            JSON.writerWithDefaultPrettyPrinter().writeValue(out.resolve("checks.json").toFile(), CHECKS);
            JSON.writerWithDefaultPrettyPrinter().writeValue(out.resolve("http-responses.json").toFile(), REQUESTS);
            JSON.writerWithDefaultPrettyPrinter().writeValue(out.resolve("result.json").toFile(), Map.of(
                "passed", passed, "checks", CHECKS.size(), "agent_calls", agentCalls,
                "database", "isolated in-memory H2", "agent", "fixed Java test double",
                "external_network", false, "daily_services_changed", false));
        }
    }

    @Configuration
    static class FixedAgent {
        @Bean @Primary
        CustomerServiceAgentClient fixedCapabilityAgent() {
            return new CustomerServiceAgentClient() {
                public JsonNode chat(AgentChatRequest request) {
                    agentCalls++;
                    String message = String.valueOf(request.getRuntimeContext().get("userMessage"));
                    String id = message.contains("提醒") ? "stock_notification" : message.contains("换货") ? "exchange_handling"
                        : message.contains("人工") ? "human_takeover" : message.contains("退货") ? "return_consultation" : "refund_consultation";
                    var result = JSON.createObjectNode();
                    result.put("answer", (message.contains("多少钱") ? "模拟商品价格199元。" : "") + ServiceCapabilityCatalog.get(id).explanation());
                    result.put("next_action", "answer_user");
                    // 故意错分退款，确认 Java 咨询出口不会准备申请；混合商品走已有商品出口。
                    result.putObject("session_state").put("intent", message.contains("多少钱") ? "product_query" : message.contains("提供哪些服务") ? "service_scope_query" : "refund_request");
                    return result;
                }
                public JsonNode resume(AgentResumeRequest request) {
                    throw new AssertionError("咨询不应恢复或提交申请");
                }
            };
        }
    }
}
