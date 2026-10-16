package com.teachdemo.ecommerce.customer_service;

import com.teachdemo.ecommerce.common.ApiResponse;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/customer-service/capabilities")
public class ServiceCapabilityController {

    @GetMapping("/interaction-policy")
    public ResponseEntity<ApiResponse<?>> interactionPolicy() {
        return ResponseEntity.ok(ApiResponse.success(ServiceCapabilityCatalog.interactionPolicy()));
    }

    @GetMapping("/{capabilityId}")
    public ResponseEntity<ApiResponse<?>> get(@PathVariable String capabilityId) {
        if (!ServiceCapabilityCatalog.validId(capabilityId)) {
            return ResponseEntity.badRequest().body(ApiResponse.error("BAD_REQUEST", "能力标识不正确"));
        }
        return ResponseEntity.ok(ApiResponse.success(ServiceCapabilityCatalog.get(capabilityId)));
    }
}
