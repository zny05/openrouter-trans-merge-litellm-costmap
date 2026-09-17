"""GET /models parameters from the documentation supplied by the user.

Price filters use USD per million tokens; response pricing uses USD per token.
RSS is intentionally excluded from the JSON acquisition workflow.
"""
from decimal import Decimal, InvalidOperation

SORT_VALUES = (
    "most-popular", "newest", "top-weekly", "pricing-low-to-high",
    "pricing-high-to-low", "context-high-to-low", "throughput-high-to-low",
    "latency-low-to-high", "intelligence-high-to-low", "coding-high-to-low",
    "agentic-high-to-low", "design-arena-elo-high-to-low",
)
CATEGORY_VALUES = (
    "programming", "roleplay", "marketing", "marketing/seo", "technology",
    "science", "translation", "legal", "finance", "health", "trivia", "academia",
)
INPUT_MODALITIES = ("text", "image", "audio", "file")
OUTPUT_MODALITIES = ("all", "text", "image", "embeddings", "audio", "video", "rerank", "speech", "transcription")

# name, Chinese label, editor kind, initial value, choices, minimum, maximum
PARAMETER_GROUPS = {
    "分页 / 排序 / 搜索": [
        ("offset", "跳过记录数（≥0）", "integer", "0", (), 0, None),
        ("limit", "返回数量（1–1000）", "integer", "1000", (), 1, 1000),
        ("sort", "排序方式", "enum", "pricing-low-to-high", SORT_VALUES, None, None),
        ("q", "名称或 slug 搜索", "text", "", (), None, None),
        ("category", "用途类别", "enum", "programming", CATEGORY_VALUES, None, None),
        ("context", "最小上下文（tokens）", "integer", "128000", (), 1, None),
    ],
    "模态 / 能力": [
        ("input_modalities", "输入模态（多选）", "multi", "text", INPUT_MODALITIES, None, None),
        ("output_modalities", "输出模态（all 不能与其他项同时选）", "multi", "text", OUTPUT_MODALITIES, None, None),
        ("supported_parameters", "支持的参数（逗号分隔）", "csv", "temperature", (), None, None),
    ],
    "价格 / 年龄": [
        ("min_price", "最低输入价（美元/百万 tokens）", "number", "0", (), 0, None),
        ("max_price", "最高输入价（美元/百万 tokens）", "number", "10", (), 0, None),
        ("min_output_price", "最低输出价（美元/百万 tokens）", "number", "0", (), 0, None),
        ("max_output_price", "最高输出价（美元/百万 tokens）", "number", "10", (), 0, None),
        ("min_age_days", "最小模型年龄（天）", "integer", "0", (), 0, None),
        ("max_age_days", "最大模型年龄（天）", "integer", "90", (), 0, None),
    ],
    "评分 / 成功率": [
        ("min_intelligence_index", "最低智能指数", "number", "50", (), 0, None),
        ("max_intelligence_index", "最高智能指数", "number", "100", (), 0, None),
        ("min_coding_index", "最低编程指数", "number", "50", (), 0, None),
        ("max_coding_index", "最高编程指数", "number", "100", (), 0, None),
        ("min_agentic_index", "最低代理能力指数", "number", "50", (), 0, None),
        ("max_agentic_index", "最高代理能力指数", "number", "100", (), 0, None),
        ("min_tool_success_rate", "最低工具成功率（0–1）", "number", "0.9", (), 0, 1),
        ("max_tool_success_rate", "最高工具成功率（0–1）", "number", "1", (), 0, 1),
    ],
    "组织 / 隐私 / 地区": [
        ("arch", "架构或模型家族", "text", "GPT", (), None, None),
        ("model_authors", "模型作者（逗号分隔）", "csv", "openai,anthropic", (), None, None),
        ("providers", "托管提供商（逗号分隔）", "csv", "OpenAI,Anthropic", (), None, None),
        ("distillable", "可蒸馏（true/false）", "enum", "true", ("true", "false"), None, None),
        ("zdr", "仅零数据保留端点", "enum", "true", ("true",), None, None),
        ("region", "数据地区", "enum", "eu", ("eu", "us"), None, None),
    ],
}
PARAMETERS = {spec[0]: spec for group in PARAMETER_GROUPS.values() for spec in group}


def validate_parameters(parameters):
    result = {}
    for name, value in parameters:
        name, value = name.strip(), value.strip()
        if name in {"use_rss", "use_rss_chat_links"}:
            raise ValueError("RSS 参数不适用于 JSON 下载、转换流程。")
        if name not in PARAMETERS:
            raise ValueError(f"文档中未定义参数：{name}")
        if name in result:
            raise ValueError(f"参数重复：{name}")
        if not value:
            raise ValueError(f"已勾选参数 {name} 的值不能为空。")
        _, _, kind, _, choices, minimum, maximum = PARAMETERS[name]
        if kind in {"integer", "number"}:
            if kind == "integer" and (not value.isascii() or not value.isdecimal()):
                raise ValueError(f"{name} 必须为整数。")
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise ValueError(f"{name} 必须为数值。") from None
            if not number.is_finite():
                raise ValueError(f"{name} 必须为有限数值。")
            if minimum is not None and number < minimum:
                raise ValueError(f"{name} 不能小于 {minimum}。")
            if maximum is not None and number > maximum:
                raise ValueError(f"{name} 不能大于 {maximum}。")
        elif kind == "enum":
            if value not in choices:
                raise ValueError(f"{name} 必须选择：{', '.join(choices)}")
        elif kind in {"multi", "csv"}:
            items = [item.strip() for item in value.split(",")]
            if not all(items) or len(items) != len(set(items)):
                raise ValueError(f"{name} 包含空项或重复项。")
            if kind == "multi":
                if any(item not in choices for item in items):
                    raise ValueError(f"{name} 包含不支持的模态。")
                if "all" in items and len(items) > 1:
                    raise ValueError("output_modalities 的 all 必须单独选择。")
            value = ",".join(items)
        result[name] = value
    for name in result:
        if name.startswith("min_"):
            maximum = "max_" + name[4:]
            if maximum in result and Decimal(result[name]) > Decimal(result[maximum]):
                raise ValueError(f"{name} 不能大于 {maximum}。")
    return list(result.items())


def validate_api_key(api_key):
    key = api_key.strip()
    # Documentation requires auth, but a live public request also succeeded.
    if not key:
        return ""
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("API Key 包含空格或非法字符；请只粘贴密钥，不含 Bearer 前缀。")
    return key
