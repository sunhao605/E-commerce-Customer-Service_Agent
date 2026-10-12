package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.web.bind.annotation.*;

/** 仅隔离验收编译：复用H2样本，固定Agent刻意将未支持业务判为退款。 */
public class HumanSupportCheckServer {
    static final AtomicInteger agentCalls = new AtomicInteger();

    public static void main(String[] args) throws Exception {
        CheckServer.main(args);
        var store = CheckServer.context.getBean(CustomerServiceSessionStore.class);
        var submitted = store.getOrCreate("submitted-synthetic", "U1001");
        submitted.setPendingAction("SUBMITTED");
        submitted.setHandled(true);
        submitted.setRelatedOrderNo("ORD9801");
        submitted.setRelatedAfterSaleNo("EXISTING-SYNTHETIC");
        store.save(submitted);
        System.out.println("HUMAN_SUPPORT_CHECK_READY");
    }

    @Configuration
    static class FixedAgent {
        @Bean
        @Primary
        CustomerServiceAgentClient fixedHumanSupportAgent() {
            return new CustomerServiceAgentClient() {
                public JsonNode chat(AgentChatRequest request) {
                    agentCalls.incrementAndGet();
                    String message = String.valueOf(request.getRuntimeContext().get("userMessage"));
                    var result = CheckServer.JSON.createObjectNode();
                    result.put("answer", "本次尚未提交，请核对业务信息。");
                    result.put("next_action", "answer_user");
                    var state = result.putObject("session_state");
                    state.put("intent", message.contains("七天无理由") ? "return_request" : "refund_request");
                    String order = message.contains("ORD9803") ? "ORD9803" : "ORD9801";
                    state.putObject("context_report").put("chosen_order_id", order);
                    if (message.contains("核验失败样本")) {
                        result.put("answer", "这次信息暂时无法核实，请人工核验。");
                        result.put("next_action", "transfer_to_human");
                        state.put("intent", "low_confidence_query");
                    }
                    if (message.contains("审批样本")) {
                        result.put("next_action", "transfer_to_human");
                        var workflow = state.putObject("workflow");
                        workflow.put("status", "paused");
                        workflow.put("pending_action", "require_approval");
                        workflow.put("workflow_id", "fixed-approval");
                        workflow.put("resume_token", "fixed-approval-token");
                        workflow.put("workflow_type", "unshipped_refund");
                        workflow.put("order_id", "ORD9801");
                    }
                    return result;
                }
                public JsonNode resume(AgentResumeRequest request) {
                    throw new AssertionError("不应调用Agent恢复或真人服务");
                }
            };
        }
    }

    @RestController
    static class Calls {
        @GetMapping("/api/check/agent-calls")
        Map<String, Integer> calls() { return Map.of("count", agentCalls.get()); }
    }
}
