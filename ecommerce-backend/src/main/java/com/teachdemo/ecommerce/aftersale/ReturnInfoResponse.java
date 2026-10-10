package com.teachdemo.ecommerce.aftersale;

import java.time.LocalDateTime;

public record ReturnInfoResponse(LocalDateTime signedAt, Boolean eligibleAtSubmit, String address,
                                 String contact, String phone, String carrier, String trackingNo,
                                 LocalDateTime sentAt, LocalDateTime receivedAt) {
    public static ReturnInfoResponse from(AfterSaleRequest request) {
        if (!"RETURN".equalsIgnoreCase(request.getRequestType())) return null;
        return new ReturnInfoResponse(request.getReturnSignedAt(), request.getReturnEligibleAtSubmit(),
            request.getReturnAddress(), request.getReturnContact(), request.getReturnPhone(),
            request.getReturnCarrier(), request.getReturnTrackingNo(), request.getReturnSentAt(),
            request.getReturnReceivedAt());
    }
}
