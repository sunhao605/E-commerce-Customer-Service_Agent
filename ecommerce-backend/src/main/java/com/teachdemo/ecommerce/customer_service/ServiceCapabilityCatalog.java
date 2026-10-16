package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * 当前客服渠道的业务能力目录。只读，不执行工具或业务写入。
 * inventory_query 对应商品价格库存读取；stock_notification 没有登记接口；
 * exchange_handling 与 human_takeover 只到占位人工入口，没有换货单或真人接手；
 * refund_consultation 与 return_consultation 只说明办理范围，不核验订单资格。
 */
public final class ServiceCapabilityCatalog {

    public static final String RESOURCE = "/service-capabilities.json";
    private static final Pattern ID = Pattern.compile("[a-z][a-z0-9_]{0,63}");
    private static final Pattern EXPLICIT_APPLY = Pattern.compile(
        "(?:我要|我想|帮我|给我|现在|直接|那就)?(?:申请|办理|退款吧|退货吧)");
    private static final Set<String> STATUSES = Set.of("supported", "unsupported", "unknown");
    private static final List<String> APPLY_NEGATIONS = List.of("不要申请", "先别申请", "暂不申请", "不申请", "别申请");
    private static final String[] REMINDER_CUES = {
        "到货提醒", "补货提醒", "到货通知", "补货通知", "有货后提醒", "有货后通知", "提醒我", "通知我"
    };
    private static final String[] REMINDER_NEGATIVES = {"不要", "不用", "不需要", "无需"};

    private static final String VERSION;
    private static final String CHANNEL;
    private static final Map<String, ServiceCapabilityResponse> ITEMS;
    private static final String INTERACTION_SCHEMA = "interaction_policy_v1";
    private static final Set<String> ENTRY_STATUSES = Set.of("available", "unavailable");
    private static final Set<String> ENTRY_ACTORS = Set.of("user", "assistant");
    private static final Set<String> ENTRY_EFFECTS = Set.of("open_entry");
    private static final InteractionPolicyResponse INTERACTION_POLICY;

    static {
        ObjectMapper mapper = new ObjectMapper();
        try (InputStream input = ServiceCapabilityCatalog.class.getResourceAsStream(RESOURCE)) {
            if (input == null) {
                throw new IllegalStateException("missing " + RESOURCE);
            }
            JsonNode root = mapper.readTree(input);
            VERSION = root.path("version").asText();
            CHANNEL = root.path("channel").asText();
            if (VERSION.isBlank() || CHANNEL.isBlank()) {
                throw new IllegalStateException("capability catalog is missing version or channel");
            }
            Map<String, ServiceCapabilityResponse> items = new LinkedHashMap<>();
            for (JsonNode node : root.path("capabilities")) {
                String capabilityId = node.path("capabilityId").asText();
                String supportStatus = node.path("supportStatus").asText();
                if (!validId(capabilityId) || !STATUSES.contains(supportStatus) || "unknown".equals(supportStatus)) {
                    throw new IllegalStateException("invalid capability entry: " + capabilityId);
                }
                List<String> alternatives = new ArrayList<>();
                for (JsonNode alternative : node.path("alternatives")) {
                    alternatives.add(alternative.asText());
                }
                items.put(capabilityId, new ServiceCapabilityResponse(
                    capabilityId,
                    CHANNEL,
                    supportStatus,
                    node.path("scope").asText(),
                    node.path("explanation").asText(),
                    List.copyOf(alternatives),
                    VERSION
                ));
            }
            if (items.isEmpty()) {
                throw new IllegalStateException("capability catalog is empty");
            }
            ITEMS = Map.copyOf(items);
            INTERACTION_POLICY = loadInteractionPolicy(root.path("interactionPolicy"));
        } catch (IOException ex) {
            throw new IllegalStateException("cannot read " + RESOURCE, ex);
        }
    }

    private static InteractionPolicyResponse loadInteractionPolicy(JsonNode node) {
        if (node == null || node.isMissingNode() || node.isNull()) {
            return new InteractionPolicyResponse(INTERACTION_SCHEMA, CHANNEL, VERSION, List.of());
        }
        if (!INTERACTION_SCHEMA.equals(node.path("schemaVersion").asText())) {
            throw new IllegalStateException("unknown interaction policy schema: " + node.path("schemaVersion").asText());
        }
        if (!CHANNEL.equals(node.path("channel").asText()) || node.path("version").asText().isBlank()) {
            throw new IllegalStateException("interaction policy is missing channel or version");
        }
        List<InteractionPolicyEntry> entries = new ArrayList<>();
        Set<String> seen = new java.util.HashSet<>();
        for (JsonNode entry : node.path("entries")) {
            String id = entry.path("id").asText();
            String status = entry.path("status").asText();
            String actor = entry.path("actor").asText();
            String effect = entry.path("completionEffect").asText();
            if (!validId(id) || !seen.add(id) || !ENTRY_STATUSES.contains(status)
                    || !ENTRY_ACTORS.contains(actor) || !ENTRY_EFFECTS.contains(effect)
                    || entry.path("surface").asText().isBlank()
                    || entry.path("initiation").asText().isBlank()
                    || entry.path("destinationState").asText().isBlank()
                    || !entry.path("confirmationRequired").isBoolean()) {
                throw new IllegalStateException("invalid interaction policy entry: " + id);
            }
            entries.add(new InteractionPolicyEntry(
                id,
                entry.path("surface").asText(),
                status,
                actor,
                entry.path("initiation").asText(),
                entry.path("confirmationRequired").asBoolean(),
                effect,
                entry.path("destinationState").asText()
            ));
        }
        return new InteractionPolicyResponse(INTERACTION_SCHEMA, CHANNEL,
            node.path("version").asText(), List.copyOf(entries));
    }

    private ServiceCapabilityCatalog() {
    }

    public static boolean validId(String capabilityId) {
        return capabilityId != null && ID.matcher(capabilityId).matches();
    }

    public static InteractionPolicyResponse interactionPolicy() {
        return INTERACTION_POLICY;
    }

    public static ServiceCapabilityResponse get(String capabilityId) {
        ServiceCapabilityResponse found = ITEMS.get(capabilityId);
        if (found != null) {
            return found;
        }
        return new ServiceCapabilityResponse(
            capabilityId,
            CHANNEL,
            "unknown",
            "未登记的业务能力",
            "目前还不能确认是否提供这项服务。",
            List.of(),
            VERSION
        );
    }

    /** 能力咨询只回答范围，不进入换货/人工占位卡，也不准备售后申请。 */
    public static boolean isCapabilityConsultation(String message) {
        if (message == null || message.isBlank() || explicitApply(message) || mentionsSpecificOrder(message)) {
            return false;
        }
        if (wantsReminder(message)) {
            return true;
        }
        if (!message.matches("(?s).*(?:支不支持|是否支持|能不能|能否|可不可以|支持[^。！？?\\n]{0,16}吗).*")) {
            return false;
        }
        return message.matches("(?s).*(?:查库存|库存查询|查询库存|库存|换货|换一件|换新|换一个|转人工|转接人工|人工客服|真人客服|找人工|真人|退款|退钱|退货).*");
    }

    private static boolean mentionsSpecificOrder(String message) {
        return message.matches("(?s).*(?:这笔|这单|这个订单|资格|SO[A-Za-z0-9_-]{6,}|ORD\\d{4,}).*");
    }

    private static boolean explicitApply(String message) {
        String text = message;
        for (String phrase : APPLY_NEGATIONS) {
            text = text.replace(phrase, "");
        }
        return EXPLICIT_APPLY.matcher(text).find();
    }

    private static boolean wantsReminder(String message) {
        for (String cue : REMINDER_CUES) {
            if (!message.contains(cue)) {
                continue;
            }
            boolean negated = false;
            for (String negative : REMINDER_NEGATIVES) {
                if (message.contains(negative + cue)) {
                    negated = true;
                    break;
                }
            }
            if (!negated) {
                return true;
            }
        }
        return false;
    }
}
