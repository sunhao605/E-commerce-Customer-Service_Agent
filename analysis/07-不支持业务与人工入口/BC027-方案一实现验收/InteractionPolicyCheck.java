package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.List;

/** BC-027方案一：新交互策略GET的隔离验证。内存H2，不触日常MySQL/Java。 */
public class InteractionPolicyCheck {
    static final List<String[]> CHECKS = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        CheckServer.main(args);
        String base = "http://127.0.0.1:18102";
        HttpClient client = HttpClient.newHttpClient();

        // 1. 无身份：interaction-policy 与单项能力 GET 均拒绝（鉴权不降低）
        HttpResponse<String> anon = get(client, base + "/api/customer-service/capabilities/interaction-policy", null);
        check(anon.statusCode() == 401, "anonymous interaction-policy is 401: " + anon.statusCode());
        HttpResponse<String> anonCap = get(client, base + "/api/customer-service/capabilities/human_takeover", null);
        check(anonCap.statusCode() == 401, "anonymous capability GET is 401: " + anonCap.statusCode());

        // 2. AGENT_SERVICE 身份：新路由返回策略且字段完整
        HttpResponse<String> policy = get(client, base + "/api/customer-service/capabilities/interaction-policy", "course-debug-agent-service");
        check(policy.statusCode() == 200, "interaction-policy GET 200: " + policy.statusCode());
        JsonNode body = CheckServer.JSON.readTree(policy.body());
        check(body.path("success").asBoolean(), "interaction-policy success=true");
        JsonNode data = body.path("data");
        check("interaction_policy_v1".equals(data.path("schemaVersion").asText()), "schemaVersion=interaction_policy_v1");
        check("customer_service_agent".equals(data.path("channel").asText()), "channel=customer_service_agent");
        check(!data.path("version").asText().isBlank(), "version present");
        JsonNode entries = data.path("entries");
        check(entries.isArray() && entries.size() == 1, "one entry");
        JsonNode entry = entries.get(0);
        check("human_support".equals(entry.path("id").asText()), "entry id=human_support");
        check("mall_chat".equals(entry.path("surface").asText()), "surface=mall_chat");
        check("available".equals(entry.path("status").asText()), "status=available");
        check("user".equals(entry.path("actor").asText()), "actor=user");
        check("message_request".equals(entry.path("initiation").asText()), "initiation=message_request");
        check(entry.path("confirmationRequired").isBoolean() && entry.path("confirmationRequired").asBoolean(), "confirmationRequired=true");
        check("open_entry".equals(entry.path("completionEffect").asText()), "completionEffect=open_entry");
        check("placeholder".equals(entry.path("destinationState").asText()), "destinationState=placeholder");

        // 3. 字面量路由不吞单项能力ID；原GET响应兼容
        HttpResponse<String> cap = get(client, base + "/api/customer-service/capabilities/human_takeover", "course-debug-agent-service");
        JsonNode capBody = CheckServer.JSON.readTree(cap.body());
        check(cap.statusCode() == 200 && "unsupported".equals(capBody.path("data").path("supportStatus").asText()),
            "human_takeover still resolves via /{capabilityId}: " + cap.statusCode());
        HttpResponse<String> unknown = get(client, base + "/api/customer-service/capabilities/not_registered", "course-debug-agent-service");
        JsonNode unknownBody = CheckServer.JSON.readTree(unknown.body());
        check("unknown".equals(unknownBody.path("data").path("supportStatus").asText()), "unknown capability id still unknown");
        HttpResponse<String> bad = get(client, base + "/api/customer-service/capabilities/../products", "course-debug-agent-service");
        check(bad.statusCode() == 400, "invalid id still 400: " + bad.statusCode());

        System.out.println("INTERACTION_POLICY_CHECK_READY");
        long failed = CHECKS.stream().filter(c -> !"1".equals(c[0])).count();
        System.out.println("checks=" + CHECKS.size() + " failed=" + failed);
        for (String[] c : CHECKS) {
            System.out.println(("1".equals(c[0]) ? "PASS " : "FAIL ") + c[1]);
        }
        System.exit(failed == 0 ? 0 : 1);
    }

    static HttpResponse<String> get(HttpClient client, String url, String token) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(url)).GET();
        if (token != null) {
            builder.header("X-Agent-Service-Token", token);
        }
        return client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    static void check(boolean ok, String label) {
        CHECKS.add(new String[]{ok ? "1" : "0", label});
    }
}
