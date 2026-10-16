package com.teachdemo.ecommerce.customer_service;

import java.util.List;

/**
 * 当前客服渠道的交互策略快照。由业务端维护，与能力目录共用同一份资源和版本。
 */
public record InteractionPolicyResponse(
    String schemaVersion,
    String channel,
    String version,
    List<InteractionPolicyEntry> entries
) {
}
