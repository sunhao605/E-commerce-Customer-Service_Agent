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
public class PerTopicResultCheck {
    static final List<String[]> CHECKS = new ArrayList<>();
    static ServletWebServerApplicationContext context;
    static HttpClient client;
    static JsonNode replay;
    static java.net.CookieManager cookieManager;

    static void check(boolean ok, String name) {
        CHECKS.add(new String[]{ok ? "PASS" : "FAIL", name});
        System.out.println((ok ? "[PASS] " : "[FAIL] ") + name);
    }

    /** 按消息内容返回固定 Python ChatResponse，模拟改造后的路由输出。 */
    static String stubResponse(String message) {
        for (JsonNode row : replay) {
            if (message.equals(row.path("question").asText())) return row.path("response").toString();
        }
        var root = IsolatedCancelCheck.JSON.createObjectNode();
        String intent = message.equals("ORD9802") || message.contains("继续原退款") || message.contains("我要退款") ? "refund_request"
            : message.equals("ORD9801") ? "order_query"
            : message.contains("七天无理由退货") ? "return_request"
            : message.contains("物流") ? "order_query" : message.contains("发票") ? "invoice_query" : "general_chat";
        boolean control = message.contains("先不退了");
        root.put("answer", control ? "发票规则可查；本轮尚无停止办理回执。" : "隔离只读答复");
        var state = root.putObject("session_state");
        state.put("intent", intent);
        if (!message.contains("旧契约")) state.put("read_only_turn", !intent.equals("return_request") && !intent.equals("refund_request") && !control);
        state.put("task_control_requested", control);
        if (message.contains("故障")) root.put("next_action", "transfer_to_human");
        state.putObject("context_report").put("chosen_order_id", message.equals("ORD9802") || message.contains("换个对象") ? "ORD9802" : message.contains("继续原退款") || message.contains("我要退款 ORD9801") ? "ORD9801" : intent.equals("return_request") ? "ORD9803" : null);
        if (message.contains("继续原退款")) state.put("task_relation", "resume");
        if (message.equals("我要退款")) root.putObject("clarification").put("clarification_field", "order_id");
        return root.toString();
    }

    static String snapshot(CustomerServiceSession s) {
        return java.util.Arrays.asList(s.getPendingAction(), s.getActionType(), s.getRelatedOrderNo(),
            s.getRefundReason(), s.getRefundAmount(), s.getRefundItemSummary(), s.getExpiresAt(),
            s.getDraftRevision(), s.isHandled()).toString();
    }

    public static void main(String[] args) throws Exception {
        replay = IsolatedCancelCheck.JSON.readTree(java.nio.file.Files.readString(java.nio.file.Path.of("/workspace/analysis/02-意图与政策咨询/逐项结果实现验收/.runtime/agent-responses.json")));
        HttpServer stub = HttpServer.create(new InetSocketAddress(28101), 0);
        stub.createContext("/chat", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            JsonNode req = IsolatedCancelCheck.JSON.readTree(body);
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
        System.exit(fail > 0 ? 1 : 0);
    }

    static void login() throws Exception {
        // 登录态走 HttpSession Cookie，后续请求携带同一 CookieManager。
        cookieManager = new java.net.CookieManager();
        client = HttpClient.newBuilder().cookieHandler(cookieManager).build();
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:28102/api/auth/login"))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(
                IsolatedCancelCheck.JSON.writeValueAsString(Map.of("username", "zhangsan", "password", "123456")))).build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        check(resp.statusCode() == 200, "登录zhangsan: status=" + resp.statusCode());
    }

    static void run() throws Exception {
        String url = "http://127.0.0.1:28102/api/customer-service/chat";
        JsonNode prepared = IsolatedCancelCheck.JSON.readTree(post(client, url, "我要为 ORD9803 申请七天无理由退货").body()).path("data");
        check("CONFIRM_SUBMIT".equals(prepared.path("pendingAction").asText()), "原售后流程仍能建立确认卡");
        var session = context.getBean(CustomerServiceSessionStore.class).find("isolated-readonly").orElseThrow();
        String before = snapshot(session);
        for (JsonNode row : replay) {
            String message = row.path("question").asText();
            var raw = post(client, url, message);
            JsonNode response = IsolatedCancelCheck.JSON.readTree(raw.body()).path("data");
            check(raw.statusCode() == 200, "跨域响应HTTP正常：" + message);
            check(row.path("response").path("answer").asText().equals(response.path("answer").asText()), "Python最终正文原样消费：" + message);
            check(!"CONFIRM_SUBMIT".equals(response.path("pendingAction").asText()), "跨域插问不重发确认卡：" + message);
            check(before.equals(snapshot(session)), "跨域插问保留草稿字段与版本：" + message);
        }
        for (String message : List.of("发票多久开好？", "先查一下物流", "谢谢，我再想想", "旧契约：我再想想", "质量问题售后")) {
            JsonNode response = IsolatedCancelCheck.JSON.readTree(post(client, url, message).body()).path("data");
            check(!"CONFIRM_SUBMIT".equals(response.path("pendingAction").asText()), "插问不重发售后卡：" + message);
            check(before.equals(snapshot(session)), "业务草稿字段不变：" + message);
        }
        JsonNode control = IsolatedCancelCheck.JSON.readTree(post(client, url, "先不退了，发票政策是什么？").body()).path("data");
        check("TASK_CONTROL_PENDING".equals(control.path("pendingAction").asText()), "取消执行关闭时控制请求不冒充完成");
        check(before.equals(snapshot(session)), "控制提案不直接清空或重新准备草稿");
        check(!control.path("answer").asText().contains("已取消"), "没有取消回执就不宣称已取消");
        JsonNode faultControl = IsolatedCancelCheck.JSON.readTree(post(client, url, "先不退了，查询故障").body()).path("data");
        check("TASK_CONTROL_PENDING".equals(faultControl.path("pendingAction").asText()), "局部故障不跳过已有任务控制校验");
        check(before.equals(snapshot(session)), "故障与控制请求共存不修改草稿");
        JsonNode cancel = IsolatedCancelCheck.JSON.readTree(post(client, url, "不退了").body()).path("data");
        check("CANCELLED".equals(cancel.path("pendingAction").asText()), "原明确取消路径仍有效");
        check(session.isHandled() && "CANCELLED".equals(session.getPendingAction()), "业务层实际记录取消");
        post(client, url, "谢谢");
        check(session.isHandled() && "CANCELLED".equals(session.getPendingAction()), "后续闲聊不复活旧任务");
        JsonNode missing = IsolatedCancelCheck.JSON.readTree(post(client, url, "我要退款").body()).path("data");
        check("REFUND_ORDER_REQUIRED".equals(missing.path("pendingAction").asText()), "建立缺订单的退款待办");
        JsonNode refundSelection = IsolatedCancelCheck.JSON.readTree(post(client, url, "ORD9802").body()).path("data");
        check("REFUND_REASON_REQUIRED".equals(refundSelection.path("pendingAction").asText()), "退款焦点下纯订单号仍可续办");
        check("ORD9802".equals(session.getRelatedOrderNo()), "退款焦点下绑定用户补充的订单");
        post(client, url, "不退了");
        post(client, url, "我要退款");
        post(client, url, "先查物流");
        String missingSnapshot = snapshot(session);
        JsonNode querySelection = IsolatedCancelCheck.JSON.readTree(post(client, url, "ORD9801").body()).path("data");
        check("READ_ONLY_CONSULTATION".equals(querySelection.path("pendingAction").asText()), "物流补订单号先按当前查询焦点路由");
        check(missingSnapshot.equals(snapshot(session)), "物流补订单号不改变后台退款待办");
        // 原退款目标A、近期资格查询B、页面C；明确恢复A不能被B/C拦住。
        JsonNode original = IsolatedCancelCheck.JSON.readTree(post(client, url, "我要退款 ORD9801 原因：买错了").body()).path("data");
        check("CONFIRM_SUBMIT".equals(original.path("pendingAction").asText()), "建立完整的原退款确认卡");
        String originalDraft = snapshot(session);
        session.setAfterSaleQuery(Map.of("order_id", "ORD9802"));
        JsonNode mismatched = IsolatedCancelCheck.JSON.readTree(post(client, url, "继续原退款，换个对象", "ORD9803").body()).path("data");
        check("ELIGIBILITY_ORDER_REQUIRED".equals(mismatched.path("pendingAction").asText()), "恢复标签不能绕过与原订单不符的对象校验");
        check(originalDraft.equals(snapshot(session)), "对象不符时原草稿保持不变");
        JsonNode resume = IsolatedCancelCheck.JSON.readTree(post(client, url, "继续原退款", "ORD9803").body()).path("data");
        check(!"ELIGIBILITY_ORDER_REQUIRED".equals(resume.path("pendingAction").asText()), "明确恢复原任务不被最近查询与页面冲突误拦");
        check("ORD9801".equals(session.getRelatedOrderNo()), "恢复后保持原退款订单");
        check(originalDraft.equals(snapshot(session)), "恢复原退款不改变原业务字段与版本");

        // 过期任务的恢复由原拥有方拒绝，不能用新焦点绕过。
        session.setHandled(false);
        session.setPendingAction("CONFIRM_SUBMIT");
        session.setExpiresAt(java.time.LocalDateTime.now().minusMinutes(1));
        JsonNode expired = IsolatedCancelCheck.JSON.readTree(post(client, url, "不退了").body()).path("data");
        check("EXPIRED".equals(expired.path("pendingAction").asText()), "过期任务沿原校验处理");
    }

    static HttpResponse<String> post(HttpClient client, String url, String message) throws Exception {
        return post(client, url, message, null);
    }

    static HttpResponse<String> post(HttpClient client, String url, String message, String orderNo) throws Exception {
        var request = IsolatedCancelCheck.JSON.createObjectNode();
        request.put("sessionId", "isolated-readonly");
        request.put("message", message);
        if (orderNo != null) request.putObject("pageContext").put("orderNo", orderNo);
        String body = request.toString();
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)).build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
