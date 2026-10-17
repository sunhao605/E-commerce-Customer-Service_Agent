package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;

import com.fasterxml.jackson.databind.node.MissingNode;

import com.teachdemo.ecommerce.account.AccountPrincipal;

import com.teachdemo.ecommerce.account.UserProfile;

import com.teachdemo.ecommerce.account.UserProfileRepository;

import com.teachdemo.ecommerce.aftersale.AfterSaleRequest;

import com.teachdemo.ecommerce.aftersale.AfterSaleRequestRepository;

import com.teachdemo.ecommerce.catalog.ProductRepository;

import com.teachdemo.ecommerce.customer_service.AgentChatRequest;

import com.teachdemo.ecommerce.customer_service.CustomerServiceActionResponse;

import com.teachdemo.ecommerce.customer_service.CustomerServiceChatRequest;

import com.teachdemo.ecommerce.customer_service.CustomerServicePageContextRequest;

import com.teachdemo.ecommerce.customer_service.CustomerServiceResponse;

import com.teachdemo.ecommerce.customer_service.CustomerServiceResumeRequest;

import com.teachdemo.ecommerce.customer_service.CustomerServiceSession;

import com.teachdemo.ecommerce.customer_service.CustomerServiceSessionStore;

import com.teachdemo.ecommerce.customer_service.RuntimeOrderContextResponse;

import com.teachdemo.ecommerce.order.OrderEntity;

import com.teachdemo.ecommerce.order.OrderRepository;
import com.teachdemo.ecommerce.order.ShopService;
import com.teachdemo.ecommerce.order.ShopOrderResponse;
import com.teachdemo.ecommerce.aftersale.ShopAfterSaleResponse;
import com.teachdemo.ecommerce.common.BusinessException;

import java.time.LocalDateTime;

import java.util.ArrayList;
import java.util.HashMap;

import java.util.List;

import java.util.Locale;

import java.util.Map;

import java.util.Set;

import java.util.UUID;
import java.util.Objects;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.springframework.http.HttpStatus;

import org.springframework.stereotype.Service;

import org.springframework.util.StringUtils;

@Service
public class CustomerServiceGatewayService {

    public static final String FALLBACK_ANSWER = "客服服务暂时繁忙，您可以稍后再试，或联系人工客服继续处理。";

    private static final Set<String> PAGE_TYPES = Set.of(
        "HOME", "PRODUCT_DETAIL", "CART", "ORDER_LIST", "ORDER_DETAIL", "BALANCE", "AFTER_SALE");

    private final CustomerServiceAgentClient agentClient;
    private final CustomerServiceSessionStore sessionStore;
    private final UserProfileRepository userProfileRepository;
    private final OrderRepository orderRepository;
    private final AfterSaleRequestRepository afterSaleRequestRepository;
    private final ProductRepository productRepository;
    private final RuntimeOrderContextService runtimeOrderContextService;
    private final ShopService shopService;
    private static final Pattern ORDER_NO = Pattern.compile("\\b(?:SO[A-Za-z0-9_-]{6,}|ORD\\d{4,})\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern REFUND_REASON = Pattern.compile("(?:退款原因|原因)\\s*[:：]\\s*(.+)|因为\\s*(.+)");
    // shortcut: 只接收独立、明确的取消句；复合请求需要另行定义取消对象和新任务。
    private static final Pattern CANCEL_DRAFT = Pattern.compile("(?:算了[，,\\s]*)?(?:取消本次申请|不退了|不申请了|先不退了|不退款了|不退货了|不提交了)[。！!\\s]*");

    public CustomerServiceGatewayService(CustomerServiceAgentClient agentClient,
                                         CustomerServiceSessionStore sessionStore,
                                         UserProfileRepository userProfileRepository,
                                         OrderRepository orderRepository,
                                         AfterSaleRequestRepository afterSaleRequestRepository,
                                         ProductRepository productRepository,
                                         RuntimeOrderContextService runtimeOrderContextService,
                                         ShopService shopService) {
        this.agentClient = agentClient;
        this.sessionStore = sessionStore;
        this.userProfileRepository = userProfileRepository;
        this.orderRepository = orderRepository;
        this.afterSaleRequestRepository = afterSaleRequestRepository;
        this.productRepository = productRepository;
        this.runtimeOrderContextService = runtimeOrderContextService;
        this.shopService = shopService;
    }

    public CustomerServiceResponse chat(CustomerServiceChatRequest request, AccountPrincipal principal) {
        UserProfile profile = loadProfile(principal);
        CustomerServicePageContextRequest pageContext = normalizeAndValidatePageContext(request.getPageContext(), profile.getUserId());
        String sessionId = StringUtils.hasText(request.getSessionId())
            ? request.getSessionId().trim()
            : "cs-" + UUID.randomUUID();
        CustomerServiceSession session = sessionStore.getOrCreate(sessionId, profile.getUserId());
        synchronized (session) {
            assertSessionOwner(session, profile.getUserId());
            session.setCancellationJudged(false);
            String message = request.getMessage();
            String unsupported = unsupportedServiceReason(message);
            if ((unsupported != null || wantsHumanSupport(message))
                && !ServiceCapabilityCatalog.isCapabilityConsultation(message)) {
                CustomerServiceResponse response = humanSupportResponse(session,
                    unsupported == null ? "你希望联系人工客服。" : "你希望咨询或办理" + unsupported + "，当前客服助手暂不能办理这类业务。");
                rememberTurn(session, message, response.getAnswer());
                return response;
            }
            if (session.getAfterSaleQuery() != null && message.trim().matches("(?:好的|好|可以|知道了|先不办|暂时不办)[。！!\\s]*")) {
                if (message.contains("不办")) session.setAfterSaleQuery(null);
                CustomerServiceResponse response = new CustomerServiceResponse("好的，这次只咨询，没有准备新的申请。需要办理时请明确提出申请。",
                    sessionId, false, "ELIGIBILITY_QUERY", null, null, null, null, null, List.of(), false);
                rememberTurn(session, message, response.getAnswer());
                return response;
            }
            // 独立取消和已知办理的纯订单输入仍走原续接；查询选单不走办理续接。
            if (CANCEL_DRAFT.matcher(message.trim()).matches() || session.getAfterSaleQuery() == null
                && "REFUND_ORDER_REQUIRED".equals(session.getPendingAction()) && ORDER_NO.matcher(message.trim()).matches()) {
                CustomerServiceResponse followup = refundFollowup(session, message, pageContext, profile.getUserId(), false);
                if (followup != null) {
                    rememberTurn(session, message, followup.getAnswer());
                    return followup;
                }
            }
            Map<String, Object> runtimeContext = buildRuntimeContext(profile, pageContext, request.getMessage());
            runtimeContext.put("mallAfterSaleRouting", true);
            runtimeContext.put("afterSaleQuery", session.getAfterSaleQuery());
            if (session.getPendingAction() != null && !session.isHandled()) {
                runtimeContext.put("afterSaleOpenTask", Map.of("intent", "RETURN".equals(session.getActionType()) ? "return_request" : "refund_request",
                    "pending_slot", "REFUND_ORDER_REQUIRED".equals(session.getPendingAction()) ? "order_id" : "reason"));
            }
            runtimeContext.put("cancelledAfterSaleDraft", "CANCELLED".equals(session.getPendingAction()));
            runtimeContext.put("afterSaleDraft", afterSaleDraftSummary(session));
            if (session.isCancellationJudged()) {
                runtimeContext.put("cancellationJudged", true);
            }
            if (!session.recentDialogue().isEmpty()) {
                runtimeContext.put("recentDialogue", session.recentDialogue());
            }
            AgentChatRequest agentRequest = new AgentChatRequest(
                sessionId,
                profile.getUserId(),
                profile.getNickname(),
                profile.getMemberLevel(),
                profile.getRiskLevel(),
                buildAgentMessage(message, pageContext, profile.getUserId()),
                runtimeContext
            );
            try {
                JsonNode agentResponse = agentClient.chat(agentRequest);
                boolean taskControlRequested = agentResponse != null
                    && agentResponse.path("session_state").path("task_control_requested").asBoolean(false);
                if (!taskControlRequested && agentResponse != null && "transfer_to_human".equals(agentResponse.path("next_action").asText())
                    && !isApprovalRequired(workflowNode(agentResponse))) {
                    CustomerServiceResponse response = humanSupportResponse(session,
                        text(agentResponse, "answer", "当前问题需要人工进一步核验。"));
                    rememberTurn(session, message, response.getAnswer());
                    return response;
                }
                if (!taskControlRequested && agentResponse != null && "after_sale_eligibility_query".equals(agentResponse.path("session_state").path("intent").asText())) {
                    CustomerServiceResponse response = eligibilityQueryResponse(agentResponse, session);
                    rememberTurn(session, message, response.getAnswer());
                    return response;
                }
                // 只读商品咨询直接返回：不续接旧退款、不重发待确认卡，也不改动任何售后会话状态。
                if (!taskControlRequested && agentResponse != null && "product_query".equals(agentResponse.path("session_state").path("intent").asText())) {
                    CustomerServiceResponse response = new CustomerServiceResponse(text(agentResponse, "answer", FALLBACK_ANSWER),
                        sessionId, false, "PRODUCT_QUERY", null, null, null, null, null, List.of(), false);
                    rememberTurn(session, message, response.getAnswer());
                    return response;
                }
                // 能力咨询只返回查询结果：不准备申请，不重发待确认卡，也不清掉原草稿。
                if (!taskControlRequested && agentResponse != null && ("service_scope_query".equals(agentResponse.path("session_state").path("intent").asText())
                    || ServiceCapabilityCatalog.isCapabilityConsultation(message))) {
                    CustomerServiceResponse response = new CustomerServiceResponse(text(agentResponse, "answer", FALLBACK_ANSWER),
                        sessionId, false, "CAPABILITY_QUERY", null, null, null, null, null, List.of(), false);
                    rememberTurn(session, message, response.getAnswer());
                    return response;
                }
                // MULTI-READ-001：Python 代码校验的纯只读轮次（组合咨询、插问）直接返回，
                // 不进入退款原因提取、草稿续办，也不重发已有待确认卡。
                if (agentResponse != null && isReadOnlyTurn(agentResponse)) {
                    CustomerServiceResponse response = new CustomerServiceResponse(text(agentResponse, "answer", FALLBACK_ANSWER),
                        sessionId, false, "READ_ONLY_CONSULTATION", null, null, null, null, null, List.of(), false);
                    rememberTurn(session, message, response.getAnswer());
                    return response;
                }
                String followupMessage = message;
                String selected = agentResponse == null ? null : text(agentResponse.path("session_state").path("context_report"), "chosen_order_id", null);
                String intent = agentResponse == null ? null : text(agentResponse.path("session_state"), "intent", null);
                if (Set.of("refund_request", "return_request").contains(intent == null ? "" : intent) && selected != null
                    && extractOrderNo(message) == null) {
                    followupMessage += " " + selected;
                }
                if (Set.of("refund_request", "return_request").contains(intent == null ? "" : intent)
                    && session.getAfterSaleQuery() != null && extractOrderNo(message) == null && pageContext != null
                    && pageContext.getOrderNo() != null && session.getAfterSaleQuery().get("order_id") != null
                    && !Objects.equals(pageContext.getOrderNo(), session.getAfterSaleQuery().get("order_id"))) {
                    CustomerServiceResponse response = new CustomerServiceResponse("当前页面订单与刚查询的订单不同，请明确要申请哪一笔订单。尚未准备新申请。",
                        sessionId, false, "ELIGIBILITY_ORDER_REQUIRED", null, null, null, null, null, List.of(), false);
                    rememberTurn(session, message, response.getAnswer());
                    return response;
                }
                CustomerServiceResponse followup = refundFollowup(session, followupMessage, pageContext, profile.getUserId(), taskControlRequested);
                if (followup != null) {
                    rememberTurn(session, message, followup.getAnswer());
                    return followup;
                }
                if (taskControlRequested) {
                    CustomerServiceResponse response = new CustomerServiceResponse(text(agentResponse, "answer", FALLBACK_ANSWER),
                        sessionId, false, "TASK_CONTROL_PENDING", null, null, null, null, null, List.of(), false);
                    rememberTurn(session, message, response.getAnswer());
                    return response;
                }
                CustomerServiceResponse response = sanitizeChatResponse(agentResponse, session, pageContext, profile.getUserId(), request.getMessage());
                rememberTurn(session, request.getMessage(), response.getAnswer());
                return response;
            } catch (RuntimeException ex) {
                sessionStore.save(session);
                CustomerServiceResponse response = fallback(sessionId);
                rememberTurn(session, request.getMessage(), response.getAnswer());
                return response;
            }
        }
    }

    public CustomerServiceResponse resume(CustomerServiceResumeRequest request, AccountPrincipal principal) {
        UserProfile profile = loadProfile(principal);
        CustomerServiceSession session = sessionStore.find(request.getSessionId())
            .orElseThrow(() -> new CustomerServiceException(HttpStatus.NOT_FOUND, "NOT_FOUND", "客服会话不存在"));
        synchronized (session) {
            assertSessionOwner(session, profile.getUserId());
            String decision = request.getDecision().trim().toUpperCase(Locale.ROOT);
            if ("KEEP".equals(decision) || "CANCEL_DRAFT".equals(decision)) {
                return resumeConflict(session, request, decision);
            }
            if (!Set.of("CONFIRM_SUBMIT", "CANCEL_SUBMIT").contains(decision)) {
                throw new CustomerServiceException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "decision 只支持 CONFIRM_SUBMIT、CANCEL_SUBMIT、KEEP 或 CANCEL_DRAFT");
            }
            if ("CONFIRM_SUBMIT".equals(decision) && session.getConflictToken() != null
                && session.getConflictToken().equals(request.getResumeToken())) {
                throw new CustomerServiceException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "这张卡片不能提交申请");
            }
            validateResumeSession(session, request);
            if (session.isHandled()) {
                if ("CONFIRM_SUBMIT".equals(decision) && session.getRelatedAfterSaleNo() != null) {
                    AfterSaleRequest existing = afterSaleRequestRepository
                        .findByRequestIdAndUserId(session.getRelatedAfterSaleNo(), profile.getUserId())
                        .orElseThrow(() -> new CustomerServiceException(HttpStatus.NOT_FOUND, "NOT_FOUND", "申请记录暂时无法核实"));
                    return submittedResponse(session, existing.getRequestId(), existing.getStatus());
                }
                throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "本次确认已处理，旧卡不可用");
            }
            if ("CANCEL_SUBMIT".equals(decision)) {
                return cancelPendingRefund(session);
            }
            try {
                ShopAfterSaleResponse application = "RETURN".equals(session.getActionType())
                    ? shopService.submitCustomerReturn(profile.getUserId(), session.getRelatedOrderNo(), session.getRefundAmount())
                    : shopService.submitCustomerRefund(profile.getUserId(), session.getRelatedOrderNo(),
                        session.getRefundReason(), session.getRefundAmount());
                session.setRelatedAfterSaleNo(application.requestNo());
                session.setHandled(true);
                sessionStore.save(session);
                return submittedResponse(session, application.requestNo(), application.status());
            } catch (BusinessException ex) {
                session.setHandled(true);
                sessionStore.save(session);
                throw ex;
            } catch (RuntimeException ex) {
                return fallback(session.getSessionId());
            }
        }
    }

    private UserProfile loadProfile(AccountPrincipal principal) {
        return userProfileRepository.findByUserId(principal.getUserId())
            .orElseThrow(() -> new CustomerServiceException(HttpStatus.NOT_FOUND, "NOT_FOUND", "用户资料不存在"));
    }

    private CustomerServicePageContextRequest normalizeAndValidatePageContext(CustomerServicePageContextRequest pageContext,
                                                                             String userId) {
        if (pageContext == null) {
            return null;
        }
        if (StringUtils.hasText(pageContext.getType())) {
            String type = normalizePageType(pageContext.getType());
            if (!PAGE_TYPES.contains(type)) {
                throw new CustomerServiceException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "页面上下文类型不合法");
            }
            pageContext.setType(type);
        }
        if (pageContext.getProductId() != null && !productRepository.existsById(pageContext.getProductId())) {
            throw new CustomerServiceException(HttpStatus.NOT_FOUND, "NOT_FOUND", "关联商品不存在");
        }
        validateOrderOwnership(pageContext.getOrderNo(), userId);
        validateAfterSaleOwnership(pageContext.getAfterSaleNo(), userId);
        return pageContext;
    }

    private static String normalizePageType(String type) {
        String normalized = type.trim().toUpperCase(Locale.ROOT);
        return switch (normalized) {
            case "PRODUCT" -> "PRODUCT_DETAIL";
            case "ORDER" -> "ORDER_DETAIL";
            default -> normalized;
        };
    }

    private void validateOrderOwnership(String orderNo, String userId) {
        if (!StringUtils.hasText(orderNo)) {
            return;
        }
        OrderEntity order = orderRepository.findByOrderNo(orderNo.trim())
            .orElseThrow(() -> new CustomerServiceException(HttpStatus.NOT_FOUND, "NOT_FOUND", "关联订单不存在"));
        if (!userId.equals(order.getUserId())) {
            throw new CustomerServiceException(HttpStatus.FORBIDDEN, "FORBIDDEN", "关联订单不属于当前用户");
        }
    }

    private void validateAfterSaleOwnership(String afterSaleNo, String userId) {
        if (!StringUtils.hasText(afterSaleNo)) {
            return;
        }
        AfterSaleRequest afterSale = afterSaleRequestRepository.findByRequestId(afterSaleNo.trim())
            .orElseThrow(() -> new CustomerServiceException(HttpStatus.NOT_FOUND, "NOT_FOUND", "关联售后申请不存在"));
        if (!userId.equals(afterSale.getUserId())) {
            throw new CustomerServiceException(HttpStatus.FORBIDDEN, "FORBIDDEN", "关联售后申请不属于当前用户");
        }
    }

    private Map<String, Object> buildRuntimeContext(UserProfile profile, CustomerServicePageContextRequest pageContext, String userMessage) {
        Map<String, Object> context = new HashMap<>();
        // 保留用户原句，避免追加的页面订单号覆盖“第一笔”等明确选择。
        context.put("userMessage", userMessage);
        context.put("userId", profile.getUserId());
        context.put("nickname", profile.getNickname());
        context.put("mobile", profile.getMobile());
        context.put("memberLevel", profile.getMemberLevel());
        context.put("riskLevel", profile.getRiskLevel());
        RuntimeOrderContextResponse orderContext =
            runtimeOrderContextService.loadCurrentUserOrders(profile.getUserId(), null, null);
        context.put("currentUserOrders", orderContext.orders());
        context.put("currentUserOrdersTruncated", orderContext.truncated());
        if (pageContext != null) {
            context.put("currentPage", pageContext.getType());
            context.put("relatedProductId", pageContext.getProductId());
            context.put("relatedOrderNo", trimToNull(pageContext.getOrderNo()));
            context.put("relatedAfterSaleNo", trimToNull(pageContext.getAfterSaleNo()));
            List<Map<String, String>> needMoreInfoRequests = needMoreInfoRequests(pageContext, profile.getUserId()).stream()
                .map(request -> {
                    Map<String, String> item = new HashMap<>();
                    item.put("requestId", request.getRequestId());
                    item.put("orderNo", request.getOrderNo());
                    item.put("requestType", request.getRequestType());
                    item.put("status", request.getStatus());
                    item.put("handlingNote", request.getHandlingNote());
                    return item;
                })
                .toList();
            if (!needMoreInfoRequests.isEmpty()) {
                context.put("relatedNeedMoreInfoAfterSales", needMoreInfoRequests);
            }
        }
        return context;
    }

    enum CancelFollowup { CANCEL, PASS }

    static CancelFollowup decideFollowup(boolean exactSentence, JsonNode judgment, boolean versionMatches) {
        if (judgment == null || judgment.isNull() || judgment.isMissingNode()) {
            return exactSentence ? CancelFollowup.CANCEL : CancelFollowup.PASS;
        }
        if (judgment.path("executed").asBoolean(false) && versionMatches) {
            return CancelFollowup.CANCEL;
        }
        if ("keep".equals(judgment.path("action").asText(""))) {
            return CancelFollowup.PASS;
        }
        if (exactSentence) {
            return CancelFollowup.CANCEL;
        }
        return CancelFollowup.PASS;
    }

    private JsonNode cancellationJudgment(CustomerServiceSession session, String message) {
        if (!"1".equals(System.getenv("CANCEL_INTENT_EXECUTE"))) {
            return null;
        }
        session.setCancellationJudged(true);
        Map<String, Object> runtime = new HashMap<>();
        runtime.put("userMessage", message);
        runtime.put("recentDialogue", session.recentDialogue());
        runtime.put("afterSaleDraft", afterSaleDraftSummary(session));
        if (session.getObjectWait() != null && !session.getObjectWait().isEmpty()) {
            runtime.put("objectWait", session.getObjectWait());
        }
        Map<String, Object> body = new HashMap<>();
        body.put("session_id", session.getSessionId());
        body.put("runtime_user_id", session.getUserId());
        body.put("user_message", message);
        body.put("runtime_context", runtime);
        try {
            return agentClient.judgeCancellation(body);
        } catch (RuntimeException ex) {
            return null;
        }
    }

    private static boolean draftVersionMatches(CustomerServiceSession session, JsonNode judgment) {
        String expected = judgment.path("draft_version").asText("");
        return !expected.isEmpty() && expected.equals(Integer.toString(session.getDraftRevision()));
    }

    private CustomerServiceResponse clarificationReply(CustomerServiceSession session, JsonNode judgment, boolean exactCancel) {
        if (judgment == null || judgment.isNull()) {
            return null;
        }
        String action = judgment.path("action").asText("");
        String reply = judgment.path("reply").asText("");
        boolean shipping = "keep_unknown_fact".equals(action);
        if ("confirm".equals(action)) {
            return conflictCard(session, StringUtils.hasText(reply) ? reply : "请选择保留当前办理，或取消这次尚未提交的售后申请。");
        }
        if ("score_low".equals(action) || ("object_noted".equals(action)
            && "below_threshold".equals(judgment.path("reason").asText()))) {
            session.setObjectWait(null);
            String kind = "RETURN".equals(session.getActionType()) ? "退货退款" : "退款";
            return conflictCard(session, StringUtils.hasText(reply) ? reply : "是否停止本次尚未提交的" + kind + "申请？");
        }
        boolean clarify = Set.of("ask_object", "ask_decision", "keep_other", "candidates_changed", "object_noted", "hold_review").contains(action);
        if (!shipping && (exactCancel || !clarify)) {
            return null;
        }
        if (!StringUtils.hasText(reply)) {
            return null;
        }
        session.setObjectWait(judgment.path("object_wait").isObject() ? readObjectWait(judgment.path("object_wait")) : null);
        return plainResponse(session, reply, action.toUpperCase(Locale.ROOT));
    }

    private CustomerServiceResponse conflictCard(CustomerServiceSession session, String reply) {
        String token = UUID.randomUUID().toString();
        session.setResumeToken(token);
        session.setConflictToken(token);
        session.setConflictRevision(session.getDraftRevision());
        String order = session.getRelatedOrderNo();
        String summary;
        String cancelLabel;
        if (StringUtils.hasText(order)) {
            String product = session.getRefundItemSummary();
            String type = "RETURN".equals(session.getActionType()) ? "退货退款" : "退款";
            summary = "订单：" + order
                + (StringUtils.hasText(product) ? "；商品：" + product : "")
                + "；售后类型：" + type
                + "。请选择保留当前办理，或取消这次尚未提交的售后申请。";
            cancelLabel = "取消这次尚未提交的售后申请";
        } else {
            summary = "结束这次未提交的退款办理。当前还没有订单号。";
            cancelLabel = "结束这次未提交的退款办理";
        }
        sessionStore.save(session);
        return new CustomerServiceResponse(reply, session.getSessionId(), true, session.getPendingAction(),
            "请选择是否保留办理", summary, order, null, token,
            List.of(
                new CustomerServiceActionResponse("KEEP", "保留当前办理"),
                new CustomerServiceActionResponse("CANCEL_DRAFT", cancelLabel)
            ),
            false);
    }

    private CustomerServiceResponse resumeConflict(CustomerServiceSession session, CustomerServiceResumeRequest request, String decision) {
        if (session.isExpired(LocalDateTime.now())) {
            throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "本次确认已过期，请重新发起咨询");
        }
        if (session.isHandled() || "CANCELLED".equals(session.getPendingAction())) {
            throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "本次确认已处理，旧卡不可用");
        }
        String token = session.getConflictToken();
        if (!StringUtils.hasText(token) || !token.equals(request.getResumeToken())) {
            throw new CustomerServiceException(HttpStatus.FORBIDDEN, "FORBIDDEN", "恢复凭证不匹配");
        }
        if (session.getConflictRevision() != session.getDraftRevision()) {
            throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "办理内容已变化，旧卡不可用");
        }
        if (StringUtils.hasText(request.getRelatedOrderNo()) && StringUtils.hasText(session.getRelatedOrderNo())
            && !session.getRelatedOrderNo().equals(request.getRelatedOrderNo())) {
            throw new CustomerServiceException(HttpStatus.FORBIDDEN, "FORBIDDEN", "关联订单不匹配");
        }
        session.setConflictToken(null);
        if ("KEEP".equals(decision)) {
            rememberTurn(session, "客户点击「保留当前办理」，明确选择继续本次售后申请，不停止申请。", "继续办理本次售后申请，尚未提交。");
            if ("CONFIRM_SUBMIT".equals(session.getPendingAction())) {
                session.setResumeToken(UUID.randomUUID().toString());
                sessionStore.save(session);
                return pendingRefundResponse(session, "继续办理本次售后申请，尚未提交。");
            }
            if (token.equals(session.getResumeToken())) {
                session.setResumeToken(null);
            }
            return plainResponse(session, "继续办理本次售后申请。", "KEEP");
        }
        return cancelPendingRefund(session);
    }

    private Map<String, Object> readObjectWait(JsonNode wait) {
        Map<String, Object> map = new HashMap<>();
        map.put("kind", "object");
        map.put("draft_version", wait.path("draft_version").asText(""));
        map.put("draft_order", wait.path("draft_order").isTextual() ? wait.path("draft_order").asText() : null);
        map.put("decision", wait.path("decision").asText("uncertain"));
        if (wait.path("decision_score").isNumber()) {
            map.put("decision_score", wait.path("decision_score").asDouble());
        }
        map.put("extra_task", wait.path("extra_task").asText("uncertain"));
        List<String> candidates = new ArrayList<>();
        if (wait.path("candidates").isArray()) {
            wait.path("candidates").forEach(item -> {
                if (item.isTextual() && StringUtils.hasText(item.asText())) {
                    candidates.add(item.asText());
                }
            });
        }
        map.put("candidates", candidates);
        return map;
    }

    private Map<String, Object> afterSaleDraftSummary(CustomerServiceSession session) {
        String pending = session.getPendingAction();
        boolean submitted = session.isHandled() && StringUtils.hasText(session.getRelatedAfterSaleNo());
        String stage;
        if (submitted || "SUBMITTED".equals(pending)) {
            stage = "submitted";
        } else if ("CANCELLED".equals(pending)) {
            stage = "cancelled";
        } else if (session.isExpired(LocalDateTime.now())) {
            stage = "expired";
        } else if ("REFUND_ORDER_REQUIRED".equals(pending)) {
            stage = "choosing_order";
        } else if ("REFUND_REASON_REQUIRED".equals(pending)) {
            stage = "awaiting_reason";
        } else if ("CONFIRM_SUBMIT".equals(pending)) {
            stage = "draft";
        } else {
            stage = "none";
        }
        boolean active = "choosing_order".equals(stage) || "awaiting_reason".equals(stage) || "draft".equals(stage);
        Map<String, Object> draft = new HashMap<>();
        draft.put("stage", stage);
        draft.put("order", active || "submitted".equals(stage) ? session.getRelatedOrderNo() : null);
        String actionType = session.getActionType();
        draft.put("afterSaleType", "REFUND".equals(actionType) || "RETURN".equals(actionType) ? actionType : null);
        draft.put("submitted", submitted);
        draft.put("valid", active);
        draft.put("version", Integer.toString(session.getDraftRevision()));
        draft.put("ownerVerified", true);
        return draft;
    }

    private void rememberTurn(CustomerServiceSession session, String userMessage, String answer) {
        session.appendDialogue("user", userMessage);
        session.appendDialogue("assistant", answer);
    }

    private String buildAgentMessage(String message, CustomerServicePageContextRequest pageContext, String userId) {
        if (pageContext == null) {
            return message;
        }
        StringBuilder builder = new StringBuilder(message).append("\n\n[荒野户外商城页面上下文]");
        if (StringUtils.hasText(pageContext.getType())) {
            builder.append(" 当前页面=").append(pageContext.getType()).append(";");
        }
        if (pageContext.getProductId() != null) {
            builder.append(" 关联商品ID=").append(pageContext.getProductId()).append(";");
        }
        if (StringUtils.hasText(pageContext.getOrderNo())) {
            builder.append(" 关联订单号=").append(pageContext.getOrderNo().trim()).append(";");
        }
        if (StringUtils.hasText(pageContext.getAfterSaleNo())) {
            builder.append(" 关联售后申请号=").append(pageContext.getAfterSaleNo().trim()).append(";");
        }
        List<AfterSaleRequest> needMoreInfoRequests = needMoreInfoRequests(pageContext, userId);
        for (AfterSaleRequest request : needMoreInfoRequests) {
            builder.append(" 该订单存在待客户补充材料的售后申请=")
                .append(request.getRequestId())
                .append("; 处理意见=")
                .append(StringUtils.hasText(request.getHandlingNote()) ? request.getHandlingNote() : "需要补充材料")
                .append(";");
        }
        builder.append(" 以上上下文已由电商后端校验归属。");
        return builder.toString();
    }

    private List<AfterSaleRequest> needMoreInfoRequests(CustomerServicePageContextRequest pageContext, String userId) {
        if (pageContext == null || !StringUtils.hasText(pageContext.getOrderNo())) {
            return List.of();
        }
        return afterSaleRequestRepository.findByOrderNo(pageContext.getOrderNo().trim()).stream()
            .filter(request -> !StringUtils.hasText(userId) || userId.equals(request.getUserId()))
            .filter(request -> "NEED_MORE_INFO".equalsIgnoreCase(request.getStatus()))
            .toList();
    }

    private CustomerServiceResponse refundFollowup(CustomerServiceSession session, String message,
                                                   CustomerServicePageContextRequest page, String userId, boolean controlOnly) {
        boolean exactCancel = CANCEL_DRAFT.matcher(message.trim()).matches();
        if (session.getPendingAction() == null || session.isHandled()) {
            if (exactCancel) {
                return plainResponse(session, "当前没有可取消的未提交售后办理。本次未撤销任何已提交申请。", "NO_PENDING_APPLICATION");
            }
            return null;
        }
        if (session.isExpired(LocalDateTime.now())) {
            clearPendingRefund(session);
            return plainResponse(session, "本次确认已过期，请重新提出退款诉求并核对申请。", "EXPIRED");
        }
        // 补订单是已知办理的输入，不让旧取消对话覆盖；未决确认仍先处理。
        if ("REFUND_ORDER_REQUIRED".equals(session.getPendingAction()) && session.getConflictToken() == null
            && session.getObjectWait() == null && ORDER_NO.matcher(message.trim()).matches()) {
            return prepareRefund(session, userId, message.trim(), session.getRefundReason(),
                "本次仅准备申请，尚未提交。", session.getActionType());
        }
        JsonNode judgment = cancellationJudgment(session, message);
        CustomerServiceResponse clarification = clarificationReply(session, judgment, exactCancel);
        if (clarification != null) {
            return clarification;
        }
        boolean versionMatches = judgment != null && draftVersionMatches(session, judgment);
        if (decideFollowup(exactCancel, judgment, versionMatches) == CancelFollowup.CANCEL) {
            return cancelPendingRefund(session);
        }
        if (controlOnly) return null;
        if ("RETURN".equals(session.getActionType()) && isOtherReturnReason(message)) {
            clearPendingRefund(session);
            return plainResponse(session, "质量问题等其他售后请联系人工核验，原无理由退货确认卡已失效。本次未提交。", "RETURN_TYPE_REQUIRED");
        }
        String requestedOrder = extractOrderNo(message);
        if (requestedOrder == null && page != null) {
            requestedOrder = trimToNull(page.getOrderNo());
        }
        if (session.getRelatedOrderNo() != null && requestedOrder != null
            && !requestedOrder.equals(session.getRelatedOrderNo())) {
            clearPendingRefund(session);
            return null;
        }
        if (session.getRelatedOrderNo() == null) {
            String reason = extractRefundReason(message);
            if (reason != null) {
                session.bumpDraftRevision();
                session.setObjectWait(null);
                session.setRefundReason(reason);
                sessionStore.save(session);
                if (requestedOrder == null) {
                    return plainResponse(session, "退款原因已记录，请补充本人的订单号。本次尚未提交。", "REFUND_ORDER_REQUIRED");
                }
            }
            return null;
        }
        String reason = extractRefundReason(message);
        if (reason != null || message.matches(".*(?:金额|多少钱|多少|整单|审核|提交|原因|流程|政策|规则).*")) {
            return prepareRefund(session, userId, session.getRelatedOrderNo(), reason,
                "本次仅准备申请，尚未提交。提交后等待管理员审核，不代表退款成功。", session.getActionType());
        }
        return null;
    }

    private CustomerServiceResponse prepareRefund(CustomerServiceSession session, String userId, String orderNo,
                                                  String reason, String answer, String actionType) {
        try {
            boolean returning = "RETURN".equals(actionType);
            ShopOrderResponse order = returning ? shopService.getCustomerReturnOrder(userId, orderNo)
                : shopService.getCustomerRefundOrder(userId, orderNo);
            session.setAfterSaleQuery(null);
            if (returning) reason = "七天无理由退货";
            if (reason != null && (reason.isBlank() || reason.length() > 200)) {
                clearPendingRefund(session);
                return plainResponse(session, "请提供 1 至 200 字的退款原因，本次未提交。", "BLOCKED");
            }
            boolean newDraft = session.isHandled() || !Objects.equals(actionType, session.getActionType())
                || !Objects.equals(orderNo, session.getRelatedOrderNo())
                || session.getResumeToken() == null;
            boolean changed = newDraft || session.getRefundAmount() == null
                || order.totalAmount().compareTo(session.getRefundAmount()) != 0
                || !Objects.equals(order.itemSummary(), session.getRefundItemSummary())
                || reason != null && !Objects.equals(reason, session.getRefundReason());
            if (newDraft) {
                session.setRefundReason(reason);
                session.setRelatedAfterSaleNo(null);
                session.setExpiresAt(LocalDateTime.now().plusMinutes(30));
            } else if (reason != null) {
                session.setRefundReason(reason);
            }
            if (changed) {
                // 客户确认凭证独立于调试台审批凭证，内容变化后旧卡立即失效。
                session.bumpDraftRevision();
                session.setObjectWait(null);
                session.setResumeToken(UUID.randomUUID().toString());
                session.setWorkflowId("customer-refund-" + UUID.randomUUID());
            }
            session.setRelatedOrderNo(orderNo);
            session.setRefundAmount(order.totalAmount());
            session.setRefundItemSummary(order.itemSummary());
            session.setActionType(actionType);
            session.setHandled(false);
            session.setPendingAction(session.getRefundReason() == null ? "REFUND_REASON_REQUIRED" : "CONFIRM_SUBMIT");
            sessionStore.save(session);
            return pendingRefundResponse(session, answer);
        } catch (BusinessException ex) {
            clearPendingRefund(session);
            return plainResponse(session, ex.getMessage(), "BLOCKED");
        }
    }

    private CustomerServiceResponse pendingRefundResponse(CustomerServiceSession session, String answer) {
        if (!StringUtils.hasText(session.getRefundReason())) {
            return plainResponse(session, "订单 " + session.getRelatedOrderNo() + " 的整单实付金额为 "
                + session.getRefundAmount().toPlainString() + " 元。请补充退款原因，例如“原因：买错了”。本次尚未提交。",
                "REFUND_REASON_REQUIRED");
        }
        boolean returning = "RETURN".equals(session.getActionType());
        String summary = "订单：" + session.getRelatedOrderNo() + "；商品：" + session.getRefundItemSummary()
            + "；范围：" + (returning ? "整单退货退款" : "整单退款") + "；实付金额：" + session.getRefundAmount().toPlainString()
            + " 元；原因：" + session.getRefundReason() + "；状态：等待本人确认，尚未提交。确认后等待审核，提交不代表退款成功。"
            + (returning ? "同意退货后按地址寄回商品、配件及赠品；请保持完好，商家实际收到且验收通过后才退款。" : "");
        return new CustomerServiceResponse(answer, session.getSessionId(), true, "CONFIRM_SUBMIT",
            (returning ? "确认提交退货退款申请" : "确认提交退款申请"), summary, session.getRelatedOrderNo(), null,
            session.getResumeToken(), confirmationActions(), false);
    }

    private CustomerServiceResponse submittedResponse(CustomerServiceSession session, String requestNo, String status) {
        String label = switch (status) {
            case "PENDING_REVIEW" -> "待审核";
            case "NEED_MORE_INFO" -> "待补充材料";
            case "APPROVED" -> "已通过";
            case "REJECTED" -> "未通过";
            case "WAITING_RETURN" -> "待买家寄回";
            case "RETURN_IN_TRANSIT" -> "待商家收货验收";
            case "RETURN_INSPECTION_FAILED" -> "验收未通过，待人工处理";
            case "REFUNDED" -> "已退款";
            default -> status;
        };
        return new CustomerServiceResponse(("RETURN".equals(session.getActionType()) ? "退货退款" : "退款") + "申请已登记，申请号：" + requestNo + "；当前状态：" + label
            + "。可在售后记录中查看，提交不代表退款成功。", session.getSessionId(), false, "SUBMITTED",
            "已提交，" + label, null, session.getRelatedOrderNo(), requestNo, null, List.of(), false);
    }

    private CustomerServiceResponse plainResponse(CustomerServiceSession session, String answer, String action) {
        sessionStore.save(session);
        return new CustomerServiceResponse(answer, session.getSessionId(), false, action, null, null,
            session.getRelatedOrderNo(), session.getRelatedAfterSaleNo(), null, List.of(), false);
    }

    private CustomerServiceResponse humanSupportResponse(CustomerServiceSession session, String answer) {
        sessionStore.save(session);
        String token = session.isHandled() ? null : session.getResumeToken();
        return new CustomerServiceResponse(answer + " 可前往人工客服继续处理，本次未新增售后申请。",
            session.getSessionId(), true, "HUMAN_SUPPORT", "是否转接人工客服？",
            "由人工客服继续为你处理；前往人工客服不会提交售后申请。",
            session.getRelatedOrderNo(), session.getRelatedAfterSaleNo(), token,
            List.of(new CustomerServiceActionResponse("OPEN_HUMAN_SUPPORT", "确认前往"),
                new CustomerServiceActionResponse("STAY_WITH_AGENT", "暂不前往")), false);
    }

    // shortcut: 首版覆盖明确业务词及其直接否定；复杂组合售后需另行定义目标和顺序。
    private static String unsupportedServiceReason(String message) {
        String text = message.replaceAll("(?:不是|不要|不用|无需|不需要|不想要|不想|别|不)(?:再)?(?:给我|帮我)?(?:换货|换一件|换新|换一个|维修|修理|返修|修一下|保修|补发|部分(?:订单)?退款|部分退货)", "")
            .replaceAll("(?:非|没有|不是)质量问题", "");
        if (text.matches("(?s).*(?:部分(?:订单)?退款|部分退货|退一部分|退部分|(?:只|仅)退(?:其中)?(?:一|1|两|2)件|(?:只|仅)退(?:这|那|其中的)(?:一)?(?:件|个|款)|退款.*部分金额).*")) return "部分退款或部分退货";
        if (text.matches("(?s).*(?:换货|换一件|换新|换一个).*")) return "换货";
        if (text.matches("(?s).*(?:维修|修理|返修|修一下|保修).*")) return "维修";
        if (text.contains("补发")) return "补发";
        if (text.matches("(?s).*(?:质量问题|商品破损|发错货|少件|与描述不符|收到.{0,8}(?:破了|坏了)).*")) return "质量问题售后";
        return null;
    }

    private static boolean wantsHumanSupport(String message) {
        String text = message.replaceAll("(?:不要|不用|不需要|别|不)(?:再)?(?:给我|帮我)?(?:转(?:接)?人工|人工客服|真人客服)", "");
        return text.matches("(?s).*(?:转(?:接)?人工|人工客服|真人客服|找人工).*");
    }

    private CustomerServiceResponse cancelPendingRefund(CustomerServiceSession session) {
        clearPendingRefund(session);
        session.setHandled(true);
        session.setPendingAction("CANCELLED");
        return plainResponse(session, "本次售后申请已取消，未提交。", "CANCELLED");
    }

    private void clearPendingRefund(CustomerServiceSession session) {
        session.bumpDraftRevision();
        session.setObjectWait(null);
        session.setActionType(null);
        session.setPendingAction(null);
        session.setResumeToken(null);
        session.setWorkflowId(null);
        session.setRefundReason(null);
        session.setRefundAmount(null);
        session.setRefundItemSummary(null);
        session.setRelatedOrderNo(null);
        session.setRelatedAfterSaleNo(null);
        session.setHandled(false);
        session.setExpiresAt(null);
        sessionStore.save(session);
    }

    private CustomerServiceResponse eligibilityQueryResponse(JsonNode agentResponse, CustomerServiceSession session) {
        JsonNode query = agentResponse.path("session_state").path("eligibility_query");
        Map<String, Object> context = new HashMap<>();
        context.put("intent", "after_sale_eligibility_query");
        context.put("order_id", text(query, "order_id", null));
        context.put("application_type", text(query, "application_type", null));
        context.put("pending_slot", text(query, "pending_slot", null));
        List<Map<String, String>> candidates = new ArrayList<>();
        for (JsonNode item : query.path("candidates")) {
            candidates.add(Map.of("value", item.path("value").asText(), "label", item.path("label").asText()));
        }
        context.put("candidates", candidates);
        session.setAfterSaleQuery(context);
        sessionStore.save(session);
        return new CustomerServiceResponse(text(agentResponse, "answer", FALLBACK_ANSWER), session.getSessionId(), false,
            context.get("pending_slot") == null ? "ELIGIBILITY_QUERY" : "ELIGIBILITY_ORDER_REQUIRED",
            null, null, (String) context.get("order_id"), null, null, List.of(), false);
    }

    private CustomerServiceResponse sanitizeChatResponse(JsonNode agentResponse, CustomerServiceSession session,
                                                         CustomerServicePageContextRequest pageContext, String userId,
                                                         String userMessage) {
        if (agentResponse == null || !agentResponse.hasNonNull("answer")) {
            return fallback(session.getSessionId());
        }
        JsonNode workflow = workflowNode(agentResponse);
        String answer = text(agentResponse, "answer", FALLBACK_ANSWER);
        String intent = agentResponse.path("session_state").path("intent").asText();
        if ((workflow.isMissingNode() || workflow.isNull()) && Set.of("refund_request", "return_request").contains(intent)) {
            String type = "return_request".equals(intent) ? "RETURN" : "REFUND";
            String orderNo = text(agentResponse.path("session_state").path("context_report"), "chosen_order_id", null);
            if ("order_id".equals(agentResponse.path("clarification").path("clarification_field").asText())) orderNo = null;
            session.setAfterSaleQuery(null);
            if ("RETURN".equals(type) && isOtherReturnReason(userMessage)) {
                return plainResponse(session, "质量问题等其他售后请联系人工核验，本次未准备无理由退货申请。", "RETURN_TYPE_REQUIRED");
            }
            if (orderNo != null) {
                return prepareRefund(session, userId, orderNo, extractRefundReason(userMessage),
                    "请核对申请，确认提交后等待后台审核。", type);
            }
            session.bumpDraftRevision();
            session.setObjectWait(null);
            session.setHandled(false);
            session.setActionType(type);
            session.setPendingAction("REFUND_ORDER_REQUIRED");
            session.setRefundReason("RETURN".equals(type) ? "七天无理由退货" : extractRefundReason(userMessage));
            session.setExpiresAt(LocalDateTime.now().plusMinutes(30));
            return plainResponse(session, answer, "REFUND_ORDER_REQUIRED");
        }
        if (isOtherReturnReason(userMessage) && ("received_return".equals(text(workflow, "workflow_type", null))
            || "return_request".equals(agentResponse.path("session_state").path("intent").asText()))) {
            clearPendingRefund(session);
            return plainResponse(session, "质量问题等其他售后请联系人工核验，本次未提交无理由退货申请。", "RETURN_TYPE_REQUIRED");
        }
        if ("received_return".equals(text(workflow, "workflow_type", null))
            && !userMessage.matches(".*(?:不申请|不提交|只了解|先了解).*")) {
            if (!userMessage.contains("七天无理由") && !"七天无理由".equals(text(workflow, "return_reason", null))
                && !"RETURN".equals(session.getActionType())) {
                clearPendingRefund(session);
                return plainResponse(session, "如需七天无理由退货，请明确回复‘申请七天无理由退货’；质量问题等其他售后请联系人工核验。本次未提交。", "RETURN_TYPE_REQUIRED");
            }
            String orderNo = firstText(workflow, "order_id", "order_no");
            if (!StringUtils.hasText(orderNo)) orderNo = extractOrderNo(userMessage);
            if (!StringUtils.hasText(orderNo) && pageContext != null) orderNo = trimToNull(pageContext.getOrderNo());
            if (!StringUtils.hasText(orderNo)) {
                session.setActionType("RETURN");
                session.setPendingAction("REFUND_ORDER_REQUIRED");
                session.setExpiresAt(LocalDateTime.now().plusMinutes(30));
                return plainResponse(session, "请补充本人的订单号，本次未提交。", "REFUND_ORDER_REQUIRED");
            }
            return prepareRefund(session, userId, orderNo, null,
                "请核对退货退款申请。同意退货后先寄回，商家收到且验收通过才退款。", "RETURN");
        }
        if (isApprovalRequired(workflow) && "unshipped_refund".equals(text(workflow, "workflow_type", null))
            && !userMessage.matches(".*(?:不申请|不提交|只了解|先了解).*")) {
            String orderNo = firstText(workflow, "order_id", "order_no");
            if (!StringUtils.hasText(orderNo)) {
                return plainResponse(session, "尚未核实本人的订单号，本次未提交，请补充订单号。", "REFUND_ORDER_REQUIRED");
            }
            String reason = extractRefundReason(userMessage);
            if (reason == null && "REFUND_ORDER_REQUIRED".equals(session.getPendingAction())) {
                reason = session.getRefundReason();
            }
            return prepareRefund(session, userId, orderNo, reason,
                "请核对退款申请。确认后只登记为待审核，是否通过由管理员独立决定。", "REFUND");
        }
        if (isApprovalRequired(workflow)) {
            clearPendingRefund(session);
            return plainResponse(session, "客服支持未发货整单退款及符合条件的签收七天无理由退货；本次未提交，请联系人工核验。", "BLOCKED");
        }
        boolean readOnlyTurn = isReadOnlyTurn(agentResponse);
        if (!readOnlyTurn && session.getRelatedOrderNo() != null && !session.isHandled() && session.getPendingAction() != null) {
            return pendingRefundResponse(session, answer + " 本次待确认申请尚未提交。");
        }
        if (Set.of("refund_request", "return_request").contains(agentResponse.path("session_state").path("intent").asText())
            && "order_id".equals(agentResponse.path("clarification").path("clarification_field").asText())) {
            session.bumpDraftRevision();
            session.setObjectWait(null);
            session.setHandled(false);
            session.setActionType("return_request".equals(agentResponse.path("session_state").path("intent").asText()) ? "RETURN" : "REFUND");
            session.setPendingAction("REFUND_ORDER_REQUIRED");
            session.setRefundReason("RETURN".equals(session.getActionType()) ? "七天无理由退货" : extractRefundReason(userMessage));
            session.setExpiresAt(LocalDateTime.now().plusMinutes(30));
            sessionStore.save(session);
        }
        return new CustomerServiceResponse(answer, session.getSessionId(), false, null, null, null,
            pageContext == null ? null : trimToNull(pageContext.getOrderNo()),
            pageContext == null ? null : trimToNull(pageContext.getAfterSaleNo()),
            null, List.of(), false);
    }

    private boolean isOtherReturnReason(String message) {
        return message.matches("(?s).*(?:质量问题|商品破损|发错货|少件|与描述不符).*");
    }

    private boolean isReadOnlyTurn(JsonNode response) {
        JsonNode state = response.path("session_state");
        JsonNode workflow = workflowNode(response);
        if (state.path("task_control_requested").asBoolean(false)
            || Set.of("refund_request", "return_request").contains(state.path("intent").asText())
            || !(workflow.isNull() || workflow.isMissingNode())) return false;
        // 旧契约缺字段时，没有办理计划的消息也不能顺带修改草稿。
        return !state.has("read_only_turn") || state.path("read_only_turn").asBoolean(false);
    }

    private void validateResumeSession(CustomerServiceSession session, CustomerServiceResumeRequest request) {
        if (session.isExpired(LocalDateTime.now())) {
            throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "本次确认已过期，请重新发起咨询");
        }
        if ("CANCELLED".equals(session.getPendingAction())) {
            throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "本次申请已取消，旧卡不可用");
        }
        if (!StringUtils.hasText(session.getResumeToken()) || !session.getResumeToken().equals(request.getResumeToken())) {
            throw new CustomerServiceException(HttpStatus.FORBIDDEN, "FORBIDDEN", "恢复凭证不匹配");
        }
        if (StringUtils.hasText(request.getRelatedOrderNo())
            && StringUtils.hasText(session.getRelatedOrderNo())
            && !session.getRelatedOrderNo().equals(request.getRelatedOrderNo())) {
            throw new CustomerServiceException(HttpStatus.FORBIDDEN, "FORBIDDEN", "关联订单不匹配");
        }
        if (!"CONFIRM_SUBMIT".equals(session.getPendingAction()) || !StringUtils.hasText(session.getRefundReason())
            || !StringUtils.hasText(session.getRelatedOrderNo())) {
            throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "当前会话没有待确认申请");
        }
    }

    private void assertSessionOwner(CustomerServiceSession session, String userId) {
        if (!userId.equals(session.getUserId())) {
            throw new CustomerServiceException(HttpStatus.FORBIDDEN, "FORBIDDEN", "客服会话不属于当前用户");
        }
    }

    private CustomerServiceResponse fallback(String sessionId) {
        return new CustomerServiceResponse(FALLBACK_ANSWER, sessionId, false, null, null, null,
            null, null, null, List.of(), true);
    }

    private static JsonNode workflowNode(JsonNode root) {
        JsonNode sessionState = root.path("session_state");
        if (sessionState.has("workflow")) {
            return sessionState.path("workflow");
        }
        if (root.has("workflow")) {
            return root.path("workflow");
        }
        return MissingNode.getInstance();
    }

    private static boolean isApprovalRequired(JsonNode workflow) {
        return workflow != null
            && "paused".equals(text(workflow, "status", null))
            && "require_approval".equals(text(workflow, "pending_action", null))
            && StringUtils.hasText(text(workflow, "resume_token", null))
            && StringUtils.hasText(text(workflow, "workflow_id", null));
    }

    private static List<CustomerServiceActionResponse> confirmationActions() {
        return List.of(
            new CustomerServiceActionResponse("CONFIRM_SUBMIT", "确认提交申请"),
            new CustomerServiceActionResponse("CANCEL_SUBMIT", "取消本次申请")
        );
    }

    private static String extractOrderNo(String message) {
        Matcher match = ORDER_NO.matcher(message);
        return match.find() ? match.group() : null;
    }

    private static String extractRefundReason(String message) {
        if (message.contains("？") || message.contains("?")) {
            return null;
        }
        Matcher match = REFUND_REASON.matcher(message);
        if (match.find()) {
            return (match.group(1) == null ? match.group(2) : match.group(1)).trim();
        }
        for (String term : List.of("买错", "不想要", "不需要", "重复购买", "重复下单", "行程取消", "计划取消")) {
            if (message.contains(term)) {
                return message.trim();
            }
        }
        return null;
    }

    private static String text(JsonNode node, String field, String defaultValue) {
        if (node == null || !node.hasNonNull(field)) {
            return defaultValue;
        }
        return node.path(field).asText(defaultValue);
    }

    private static String firstText(JsonNode node, String first, String second) {
        String value = text(node, first, null);
        if (StringUtils.hasText(value)) {
            return value;
        }
        return text(node, second, null);
    }

    private static String trimToNull(String value) {
        if (!StringUtils.hasText(value)) {
            return null;
        }
        return value.trim();
    }
}
