package com.teachdemo.ecommerce.aftersale;

import com.teachdemo.ecommerce.account.BalanceAccount;

import com.teachdemo.ecommerce.account.BalanceAccountRepository;

import com.teachdemo.ecommerce.account.BalanceTransaction;

import com.teachdemo.ecommerce.account.BalanceTransactionRepository;

import com.teachdemo.ecommerce.account.UserProfile;

import com.teachdemo.ecommerce.account.UserProfileRepository;

import com.teachdemo.ecommerce.aftersale.AdminAfterSaleResponse;

import com.teachdemo.ecommerce.aftersale.AdminAfterSaleReviewRequest;

import com.teachdemo.ecommerce.aftersale.AdminAfterSaleReviewResponse;

import com.teachdemo.ecommerce.aftersale.AfterSaleRequest;

import com.teachdemo.ecommerce.aftersale.AfterSaleRequestRepository;

import com.teachdemo.ecommerce.aftersale.ApprovalRecord;

import com.teachdemo.ecommerce.aftersale.ApprovalRecordRepository;

import com.teachdemo.ecommerce.aftersale.ApprovalRecordResponse;

import com.teachdemo.ecommerce.common.BusinessException;

import com.teachdemo.ecommerce.order.OrderEntity;

import com.teachdemo.ecommerce.order.OrderRepository;
import com.teachdemo.ecommerce.order.ShopService;
import java.util.UUID;

import java.math.BigDecimal;

import java.time.LocalDateTime;

import java.util.Comparator;

import java.util.List;

import java.util.Locale;
import java.util.Set;

import org.springframework.stereotype.Service;

import org.springframework.transaction.annotation.Transactional;

import org.springframework.util.StringUtils;

@Service
public class AdminAfterSaleService {

    private final AfterSaleRequestRepository afterSaleRequestRepository;
    private final ApprovalRecordRepository approvalRecordRepository;
    private final BalanceAccountRepository balanceAccountRepository;
    private final BalanceTransactionRepository balanceTransactionRepository;
    private final OrderRepository orderRepository;
    private final UserProfileRepository userProfileRepository;
    private final ShopService shopService;

    public AdminAfterSaleService(AfterSaleRequestRepository afterSaleRequestRepository,
                                 ApprovalRecordRepository approvalRecordRepository,
                                 BalanceAccountRepository balanceAccountRepository,
                                 BalanceTransactionRepository balanceTransactionRepository,
                                 OrderRepository orderRepository,
                                 UserProfileRepository userProfileRepository, ShopService shopService) {
        this.afterSaleRequestRepository = afterSaleRequestRepository;
        this.approvalRecordRepository = approvalRecordRepository;
        this.balanceAccountRepository = balanceAccountRepository;
        this.balanceTransactionRepository = balanceTransactionRepository;
        this.orderRepository = orderRepository;
        this.userProfileRepository = userProfileRepository;
        this.shopService = shopService;
    }

    public List<AdminAfterSaleResponse> list(String requestNo, String orderNo, String userId, String type, String status) {
        return afterSaleRequestRepository.findAll().stream()
            .filter(request -> !StringUtils.hasText(requestNo) || request.getRequestId().contains(requestNo))
            .filter(request -> !StringUtils.hasText(orderNo) || orderNo.equals(request.getOrderNo()))
            .filter(request -> !StringUtils.hasText(userId) || userId.equals(request.getUserId()))
            .filter(request -> !StringUtils.hasText(type) || normalize(type).equals(normalize(request.getRequestType())))
            .filter(request -> !StringUtils.hasText(status) || normalize(status).equals(normalize(request.getStatus())))
            .sorted(Comparator.comparing(AfterSaleRequest::getCreatedAt, Comparator.nullsLast(Comparator.naturalOrder())).reversed())
            .map(this::toResponse)
            .toList();
    }

    public AdminAfterSaleResponse get(String requestNo) {
        return toResponse(findRequest(requestNo));
    }

    @Transactional
    public AdminAfterSaleReviewResponse approve(String requestNo, AdminAfterSaleReviewRequest request,
                                                String reviewerUsername) {
        AfterSaleRequest afterSale = findRequestForReview(requestNo);
        assertReviewable(afterSale);
        if ("RETURN".equals(normalize(afterSale.getRequestType()))) return approveReturn(afterSale, request, reviewerUsername);
        LocalDateTime now = LocalDateTime.now();
        BigDecimal amount = approvedAmount(afterSale, request);
        String approvalNo = nextApprovalNo();
        BalanceTransaction transaction = null;
        String transactionType = balanceTransactionType(afterSale.getRequestType());
        if (transactionType != null && amount.compareTo(BigDecimal.ZERO) > 0) {
            OrderEntity order = orderRepository.findOwnOrderForUpdate(afterSale.getOrderNo(), afterSale.getUserId()).orElseThrow();
            shopService.assertNoActiveReturn(order);
            if ("REFUND".equals(transactionType) && !"UNSHIPPED".equals(order.getFulfillmentStatus())) {
                throw BusinessException.conflict("RETURN_REQUIRED", "已发货订单须通过退货收货验收后退款，不能直接批准退款或取消申请");
            }
            transaction = creditBalance(afterSale, transactionType, amount, now);
        }
        if (transaction != null && "REFUND".equals(transactionType)) {
            completeUnshippedRefund(afterSale, amount);
        }
        ApprovalRecord record = approvalRecordRepository.save(new ApprovalRecord(approvalNo, "AFTER_SALE",
            afterSale.getRequestId(), "APPROVED", reviewerUsername, noteOrDefault(request.getReviewNote(), "审批通过"),
            now, now));
        afterSale.updateReview("APPROVED", record.getApprovalNo(), record.getReviewNote(), now);
        return new AdminAfterSaleReviewResponse(afterSale.getRequestId(), afterSale.getStatus(), record.getApprovalNo(),
            transaction == null ? null : transaction.getTransactionNo(), amount, reviewerUsername, now);
    }

    private AdminAfterSaleReviewResponse approveReturn(AfterSaleRequest afterSale, AdminAfterSaleReviewRequest request,
                                                        String reviewer) {
        OrderEntity order = orderRepository.findOwnOrderForUpdate(afterSale.getOrderNo(), afterSale.getUserId()).orElseThrow();
        shopService.validateReturnApplication(afterSale, order);
        assertReturnAmount(afterSale, request);
        String address = requiredText(request.getReturnAddress(), 200, "请填写真实退货地址");
        String contact = requiredText(request.getReturnContact(), 50, "请填写退货联系人");
        String phone = requiredText(request.getReturnPhone(), 50, "请填写退货联系电话");
        afterSale.setReturnDestination(address, contact, phone);
        return recordReturnReview(afterSale, "WAITING_RETURN", reviewer,
            noteOrDefault(request.getReviewNote(), "已同意退货，请按地址寄回；收到且验收通过后退款。"), null, BigDecimal.ZERO);
    }

    @Transactional
    public AdminAfterSaleReviewResponse completeReturn(String requestNo, AdminAfterSaleReviewRequest request,
                                                        String reviewer) {
        AfterSaleRequest afterSale = findRequestForReview(requestNo);
        assertReturnInspectionState(afterSale);
        if (!Boolean.TRUE.equals(request.getReceivedAndInspected())) {
            throw BusinessException.badRequest("RETURN_RECEIPT_REQUIRED", "必须确认已实际收到商品且验收通过，才能退款");
        }
        assertReturnAmount(afterSale, request);
        OrderEntity order = orderRepository.findOwnOrderForUpdate(afterSale.getOrderNo(), afterSale.getUserId()).orElseThrow();
        BigDecimal amount = shopService.validateReturnApplication(afterSale, order);
        LocalDateTime now = LocalDateTime.now(java.time.ZoneId.of("Asia/Shanghai"));
        BalanceTransaction transaction = creditBalance(afterSale, "REFUND", amount, now);
        order.markReturnedRefunded();
        afterSale.recordReturnReceipt(now);
        return recordReturnReview(afterSale, "REFUNDED", reviewer,
            noteOrDefault(request.getReviewNote(), "已实际收到商品且验收通过，整单退款已退回模拟余额。"), transaction, amount);
    }

    @Transactional
    public AdminAfterSaleReviewResponse failReturnInspection(String requestNo, AdminAfterSaleReviewRequest request,
                                                             String reviewer) {
        AfterSaleRequest afterSale = findRequestForReview(requestNo);
        assertReturnInspectionState(afterSale);
        String note = requiredText(request.getReviewNote(), 200, "请说明验收未通过的具体原因");
        afterSale.recordReturnReceipt(LocalDateTime.now(java.time.ZoneId.of("Asia/Shanghai")));
        return recordReturnReview(afterSale, "RETURN_INSPECTION_FAILED", reviewer, note, null, BigDecimal.ZERO);
    }

    private void assertReturnInspectionState(AfterSaleRequest request) {
        if (!"RETURN".equals(normalize(request.getRequestType()))
            || !Set.of("RETURN_IN_TRANSIT", "RETURN_INSPECTION_FAILED").contains(request.getStatus())
            || !StringUtils.hasText(request.getReturnCarrier()) || !StringUtils.hasText(request.getReturnTrackingNo())
            || !StringUtils.hasText(request.getReturnAddress()) || request.getReturnSentAt() == null) {
            throw BusinessException.conflict("RETURN_STATUS_INVALID", "退货须先同意并填写退回物流，且未完成退款");
        }
    }

    private void assertReturnAmount(AfterSaleRequest afterSale, AdminAfterSaleReviewRequest request) {
        if (request.getApprovedAmount() != null && (afterSale.getAmount() == null
            || request.getApprovedAmount().compareTo(afterSale.getAmount()) != 0)) {
            throw BusinessException.badRequest("RETURN_AMOUNT_INVALID", "七天无理由退货仅支持整单实付金额，不能修改退款金额");
        }
    }

    private String requiredText(String value, int max, String message) {
        if (!StringUtils.hasText(value) || value.trim().length() > max) {
            throw BusinessException.badRequest("RETURN_INFO_REQUIRED", message + "（最多 " + max + " 字）");
        }
        return value.trim();
    }

    private AdminAfterSaleReviewResponse recordReturnReview(AfterSaleRequest request, String status, String reviewer,
        String note, BalanceTransaction transaction, BigDecimal amount) {
        LocalDateTime now = LocalDateTime.now(java.time.ZoneId.of("Asia/Shanghai"));
        ApprovalRecord record = approvalRecordRepository.save(new ApprovalRecord(nextApprovalNo(), "AFTER_SALE",
            request.getRequestId(), status, reviewer, requiredText(note, 200, "请填写处理意见"), now, now));
        request.updateReview(status, record.getApprovalNo(), record.getReviewNote(), now);
        return new AdminAfterSaleReviewResponse(request.getRequestId(), status, record.getApprovalNo(),
            transaction == null ? null : transaction.getTransactionNo(), amount, reviewer, now);
    }

    @Transactional
    public AdminAfterSaleReviewResponse reject(String requestNo, AdminAfterSaleReviewRequest request,
                                               String reviewerUsername) {
        return reviewWithoutBalance(requestNo, request, reviewerUsername, "REJECTED", "审批拒绝");
    }

    @Transactional
    public AdminAfterSaleReviewResponse needMoreInfo(String requestNo, AdminAfterSaleReviewRequest request,
                                                     String reviewerUsername) {
        return reviewWithoutBalance(requestNo, request, reviewerUsername, "NEED_MORE_INFO", "需要补充材料");
    }

    private AdminAfterSaleReviewResponse reviewWithoutBalance(String requestNo, AdminAfterSaleReviewRequest request,
                                                              String reviewerUsername, String status,
                                                              String defaultNote) {
        AfterSaleRequest afterSale = findRequestForReview(requestNo);
        assertReviewable(afterSale);
        if ("RETURN".equals(normalize(afterSale.getRequestType()))) {
            if (!"REJECTED".equals(status)) throw BusinessException.badRequest("RETURN_NOT_ALLOWED", "退货申请请明确同意或驳回");
            requiredText(request.getReviewNote(), 200, "请填写驳回原因");
        }
        LocalDateTime now = LocalDateTime.now();
        ApprovalRecord record = approvalRecordRepository.save(new ApprovalRecord(nextApprovalNo(), "AFTER_SALE",
            afterSale.getRequestId(), status, reviewerUsername, noteOrDefault(request.getReviewNote(), defaultNote),
            now, now));
        afterSale.updateReview(status, record.getApprovalNo(), record.getReviewNote(), now);
        return new AdminAfterSaleReviewResponse(afterSale.getRequestId(), afterSale.getStatus(), record.getApprovalNo(),
            null, BigDecimal.ZERO, reviewerUsername, now);
    }

    private void completeUnshippedRefund(AfterSaleRequest afterSale, BigDecimal amount) {
        OrderEntity order = orderRepository.findOwnOrderForUpdate(afterSale.getOrderNo(), afterSale.getUserId())
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "退款关联订单不存在"));
        // 部分金额不代表整单退款，已发货售后也不关闭为未发货退款。
        if (amount.compareTo(order.getTotalAmount()) == 0 && "PAID".equals(order.getPaymentStatus())
            && Set.of("PAID_PENDING_SHIPMENT", "PENDING_SHIPMENT").contains(order.getStatus())
            && "UNSHIPPED".equals(order.getFulfillmentStatus()) && order.getShippedAt() == null
            && order.getDeliveredAt() == null && !StringUtils.hasText(order.getLogisticsNo())) {
            order.markRefunded();
        }
    }

    private BalanceTransaction creditBalance(AfterSaleRequest afterSale, String type, BigDecimal amount, LocalDateTime now) {
        balanceTransactionRepository.findByAfterSaleNo(afterSale.getRequestId()).ifPresent(transaction -> {
            throw BusinessException.conflict("AFTER_SALE_ALREADY_REVIEWED", "售后申请已入账，不能重复审批");
        });
        BalanceAccount account = balanceAccountRepository.findByUserIdForUpdate(afterSale.getUserId())
            .orElseThrow(() -> BusinessException.notFound("BALANCE_ACCOUNT_NOT_FOUND", "余额账户不存在"));
        BigDecimal before = account.getAvailableBalance();
        BigDecimal after = before.add(amount);
        account.updateBalance(after, now);
        return balanceTransactionRepository.save(new BalanceTransaction("BT-AS-" + afterSale.getRequestId(),
            afterSale.getUserId(), afterSale.getOrderNo(), afterSale.getRequestId(), type, amount, before, after,
            type + " 审批入账：" + afterSale.getRequestId(), now));
    }

    private BigDecimal approvedAmount(AfterSaleRequest afterSale, AdminAfterSaleReviewRequest request) {
        BigDecimal amount = request.getApprovedAmount() != null ? request.getApprovedAmount() : afterSale.getAmount();
        if (amount == null && isCancelOrder(afterSale.getRequestType())) {
            amount = orderRepository.findByOrderNo(afterSale.getOrderNo()).map(OrderEntity::getTotalAmount).orElse(BigDecimal.ZERO);
        }
        if (amount == null) {
            amount = BigDecimal.ZERO;
        }
        if (amount.compareTo(BigDecimal.ZERO) < 0) {
            throw BusinessException.badRequest("AFTER_SALE_AMOUNT_INVALID", "审批金额不能小于 0");
        }
        return amount;
    }

    private String balanceTransactionType(String requestType) {
        String normalized = normalize(requestType);
        if ("REFUND".equals(normalized) || "CANCEL_ORDER".equals(normalized)) {
            return "REFUND";
        }
        if ("COMPENSATION".equals(normalized)) {
            return "COMPENSATION";
        }
        return null;
    }

    private boolean isCancelOrder(String requestType) {
        return "CANCEL_ORDER".equals(normalize(requestType));
    }

    private void assertReviewable(AfterSaleRequest request) {
        String status = normalize(request.getStatus());
        if ("RETURN".equals(normalize(request.getRequestType())) && !"PENDING_REVIEW".equals(status)) {
            throw BusinessException.conflict("RETURN_STATUS_INVALID", "退货初审已处理，请按当前退货进度操作");
        }
        if ("APPROVED".equals(status) || "REJECTED".equals(status) || "CANCELED".equals(status)) {
            throw BusinessException.conflict("AFTER_SALE_ALREADY_REVIEWED", "售后申请已审批，不能重复处理");
        }
    }

    private String nextApprovalNo() {
        return "AR-" + UUID.randomUUID();
    }

    private String noteOrDefault(String note, String defaultNote) {
        return StringUtils.hasText(note) ? note : defaultNote;
    }

    private AfterSaleRequest findRequest(String requestNo) {
        return afterSaleRequestRepository.findByRequestId(requestNo)
            .orElseThrow(() -> BusinessException.notFound("AFTER_SALE_NOT_FOUND", "售后申请不存在"));
    }

    private AfterSaleRequest findRequestForReview(String requestNo) {
        // 只查归属字段，避免锁前加载实体后在并发审批中沿用旧状态。
        AfterSaleRequestRepository.RequestOwner request = afterSaleRequestRepository.findOwnerByRequestId(requestNo)
            .orElseThrow(() -> BusinessException.notFound("AFTER_SALE_NOT_FOUND", "售后申请不存在"));
        // 所有资金动作按订单、售后、余额的顺序锁定，避免与提交/退回物流交叉等待。
        orderRepository.findOwnOrderForUpdate(request.getOrderNo(), request.getUserId())
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "关联订单不存在"));
        return afterSaleRequestRepository.findByRequestIdForUpdate(requestNo)
            .orElseThrow(() -> BusinessException.notFound("AFTER_SALE_NOT_FOUND", "售后申请不存在"));
    }

    private AdminAfterSaleResponse toResponse(AfterSaleRequest request) {
        UserProfile user = userProfileRepository.findByUserId(request.getUserId()).orElse(null);
        List<ApprovalRecordResponse> records = approvalRecordRepository.findByTargetNoOrderByCreatedAtDesc(request.getRequestId()).stream()
            .map(this::toRecordResponse)
            .toList();
        BigDecimal preview = balanceTransactionType(request.getRequestType()) == null
            ? BigDecimal.ZERO : (request.getAmount() == null ? BigDecimal.ZERO : request.getAmount());
        return new AdminAfterSaleResponse(request.getRequestId(), request.getOrderNo(), request.getUserId(),
            user == null ? null : user.getNickname(), request.getRequestType(), request.getStatus(),
            request.getAmount(), request.getReason(), request.getHandlingNote(), records, preview,
            request.getCreatedAt(), request.getUpdatedAt(), ReturnInfoResponse.from(request));
    }

    private ApprovalRecordResponse toRecordResponse(ApprovalRecord record) {
        return new ApprovalRecordResponse(record.getApprovalNo(), record.getTargetType(), record.getTargetNo(),
            record.getStatus(), record.getReviewerUsername(), record.getReviewNote(), record.getCreatedAt(),
            record.getReviewedAt());
    }

    private String normalize(String value) {
        return value == null ? "" : value.toUpperCase(Locale.ROOT);
    }
}
