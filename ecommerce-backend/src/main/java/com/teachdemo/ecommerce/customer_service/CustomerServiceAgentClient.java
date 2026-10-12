package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;

import com.teachdemo.ecommerce.customer_service.AgentChatRequest;

import com.teachdemo.ecommerce.customer_service.AgentResumeRequest;

import java.util.Map;

public interface CustomerServiceAgentClient {

    JsonNode chat(AgentChatRequest request);

    JsonNode resume(AgentResumeRequest request);

    default JsonNode judgeCancellation(Map<String, Object> request) {
        return null;
    }
}
