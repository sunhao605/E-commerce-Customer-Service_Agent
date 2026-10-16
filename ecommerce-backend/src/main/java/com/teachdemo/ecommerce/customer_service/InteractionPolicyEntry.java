package com.teachdemo.ecommerce.customer_service;

/**
 * 业务维护的用户交互入口元数据。只描述已存在入口的表达方式，不提供执行服务。
 * available 仅表示入口可用；open_entry 只允许前往入口，不是提交、排队或受理凭据。
 */
public record InteractionPolicyEntry(
    String id,
    String surface,
    String status,
    String actor,
    String initiation,
    boolean confirmationRequired,
    String completionEffect,
    String destinationState
) {
}
