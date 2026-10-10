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
            String message = request.getMessage();
            String continuingType = session.getActionType();
            boolean continuingRefund = session.getPendingAction() != null && !session.isHandled();
            boolean needsOrder = "REFUND_ORDER_REQUIRED".equals(session.getPendingAction());
            String requestedOrder = extractOrderNo(message);
            if (requestedOrder == null && pageContext != null) requestedOrder = trimToNull(pageContext.getOrderNo());
            boolean changedOrder = requestedOrder != null && !Objects.equals(requestedOrder, session.getRelatedOrderNo());
            CustomerServiceResponse followup = refundFollowup(session, message, pageContext, profile.getUserId());
            if (followup != null) {
                return followup;
            }
            if (continuingRefund && requestedOrder != null && (needsOrder || changedOrder)) {
                message += "\n客户正在补充或更换" + ("RETURN".equals(continuingType) ? "七天无理由退货退款" : "未发货整单退款") + "申请的订单号，尚未确认提交。";
            }
            AgentChatRequest agentRequest = new AgentChatRequest(
                sessionId,
                profile.getUserId(),
                profile.getNickname(),
                profile.getMemberLevel(),
                profile.getRiskLevel(),
                buildAgentMessage(message, pageContext, profile.getUserId()),
                buildRuntimeContext(profile, pageContext)
            );
            try {
                JsonNode agentResponse = agentClient.chat(agentRequest);
                return sanitizeChatResponse(agentResponse, session, pageContext, profile.getUserId(), request.getMessage());
            } catch (RuntimeException ex) {
                sessionStore.save(session);
                return fallback(sessionId);
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
            if (!Set.of("CONFIRM_SUBMIT", "CANCEL_SUBMIT").contains(decision)) {
                throw new CustomerServiceException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "decision 只支持 CONFIRM_SUBMIT 或 CANCEL_SUBMIT");
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
                session.setHandled(true);
                sessionStore.save(session);
                return plainResponse(session, "本次售后申请已取消，未提交。", "CANCELLED");
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

    private Map<String, Object> buildRuntimeContext(UserProfile profile, CustomerServicePageContextRequest pageContext) {
        Map<String, Object> context = new HashMap<>();
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
                                                   CustomerServicePageContextRequest page, String userId) {
        if (session.getPendingAction() == null || session.isHandled()) {
            return null;
        }
        if (session.isExpired(LocalDateTime.now())) {
            clearPendingRefund(session);
            return plainResponse(session, "本次确认已过期，请重新提出退款诉求并核对申请。", "EXPIRED");
        }
        if (message.trim().matches("(?:取消本次申请|不退了|不申请了)[。！!]?")) {
            session.setHandled(true);
            return plainResponse(session, "本次售后申请已取消，未提交。", "CANCELLED");
        }
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

    private void clearPendingRefund(CustomerServiceSession session) {
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

    private CustomerServiceResponse sanitizeChatResponse(JsonNode agentResponse, CustomerServiceSession session,
                                                         CustomerServicePageContextRequest pageContext, String userId,
                                                         String userMessage) {
        if (agentResponse == null || !agentResponse.hasNonNull("answer")) {
            return fallback(session.getSessionId());
        }
        JsonNode workflow = workflowNode(agentResponse);
        String answer = text(agentResponse, "answer", FALLBACK_ANSWER);
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
        if (session.getRelatedOrderNo() != null && !session.isHandled() && session.getPendingAction() != null) {
            return pendingRefundResponse(session, answer + " 本次待确认申请尚未提交。");
        }
        if (Set.of("refund_request", "return_request").contains(agentResponse.path("session_state").path("intent").asText())
            && "order_id".equals(agentResponse.path("clarification").path("clarification_field").asText())) {
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

    private void validateResumeSession(CustomerServiceSession session, CustomerServiceResumeRequest request) {
        if (session.isExpired(LocalDateTime.now())) {
            throw new CustomerServiceException(HttpStatus.CONFLICT, "CONFLICT", "本次确认已过期，请重新发起咨询");
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
