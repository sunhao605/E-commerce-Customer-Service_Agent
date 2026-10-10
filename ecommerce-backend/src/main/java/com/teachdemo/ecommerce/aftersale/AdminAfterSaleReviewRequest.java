package com.teachdemo.ecommerce.aftersale;

import java.math.BigDecimal;

public class AdminAfterSaleReviewRequest {

    private String reviewNote;
    private BigDecimal approvedAmount;
    private String returnAddress;
    private String returnContact;
    private String returnPhone;
    private Boolean receivedAndInspected;

    public String getReturnAddress() { return returnAddress; }
    public String getReturnContact() { return returnContact; }
    public String getReturnPhone() { return returnPhone; }
    public Boolean getReceivedAndInspected() { return receivedAndInspected; }

    public String getReviewNote() {
        return reviewNote;
    }

    public BigDecimal getApprovedAmount() {
        return approvedAmount;
    }
}
