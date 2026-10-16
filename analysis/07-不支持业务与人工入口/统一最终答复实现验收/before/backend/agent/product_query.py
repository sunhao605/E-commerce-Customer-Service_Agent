"""商品对象定位：客户原句 → 确定商品编号、搜索或候选澄清。

模型理解表达，这里绑定编号。编号只能来自受控页面上下文、已展示候选快照或本会话
最近成功读取详情的商品；多结果不默选第一款，序号只按展示快照解析。
"""

from __future__ import annotations

import re
from typing import Any

# 首版只支持“第一款／第二款／第三款”和“第1款／第2款／第3款”，与订单“第N笔”互不干扰。
SELECTION_PATTERN = re.compile(r"^(?:我选择\s*)?第([1-3]|[一二三])款[。！!？?\s]*$")
STOP_PENDING_PATTERN = re.compile(r"^(?:先不看了?|不看了?|算了|先不选了)[。！!？?\s]*$")
JUST_NOW_CUES = ("刚才那款", "刚才这款", "刚刚那款", "上次那款")
THIS_ITEM_CUES = ("这件", "这款", "此商品", "该商品")
PRODUCT_KNOWLEDGE_CUES = ("保养", "清洁", "收纳", "搭建", "漏水", "返潮", "冷凝", "里面有水", "配件", "缺件", "支架", "晃动", "温标", "承重", "重量", "容量", "续航", "防水", "材质", "兼容", "安装", "充电", "锁止", "展开", "印错")
PRODUCT_FACT_CUES = ("多少钱", "价格", "库存", "有货", "没货", "活动", "优惠", "满减", "会员", "推荐", "哪款", "比较", "区别", "承重", "重量", "容量", "续航", "防水等级", "规格")


def needs_product_knowledge(message: str) -> bool:
    # 商品名称里的“防水／收纳”不表示顾客在询问使用知识。
    text = message
    for name in ("防水收纳袋", "防水袋", "收纳袋", "收纳车", "收纳柜", "露营配件"):
        text = text.replace(name, "")
    # shortcut: 首版按主题词识别，新问法需补例子验证后再扩展。
    return any(cue in text for cue in PRODUCT_KNOWLEDGE_CUES)


def needs_product_facts(message: str) -> bool:
    return not needs_product_knowledge(message) or any(cue in message for cue in PRODUCT_FACT_CUES)


def wants_stock_reminder(message: str) -> bool:
    # shortcut: 只识别明确提醒请求；扩大表达范围时补否定和指代样例。
    cues = ("到货提醒", "补货提醒", "到货通知", "补货通知", "有货后提醒", "有货后通知", "提醒我", "通知我")
    return any(cue in message and not any(negative + cue in message for negative in ("不要", "不用", "不需要", "无需"))
               for cue in cues)


# ponytail: 只认点名的提醒承诺。婉转说法如「我记着」会漏；升级路径是每句承诺带证据编号。
_UNSUPPORTED_STOCK_PROMISE = re.compile(
    r"已(?:经|为你|帮你)?开启.{0,16}(?:提醒|通知)"
    r"|帮你(?:留意|登记|设置|盯)"
    r"|为你留意"
    r"|(?<!不)(?:可以|能够|会)(?:帮你|为你)?留意"
    r"|(?:到货|补货|有货)后.{0,8}(?:提醒|通知|告诉)"
    r"|有货了.{0,8}(?:提醒|通知|告诉)"
    r"|留意(?:补货|到货|库存)"
)
_REFUSAL_MARKS = ("不支持", "不能", "无法", "没有这项", "做不到")
_SENTENCE = re.compile(r"[^。！？\n；;]*[。！？\n；;]?")
# 客服现在完不成这些动作。某个工具真的做成之后，调用时把名字放进 performed。
_UNPERFORMED_ACTIONS = (("stock_watch", _UNSUPPORTED_STOCK_PROMISE),)


def _customer_sentences(text: str) -> list[str]:
    return [piece for piece in _SENTENCE.findall(text or "") if piece.strip()]


def _claims_unperformed_action(sentence: str, performed: frozenset[str] = frozenset()) -> bool:
    if any(mark in sentence for mark in _REFUSAL_MARKS):
        return False
    return any(name not in performed and pattern.search(sentence) for name, pattern in _UNPERFORMED_ACTIONS)


def promises_unsupported_stock_watch(text: str) -> bool:
    """回复是否声称会登记或执行到货、补货提醒。项目没有这项服务。"""
    return any(_claims_unperformed_action(sentence) for sentence in _customer_sentences(text))


def without_unperformed_actions(text: str, performed: frozenset[str] = frozenset()) -> str:
    """删掉宣布了未完成动作的句子，保留其余。"""
    return "".join(
        sentence for sentence in _customer_sentences(text)
        if not _claims_unperformed_action(sentence, performed)
    ).strip()


FOLLOWUP_CUES = ("多少钱", "价格", "库存", "有货", "没货", "活动", "优惠", "包邮", "几件", "还剩", "贵吗", "便宜")

_STRIP = re.compile(r"[\s，。！？、；：,.!?;:~·\-—_*「」『』\"'“”‘’()（）\[\]【】]")
_SELECTION_NUMERAL = {word: index for index, word in enumerate("一二三", 1)}

MAX_SHOWN_CANDIDATES = 3


def normalize_product_text(text: str) -> str:
    """比较前只去掉首尾空白与无意义标点，不删除款式、容量等区别。"""
    return _STRIP.sub("", text or "")


def product_name(product: dict[str, Any]) -> str:
    return str(product.get("name") or product.get("productName") or "").strip()


def unique_name_match(products: list[dict[str, Any]], match_text: str | None) -> dict[str, Any] | None:
    """候选名称唯一包含于客户原句才选定；互为子串时只保留最长名称，保住款式区别。"""
    if not match_text:
        return None
    normalized_text = normalize_product_text(match_text)
    matched = [p for p in products if (name := normalize_product_text(product_name(p))) and name in normalized_text]
    maximal = [
        p for p in matched
        if not any(q is not p and normalize_product_text(product_name(p)) in normalize_product_text(product_name(q)) for q in matched)
    ]
    return maximal[0] if len(maximal) == 1 else None


def product_selection_index(message: str) -> int | None:
    """纯选择答复才解析序号，返回 1 基序号；其他表达一律不当作选择。"""
    match = SELECTION_PATTERN.fullmatch((message or "").strip())
    if not match:
        return None
    numeral = match.group(1)
    return int(numeral) if numeral.isdigit() else _SELECTION_NUMERAL.get(numeral)


def candidate_prompt(candidates: list[dict[str, Any]], *, more_count: int = 0, keyword: str = "商品") -> str:
    """候选文案由代码生成，编号与展示顺序即后续选择绑定快照。"""
    lines = [f"找到多款{keyword}，请告诉我要咨询哪一款："]
    lines += [f"{index}. {product_name(item)}" for index, item in enumerate(candidates, 1)]
    if more_count > 0:
        lines.append(f"另外还有 {more_count} 款匹配，可以补充更完整的名称或款式。")
    lines.append("回复“第一款”或“第二款”即可，也可以直接补充完整商品名。")
    return "\n".join(lines)


def resolve_product_target(
    *,
    message: str,
    page_product_id: int | str | None,
    product_state: dict[str, Any] | None,
    keyword_present: bool,
    model_available: bool,
) -> dict[str, Any]:
    """按客户原句解析商品指向。返回 kind: detail/search/reshow/clarify/end_pending。"""
    state = product_state or {}
    pending = state.get("pending_candidates") or []
    last_id = state.get("product_id")

    explicit_id = re.search(r"商品\s*(?:编号|ID)\s*[:：]?\s*([1-9][0-9]*)(?![A-Za-z0-9_/-])", message, re.IGNORECASE)
    if explicit_id:
        return {"kind": "detail", "product_id": explicit_id.group(1), "source": "explicit_id"}
    selection = product_selection_index(message)
    if selection is not None:
        if not pending:
            return {"kind": "clarify", "reason": "no_pending_candidates"}
        if not 1 <= selection <= len(pending):
            return {"kind": "reshow", "reason": "selection_out_of_range"}
        return {"kind": "detail", "product_id": pending[selection - 1]["id"], "source": "candidate"}
    if STOP_PENDING_PATTERN.fullmatch(message.strip()):
        return {"kind": "end_pending"} if pending else {"kind": "clarify", "reason": "no_object"}
    if keyword_present:
        # 明确名称或类别词优先，页面商品和旧候选都不能覆盖新诉求。
        return {"kind": "search"}
    if any(cue in message for cue in JUST_NOW_CUES):
        if pending:
            return {"kind": "reshow", "reason": "pending_candidates"}
        if last_id is not None:
            return {"kind": "detail", "product_id": last_id, "source": "last_selected"}
        return {"kind": "clarify", "reason": "no_last_product"}
    if any(cue in message for cue in THIS_ITEM_CUES):
        if page_product_id is not None:
            return {"kind": "detail", "product_id": page_product_id, "source": "page"}
        if pending:
            return {"kind": "reshow", "reason": "pending_candidates"}
        if last_id is not None:
            return {"kind": "detail", "product_id": last_id, "source": "last_selected"}
        return {"kind": "clarify", "reason": "no_object"}
    if any(cue in message for cue in FOLLOWUP_CUES):
        if page_product_id is not None:
            return {"kind": "detail", "product_id": page_product_id, "source": "page"}
        if pending:
            return {"kind": "reshow", "reason": "pending_candidates"}
        if last_id is not None:
            return {"kind": "detail", "product_id": last_id, "source": "last_selected"}
        return {"kind": "clarify", "reason": "no_object"}
    if model_available:
        # 名称不在受控词表时由模型提取搜索词，绑定仍按名称核对。
        return {"kind": "search"}
    if pending:
        return {"kind": "reshow", "reason": "pending_candidates"}
    return {"kind": "clarify", "reason": "no_object"}


CLARIFY_MESSAGES = {
    "no_pending_candidates": "当前没有生效的商品候选可以按序号选择。请告诉我商品名称或款式，我再帮你查询。",
    "no_last_product": "我还没有查到之前咨询过的商品，请告诉我商品名称或款式。",
    "no_object": "请告诉我要咨询的商品名称或款式，例如“轻量露营折叠椅”。",
}
