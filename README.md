# OpenRouter → LiteLLM Cost Map 工具

一套用于获取、转换和合并 OpenRouter 模型价格数据的桌面工具，输出 LiteLLM `model_prices_and_context_window` 规范格式的 JSON cost map，并提供图形界面完成全部流程。

## 项目背景

- **OpenRouter** 的 `GET /api/v1/models` 接口返回自己的字段结构（`pricing.prompt`、`architecture` 等），没有内置转换成 LiteLLM 格式的功能。
- **LiteLLM** 的 cost map 使用自己的规范字段（`input_cost_per_token`、`litellm_provider`、`mode` 等）。
- 本项目通过字段映射脚本在两种格式之间转换，并将自定义条目与 LiteLLM 官方 cost map 合并成一个合法 JSON 文件。

## 功能总览

GUI 有两个标签页，覆盖完整流程：

```
获取 OpenRouter 数据 → 转换为 LiteLLM 格式 → 与官方 cost map 合并
```

### 1. 获取 OpenRouter 数据

- 调用 `GET https://openrouter.ai/api/v1/models`，按官方文档实现了 **29 个查询参数**，分组显示在复选框界面中，勾选即发送，取消勾选不发送：
  - 分页 / 排序 / 搜索：`offset`、`limit`、`sort`、`q`、`category`、`context`
  - 模态 / 能力：`input_modalities`、`output_modalities`、`supported_parameters`
  - 价格 / 年龄：`min_price`、`max_price`、`min_output_price`、`max_output_price`、`min_age_days`、`max_age_days`（价格单位为**美元/百万 tokens**）
  - 评分：智能 / 编程 / 代理指数、工具成功率上下限
  - 组织 / 隐私 / 地区：`arch`、`model_authors`、`providers`、`distillable`、`zdr`、`region`
- 枚举参数使用下拉框，模态参数支持多选；数值参数带范围校验（如 `limit` 1–1000、最小值不能大于最大值）。
- API Key 可选：文档要求认证，但实测无密钥也可请求。Key 仅在内存中使用，不写入日志或输出文件。
- `use_rss` / `use_rss_chat_links` 显示但禁用——RSS 返回订阅源而非 JSON，不适用于转换流程。
- 只保存本次响应，不自动翻页；`total_count` 大于返回数量时会提示数据不完整。
- 获取成功后自动填入"转换源文件"路径。

> 提示：取消勾选 `offset` 和 `limit` 可获取完整匹配列表；需要所有输出模态时启用 `output_modalities` 并仅选 `all`（不设置时服务端默认 `text`）。

### 2. 转换 OpenRouter JSON → LiteLLM 格式

字段映射关系（详见 [convert_to_litellm.py](convert_to_litellm.py)）：

| OpenRouter 字段 | LiteLLM 字段 |
|---|---|
| `pricing.prompt` | `input_cost_per_token` |
| `pricing.completion` | `output_cost_per_token` |
| `pricing.audio` / `audio_output` | `input_cost_per_audio_token` / `output_cost_per_audio_token` |
| `pricing.internal_reasoning` | `output_cost_per_reasoning_token` |
| `pricing.input_cache_read` / `input_cache_write` | `cache_read_input_token_cost` / `cache_creation_input_token_cost` |
| `pricing.image` / `request` | `input_cost_per_image` / `input_cost_per_request` |
| `context_length` | `max_input_tokens` |
| `top_provider.max_completion_tokens`（缺失时回退上下文长度） | `max_output_tokens`、`max_tokens` |
| `architecture.input_modalities` | `supports_vision`、`supports_audio_input` |
| `architecture.output_modalities` | `supports_audio_output` |
| `supported_parameters` | 各 `supports_*` 能力标志 |

转换规则：

- 模型键统一加 `openrouter/` 前缀，保留完整 API 模型 ID（如 `openrouter/openai/gpt-4o`）。
- `litellm_provider` 统一为 `"openrouter"`，`mode` 统一为 `"chat"`（多模态聊天模型仍是聊天模型）。
- 价格保持原始单位（每 token，除非字段名注明其他单位）。
- **无法从数据推导的字段一律省略**，不用 0 或 true 填充未知信息。
- OpenRouter 动态路由模型的 `-1` 价格哨兵值被省略。
- 条件性价格覆盖（`overrides`）不展开，仅保留基础价格；`expiration_date` 不当作 `deprecation_date` 使用。

### 3. 与官方 LiteLLM cost map 合并

- 输出保持 LiteLLM 可加载的扁平 JSON 结构（不加包装层，JSON 无注释）。
- 排列方式：**自定义完整块在前，官方非冲突块在后**，块间以空行分隔，便于维护时区分两个来源。
- **同名冲突**：整条采用自定义配置，不混合内部字段、不改模型名；日志会列出冲突模型及数量。
- 输入文件永不修改；输出不能覆盖任何输入文件；覆盖其他已有目标文件前需确认。
- 写入前做严格 JSON 回读校验，失败则不生成文件。

### 日志区

"运行日志 / 冲突模型"区域与功能区间有可拖动分隔条，可上下调整大小；合并冲突时会显示冲突数量和完整模型列表。

## 使用方法

### 方式一：可执行文件（推荐）

双击 `dist/LiteLLM-CostMap-Tool.exe`（约 10.5 MiB，单文件，无需安装 Python，仅支持 Windows 64 位）。首次启动需 1–3 秒解压，属正常现象。exe 不随仓库分发，按下方命令自行打包。

### 方式二：源码运行

需要 Python 3.12+（仅标准库，无第三方依赖）：

```powershell
cd C:\Users\HadesCanyon\Downloads\Gemini
python .\litellm_converter_gui.py
```

将以下文件保持在同一目录：

| 文件 | 作用 |
|---|---|
| [litellm_converter_gui.py](litellm_converter_gui.py) | GUI 主程序 |
| [convert_to_litellm.py](convert_to_litellm.py) | 转换规则（被 GUI 导入，也可单独命令行运行） |
| [openrouter_query_params.py](openrouter_query_params.py) | 官方文档参数定义与校验 |
| [test_litellm_converter_gui.py](test_litellm_converter_gui.py) | 离线自动化测试 |

### 方式三：命令行转换（无界面）

```powershell
python .\convert_to_litellm.py
```

将 OpenRouter 模型数据保存为 `openrouter-models.json` 并与脚本放在同一目录，生成 `litellm-cost-map.json`（每次运行直接覆盖）。

### 重新打包 exe

```powershell
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "LiteLLM-CostMap-Tool" --hidden-import convert_to_litellm --hidden-import openrouter_query_params litellm_converter_gui.py
```

exe 中默认文件路径按 exe 所在目录解析；打包产物在 `dist/`，`build/` 和 `.spec` 是中间产物。

## 测试

```powershell
python -m unittest test_litellm_converter_gui -v
```

覆盖：转换/合并端到端流程、真实快照 HTTP 模拟管线、29 个参数的校验规则、非法响应不生成输出、失败时保留已有输出文件、重定向拒发凭据等。

## 已知限制

- 转换统一输出 `mode: "chat"`；非聊天模型（如 embedding、image_generation）需人工调整。
- 仅保留基础价格，不展开条件性价格覆盖（`overrides`），不是完整计费计算器。
- 缺少输出上限的模型回退使用上下文长度作为 `max_output_tokens`。
- 5 个 OpenRouter 动态路由模型（`openrouter/auto` 等）使用 `-1` 价格哨兵，相关价格字段被省略。
- `search_context_cost_per_query`、`supported_regions`、`deprecation_date` 在 OpenRouter 数据中无对应来源，不生成。
- 合并冲突时自定义整条覆盖官方同名条目（不保留官方版本）。
- 下载只保存单次响应，不自动翻页。

## 文件说明

| 文件 | 说明 |
|---|---|
| [Costmap_info.md](Costmap_info.md) | 需求与背景说明（LiteLLM 模板、OpenRouter 接口、迭代记录） |
| `openrouter-models-latest_*.json` | OpenRouter 模型数据快照（GUI 获取输出示例） |
| [litellm-cost-map.json](litellm-cost-map.json) | 转换输出示例 |
| [model_prices_and_context_window.json](model_prices_and_context_window.json) | LiteLLM 官方 cost map（合并输入） |
| [litellm/](litellm/) | **git 子模块**：[BerriAI/litellm](https://github.com/BerriAI/litellm) 仓库；GUI 默认从 `litellm/model_prices_and_context_window.json` 读取官方 cost map |
| `model_prices_and_context_window-merged.json` | 合并输出示例 |
