package com.teachdemo.ecommerce.order;

import com.teachdemo.ecommerce.account.BalanceAccount;

import com.teachdemo.ecommerce.account.BalanceAccountRepository;

import com.teachdemo.ecommerce.account.BalanceResponse;

import com.teachdemo.ecommerce.account.BalanceTransaction;

import com.teachdemo.ecommerce.account.BalanceTransactionRepository;

import com.teachdemo.ecommerce.account.BalanceTransactionResponse;

import com.teachdemo.ecommerce.account.UserProfile;

import com.teachdemo.ecommerce.account.UserProfileRepository;

import com.teachdemo.ecommerce.aftersale.AfterSaleRequest;

import com.teachdemo.ecommerce.aftersale.AfterSaleRequestRepository;

import com.teachdemo.ecommerce.aftersale.ShopAfterSaleCreateRequest;

import com.teachdemo.ecommerce.aftersale.ShopAfterSaleResponse;

import com.teachdemo.ecommerce.catalog.Product;

import com.teachdemo.ecommerce.catalog.ProductPromotion;

import com.teachdemo.ecommerce.catalog.ProductPromotionRepository;

import com.teachdemo.ecommerce.catalog.ProductPromotionResponse;

import com.teachdemo.ecommerce.catalog.ProductRepository;

import com.teachdemo.ecommerce.catalog.ShopProductResponse;

import com.teachdemo.ecommerce.common.BusinessException;

import com.teachdemo.ecommerce.order.CartItem;

import com.teachdemo.ecommerce.order.CartItemRepository;

import com.teachdemo.ecommerce.order.CartItemRequest;

import com.teachdemo.ecommerce.order.CartItemResponse;

import com.teachdemo.ecommerce.order.CartItemUpdateRequest;

import com.teachdemo.ecommerce.order.CartResponse;

import com.teachdemo.ecommerce.order.CreateOrderRequest;

import com.teachdemo.ecommerce.order.OrderEntity;

import com.teachdemo.ecommerce.order.OrderItem;

import com.teachdemo.ecommerce.order.OrderItemRepository;

import com.teachdemo.ecommerce.order.OrderRepository;

import com.teachdemo.ecommerce.order.PaymentResponse;

import com.teachdemo.ecommerce.order.ShopOrderItemResponse;

import com.teachdemo.ecommerce.order.ShopOrderResponse;

import java.math.BigDecimal;

import java.time.Clock;
import java.time.ZoneId;
import com.teachdemo.ecommerce.aftersale.ReturnInfoResponse;
import com.teachdemo.ecommerce.aftersale.ReturnShipmentRequest;

import java.time.LocalDateTime;

import java.time.format.DateTimeFormatter;

import java.util.Comparator;

import java.util.LinkedHashSet;

import java.util.List;

import java.util.Map;

import java.util.Set;

import java.util.UUID;

import java.util.function.Function;

import java.util.stream.Collectors;

import org.springframework.stereotype.Service;

import org.springframework.transaction.annotation.Transactional;

import org.springframework.util.StringUtils;

@Service
public class ShopService {

    private static final Set<String> AFTER_SALE_TYPES = Set.of("REFUND", "RETURN", "COMPENSATION", "CANCEL_ORDER");

    private final ProductRepository productRepository;
    private final CartItemRepository cartItemRepository;
    private final OrderRepository orderRepository;
    private final OrderItemRepository orderItemRepository;
    private final BalanceAccountRepository balanceAccountRepository;
    private final BalanceTransactionRepository balanceTransactionRepository;
    private final AfterSaleRequestRepository afterSaleRequestRepository;
    private final UserProfileRepository userProfileRepository;
    private final ProductPromotionRepository productPromotionRepository;
    private final Clock clock;

    public ShopService(ProductRepository productRepository,
                       CartItemRepository cartItemRepository,
                       OrderRepository orderRepository,
                       OrderItemRepository orderItemRepository,
                       BalanceAccountRepository balanceAccountRepository,
                       BalanceTransactionRepository balanceTransactionRepository,
                       AfterSaleRequestRepository afterSaleRequestRepository,
                       UserProfileRepository userProfileRepository,
                       ProductPromotionRepository productPromotionRepository,
                       Clock clock) {
        this.productRepository = productRepository;
        this.cartItemRepository = cartItemRepository;
        this.orderRepository = orderRepository;
        this.orderItemRepository = orderItemRepository;
        this.balanceAccountRepository = balanceAccountRepository;
        this.balanceTransactionRepository = balanceTransactionRepository;
        this.afterSaleRequestRepository = afterSaleRequestRepository;
        this.userProfileRepository = userProfileRepository;
        this.productPromotionRepository = productPromotionRepository;
        this.clock = clock;
    }

    public List<ShopProductResponse> listProducts(String keyword, String category) {
        List<Product> products = productRepository.findAll().stream()
            .filter(product -> Boolean.TRUE.equals(product.getActive()))
            .filter(product -> !StringUtils.hasText(keyword)
                || contains(product.getName(), keyword)
                || contains(product.getDescription(), keyword))
            .filter(product -> !StringUtils.hasText(category) || category.equals(product.getCategory()))
            .collect(Collectors.toList());
        Map<Long, ProductPromotion> promotions = activePromotions(products);
        return products.stream()
            .map(product -> toProductResponse(product, promotions.get(product.getId())))
            .collect(Collectors.toList());
    }

    public ShopProductResponse getProduct(Long productId) {
        Product product = findOnSaleProduct(productId);
        ProductPromotion promotion = activePromotions(List.of(product)).get(product.getId());
        return toProductResponse(product, promotion);
    }

    public CartResponse getCart(String userId) {
        List<CartItem> cartItems = cartItemRepository.findByUserId(userId);
        Map<Long, Product> products = productsById(cartItems.stream().map(CartItem::getProductId).collect(Collectors.toList()));
        Map<Long, ProductPromotion> promotions = activePromotions(products.values().stream().toList());
        UserProfile profile = userProfileRepository.findByUserId(userId)
            .orElseThrow(() -> BusinessException.notFound("USER_NOT_FOUND", "用户不存在"));
        List<CartItemResponse> items = cartItems.stream()
            .sorted(Comparator.comparing(CartItem::getId))
            .map(item -> toCartItemResponse(item, products.get(item.getProductId()), promotions.get(item.getProductId()), profile))
            .collect(Collectors.toList());
        BigDecimal selectedTotal = items.stream()
            .filter(item -> Boolean.TRUE.equals(item.selected()) && Boolean.TRUE.equals(item.settlementAvailable()))
            .map(item -> item.unitPrice().multiply(BigDecimal.valueOf(item.quantity())))
            .reduce(BigDecimal.ZERO, BigDecimal::add);
        int selectedCount = items.stream()
            .filter(item -> Boolean.TRUE.equals(item.selected()) && Boolean.TRUE.equals(item.settlementAvailable()))
            .mapToInt(CartItemResponse::quantity)
            .sum();
        return new CartResponse(items, selectedTotal, selectedCount);
    }

    @Transactional
    public CartResponse addCartItem(String userId, CartItemRequest request) {
        Product product = findOnSaleProduct(request.productId());
        int quantity = positiveQuantity(request.quantity());
        CartItem item = cartItemRepository.findByUserIdAndProductId(userId, request.productId())
            .map(existing -> {
                int mergedQuantity = existing.getQuantity() + quantity;
                ensureStock(product, mergedQuantity);
                existing.update(mergedQuantity, request.selected() == null ? existing.getSelected() : request.selected(), LocalDateTime.now());
                return existing;
            })
            .orElseGet(() -> {
                ensureStock(product, quantity);
                return new CartItem(userId, request.productId(), quantity,
                    request.selected() == null || request.selected(), LocalDateTime.now(), LocalDateTime.now());
            });
        cartItemRepository.save(item);
        return getCart(userId);
    }

    @Transactional
    public CartResponse updateCartItem(String userId, Long itemId, CartItemUpdateRequest request) {
        CartItem item = cartItemRepository.findByIdAndUserId(itemId, userId)
            .orElseThrow(() -> BusinessException.notFound("CART_ITEM_NOT_FOUND", "购物车项不存在"));
        Product product = productRepository.findById(item.getProductId())
            .orElseThrow(() -> BusinessException.notFound("PRODUCT_NOT_FOUND", "商品不存在"));
        if (!Boolean.TRUE.equals(product.getActive()) && request.quantity() != null && request.quantity() > item.getQuantity()) {
            throw BusinessException.badRequest("PRODUCT_NOT_ON_SALE", "商品已下架，不能继续增加数量");
        }
        if (request.quantity() != null) {
            ensureStock(product, positiveQuantity(request.quantity()));
        }
        item.update(request.quantity(), request.selected(), LocalDateTime.now());
        cartItemRepository.save(item);
        return getCart(userId);
    }

    @Transactional
    public void deleteCartItem(String userId, Long itemId) {
        cartItemRepository.findByIdAndUserId(itemId, userId).ifPresent(cartItemRepository::delete);
    }

    @Transactional
    public ShopOrderResponse createOrder(String userId, CreateOrderRequest request) {
        List<OrderLineDraft> drafts = buildOrderDrafts(userId, request);
        if (drafts.isEmpty()) {
            throw BusinessException.badRequest("ORDER_EMPTY", "订单商品不能为空");
        }
        drafts.forEach(draft -> {
            ensureOnSale(draft.product());
            ensureStock(draft.product(), draft.quantity());
        });
        UserProfile profile = userProfileRepository.findByUserId(userId)
            .orElseThrow(() -> BusinessException.notFound("USER_NOT_FOUND", "用户不存在"));
        Map<Long, ProductPromotion> promotions = activePromotions(drafts.stream()
            .map(OrderLineDraft::product)
            .collect(Collectors.toList()));

        BigDecimal totalAmount = drafts.stream()
            .map(draft -> currentUnitPrice(draft.product(), promotions.get(draft.product().getId()), profile)
                .multiply(BigDecimal.valueOf(draft.quantity())))
            .reduce(BigDecimal.ZERO, BigDecimal::add);
        LocalDateTime now = LocalDateTime.now(clock);
        OrderEntity order = orderRepository.save(new OrderEntity(
            nextOrderNo(), profile.getNickname(), "PENDING_PAYMENT", "UNPAID",
            totalAmount, now, userId, false, true, request.remark()));
        drafts.forEach(draft -> {
            // 下单扣库存是电商主应用的权威数据源，后续 Agent 只能读取结果，不能绕过这里直接改库存。
            draft.product().decreaseStock(draft.quantity());
            productRepository.save(draft.product());
            BigDecimal unitPrice = currentUnitPrice(draft.product(), promotions.get(draft.product().getId()), profile);
            orderItemRepository.save(new OrderItem(order, draft.product().getId(), draft.product().getName(),
                draft.quantity(), unitPrice, draft.product().getReturnable()));
        });
        if ("CART".equalsIgnoreCase(request.source()) && request.cartItemIds() != null) {
            cartItemRepository.deleteAllById(request.cartItemIds());
        }
        return toOrderResponse(order);
    }

    public List<ShopOrderResponse> listOrders(String userId, String status) {
        List<OrderEntity> orders = StringUtils.hasText(status)
            ? orderRepository.findByUserIdAndStatusOrderByCreatedAtDesc(userId, status)
            : orderRepository.findByUserIdOrderByCreatedAtDesc(userId);
        return orders.stream().map(this::toOrderResponse).collect(Collectors.toList());
    }

    public ShopOrderResponse getOrder(String userId, String orderNo) {
        OrderEntity order = findOwnOrder(userId, orderNo);
        return toOrderResponse(order);
    }

    @Transactional
    public PaymentResponse pay(String userId, String orderNo) {
        OrderEntity order = orderRepository.findOwnOrderForUpdate(orderNo, userId)
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "订单不存在"));
        if ("PAID".equals(order.getPaymentStatus())) {
            throw BusinessException.badRequest("ORDER_ALREADY_PAID", "订单已支付，不能重复支付");
        }
        if (!"PENDING_PAYMENT".equals(order.getStatus())) {
            throw BusinessException.badRequest("ORDER_STATUS_INVALID", "当前订单状态不能支付");
        }
        BalanceAccount account = balanceAccountRepository.findByUserIdForUpdate(userId)
            .orElseThrow(() -> BusinessException.notFound("BALANCE_ACCOUNT_NOT_FOUND", "余额账户不存在"));
        BigDecimal balanceBefore = account.getAvailableBalance();
        BigDecimal paidAmount = order.getTotalAmount();
        if (balanceBefore.compareTo(paidAmount) < 0) {
            throw BusinessException.badRequest("BALANCE_NOT_ENOUGH", "余额不足，无法完成支付");
        }
        BigDecimal balanceAfter = balanceBefore.subtract(paidAmount);
        LocalDateTime now = LocalDateTime.now(clock);
        account.updateBalance(balanceAfter, now);
        order.markPaid(now);
        String transactionNo = nextTransactionNo();
        balanceTransactionRepository.save(new BalanceTransaction(
            transactionNo, userId, orderNo, null, "PAYMENT", paidAmount.negate(),
            balanceBefore, balanceAfter, "模拟余额支付订单 " + orderNo, now));
        return new PaymentResponse(orderNo, order.getStatus(), order.getPaymentStatus(), paidAmount,
            balanceBefore, balanceAfter, transactionNo, now);
    }

    public BalanceResponse getBalance(String userId) {
        BalanceAccount account = balanceAccountRepository.findByUserId(userId)
            .orElseThrow(() -> BusinessException.notFound("BALANCE_ACCOUNT_NOT_FOUND", "余额账户不存在"));
        return new BalanceResponse(userId, account.getAvailableBalance(), account.getUpdatedAt());
    }

    public List<BalanceTransactionResponse> listBalanceTransactions(String userId, String type) {
        List<BalanceTransaction> transactions = StringUtils.hasText(type)
            ? balanceTransactionRepository.findByUserIdAndTypeOrderByCreatedAtDesc(userId, type)
            : balanceTransactionRepository.findByUserIdOrderByCreatedAtDesc(userId);
        return transactions.stream().map(transaction -> new BalanceTransactionResponse(
            transaction.getTransactionNo(), transaction.getType(), transaction.getAmount(),
            transaction.getBalanceBefore(), transaction.getBalanceAfter(), transaction.getOrderNo(),
            transaction.getAfterSaleNo(), transaction.getRemark(), transaction.getCreatedAt()
        )).collect(Collectors.toList());
    }

    @Transactional
    public ShopAfterSaleResponse createAfterSale(String userId, ShopAfterSaleCreateRequest request) {
        OrderEntity order = orderRepository.findOwnOrderForUpdate(request.orderNo(), userId)
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "订单不存在"));
        return createAfterSale(order, userId, request);
    }

    private ShopAfterSaleResponse createAfterSale(OrderEntity order, String userId, ShopAfterSaleCreateRequest request) {
        String type = request.type().toUpperCase();
        if (!AFTER_SALE_TYPES.contains(type)) {
            throw BusinessException.badRequest("AFTER_SALE_NOT_ALLOWED", "售后类型不支持");
        }
        if ("RETURN".equals(type)) {
            return submitCustomerReturn(userId, request.orderNo(), request.amount());
        }
        assertNoActiveReturn(order);
        validateAfterSale(order, type, request.amount());
        if (afterSaleRequestRepository.findByOrderNo(request.orderNo()).stream()
            .anyMatch(item -> userId.equals(item.getUserId()) && type.equals(item.getRequestType())
                && Set.of("PENDING_REVIEW", "NEED_MORE_INFO").contains(item.getStatus()))) {
            throw BusinessException.badRequest("AFTER_SALE_DUPLICATED", "同一订单同一类型已有待审批申请");
        }
        LocalDateTime now = LocalDateTime.now();
        AfterSaleRequest entity = new AfterSaleRequest(
            nextAfterSaleNo(), request.orderNo(), userId, type, request.reason(),
            "PENDING_REVIEW", request.amount(), true, now, now, "售后申请已提交，等待荒野户外客服审核。");
        afterSaleRequestRepository.save(entity);
        order.markAfterSaleRequested();
        return toAfterSaleResponse(entity);
    }

    public ShopOrderResponse getCustomerRefundOrder(String userId, String orderNo) {
        OrderEntity order = findOwnOrder(userId, orderNo);
        customerRefundAmount(order, userId);
        return toOrderResponse(order);
    }

    @Transactional
    public ShopAfterSaleResponse submitCustomerRefund(String userId, String orderNo, String reason,
                                                       BigDecimal confirmedAmount) {
        OrderEntity order = orderRepository.findOwnOrderForUpdate(orderNo, userId)
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "订单不存在"));
        assertNoActiveReturn(order);
        List<AfterSaleRequest> requests = afterSaleRequestRepository.findByOrderNo(orderNo).stream()
            .filter(item -> userId.equals(item.getUserId()))
            .filter(item -> Set.of("REFUND", "CANCEL_ORDER").contains(item.getRequestType()))
            .toList();
        for (AfterSaleRequest existing : requests) {
            if (Set.of("PENDING_REVIEW", "NEED_MORE_INFO").contains(existing.getStatus())) {
                if ("REFUND".equals(existing.getRequestType())) {
                    return toAfterSaleResponse(existing);
                }
                throw BusinessException.conflict("AFTER_SALE_DUPLICATED", "该订单已有待处理的取消订单申请，请查看现有申请进度");
            }
            if ("APPROVED".equals(existing.getStatus())) {
                throw BusinessException.conflict("AFTER_SALE_DUPLICATED", "该订单已有审核通过的退款申请，不能重复提交整单退款");
            }
        }
        BigDecimal amount = customerRefundAmount(order, userId);
        if (confirmedAmount == null || amount.compareTo(confirmedAmount) != 0) {
            throw BusinessException.conflict("REFUND_AMOUNT_CHANGED", "实付金额发生变化，请重新核对并确认申请");
        }
        if (!StringUtils.hasText(reason) || reason.length() > 200) {
            throw BusinessException.badRequest("REFUND_REASON_REQUIRED", "请提供 1 至 200 字的退款原因");
        }
        return createAfterSale(order, userId, new ShopAfterSaleCreateRequest(orderNo, "REFUND", amount, reason.trim()));
    }

    public ShopOrderResponse getCustomerReturnOrder(String userId, String orderNo) {
        OrderEntity order = findOwnOrder(userId, orderNo);
        customerReturnAmount(order, returnNow());
        return toOrderResponse(order);
    }

    @Transactional
    public ShopAfterSaleResponse submitCustomerReturn(String userId, String orderNo, BigDecimal confirmedAmount) {
        OrderEntity order = orderRepository.findOwnOrderForUpdate(orderNo, userId)
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "订单不存在"));
        for (AfterSaleRequest existing : afterSaleRequestRepository.findByOrderNo(orderNo)) {
            if (isActiveMoneyApplication(existing)) {
                if ("RETURN".equals(existing.getRequestType())) return toAfterSaleResponse(existing);
                throw BusinessException.conflict("AFTER_SALE_DUPLICATED", "订单已有待处理申请，请查看原申请进度");
            }
        }
        LocalDateTime now = returnNow();
        BigDecimal amount = customerReturnAmount(order, now);
        if (confirmedAmount == null || amount.compareTo(confirmedAmount) != 0) {
            throw BusinessException.conflict("REFUND_AMOUNT_CHANGED", "整单实付金额不匹配，请重新确认");
        }
        AfterSaleRequest request = new AfterSaleRequest(nextAfterSaleNo(), orderNo, userId, "RETURN",
            "七天无理由退货", "PENDING_REVIEW", amount, true, now, now,
            "退货退款申请已提交，等待后台审核；同意退货后寄回，收到且验收通过才退款。");
        request.recordReturnEligibility(order.getDeliveredAt());
        afterSaleRequestRepository.save(request);
        order.markAfterSaleRequested();
        return toAfterSaleResponse(request);
    }

    public ShopAfterSaleResponse getAfterSale(String userId, String requestNo) {
        return toAfterSaleResponse(afterSaleRequestRepository.findByRequestIdAndUserId(requestNo, userId)
            .orElseThrow(() -> BusinessException.notFound("AFTER_SALE_NOT_FOUND", "售后申请不存在或不属于本人")));
    }

    @Transactional
    public ShopAfterSaleResponse submitReturnShipment(String userId, String requestNo, ReturnShipmentRequest payload) {
        AfterSaleRequestRepository.RequestOwner found = afterSaleRequestRepository.findOwnerByRequestId(requestNo)
            .filter(owner -> userId.equals(owner.getUserId()))
            .orElseThrow(() -> BusinessException.notFound("AFTER_SALE_NOT_FOUND", "售后申请不存在或不属于本人"));
        orderRepository.findOwnOrderForUpdate(found.getOrderNo(), userId)
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "订单不存在"));
        AfterSaleRequest request = afterSaleRequestRepository.findByRequestIdForUpdate(requestNo).orElseThrow();
        String carrier = payload.carrier().trim(), tracking = payload.trackingNo().trim();
        if (!"RETURN".equals(request.getRequestType())) {
            throw BusinessException.badRequest("RETURN_NOT_ALLOWED", "只有退货申请可以填写退回物流");
        }
        if ("RETURN_IN_TRANSIT".equals(request.getStatus()) && carrier.equals(request.getReturnCarrier())
            && tracking.equals(request.getReturnTrackingNo())) return toAfterSaleResponse(request);
        if (!"WAITING_RETURN".equals(request.getStatus())) {
            throw BusinessException.conflict("RETURN_STATUS_INVALID", "当前状态不能填写退回物流");
        }
        request.recordReturnShipment(carrier, tracking, returnNow());
        return toAfterSaleResponse(request);
    }

    public BigDecimal validateReturnApplication(AfterSaleRequest request, OrderEntity order) {
        if (!Boolean.TRUE.equals(request.getReturnEligibleAtSubmit()) || request.getReturnSignedAt() == null
            || !request.getReturnSignedAt().equals(order.getDeliveredAt())) {
            throw BusinessException.conflict("RETURN_FACTS_UNVERIFIED", "缺少或变更了提交时的退货资格，请联系人工核验");
        }
        BigDecimal amount = customerReturnAmount(order, request.getCreatedAt());
        if (request.getAmount() == null || request.getAmount().compareTo(amount) != 0) {
            throw BusinessException.conflict("REFUND_PAYMENT_UNVERIFIED", "申请金额与整单实付不一致，不能退款");
        }
        return amount;
    }

    private BigDecimal customerReturnAmount(OrderEntity order, LocalDateTime submittedAt) {
        if (!Set.of("DELIVERED", "SIGNED", "COMPLETED").contains(String.valueOf(order.getStatus()))
            || !"DELIVERED".equals(order.getFulfillmentStatus()) || !"PAID".equals(order.getPaymentStatus())) {
            throw BusinessException.badRequest("RETURN_NOT_RECEIVED", "七天无理由退货须已支付并签收；运输中请联系人工处理拦截，本次未提交");
        }
        if (order.getDeliveredAt() == null || submittedAt == null) {
            throw BusinessException.badRequest("RETURN_SIGNED_TIME_MISSING", "缺少可信签收时间，请联系人工核验");
        }
        if (order.getDeliveredAt().isAfter(submittedAt)
            || submittedAt.toLocalDate().isAfter(order.getDeliveredAt().toLocalDate().plusDays(7))) {
            throw BusinessException.badRequest("RETURN_WINDOW_INVALID", "签收时间异常或已超过七天无理由期限；其他售后请联系人工核验");
        }
        List<OrderItem> items = orderItemRepository.findByOrderEntity(order);
        if (items.isEmpty() || items.stream().anyMatch(item -> item.getReturnableAtPurchase() == null)) {
            throw BusinessException.conflict("RETURNABILITY_UNVERIFIED", "缺少购买时的可退记录，请联系人工核验");
        }
        if (items.stream().anyMatch(item -> !Boolean.TRUE.equals(item.getReturnableAtPurchase()))) {
            throw BusinessException.badRequest("RETURN_PRODUCT_NOT_SUPPORTED", "整单中有商品不支持七天无理由，其他售后请联系人工核验");
        }
        return verifiedPaidAmount(order, order.getUserId());
    }

    private LocalDateTime returnNow() {
        return LocalDateTime.now(clock.withZone(ZoneId.of("Asia/Shanghai")));
    }

    private boolean isActiveMoneyApplication(AfterSaleRequest request) {
        return Set.of("REFUND", "RETURN", "CANCEL_ORDER").contains(request.getRequestType())
            && Set.of("PENDING_REVIEW", "NEED_MORE_INFO", "WAITING_RETURN", "RETURN_IN_TRANSIT",
                "RETURN_INSPECTION_FAILED", "APPROVED", "REFUNDED").contains(request.getStatus());
    }

    public void assertNoActiveReturn(OrderEntity order) {
        if (afterSaleRequestRepository.findByOrderNo(order.getOrderNo()).stream()
            .anyMatch(request -> "RETURN".equals(request.getRequestType()) && isActiveMoneyApplication(request))) {
            throw BusinessException.conflict("AFTER_SALE_DUPLICATED", "订单已有退货申请，请查看原申请进度");
        }
    }

    private BigDecimal customerRefundAmount(OrderEntity order, String userId) {
        if (!"PAID".equals(order.getPaymentStatus())
            || !Set.of("PAID_PENDING_SHIPMENT", "PENDING_SHIPMENT").contains(String.valueOf(order.getStatus()))
            || !"UNSHIPPED".equals(order.getFulfillmentStatus())
            || order.getShippedAt() != null || order.getDeliveredAt() != null || StringUtils.hasText(order.getLogisticsNo())) {
            throw BusinessException.badRequest("CUSTOMER_REFUND_NOT_ALLOWED", "首版只支持已支付、未发货的整单退款申请；本次未提交");
        }
        return verifiedPaidAmount(order, userId);
    }

    private BigDecimal verifiedPaidAmount(OrderEntity order, String userId) {
        List<BalanceTransaction> payments = balanceTransactionRepository.findByUserIdAndTypeOrderByCreatedAtDesc(userId, "PAYMENT")
            .stream().filter(item -> order.getOrderNo().equals(item.getOrderNo())).toList();
        BigDecimal amount = payments.size() == 1 && payments.get(0).getAmount() != null
            ? payments.get(0).getAmount().negate() : null;
        boolean alreadyRefunded = balanceTransactionRepository.findByUserIdAndTypeOrderByCreatedAtDesc(userId, "REFUND")
            .stream().anyMatch(item -> order.getOrderNo().equals(item.getOrderNo()));
        if (alreadyRefunded || amount == null || amount.signum() <= 0 || order.getTotalAmount() == null
            || amount.compareTo(order.getTotalAmount()) != 0) {
            throw BusinessException.conflict("REFUND_PAYMENT_UNVERIFIED", "暂时无法核实可退的整单实付金额，本次未提交，请联系人工核验");
        }
        return amount;
    }

    public List<ShopAfterSaleResponse> listAfterSales(String userId) {
        return afterSaleRequestRepository.findByUserId(userId).stream()
            .sorted(Comparator.comparing(AfterSaleRequest::getCreatedAt).reversed())
            .map(this::toAfterSaleResponse)
            .collect(Collectors.toList());
    }

    private List<OrderLineDraft> buildOrderDrafts(String userId, CreateOrderRequest request) {
        if ("DIRECT_BUY".equalsIgnoreCase(request.source())) {
            Product product = productRepository.findById(request.productId())
                .orElseThrow(() -> BusinessException.notFound("PRODUCT_NOT_FOUND", "商品不存在"));
            return List.of(new OrderLineDraft(product, positiveQuantity(request.quantity())));
        }
        List<CartItem> cartItems;
        if (request.cartItemIds() == null || request.cartItemIds().isEmpty()) {
            cartItems = cartItemRepository.findByUserId(userId).stream()
                .filter(item -> Boolean.TRUE.equals(item.getSelected()))
                .collect(Collectors.toList());
        } else {
            cartItems = cartItemRepository.findByIdInAndUserId(request.cartItemIds(), userId);
            if (cartItems.size() != new LinkedHashSet<>(request.cartItemIds()).size()) {
                throw BusinessException.notFound("CART_ITEM_NOT_FOUND", "购物车项不存在");
            }
        }
        Map<Long, Product> products = productsById(cartItems.stream().map(CartItem::getProductId).collect(Collectors.toList()));
        return cartItems.stream()
            .map(item -> new OrderLineDraft(products.get(item.getProductId()), item.getQuantity()))
            .collect(Collectors.toList());
    }

    private Map<Long, ProductPromotion> activePromotions(List<Product> products) {
        List<Long> productIds = products.stream().map(Product::getId).collect(Collectors.toList());
        if (productIds.isEmpty()) {
            return Map.of();
        }
        LocalDateTime now = LocalDateTime.now(clock);
        return productPromotionRepository.findByProductIdInAndActiveTrue(productIds).stream()
            .filter(promotion -> promotionEffectiveAt(promotion, now))
            .collect(Collectors.toMap(ProductPromotion::getProductId, Function.identity(),
                (left, right) -> left.getId() > right.getId() ? left : right));
    }

    private boolean promotionEffectiveAt(ProductPromotion promotion, LocalDateTime now) {
        return (promotion.getStartAt() == null || !promotion.getStartAt().isAfter(now))
            && (promotion.getEndAt() == null || !promotion.getEndAt().isBefore(now));
    }

    private ShopProductResponse toProductResponse(Product product, ProductPromotion promotion) {
        return new ShopProductResponse(product.getId(), product.getName(), product.getCategory(), product.getDescription(),
            product.getPrice(), product.getStock(), product.getImageUrl(), product.getReturnable(), product.getAfterSaleLimit(),
            Boolean.TRUE.equals(product.getActive()) && product.getStock() != null && product.getStock() > 0,
            promotion == null ? null : toPromotionResponse(promotion));
    }

    private ProductPromotionResponse toPromotionResponse(ProductPromotion promotion) {
        return new ProductPromotionResponse(
            promotion.getId(),
            promotion.getPromotionName(),
            promotion.getPromotionType(),
            promotion.getDiscountSummary(),
            promotion.getPromotionPrice(),
            promotion.getRequiredMemberLevel(),
            promotion.getConditionSummary(),
            promotion.getStartAt(),
            promotion.getEndAt()
        );
    }

    private CartItemResponse toCartItemResponse(CartItem item, Product product, ProductPromotion promotion, UserProfile profile) {
        if (product == null) {
            return new CartItemResponse(item.getId(), item.getProductId(), "商品不存在", null,
                BigDecimal.ZERO, BigDecimal.ZERO, null, null, false, null,
                item.getQuantity(), item.getSelected(), 0, "NOT_FOUND", false, "商品不存在");
        }
        boolean onSale = Boolean.TRUE.equals(product.getActive());
        boolean stockEnough = product.getStock() != null && product.getStock() >= item.getQuantity();
        String reason = onSale ? (stockEnough ? null : "库存不足") : "商品已下架";
        boolean promotionApplied = promotionEligible(promotion, profile);
        BigDecimal unitPrice = currentUnitPrice(product, promotion, profile);
        return new CartItemResponse(item.getId(), product.getId(), product.getName(), product.getImageUrl(),
            product.getPrice(), unitPrice, promotion == null ? null : promotion.getPromotionPrice(),
            promotion == null ? null : promotion.getPromotionName(), promotionApplied,
            promotion == null ? null : promotionConditionText(promotion, profile, promotionApplied),
            item.getQuantity(), item.getSelected(), product.getStock(), onSale ? "ON_SALE" : "OFF_SALE",
            onSale && stockEnough, reason);
    }

    private BigDecimal currentUnitPrice(Product product, ProductPromotion promotion, UserProfile profile) {
        if (promotion != null && promotion.getPromotionPrice() != null
            && promotion.getPromotionPrice().compareTo(BigDecimal.ZERO) > 0
            && promotionEligible(promotion, profile)) {
            // 促销价属于“当前用户 + 商品 + 活动条件”的实时结算事实，不能只凭商品有活动就直接降价。
            return promotion.getPromotionPrice();
        }
        return product.getPrice();
    }

    private boolean promotionEligible(ProductPromotion promotion, UserProfile profile) {
        if (promotion == null || promotion.getPromotionPrice() == null
            || promotion.getPromotionPrice().compareTo(BigDecimal.ZERO) <= 0) {
            return false;
        }
        if (!StringUtils.hasText(promotion.getRequiredMemberLevel())) {
            return true;
        }
        return memberRank(profile.getMemberLevel()) >= memberRank(promotion.getRequiredMemberLevel());
    }

    private String promotionConditionText(ProductPromotion promotion, UserProfile profile, boolean promotionApplied) {
        String condition = StringUtils.hasText(promotion.getConditionSummary())
            ? promotion.getConditionSummary()
            : "满足活动条件后可用";
        if (promotionApplied) {
            return "已满足：" + condition;
        }
        if (StringUtils.hasText(promotion.getRequiredMemberLevel())) {
            return "未满足：" + condition + "，当前为" + memberLevelLabel(profile.getMemberLevel());
        }
        return condition;
    }

    private int memberRank(String memberLevel) {
        if ("gold".equalsIgnoreCase(memberLevel)) {
            return 3;
        }
        if ("silver".equalsIgnoreCase(memberLevel)) {
            return 2;
        }
        if ("normal".equalsIgnoreCase(memberLevel)) {
            return 1;
        }
        return 0;
    }

    private String memberLevelLabel(String memberLevel) {
        if ("gold".equalsIgnoreCase(memberLevel)) {
            return "山友会员";
        }
        if ("silver".equalsIgnoreCase(memberLevel)) {
            return "银卡会员";
        }
        if ("normal".equalsIgnoreCase(memberLevel)) {
            return "普通会员";
        }
        return "未识别会员等级";
    }

    private ShopOrderResponse toOrderResponse(OrderEntity order) {
        List<ShopOrderItemResponse> items = orderItemRepository.findByOrderEntity(order).stream()
            .map(item -> new ShopOrderItemResponse(item.getProductId(), item.getProductName(), productImageUrl(item.getProductId()),
                item.getUnitPrice(), item.getQuantity()))
            .collect(Collectors.toList());
        String itemSummary = items.stream()
            .map(item -> item.productName() + " x" + item.quantity())
            .collect(Collectors.joining("，"));
        List<String> afterSaleTypes = availableAfterSaleTypes(order);
        return new ShopOrderResponse(order.getOrderNo(), order.getStatus(), order.getPaymentStatus(),
            order.getFulfillmentStatus(), order.getTotalAmount(), itemSummary, order.getRemark(),
            order.getLogisticsNo(), items, !afterSaleTypes.isEmpty(), afterSaleTypes, order.getCreatedAt(),
            order.getPaidAt(), order.getShippedAt(), order.getDeliveredAt(), null,
            afterSaleRequestRepository.findByOrderNo(order.getOrderNo()).stream()
                .filter(request -> "RETURN".equals(request.getRequestType()))
                .max(Comparator.comparing(AfterSaleRequest::getCreatedAt)).map(this::toAfterSaleResponse).orElse(null));
    }

    private ShopAfterSaleResponse toAfterSaleResponse(AfterSaleRequest request) {
        return new ShopAfterSaleResponse(request.getRequestId(), request.getOrderNo(), request.getRequestType(),
            request.getStatus(), request.getAmount(), request.getReason(), request.getCreatedAt(),
            request.getHandlingNote(), ReturnInfoResponse.from(request));
    }

    private Product findOnSaleProduct(Long productId) {
        Product product = productRepository.findById(productId)
            .orElseThrow(() -> BusinessException.notFound("PRODUCT_NOT_FOUND", "商品不存在"));
        ensureOnSale(product);
        return product;
    }

    private OrderEntity findOwnOrder(String userId, String orderNo) {
        return orderRepository.findByOrderNoAndUserId(orderNo, userId)
            .orElseThrow(() -> BusinessException.notFound("ORDER_NOT_FOUND", "订单不存在"));
    }

    private void ensureOnSale(Product product) {
        if (!Boolean.TRUE.equals(product.getActive())) {
            throw BusinessException.notFound("PRODUCT_NOT_ON_SALE", "商品不存在或已下架");
        }
    }

    private void ensureStock(Product product, int quantity) {
        if (product.getStock() == null || product.getStock() < quantity) {
            throw BusinessException.badRequest("STOCK_NOT_ENOUGH", "商品库存不足");
        }
    }

    private int positiveQuantity(Integer quantity) {
        if (quantity == null || quantity <= 0) {
            throw BusinessException.badRequest("QUANTITY_INVALID", "数量必须大于 0");
        }
        return quantity;
    }

    private void validateAfterSale(OrderEntity order, String type, BigDecimal amount) {
        if (amount == null || amount.compareTo(BigDecimal.ZERO) <= 0) {
            throw BusinessException.badRequest("AFTER_SALE_NOT_ALLOWED", "售后金额必须大于 0");
        }
        if ("COMPENSATION".equals(type)) {
            if (!"PAID".equals(order.getPaymentStatus()) || amount.compareTo(new BigDecimal("100.00")) > 0) {
                throw BusinessException.badRequest("AFTER_SALE_NOT_ALLOWED", "当前订单不满足补偿申请条件");
            }
            return;
        }
        if ("CANCEL_ORDER".equals(type)) {
            if (!"PENDING_PAYMENT".equals(order.getStatus()) && !"PAID_PENDING_SHIPMENT".equals(order.getStatus())) {
                throw BusinessException.badRequest("AFTER_SALE_NOT_ALLOWED", "当前订单不能申请取消");
            }
        } else if ("REFUND".equals(type) && !"UNSHIPPED".equals(order.getFulfillmentStatus())) {
            throw BusinessException.badRequest("AFTER_SALE_NOT_ALLOWED", "已发货订单请按退货退款流程申请，不能直接申请未发货退款");
        } else if (!"PAID".equals(order.getPaymentStatus())) {
            throw BusinessException.badRequest("AFTER_SALE_NOT_ALLOWED", "当前订单未支付，不能申请该售后类型");
        }
        if (amount.compareTo(order.getTotalAmount()) > 0) {
            throw BusinessException.badRequest("AFTER_SALE_NOT_ALLOWED", "售后金额不能超过订单金额");
        }
    }

    private List<String> availableAfterSaleTypes(OrderEntity order) {
        if ("PENDING_PAYMENT".equals(order.getStatus())) {
            return List.of("CANCEL_ORDER");
        }
        if (!"PAID".equals(order.getPaymentStatus())) {
            return List.of();
        }
        if ("PAID_PENDING_SHIPMENT".equals(order.getStatus()) || "PENDING_SHIPMENT".equals(order.getStatus())) {
            return List.of("REFUND", "CANCEL_ORDER", "COMPENSATION");
        }
        return List.of("RETURN", "COMPENSATION");
    }

    private String productImageUrl(Long productId) {
        if (productId == null) {
            return null;
        }
        return productRepository.findById(productId)
            .map(Product::getImageUrl)
            .orElse(null);
    }

    private Map<Long, Product> productsById(List<Long> productIds) {
        return productRepository.findAllById(productIds).stream()
            .collect(Collectors.toMap(Product::getId, Function.identity()));
    }

    private boolean contains(String value, String keyword) {
        return value != null && value.toLowerCase().contains(keyword.toLowerCase());
    }

    private String nextOrderNo() {
        return "SO" + LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMddHHmmssSSS"))
            + "-" + randomSuffix();
    }

    private String nextTransactionNo() {
        return "BT-" + LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMddHHmmssSSS"))
            + "-" + randomSuffix();
    }

    private String nextAfterSaleNo() {
        return "AS-" + LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMddHHmmssSSS"))
            + "-" + randomSuffix();
    }

    private String randomSuffix() {
        return UUID.randomUUID().toString().substring(0, 8);
    }

    private record OrderLineDraft(Product product, int quantity) {
    }
}
