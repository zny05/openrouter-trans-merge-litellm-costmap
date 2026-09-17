# -*- coding: utf-8 -*-
"""Tkinter UI for conversion and custom-first flat cost-map merging.

Run: python litellm_converter_gui.py
Keep convert_to_litellm.py beside this file; its conversion rules are reused.
Inputs are never overwritten. Duplicate keys and non-finite JSON are rejected.
"""
import json
import math
import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from urllib import error, parse, request
from tkinter import filedialog, messagebox, scrolledtext, ttk

from convert_to_litellm import convert as convert_model
from openrouter_query_params import PARAMETER_GROUPS, validate_parameters, validate_api_key

# In a frozen exe (PyInstaller onefile), __file__ points into a temporary
# extraction dir; resolve defaults relative to the executable's folder instead.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
# Prefer the submodule copy of the official cost map; fall back to a local file.
_SUBMODULE_COSTMAP = ROOT / "litellm" / "model_prices_and_context_window.json"
DEFAULT_OFFICIAL = (_SUBMODULE_COSTMAP if _SUBMODULE_COSTMAP.is_file()
                    else ROOT / "model_prices_and_context_window.json")
DEFAULT_SOURCE = ROOT / "openrouter-models.json"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复键：{key}")
        result[key] = value
    return result


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"JSON 数值超出支持范围：{value}")
    return result


def reject_constant(value):
    raise ValueError(f"JSON 不允许特殊数值：{value}")


def parse_json(text):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_float=finite_float, parse_constant=reject_constant)


def load_json(path):
    try:
        return parse_json(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}\nJSON 错误：第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}") from exc


def load_model_object(path, label):
    data = load_json(path)
    if (not isinstance(data, dict) or not data
            or not all(isinstance(v, dict) for v in data.values())):
        raise ValueError(f"{label} 必须是非空的 LiteLLM 模型名 → 配置对象，不能是 OpenRouter 原始响应。")
    return data


def check_destination(output_path, inputs, overwrite=False):
    output = Path(output_path).expanduser().resolve()
    for source in inputs:
        source = Path(source).expanduser().resolve()
        if output == source or (output.exists() and source.exists() and output.samefile(source)):
            raise ValueError("输出文件不能与任一输入文件相同；请另选文件名。")
    if output.suffix.lower() != ".json":
        raise ValueError("目标文件名必须以 .json 结尾。")
    if not output.parent.is_dir():
        raise ValueError("目标文件夹不存在。")
    if output.exists():
        if not output.is_file():
            raise ValueError("目标路径不是文件。")
        if not overwrite:
            raise FileExistsError(f"目标文件已存在：{output}")
    return output


def write_blocks(output_path, blocks, inputs, overwrite=False):
    """Validate before writing; separate contiguous blocks with a blank line."""
    output = check_destination(output_path, inputs, overwrite)
    parts = []
    expected = {}
    for block in blocks:
        if expected.keys() & block.keys():
            raise ValueError("输出内容块存在重复模型键。")
        expected.update(block)
        if block:
            text = json.dumps(block, indent=2, ensure_ascii=False, allow_nan=False)
            parts.append(text[2:-2])  # omit the outer braces, retain indentation
    text = "{\n" + ",\n\n".join(parts) + "\n}\n"
    if parse_json(text) != expected:
        raise ValueError("输出 JSON 回读校验失败。")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        check_destination(output, inputs, overwrite)
        if overwrite:
            os.replace(temporary, output)
        else:
            # Exclusive publication: never silently replace a newly created file.
            os.link(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def do_convert(source_path, output_path, overwrite=False):
    raw = load_json(source_path)
    models = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(models, list) or not models:
        raise ValueError("请选择 OpenRouter /models 响应（含 data 数组）或非空模型数组。")
    result = {}
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
            raise ValueError("每个 OpenRouter 模型必须包含非空字符串 id。")
        if not isinstance(model.get("pricing"), dict) or not isinstance(model.get("architecture"), dict):
            raise ValueError(f"模型 {model['id']} 缺少 pricing 或 architecture 对象。")
        key = "openrouter/" + model["id"]
        if key in result:
            raise ValueError(f"重复的模型 ID：{model['id']}")
        result[key] = convert_model(model)
    write_blocks(output_path, [result], [source_path], overwrite)
    return result


def do_merge(official_path, custom_path, output_path, overwrite=False):
    official = load_model_object(official_path, "官方 cost map")
    custom = load_model_object(custom_path, "自定义 cost map")
    conflicts = [key for key in custom if key in official]
    remainder = {key: value for key, value in official.items() if key not in custom}
    merged = {**custom, **remainder}
    write_blocks(output_path, [custom, remainder], [official_path, custom_path], overwrite)
    return official, custom, merged, conflicts


MODELS_URL = "https://openrouter.ai/api/v1/models"


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an optional Authorization header to another endpoint.
        return None


def build_models_url(parameters):
    pairs = validate_parameters(parameters)
    return MODELS_URL + ("?" + parse.urlencode(pairs) if pairs else "")


def do_fetch(parameters, api_key, output_path, overwrite=False):
    check_destination(output_path, [], overwrite)
    url = build_models_url(parameters)
    headers = {"Accept": "application/json", "User-Agent": "CostMapGUI/1.0"}
    key = validate_api_key(api_key)
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        opener = request.build_opener(NoRedirect())
        with opener.open(request.Request(url, headers=headers), timeout=30) as response:
            body = response.read(20 * 1024 * 1024 + 1)
        if len(body) > 20 * 1024 * 1024:
            raise ValueError("响应超过 20 MiB 限制，未保存。")
    except error.HTTPError as exc:
        raise ValueError(f"OpenRouter 返回 HTTP {exc.code}；请检查认证、参数或稍后重试。") from None
    except (error.URLError, TimeoutError, OSError):
        raise ValueError("网络连接失败或超时；请检查网络、代理和证书，稍后重试。") from None
    try:
        raw = parse_json(body.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("服务未返回有效 JSON；未保存文件。") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        raise ValueError("响应缺少 data 模型数组；未保存文件。")
    ids = set()
    for model in raw["data"]:
        if (not isinstance(model, dict) or not isinstance(model.get("id"), str)
                or not model["id"] or not isinstance(model.get("pricing"), dict)
                or not isinstance(model.get("architecture"), dict)):
            raise ValueError("响应中存在无效的模型条目；未保存文件。")
        if model["id"] in ids:
            raise ValueError("响应包含重复模型 ID；未保存文件。")
        ids.add(model["id"])
    write_blocks(output_path, [raw], [], overwrite)
    return raw


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OpenRouter → LiteLLM Cost Map 转换 / 合并工具")
        self.geometry("1000x700")
        self.minsize(850, 580)
        self.events = queue.Queue()
        self.busy = False
        self.buttons = []
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)

    def _build(self):
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.main_panes = tk.PanedWindow(frame, orient=tk.VERTICAL, sashwidth=8,
                                        sashrelief=tk.RAISED, opaqueresize=True)
        self.main_panes.pack(fill="both", expand=True)
        tabs = ttk.Notebook(self.main_panes)
        self.main_panes.add(tabs, minsize=160, height=430, stretch="always")
        fetch_tab = ttk.Frame(tabs, padding=8)
        local_tab = ttk.Frame(tabs, padding=8)
        tabs.add(fetch_tab, text="获取 OpenRouter 数据")
        tabs.add(local_tab, text="转换 / 合并")
        self._build_fetch(fetch_tab)
        box1 = ttk.LabelFrame(local_tab, text="功能一：转换 OpenRouter 模型 JSON", padding=8)
        box1.pack(fill="x", pady=5)
        self.source_var = tk.StringVar(value=str(DEFAULT_SOURCE))
        self.output_var = tk.StringVar(value=str(ROOT / "litellm-cost-map.json"))
        self._path_row(box1, 0, "OpenRouter 源文件：", self.source_var)
        self._path_row(box1, 1, "转换输出文件：", self.output_var, save=True)
        button = ttk.Button(box1, text="开始转换", command=lambda: self._start("convert"))
        button.grid(row=2, column=1, sticky="w", pady=8)
        self.buttons.append(button)
        ttk.Label(box1, text="沿用 CLI 转换规则；仅基础价格，未知字段省略，不展开条件价格覆盖。"
                  ).grid(row=3, column=0, columnspan=3, sticky="w")

        box2 = ttk.LabelFrame(local_tab, text="功能二：合并 LiteLLM cost map", padding=8)
        box2.pack(fill="x", pady=5)
        self.official_var = tk.StringVar(value=str(DEFAULT_OFFICIAL))
        self.custom_var = tk.StringVar(value=self.output_var.get())
        self.merged_var = tk.StringVar(value=str(ROOT / "model_prices_and_context_window-merged.json"))
        self._path_row(box2, 0, "官方 cost map：", self.official_var)
        self._path_row(box2, 1, "自定义 cost map：", self.custom_var)
        self._path_row(box2, 2, "合并输出文件：", self.merged_var, save=True)
        ttk.Label(box2, text="自定义完整块在前，官方非冲突块在后（块间空行）。\n"
                  "同名模型整条采用自定义配置；不改模型名、不混合字段、不修改输入文件。"
                  ).grid(row=3, column=0, columnspan=3, sticky="w", pady=6)
        button = ttk.Button(box2, text="开始合并", command=lambda: self._start("merge"))
        button.grid(row=4, column=1, sticky="w", pady=5)
        self.buttons.append(button)
        log_panel = ttk.Frame(self.main_panes)
        self.main_panes.add(log_panel, minsize=120, stretch="always")
        self.status = tk.StringVar(value="就绪")
        ttk.Label(log_panel, textvariable=self.status).pack(anchor="w", pady=5)
        logbox = ttk.LabelFrame(log_panel, text="运行日志 / 冲突模型（拖动上方分隔条调整大小）", padding=6)
        logbox.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(logbox, state="disabled", height=14)
        self.log.pack(fill="both", expand=True)

    def _build_fetch(self, parent):
        ttk.Label(parent, text="仅发送勾选参数。取消 offset 和 limit 可获取完整匹配列表；\n"
                  "输出模态未设置时服务默认 text；获取所有模态请启用 output_modalities 并选择 all。"
                  ).pack(anchor="w", pady=4)
        auth = ttk.Frame(parent)
        auth.pack(fill="x", pady=4)
        ttk.Label(auth, text="API Key（可选，仅在内存中）：").pack(side="left")
        self.api_key_var = tk.StringVar()
        ttk.Entry(auth, textvariable=self.api_key_var, show="*", width=55).pack(side="left", fill="x", expand=True)
        ttk.Label(parent, text="文档要求认证；若无密钥请求被拒绝，请填写 API Key。仅在内存使用。"
                  ).pack(anchor="w")
        params_tabs = ttk.Notebook(parent, height=250)
        params_tabs.pack(fill="both", expand=True, pady=4)
        self.query_rows = []
        for group, specs in PARAMETER_GROUPS.items():
            page = ttk.Frame(params_tabs)
            params_tabs.add(page, text=group)
            canvas = tk.Canvas(page, highlightthickness=0, height=240)
            scroll = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            params = ttk.Frame(canvas, padding=4)
            window = canvas.create_window((0, 0), window=params, anchor="nw")
            params.bind("<Configure>", lambda event, c=canvas: c.configure(scrollregion=c.bbox("all")))
            canvas.bind("<Configure>", lambda event, c=canvas, w=window: c.itemconfigure(w, width=event.width))
            params.columnconfigure(1, weight=1)
            for row, (name, label, kind, initial, choices, _, _) in enumerate(specs):
                enabled = tk.BooleanVar(value=name in {"limit", "sort"})
                value_var = tk.StringVar(value=initial)
                name_var = tk.StringVar(value=name)
                check = ttk.Checkbutton(params, text=name, variable=enabled)
                check.grid(row=row * 2, column=0, sticky="w", padx=4, pady=2)
                self.buttons.append(check)
                if kind == "enum":
                    editor = ttk.Combobox(params, textvariable=value_var, values=choices, state="readonly")
                    editor.grid(row=row * 2, column=1, sticky="ew", padx=4)
                elif kind == "multi":
                    editor = ttk.Frame(params)
                    editor.grid(row=row * 2, column=1, sticky="ew", padx=4)
                    variables = [(choice, tk.BooleanVar(value=choice == initial)) for choice in choices]
                    for index, (choice, selected) in enumerate(variables):
                        button = ttk.Checkbutton(editor, text=choice, variable=selected,
                            command=lambda vs=variables, target=value_var: target.set(",".join(k for k, v in vs if v.get())))
                        button.grid(row=index // 5, column=index % 5, sticky="w")
                        self.buttons.append(button)
                else:
                    ttk.Entry(params, textvariable=value_var).grid(row=row * 2, column=1, sticky="ew", padx=4)
                ttk.Label(params, text=label).grid(row=row * 2 + 1, column=1, sticky="w", padx=4, pady=(0, 4))
                self.query_rows.append((enabled, name_var, value_var))
        rss = ttk.Frame(params_tabs, padding=10)
        params_tabs.add(rss, text="RSS（不可用）")
        for name in ("use_rss", "use_rss_chat_links"):
            ttk.Checkbutton(rss, text=name, state="disabled").pack(anchor="w", pady=4)
        ttk.Label(rss, text="RSS 返回订阅源而非 JSON，不适用于模型 cost map 转换；这两个参数不会发送。"
                  ).pack(anchor="w", pady=8)
        destination = ttk.Frame(parent)
        destination.pack(fill="x", pady=4)
        self.fetch_output_var = tk.StringVar(value=str(ROOT / "openrouter-models-latest.json"))
        self._path_row(destination, 0, "原始数据保存为：", self.fetch_output_var, save=True)
        button = ttk.Button(parent, text="获取并保存 JSON", command=lambda: self._start("fetch"))
        button.pack(anchor="w", pady=6)
        self.buttons.append(button)
        ttk.Label(parent, text="只保存本次响应，不自动翻页；筛选结果可能不是完整模型列表。\n"
                  "获取成功后自动填入转换源路径。API Key 不写入日志或输出文件。"
                  ).pack(anchor="w")

    def _path_row(self, parent, row, label, var, save=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4)
        parent.columnconfigure(1, weight=1)
        button = ttk.Button(parent, text="另存为…" if save else "浏览…",
                            command=lambda: self._pick(var, save))
        button.grid(row=row, column=2, padx=5)
        self.buttons.append(button)

    def _pick(self, var, save):
        current = Path(var.get()) if var.get() else ROOT / "output.json"
        options = {"parent": self, "filetypes": [("JSON 文件", "*.json")],
                   "initialdir": str(current.parent)}
        if save:
            path = filedialog.asksaveasfilename(
                **options, defaultextension=".json", initialfile=current.name)
        else:
            path = filedialog.askopenfilename(**options)
        if path:
            var.set(path)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self, operation):
        if self.busy:
            return
        parameters, api_key = [], ""
        if operation == "fetch":
            inputs = []
            destination = self.fetch_output_var.get().strip()
            parameters = [(name.get(), value.get()) for enabled, name, value in self.query_rows if enabled.get()]
            api_key = self.api_key_var.get()
        elif operation == "convert":
            inputs = [self.source_var.get().strip()]
            destination = self.output_var.get().strip()
        else:
            inputs = [self.official_var.get().strip(), self.custom_var.get().strip()]
            destination = self.merged_var.get().strip()
        try:
            if not destination or not all(inputs):
                raise ValueError("请先选择所有输入文件并填写目标文件名。")
            if operation == "fetch":
                build_models_url(parameters)
            inputs = [str(Path(path).expanduser().resolve()) for path in inputs]
            for path in inputs:
                if not Path(path).is_file():
                    raise ValueError(f"输入文件不存在：{path}")
            output = check_destination(destination, inputs, overwrite=True)
            overwrite = output.exists()
            if overwrite and not messagebox.askyesno(
                    "确认覆盖", f"目标文件已存在：\n{output}\n\n是否覆盖？输入文件不会被修改。", parent=self):
                return
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc), parent=self)
            return
        self.busy = True
        self.status.set("正在处理，请稍候…")
        for button in self.buttons:
            button.configure(state="disabled")
        # Only immutable path snapshots cross threads; all Tk access stays here.
        threading.Thread(target=self._worker,
                         args=(operation, inputs, str(output), overwrite, parameters, api_key), daemon=True).start()

    def _worker(self, operation, inputs, destination, overwrite, parameters=(), api_key=""):
        try:
            if operation == "fetch":
                raw = do_fetch(parameters, api_key, destination, overwrite)
                message = (f"[获取成功] {len(raw['data'])} 个模型 → {destination}\n"
                           "已保存本次原始响应并校验 JSON；未自动翻页。")
                if isinstance(raw.get("total_count"), int) and raw["total_count"] > len(raw["data"]):
                    message += f"\n注意：total_count={raw['total_count']}，本次数据不完整。"
            elif operation == "convert":
                result = do_convert(inputs[0], destination, overwrite)
                message = f"[转换成功] {len(result)} 个模型 → {destination}\n已完成严格 JSON 回读校验。"
            else:
                official, custom, merged, conflicts = do_merge(*inputs, destination, overwrite)
                message = (f"[合并成功] → {destination}\n"
                           f"自定义：{len(custom)}；官方原始：{len(official)}；"
                           f"同名冲突：{len(conflicts)}；输出合计：{len(merged)}。\n"
                           "排列：自定义完整块 → 官方非冲突块。已完成严格 JSON 回读校验。")
                if conflicts:
                    message += (f"\n以下 {len(conflicts)} 个模型整条采用自定义配置：\n"
                                + "\n".join(conflicts))
            self.events.put((True, operation, destination, message))
        except Exception as exc:
            message = str(exc)
            if api_key.strip():
                message = message.replace(api_key.strip(), "[已隐藏 API Key]")
            self.events.put((False, operation, destination, message))

    def _poll(self):
        try:
            success, operation, destination, message = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for button in self.buttons:
                button.configure(state="normal")
            self.status.set("完成" if success else "失败，请查看日志")
            self._log(message if success else f"[失败] {message}")
            if success and operation == "fetch":
                self.source_var.set(destination)
            if success and operation == "convert":
                self.custom_var.set(destination)
            if not success:
                messagebox.showerror("操作失败", message, parent=self)
        self.after(100, self._poll)

    def _close(self):
        if self.busy:
            messagebox.showinfo("正在处理", "请等待当前文件操作完成后再关闭窗口。", parent=self)
        else:
            self.destroy()


if __name__ == "__main__":
    App().mainloop()
