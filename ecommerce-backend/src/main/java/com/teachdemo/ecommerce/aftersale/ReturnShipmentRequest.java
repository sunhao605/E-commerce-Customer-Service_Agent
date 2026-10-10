package com.teachdemo.ecommerce.aftersale;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record ReturnShipmentRequest(@NotBlank @Size(max = 50) String carrier,
                                    @NotBlank @Size(max = 100) String trackingNo) {
}
