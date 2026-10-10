package com.teachdemo.ecommerce.customer_service;

import com.fasterxml.jackson.databind.JsonNode;

import com.teachdemo.ecommerce.customer_service.AgentChatRequest;

import com.teachdemo.ecommerce.customer_service.AgentResumeRequest;

public interface CustomerServiceAgentClient {

    JsonNode chat(AgentChatRequest request);

    JsonNode resume(AgentResumeRequest request);
}
