# -*- coding: utf-8 -*-
"""Convert the adjacent OpenRouter models snapshot to a LiteLLM-style cost map.

Prices are USD per token unless the destination field specifies another unit.
Unknown fields are omitted, not replaced with zero. Negative routing-price
sentinels are omitted. Conditional pricing overrides remain in the source file;
this output describes base prices only, not a complete billing estimator.

All models use the OpenRouter route, not their underlying vendor's route.
Multimodal chat models remain chat models (not speech/image endpoint models).
The context window is used as max_input_tokens; it is not an independent input
budget when output shares that window. Missing output limits fall back to it.
Expiration dates are not reinterpreted as deprecation dates.
"""
import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "openrouter-models.json"
DST = ROOT / "litellm-cost-map.json"

PRICE_FIELDS = {
    "prompt": "input_cost_per_token",
    "completion": "output_cost_per_token",
    "audio": "input_cost_per_audio_token",
    "audio_output": "output_cost_per_audio_token",
    "internal_reasoning": "output_cost_per_reasoning_token",
    "input_cache_read": "cache_read_input_token_cost",
    "input_cache_write": "cache_creation_input_token_cost",
    "image": "input_cost_per_image",
    "request": "input_cost_per_request",
}


def price_number(value):
    if value is None or value == "":
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"Non-finite price: {value!r}")
    # OpenRouter automatic routers use -1 rather than a fixed price.
    if number < 0:
        return None
    return float(number)


def convert(model):
    arch = model.get("architecture") or {}
    pricing = model.get("pricing") or {}
    top = model.get("top_provider") or {}
    params = set(model.get("supported_parameters") or [])
    inputs = set(arch.get("input_modalities") or [])
    outputs = set(arch.get("output_modalities") or [])
    context = model.get("context_length") or top.get("context_length")
    max_output = top.get("max_completion_tokens") or context

    spec = {"litellm_provider": "openrouter", "mode": "chat"}
    for key, value in {
        "max_input_tokens": context,
        "max_output_tokens": max_output,
        "max_tokens": max_output,
    }.items():
        if value is not None:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"Invalid {key} for {model['id']}: {value!r}")
            spec[key] = value

    for source_key, target_key in PRICE_FIELDS.items():
        value = price_number(pricing.get(source_key))
        if value is not None:
            spec[target_key] = value

    # These flags reflect only the metadata advertised in the snapshot.
    spec.update({
        "supports_audio_input": "audio" in inputs,
        "supports_audio_output": "audio" in outputs,
        "supports_vision": "image" in inputs,
        "supports_function_calling": "tools" in params,
        "supports_reasoning": bool(params & {"reasoning", "include_reasoning", "reasoning_effort"}),
        # response_format alone can mean JSON mode, not schema enforcement.
        "supports_response_schema": "structured_outputs" in params,
    })
    if "parallel_tool_calls" in params:
        spec["supports_parallel_function_calling"] = True
    if any(key in spec for key in ("cache_read_input_token_cost", "cache_creation_input_token_cost")):
        spec["supports_prompt_caching"] = True
    if "web_search_options" in params:
        spec["supports_web_search"] = True
    # A web_search price does not establish search-context-size pricing or
    # native web-search support. Do not fabricate either from the price alone.
    return spec


def main():
    with SRC.open(encoding="utf-8-sig") as source:
        raw = json.load(source)
    models = raw["data"] if isinstance(raw, dict) else raw
    result = {}
    for model in models:
        # Always prefix: an ID such as openrouter/auto becomes
        # openrouter/openrouter/auto, preserving the actual API model ID.
        key = "openrouter/" + model["id"]
        if key in result:
            raise ValueError(f"Duplicate model ID: {model['id']}")
        result[key] = convert(model)

    with DST.open("w", encoding="utf-8") as output:
        json.dump(result, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.write("\n")

    print(f"Converted {len(result)} models -> {DST}")
    print("Models with conditional overrides (base prices only):", sum(bool(m['pricing'].get('overrides')) for m in models))
    print("Models using context as output-limit fallback:", sum(not (m.get('top_provider') or {}).get('max_completion_tokens') for m in models))
    print("Models with omitted negative base-price sentinels:", sum(any(Decimal(str(m['pricing'][k])) < 0 for k in ('prompt', 'completion')) for m in models))


if __name__ == "__main__":
    main()
