import { api, apiRequest } from "../shared/api";
import type {
  AdminAfterSale,
  AdminOrder,
  AdminOrderUpdatePayload,
  AdminProduct,
  AdminProductPayload,
  AdminPromotion,
  AdminPromotionPayload,
  AdminUserBalance,
  ReviewPayload
} from "./types";

export const adminApi = {
  login: api.login,
  me: api.me,
  logout: api.logout,
  products: (query: { keyword?: string; category?: string; status?: string }) =>
    apiRequest<AdminProduct[]>("/api/admin/products", { query }),
  createProduct: (body: AdminProductPayload) =>
    apiRequest<AdminProduct>("/api/admin/products", { method: "POST", body }),
  updateProduct: (productId: number, body: AdminProductPayload) =>
    apiRequest<AdminProduct>(`/api/admin/products/${productId}`, { method: "PATCH", body }),
  publishProduct: (productId: number) =>
    apiRequest<AdminProduct>(`/api/admin/products/${productId}/publish`, { method: "POST" }),
  unpublishProduct: (productId: number) =>
    apiRequest<AdminProduct>(`/api/admin/products/${productId}/unpublish`, { method: "POST" }),
  promotions: (query: { keyword?: string }) =>
    apiRequest<AdminPromotion[]>("/api/admin/promotions", { query }),
  promotion: (promotionName: string) =>
    apiRequest<AdminPromotion>(`/api/admin/promotions/${encodeURIComponent(promotionName)}`),
  createPromotion: (body: AdminPromotionPayload) =>
    apiRequest<AdminPromotion>("/api/admin/promotions", { method: "POST", body }),
  updatePromotion: (promotionName: string, body: AdminPromotionPayload) =>
    apiRequest<AdminPromotion>(`/api/admin/promotions/${encodeURIComponent(promotionName)}`, { method: "PATCH", body }),
  orders: (query: {
    orderNo?: string;
    userId?: string;
    orderStatus?: string;
    paymentStatus?: string;
    fulfillmentStatus?: string;
  }) => apiRequest<AdminOrder[]>("/api/admin/orders", { query }),
  order: (orderNo: string) => apiRequest<AdminOrder>(`/api/admin/orders/${orderNo}`),
  updateOrder: (orderNo: string, body: AdminOrderUpdatePayload) =>
    apiRequest<AdminOrder>(`/api/admin/orders/${orderNo}`, { method: "PATCH", body }),
  afterSales: (query: {
    requestNo?: string;
    orderNo?: string;
    userId?: string;
    type?: string;
    status?: string;
  }) => apiRequest<AdminAfterSale[]>("/api/admin/after-sales", { query }),
  afterSale: (requestNo: string) => apiRequest<AdminAfterSale>(`/api/admin/after-sales/${requestNo}`),
  approveAfterSale: (requestNo: string, body: ReviewPayload) =>
    apiRequest(`/api/admin/after-sales/${requestNo}/approve`, { method: "POST", body }),
  rejectAfterSale: (requestNo: string, body: ReviewPayload) =>
    apiRequest(`/api/admin/after-sales/${requestNo}/reject`, { method: "POST", body }),
  needMoreInfo: (requestNo: string, body: ReviewPayload) =>
    apiRequest(`/api/admin/after-sales/${requestNo}/need-more-info`, { method: "POST", body }),
  completeReturn: (requestNo: string, body: ReviewPayload) =>
    apiRequest(`/api/admin/after-sales/${requestNo}/return-complete`, { method: "POST", body }),
  failReturnInspection: (requestNo: string, body: ReviewPayload) =>
    apiRequest(`/api/admin/after-sales/${requestNo}/return-inspection-failed`, { method: "POST", body }),
  userBalance: (userId: string) => apiRequest<AdminUserBalance>(`/api/admin/users/${userId}`)
};
