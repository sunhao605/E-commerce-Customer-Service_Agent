package com.teachdemo.ecommerce.customer_service;

import java.time.LocalDateTime;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class CustomerServiceSession {

    private final String sessionId;

    private final String userId;

    private String workflowId;

    private String resumeToken;

    private String pendingAction;

    private String actionType;

    private String relatedOrderNo;

    private String relatedAfterSaleNo;

    // ponytail: 草稿仅驻留本进程，重启需重新确认；已提交申请保存到业务数据库。
    private String refundReason;
    private BigDecimal refundAmount;
    private String refundItemSummary;

    private LocalDateTime expiresAt;

    private boolean handled;

    private LocalDateTime updatedAt;

    // ponytail: 只留最近 6 条、每条 500 字，供取消判断看上一句。更早的对话不保留。
    private final List<Map<String, String>> recentDialogue = new ArrayList<>();
    private boolean cancellationJudged;
    private int draftRevision;
    private Map<String, Object> objectWait;
    private String conflictToken;
    private int conflictRevision;

    public CustomerServiceSession(String sessionId, String userId) {
        this.sessionId = sessionId;
        this.userId = userId;
        this.updatedAt = LocalDateTime.now();
    }

    public String getSessionId() {
        return sessionId;
    }

    public String getUserId() {
        return userId;
    }

    public String getWorkflowId() {
        return workflowId;
    }

    public void setWorkflowId(String workflowId) {
        this.workflowId = workflowId;
    }

    public String getResumeToken() {
        return resumeToken;
    }

    public void setResumeToken(String resumeToken) {
        this.resumeToken = resumeToken;
    }

    public String getPendingAction() {
        return pendingAction;
    }

    public void setPendingAction(String pendingAction) {
        this.pendingAction = pendingAction;
    }

    public String getActionType() {
        return actionType;
    }

    public void setActionType(String actionType) {
        this.actionType = actionType;
    }

    public String getRelatedOrderNo() {
        return relatedOrderNo;
    }

    public void setRelatedOrderNo(String relatedOrderNo) {
        this.relatedOrderNo = relatedOrderNo;
    }

    public String getRelatedAfterSaleNo() {
        return relatedAfterSaleNo;
    }

    public void setRelatedAfterSaleNo(String relatedAfterSaleNo) {
        this.relatedAfterSaleNo = relatedAfterSaleNo;
    }

    public String getRefundReason() { return refundReason; }
    public void setRefundReason(String refundReason) { this.refundReason = refundReason; }
    public BigDecimal getRefundAmount() { return refundAmount; }
    public void setRefundAmount(BigDecimal refundAmount) { this.refundAmount = refundAmount; }
    public String getRefundItemSummary() { return refundItemSummary; }
    public void setRefundItemSummary(String refundItemSummary) { this.refundItemSummary = refundItemSummary; }

    public LocalDateTime getExpiresAt() {
        return expiresAt;
    }

    public void setExpiresAt(LocalDateTime expiresAt) {
        this.expiresAt = expiresAt;
    }

    public boolean isHandled() {
        return handled;
    }

    public void setHandled(boolean handled) {
        this.handled = handled;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void touch() {
        this.updatedAt = LocalDateTime.now();
    }

    public boolean isExpired(LocalDateTime now) {
        return expiresAt != null && expiresAt.isBefore(now);
    }

    public List<Map<String, String>> recentDialogue() {
        List<Map<String, String>> copy = new ArrayList<>();
        for (Map<String, String> turn : recentDialogue) {
            copy.add(Map.copyOf(turn));
        }
        return copy;
    }

    public boolean isCancellationJudged() {
        return cancellationJudged;
    }

    public void setCancellationJudged(boolean cancellationJudged) {
        this.cancellationJudged = cancellationJudged;
    }

    public int getDraftRevision() {
        return draftRevision;
    }

    public void bumpDraftRevision() {
        this.draftRevision++;
        if (conflictToken != null && conflictToken.equals(resumeToken)) {
            this.resumeToken = null;
        }
        this.conflictToken = null;
    }

    public String getConflictToken() {
        return conflictToken;
    }

    public void setConflictToken(String conflictToken) {
        this.conflictToken = conflictToken;
    }

    public int getConflictRevision() {
        return conflictRevision;
    }

    public void setConflictRevision(int conflictRevision) {
        this.conflictRevision = conflictRevision;
    }

    public Map<String, Object> getObjectWait() {
        return objectWait;
    }

    public void setObjectWait(Map<String, Object> objectWait) {
        this.objectWait = objectWait;
    }

    public void appendDialogue(String role, String text) {
        if (text == null || text.isBlank()) {
            return;
        }
        String content = text.length() > 500 ? text.substring(0, 500) : text;
        Map<String, String> turn = new LinkedHashMap<>();
        turn.put("role", role);
        turn.put("content", content);
        recentDialogue.add(turn);
        while (recentDialogue.size() > 6) {
            recentDialogue.remove(0);
        }
    }
}
