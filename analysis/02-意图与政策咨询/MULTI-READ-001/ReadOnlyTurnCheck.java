package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import com.sun.net.httpserver.HttpServer;
import com.teachdemo.ecommerce.EcommerceBackendApplication;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;

/**
 * MULTI-READ-001：Java 网关只读轮次消费的隔离验证。
 * stub Python Agent 固定响应；内存 H2；端口 28102/28101，不触日常 MySQL/服务。
 */
public class ReadOnlyTurnCheck {
    static final List<String[]> CHECKS = new ArrayList<>();
    static ServletWebServerApplicationContext context;
    static HttpClient client;
    static java.net.CookieManager cookieManager;

    static void check(boolean ok, String name) {
        CHECKS.add(new String[]{ok ? "PASS" : "FAIL", name});
        System.out.println((ok ? "[PASS] " : "[FAIL] ") + name);
    }

    /** 按消息内容返回固定 Python ChatResponse，模拟改造后的路由输出。 */
    static String stubResponse(String userMessage) {
        String answer;
        String intent;
        boolean readOnly;
        if (userMessage.contains("七天无理由退货")) {
            intent = "return_request"; readOnly = false;
            answer = "可以进入七天无理由退货判断。";
        } else if (userMessage.contains("发票")) {
            intent = "invoice_query"; readOnly = true;
            answer = "电子发票通常在订单完成后 24 小时内开具，可在订单详情页查看和下载。";
        } else if (userMessage.contains("退款多久到账")) {
            intent = "refund_policy_query"; readOnly = true;
            answer = "当前退款使用演示余额，不提供真实到账时限保证。";
        } else {
            intent = "general_chat"; readOnly = false;
            answer = "固定一般回复。";
        }
        return "{\"session_id\":\"isolated-readonly\",\"answer\":\"" + answer + "\","
            + "\"citations\":[],\"tool_calls\":[],\"clarification\":null,"
            + "\"reasoning_summary\":[],\"reasoning_content\":null,"
            + "\"session_state\":{\"intent\":\"" + intent + "\",\"read_only_turn\":" + readOnly + ","
            + "\"context_report\":{\"chosen_order_id\":"
            + (intent.equals("return_request") ? "\"" + userMessage.replaceAll(".*(ORD\\d+).*", "$1") + "\"" : "null") + "}}}";
    }

    public static void main(String[] args) throws Exception {
        HttpServer stub = HttpServer.create(new InetSocketAddress(28101), 0);
        stub.createContext("/chat", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            JsonNode req = CheckServer.JSON.readTree(body);
            String reply = stubResponse(req.path("user_message").asText(""));
            byte[] out = reply.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, out.length);
            exchange.getResponseBody().write(out);
            exchange.close();
        });
        stub.setExecutor(Executors.newSingleThreadExecutor());
        stub.start();
        try {
            context = (ServletWebServerApplicationContext) new SpringApplicationBuilder(
                EcommerceBackendApplication.class).run(
                "--server.address=127.0.0.1", "--server.port=28102",
                "--spring.datasource.url=jdbc:h2:mem:readonly-turn;MODE=MySQL;DB_CLOSE_DELAY=-1",
                "--spring.datasource.driver-class-name=org.h2.Driver", "--spring.datasource.username=sa",
                "--spring.datasource.password=", "--spring.jpa.hibernate.ddl-auto=create-drop",
                "--spring.jpa.show-sql=false", "--logging.level.root=WARN", "--spring.main.banner-mode=off",
                "--agent.service.base-url=http://127.0.0.1:28101", "--agent.service.read-timeout-ms=120000",
                "--agent.service.auth-token=course-debug-agent-service",
                "--customer-service.runtime-context.default-order-limit=2");
            IsolatedCancelCheck.fixture(context, "ORD9801", 2);
            IsolatedCancelCheck.fixture(context, "ORD9802", 1);
            IsolatedCancelCheck.fixture(context, "ORD9803", -1);
            // 签收样本：七天无理由退货需已签收订单（参照取消闭环检查的 scripted 夹具）。
            var orders = context.getBean(com.teachdemo.ecommerce.order.OrderRepository.class);
            var delivered = orders.findByOrderNo("ORD9803").orElseThrow();
            delivered.updateAdminFields("DELIVERED", "DELIVERED", "ISOLATED-RETURN", "隔离已签收样本",
                java.time.LocalDateTime.now(java.time.ZoneId.of("Asia/Shanghai")).minusMinutes(1));
            orders.save(delivered);
            login();
            run();
        } finally {
            if (context != null) context.close();
            stub.stop(0);
        }
        long fail = CHECKS.stream().filter(c -> c[0].equals("FAIL")).count();
        System.out.println("TOTAL=" + CHECKS.size() + " FAIL=" + fail);
        if (fail > 0) System.exit(1);
    }

    static void login() throws Exception {
        // 登录态走 HttpSession Cookie，后续请求携带同一 CookieManager。
        cookieManager = new java.net.CookieManager();
        client = HttpClient.newBuilder().cookieHandler(cookieManager).build();
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:28102/api/auth/login"))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(
                CheckServer.JSON.writeValueAsString(Map.of("username", "zhangsan", "password", "123456")))).build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        check(resp.statusCode() == 200, "登录zhangsan: status=" + resp.statusCode());
    }

    static void run() throws Exception {
        String base = "http://127.0.0.1:28102";

        // 1. 明确退货申请（已签收订单）→ Java 准备草稿并出确认卡
        HttpResponse<String> apply = post(client, base + "/api/customer-service/chat",
            "我要为 ORD9803 申请七天无理由退货");
        JsonNode applyBody = CheckServer.JSON.readTree(apply.body()).path("data");
        check("CONFIRM_SUBMIT".equals(applyBody.path("pendingAction").asText()),
            "预置退货草稿出确认卡: pendingAction=" + applyBody.path("pendingAction").asText());

        // 2. 插问发票（read_only_turn=true）→ 只读直通，不重发卡
        HttpResponse<String> invoice = post(client, base + "/api/customer-service/chat", "发票多久开好？");
        JsonNode invoiceBody = CheckServer.JSON.readTree(invoice.body()).path("data");
        check(!"CONFIRM_SUBMIT".equals(invoiceBody.path("pendingAction").asText()),
            "插问发票不重发确认卡: pendingAction=" + invoiceBody.path("pendingAction").asText());
        check(invoiceBody.path("answer").asText().contains("24 小时"), "插问答复为发票内容");

        // 3. 插问退款到账（read_only_turn=true）→ 同样直通
        HttpResponse<String> arrival = post(client, base + "/api/customer-service/chat", "退款多久到账？");
        check(!"CONFIRM_SUBMIT".equals(CheckServer.JSON.readTree(arrival.body()).path("data").path("pendingAction").asText()),
            "插问退款政策不重发确认卡");

        // 4. 草稿完整保留
        var sessionOpt = context.getBean(CustomerServiceSessionStore.class).find("isolated-readonly");
        check(sessionOpt.isPresent(), "会话存在");
        CustomerServiceSession session = sessionOpt.orElseThrow();
        check("CONFIRM_SUBMIT".equals(session.getPendingAction()), "草稿 pendingAction 保留: " + session.getPendingAction());
        check("ORD9803".equals(session.getRelatedOrderNo()), "草稿绑定订单保留: " + session.getRelatedOrderNo());
    }

    static HttpResponse<String> post(HttpClient client, String url, String message) throws Exception {
        String body = "{\"sessionId\":\"isolated-readonly\",\"message\":\"" + message + "\"}";
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)).build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
