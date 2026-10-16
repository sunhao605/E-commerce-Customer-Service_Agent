package com.teachdemo.ecommerce.customer_service;

import java.util.List;

public record ServiceCapabilityResponse(
    String capabilityId,
    String channel,
    String supportStatus,
    String scope,
    String explanation,
    List<String> alternatives,
    String version
) {
}
