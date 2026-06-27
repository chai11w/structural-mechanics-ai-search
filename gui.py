"""
gui.py - 结构力学题库检索桌面端
"""

import os
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

os.environ['no_proxy'] = '*'
os.environ['NO_PROXY'] = '*'

# ============================================================
# 读取配置（优先同目录，兼容打包后的路径）
# ============================================================

def _config_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).parent

def _load_json(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}

def load_config():
    base = _config_dir()
    cfg = _load_json(base / "config.json")
    cfg.update(_load_json(base / "config.local.json"))
    return cfg

cfg = load_config()
ROOT = Path(cfg.get("root", r"D:\桌面\答疑、帮做\结构力学\帮做"))
ANSWER_OUTPUT = Path(cfg.get("answer_output", r"D:\桌面\答疑、帮做\答案输出"))
ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY") or cfg.get("zhipuai_api_key", "")
TOP_K = cfg.get("top_k", 5)
AUTO_CHAPTER_LABEL = "自动识别章节"

# 把配置注入 search 模块
os.environ.setdefault("ZHIPUAI_API_KEY", ZHIPUAI_API_KEY)

# 动态 patch search.py 的全局变量（search.py 硬编码了路径）
import search as _search_mod
_search_mod.ROOT = ROOT
_search_mod.ANSWER_OUTPUT = ANSWER_OUTPUT
_search_mod.ZHIPUAI_API_KEY = ZHIPUAI_API_KEY
_search_mod.LAST_SEARCH_FILE = ROOT / "_last_search.json"

from search import (
    extract_loads, search as do_search, store as do_store,
    answer as do_answer, load_chapter_excel, rerank_candidates,
    resolve_question_path, add_default_numeric_unit, normalize_query_loads
)
from multi_agent_pipeline import MultiAgentCoordinator, QwenClassifier, RuleRouter, symbolic_root
from scripts.audit_unindexed_questions import (
    CHAPTERS as AUDIT_CHAPTERS,
    DEFAULT_SPECIAL_INDEX,
    audit_chapter,
    load_special_index,
)
from scripts.store_unindexed_questions import (
    apply_ready_plans,
    classify_missing,
    write_reports as write_store_unindexed_reports,
)
from zhipuai import ZhipuAI

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ============================================================
# 工具
# ============================================================

def get_chapters():
    """扫 ROOT 下所有 .xlsx，返回章节名列表"""
    if not ROOT.is_dir():
        return []
    return sorted(
        p.stem for p in ROOT.glob("*.xlsx")
        if not p.stem.startswith("_")
    )

def loads_to_display(loads_list):
    """把荷载列表转成可读字符串"""
    if not loads_list:
        return "（未识别到荷载）"
    parts = []
    for item in loads_list:
        parts.append(f"{item['type']}:{item['raw']}")
    return "  |  ".join(parts)

def _path_to_display_name(path):
    p = Path(path)
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)

def _chapter_counts_from_apply_results(apply_results):
    counts = {}
    for result in apply_results:
        for rel_path in getattr(result, "appended", []):
            chapter = str(rel_path).replace("\\", "/").split("/", 1)[0]
            if chapter:
                counts[chapter] = counts.get(chapter, 0) + 1

    ordered = [(chapter, counts[chapter]) for chapter in AUDIT_CHAPTERS if chapter in counts]
    ordered.extend(
        (chapter, count)
        for chapter, count in sorted(counts.items())
        if chapter not in AUDIT_CHAPTERS
    )
    return ordered

# ============================================================
# 主窗口
# ============================================================

class App:
    def __init__(self, root_win):
        self.win = root_win
        self.win.title("结构力学题库检索")
        self.win.resizable(False, False)

        self._image_path = tk.StringVar()
        self._chapter = tk.StringVar()
        self._mode = tk.StringVar(value="image")   # "image" | "manual"
        self._manual_loads = []   # 手动输入的荷载列表
        self._last_results = []   # 上次检索结果
        self._preview_image_ref = None
        self._preview_paths = []
        self._preview_index = 0
        self._preview_hide_after_id = None
        self._multi_agent = MultiAgentCoordinator(top_k=TOP_K)

        self._build_ui()
        self._refresh_chapters()

    # ----------------------------------------------------------
    # UI 构建
    # ----------------------------------------------------------

    def _build_ui(self):
        pad = dict(padx=10, pady=6)

        # === 顶部：模式切换 ===
        mode_frame = tk.LabelFrame(self.win, text="输入模式", **pad)
        mode_frame.pack(fill="x", padx=12, pady=(12, 0))

        tk.Radiobutton(mode_frame, text="图片检索", variable=self._mode,
                       value="image", command=self._on_mode_change).pack(side="left", padx=8)
        tk.Radiobutton(mode_frame, text="手动输入荷载", variable=self._mode,
                       value="manual", command=self._on_mode_change).pack(side="left", padx=8)

        self.btn_audit_store = tk.Button(
            mode_frame,
            text="一键审查",
            width=9,
            bg="#F5A623",
            fg="white",
            font=("", 9, "bold"),
            command=self._do_audit_store,
        )
        self.btn_audit_store.pack(side="right", padx=8)

        # === 输入区容器（固定高度，两个子面板叠放，切换时 lift）===
        self.input_container = tk.Frame(self.win, height=80)
        self.input_container.pack(fill="x", padx=12, pady=(6, 0))
        self.input_container.pack_propagate(False)

        # --- 图片区 ---
        self.img_frame = tk.LabelFrame(self.input_container, text="题目图片", padx=10, pady=6)
        self.img_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        img_row = tk.Frame(self.img_frame)
        img_row.pack(fill="x")

        self.img_entry = tk.Entry(img_row, textvariable=self._image_path, width=48)
        self.img_entry.pack(side="left", padx=(0, 6))

        tk.Button(img_row, text="浏览", command=self._browse_image).pack(side="left")

        if HAS_DND:
            self.img_entry.drop_target_register(DND_FILES)
            self.img_entry.dnd_bind('<<Drop>>', self._on_drop)
            tk.Label(self.img_frame, text="支持拖拽图片到输入框", fg="gray", font=("", 9)).pack(anchor="w")
        else:
            tk.Label(self.img_frame, text="（安装 tkinterdnd2 可支持拖拽）", fg="gray", font=("", 9)).pack(anchor="w")

        # --- 手动荷载区 ---
        self.manual_frame = tk.LabelFrame(self.input_container, text="手动输入荷载", padx=10, pady=6)
        self.manual_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        add_row = tk.Frame(self.manual_frame)
        add_row.pack(fill="x", pady=(0, 4))

        tk.Label(add_row, text="类型:").pack(side="left")
        self._load_type = tk.StringVar(value="集中")
        type_cb = ttk.Combobox(add_row, textvariable=self._load_type,
                               values=["集中", "均布", "弯矩"], width=6, state="readonly")
        type_cb.pack(side="left", padx=4)

        tk.Label(add_row, text="标注:").pack(side="left")
        self._load_raw = tk.StringVar()
        tk.Entry(add_row, textvariable=self._load_raw, width=14).pack(side="left", padx=4)

        tk.Button(add_row, text="添加", command=self._add_load).pack(side="left", padx=4)
        tk.Button(add_row, text="清空", command=self._clear_loads).pack(side="left")

        self.loads_label = tk.Label(self.manual_frame, text="荷载列表：（空）",
                                    anchor="w", fg="#333", wraplength=400)
        self.loads_label.pack(fill="x")

        # === 章节 ===
        ch_frame = tk.Frame(self.win)
        ch_frame.pack(fill="x", padx=12, pady=(8, 0))

        tk.Label(ch_frame, text="章节：").pack(side="left")
        self._ch_cb = ttk.Combobox(ch_frame, textvariable=self._chapter,
                                   width=20, state="readonly")
        self._ch_cb.pack(side="left", padx=4)
        tk.Button(ch_frame, text="刷新", command=self._refresh_chapters).pack(side="left")

        # === 操作按钮 ===
        btn_frame = tk.Frame(self.win)
        btn_frame.pack(pady=10)

        self.btn_search = tk.Button(btn_frame, text="检  索", width=10,
                                    bg="#4A90D9", fg="white", font=("", 11, "bold"),
                                    command=self._do_search)
        self.btn_search.pack(side="left", padx=8)

        self.btn_store = tk.Button(btn_frame, text="储  存", width=10,
                                   bg="#27AE60", fg="white", font=("", 11, "bold"),
                                   command=self._do_store)
        self.btn_store.pack(side="left", padx=8)

        # === 识别结果显示 ===
        self.loads_result_label = tk.Label(self.win, text="", fg="#555",
                                           font=("", 10), wraplength=480)
        self.loads_result_label.pack(padx=12, anchor="w")

        # === 状态栏 ===
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(self.win, textvariable=self.status_var, relief="sunken",
                 anchor="w", fg="gray").pack(fill="x", side="bottom")

        # === 检索结果列表 ===
        result_frame = tk.LabelFrame(self.win, text="检索结果", padx=6, pady=6)
        result_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        yscroll = tk.Scrollbar(result_frame, orient="vertical",
                               command=self._on_results_yview)
        yscroll.pack(side="right", fill="y")

        # 固定操作列：只参与纵向滚动，不参与路径横向滚动
        actions = tk.Frame(result_frame)
        actions.pack(side="right", fill="y")
        self._actions_canvas = tk.Canvas(actions, bd=0, highlightthickness=0,
                                         width=198)
        self._actions_canvas.pack(side="top", fill="both", expand=True)
        self._actions_list = tk.Frame(self._actions_canvas)
        self._actions_window = self._actions_canvas.create_window(
            (0, 0), window=self._actions_list, anchor="nw"
        )
        self._actions_rows = tk.Frame(self._actions_list)
        self._actions_rows.pack(side="top", anchor="e")

        self._preview_box = tk.LabelFrame(self._actions_list, text="第一名预览", padx=4, pady=4)
        self._preview_box.pack(side="top", fill="x", padx=(2, 4), pady=(10, 0))
        preview_bg = self.win.cget("bg")
        self._preview_inner = tk.Frame(self._preview_box, bg=preview_bg)
        self._preview_inner.configure(width=180, height=180)
        self._preview_inner.pack(fill="both", expand=True)
        self._preview_label = tk.Label(
            self._preview_inner,
            text="暂无预览",
            bg=preview_bg,
            fg="gray",
            width=22,
            height=8,
            anchor="center",
        )
        self._preview_label.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._preview_label.bind("<Button-1>", self._open_current_preview)
        self._preview_prev = tk.Button(
            self._preview_inner,
            text="‹",
            width=1,
            relief="flat",
            bg=preview_bg,
            command=lambda: self._move_preview(-1),
        )
        self._preview_next = tk.Button(
            self._preview_inner,
            text="›",
            width=1,
            relief="flat",
            bg=preview_bg,
            command=lambda: self._move_preview(1),
        )
        self._preview_prev.place(relx=0.02, rely=0.5, anchor="w")
        self._preview_next.place(relx=0.98, rely=0.5, anchor="e")
        self._hide_preview_arrows()
        for w in (self._preview_box, self._preview_inner, self._preview_label, self._preview_prev, self._preview_next):
            w.bind("<Enter>", self._show_preview_arrows)
            w.bind("<Leave>", self._schedule_hide_preview_arrows)
        self._actions_list.bind("<Configure>", self._on_result_resize)
        self._actions_canvas.bind("<Configure>", self._on_actions_canvas_resize)
        self._actions_canvas.bind("<Enter>", self._bind_result_mousewheel)
        self._actions_canvas.bind("<Leave>", self._unbind_result_mousewheel)

        # 分隔线
        tk.Frame(result_frame, width=2, bg="#cccccc").pack(side="right", fill="y", padx=2)

        # 左侧：路径，带横向滚动条；纵向滚动与固定操作列同步
        left = tk.Frame(result_frame)
        left.pack(side="left", fill="both", expand=True)

        self._result_canvas = tk.Canvas(left, bd=0, highlightthickness=0)
        xscroll = tk.Scrollbar(left, orient="horizontal",
                               command=self._result_canvas.xview)
        self._result_canvas.configure(xscrollcommand=xscroll.set,
                                      yscrollcommand=yscroll.set)
        xscroll.pack(side="bottom", fill="x")
        self._result_canvas.pack(side="top", fill="both", expand=True)

        self.result_list = tk.Frame(self._result_canvas)
        self._result_window = self._result_canvas.create_window((0, 0), window=self.result_list, anchor="nw")
        self.result_list.bind("<Configure>", self._on_result_resize)
        self._result_canvas.bind("<Enter>", self._bind_result_mousewheel)
        self._result_canvas.bind("<Leave>", self._unbind_result_mousewheel)

        # 初始状态
        self._on_mode_change()

    def _on_mode_change(self):
        if self._mode.get() == "image":
            self.img_frame.lift()
        else:
            self.manual_frame.lift()

    # ----------------------------------------------------------
    # 图片浏览 / 拖拽
    # ----------------------------------------------------------

    def _browse_image(self):
        path = filedialog.askopenfilename(
            title="选择题目图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png"), ("所有文件", "*.*")]
        )
        if path:
            self._image_path.set(path)

    def _on_drop(self, event):
        raw = event.data.strip()
        # tkinterdnd2 返回的路径可能带花括号（路径含空格时）
        raw = re.sub(r'^\{(.+)\}$', r'\1', raw)
        self._image_path.set(raw)

    # ----------------------------------------------------------
    # 手动荷载
    # ----------------------------------------------------------

    def _add_load(self):
        raw = self._load_raw.get().strip()
        typ = self._load_type.get()
        if not raw:
            messagebox.showwarning("提示", "请填写荷载标注")
            return
        raw = add_default_numeric_unit(raw, typ)
        self._manual_loads.append({"type": typ, "raw": raw})
        self._load_raw.set("")
        self._refresh_loads_label()

    def _clear_loads(self):
        self._manual_loads.clear()
        self._refresh_loads_label()

    def _refresh_loads_label(self):
        if not self._manual_loads:
            self.loads_label.config(text="荷载列表：（空）")
        else:
            text = "  |  ".join(f"{x['type']}:{x['raw']}" for x in self._manual_loads)
            self.loads_label.config(text=f"荷载列表：{text}")

    # ----------------------------------------------------------
    # 章节
    # ----------------------------------------------------------

    def _refresh_chapters(self):
        chapters = get_chapters()
        values = [AUTO_CHAPTER_LABEL] + chapters
        self._ch_cb["values"] = values
        if not self._chapter.get() or self._chapter.get() not in values:
            self._chapter.set(AUTO_CHAPTER_LABEL)

    # ----------------------------------------------------------
    # 检索
    # ----------------------------------------------------------

    def _do_search(self):
        chapter = self._chapter_for_search()
        if not chapter:
            messagebox.showwarning("提示", "请选择章节")
            return

        self.btn_search.config(state="disabled")
        self.btn_store.config(state="disabled")
        self._set_status("识别中...")
        self._clear_results()

        def run():
            try:
                if self._mode.get() == "image":
                    query_image_path = self._get_query_image_path()
                    if query_image_path is None:
                        return
                    self._set_status("Qwen识别分类中...")
                    classified = self._multi_agent.qwen.classify_image(query_image_path)
                    query_loads = classified.get("loads", [])
                    self._update_loads_display(query_loads)
                    route, _ = self._multi_agent.router.route(query_loads)
                    if route.route == "needs_review":
                        self._set_status("需要人工复核...")
                    else:
                        self._set_status(f"{self._route_display_name(route.route)}候选检索中...")
                    pipeline_result = self._multi_agent.search_loads(
                        query_loads,
                        chapter,
                        query_image_path=query_image_path,
                        rerank=True,
                        rerank_top=3,
                        status_callback=lambda text, r=route.route: self._set_route_status(r, text),
                    )
                else:
                    query_loads = self._get_query_loads()
                    if query_loads is None:
                        return
                    self._set_status("检索中...")
                    pipeline_result = self._multi_agent.search_loads(
                        query_loads,
                        chapter,
                        rerank=False,
                        status_callback=self._set_status,
                    )
                    self.win.after(0, self._clear_loads)

                self._update_loads_display(
                    pipeline_result.loads,
                    chapter_text=self._pipeline_chapter_display(pipeline_result, chapter),
                )
                if pipeline_result.route.route == "needs_review":
                    msg = f"{pipeline_result.route.category}: {pipeline_result.route.reason}"
                    self.win.after(0, lambda m=msg: messagebox.showwarning("需要复核", m))
                elif pipeline_result.route.route == "needs_chapter":
                    if self._mode.get() == "image":
                        msg = "未能从题图自动识别章节，请手动选择章节后重新检索。"
                    else:
                        msg = "手动输入荷载时请先选择具体章节。"
                    self.win.after(0, lambda m=msg: messagebox.showwarning("请选择章节", m))
                self.win.after(0, lambda r=pipeline_result: self._show_pipeline_result(r))
            except Exception as e:
                import traceback
                msg = traceback.format_exc()
                self.win.after(0, lambda m=msg: messagebox.showerror("错误", m))
            finally:
                self.win.after(0, self._restore_buttons)

        threading.Thread(target=run, daemon=True).start()

    def _chapter_for_search(self):
        chapter = self._chapter.get().strip()
        if chapter == AUTO_CHAPTER_LABEL:
            return "auto"
        return chapter

    def _get_query_image_path(self):
        img = self._image_path.get().strip()
        if not img:
            self.win.after(0, lambda: messagebox.showwarning("提示", "请选择或拖入题目图片"))
            return None
        if not Path(img).exists():
            self.win.after(0, lambda: messagebox.showerror("错误", f"图片不存在：{img}"))
            return None
        return img

    def _get_query_loads(self):
        mode = self._mode.get()
        if mode == "image":
            return None
        else:
            if not self._manual_loads:
                self.win.after(0, lambda: messagebox.showwarning("提示", "请至少添加一条荷载"))
                return None
            return list(self._manual_loads)

    def _update_loads_display(self, loads_list, chapter_text=None):
        text = "识别荷载：" + loads_to_display(loads_list)
        if chapter_text:
            text += f"\n章节：{chapter_text}"
        self.win.after(0, lambda: self.loads_result_label.config(text=text))

    def _pipeline_chapter_display(self, pipeline_result, requested_chapter):
        if pipeline_result.route.route == "needs_chapter":
            return "未确定（请手动选择）"
        if pipeline_result.chapter:
            if requested_chapter == "auto":
                return f"{pipeline_result.chapter}（自动识别）"
            return f"{pipeline_result.chapter}（手动选择）"
        if requested_chapter != "auto":
            return f"{requested_chapter}（手动选择）"
        return None

    def _route_display_name(self, route):
        return {
            "main": "主库",
            "symbolic": "字母库",
            "needs_review": "复核区",
            "needs_chapter": "待选章节",
        }.get(route, route)

    def _set_route_status(self, route, text):
        if text.startswith("候选"):
            self._set_status(f"{self._route_display_name(route)}{text}")
        else:
            self._set_status(text)

    def _show_pipeline_result(self, pipeline_result):
        if pipeline_result.route.route == "needs_chapter":
            self._show_results([])
            self._set_status("未能自动识别章节，请手动选择")
            return
        self._show_results(pipeline_result.results)
        if pipeline_result.route.route == "needs_review":
            self._set_status("需要人工复核")
        elif pipeline_result.results:
            suffix = "（已复筛）" if pipeline_result.reranked else ""
            self._set_status(f"检索完成：{self._route_display_name(pipeline_result.route.route)}{suffix}")

    def _run_search(self, query_loads, chapter, query_image_path=None):
        """直接调 search 逻辑，返回结果列表"""
        from search import fix_load_types, compute_similarity, load_chapter_excel, _safe_parse_loads
        import json as _json

        query_loads = normalize_query_loads(query_loads)
        df = load_chapter_excel(chapter)
        if df is None:
            return []

        results = []
        for _, row in df.iterrows():
            db_loads = _safe_parse_loads(row["荷载"])
            db_loads = fix_load_types(db_loads)
            score = compute_similarity(query_loads, db_loads)
            results.append((score, row["题目名称"]))

        results.sort(key=lambda x: x[0], reverse=True)

        perfect = [r for r in results if r[0] >= 1.0]
        if len(perfect) >= TOP_K:
            top = perfect
        else:
            rest = [r for r in results if r[0] < 1.0][:TOP_K - len(perfect)]
            top = perfect + rest

        top = [r for r in top if r[0] > 0]

        # 写 last_search.json（供 answer 命令使用）
        paths = []
        for i, (score, name) in enumerate(top):
            resolved_path, resolved_name, repaired = resolve_question_path(
                name, chapter_name=chapter, update_excel=True
            )
            paths.append({
                "rank": i + 1,
                "path": str(resolved_path),
                "score": score,
                "name": resolved_name if repaired else name,
            })
        last_file = ROOT / "_last_search.json"
        last_file.write_text(
            _json.dumps([{k: v for k, v in item.items() if k != "name"} for item in paths], ensure_ascii=False),
            encoding="utf-8",
        )

        if query_image_path and paths:
            self._set_status("复筛中...")
            reranked = rerank_candidates(query_image_path, paths, top_n=3)
            if reranked:
                display_results = []
                last_paths = []
                for rank, item in enumerate(reranked, 1):
                    item = dict(item)
                    coarse_rank = item.get("rank")
                    item["rank"] = rank
                    item.setdefault("name", _path_to_display_name(item["path"]))
                    display_results.append(item)
                    last_paths.append({
                        "rank": rank,
                        "path": item["path"],
                        "score": item["score"],
                        "coarse_rank": coarse_rank,
                        "rerank_score": item.get("rerank_score"),
                        "final_score": item.get("final_score"),
                        "length_score": item.get("length_score"),
                        "length_reason": item.get("length_reason"),
                        "rerank_reason": item.get("rerank_reason"),
                    })
                last_file.write_text(_json.dumps(last_paths, ensure_ascii=False), encoding="utf-8")
                return display_results

        return paths

    def _show_results(self, results):
        self._clear_results()
        self._last_results = results

        if not results:
            tk.Label(self.result_list, text="无匹配结果", fg="gray").pack(anchor="w")
            self._clear_preview()
            self._set_status("无结果")
            return

        ROW_PAD = 4

        reranked = False
        preview_paths = []
        for rank, result in enumerate(results, 1):
            if isinstance(result, dict):
                score = result["score"]
                full_path = result["path"]
                rerank_score = result.get("rerank_score")
                final_score = result.get("final_score")
                reranked = reranked or rerank_score is not None
            else:
                score, name = result
                full_path = str(ROOT / name)
                rerank_score = None
                final_score = None
            preview_paths.append(full_path)
            pct = round(score * 100)

            # 左侧：路径 Entry（中文字符按2倍宽度估算）
            text = f"{rank}.  {full_path}"
            display_w = sum(2 if ord(c) > 127 else 1 for c in text) - 3
            e = tk.Entry(self.result_list, font=("Consolas", 9),
                         relief="flat", bd=0, readonlybackground="#f0f0f0",
                         width=max(display_w, 80))
            e.insert(0, text)
            e.config(state="readonly")
            e.pack(anchor="w", pady=(ROW_PAD, ROW_PAD + 1), ipady=6)

            # 右侧：% + 按钮
            r = rank
            actions_row = tk.Frame(self._actions_rows)
            actions_row.pack(anchor="e", pady=(ROW_PAD, ROW_PAD - 1))
            if rerank_score is None:
                score_text = f"{pct}%"
            else:
                display_score = final_score if final_score is not None else rerank_score
                score_text = f"{round(float(display_score) * 100)}%"
            tk.Label(actions_row, text=score_text, width=5, anchor="center",
                     font=("", 9, "bold"),
                     fg="#27AE60" if score_text == "100%" else "#333").pack(side="left", padx=2)
            tk.Button(actions_row, text="打开图片", width=8,
                      command=lambda p=full_path: self._open_file(p)).pack(side="left", padx=2)
            tk.Button(actions_row, text="打开答案", width=8,
                      command=lambda rk=r: self._open_answer(rk)).pack(side="left", padx=(2, 4))

        self._set_preview_paths(preview_paths)
        self._add_result_preview_spacer()
        self._set_status("检索完成（已复筛）" if reranked else "检索完成")
        # 延迟刷新，等 Tkinter 完成像素级布局
        self.win.after(50, self._refresh_scroll)

    def _clear_results(self):
        for w in self.result_list.winfo_children():
            w.destroy()
        for w in self._actions_rows.winfo_children():
            w.destroy()
        self._clear_preview()

    def _add_result_preview_spacer(self):
        self._preview_box.update_idletasks()
        height = max(self._preview_box.winfo_reqheight() + 10, 160)
        tk.Frame(self.result_list, height=height).pack(anchor="w", fill="x")

    def _clear_preview(self):
        self._preview_paths = []
        self._preview_index = 0
        self._preview_image_ref = None
        self._preview_box.config(text="第一名预览")
        self._preview_label.config(image="", text="暂无预览", cursor="")
        self._hide_preview_arrows()

    def _set_preview_paths(self, image_paths):
        self._preview_paths = list(image_paths or [])
        self._preview_index = 0
        self._set_preview(self._preview_paths[0] if self._preview_paths else None)

    def _move_preview(self, delta):
        if not self._preview_paths:
            return
        self._preview_index = (self._preview_index + delta) % len(self._preview_paths)
        self._set_preview(self._preview_paths[self._preview_index])

    def _set_preview(self, image_path):
        if not image_path:
            self._clear_preview()
            return
        self._preview_box.config(text=f"第{self._preview_index + 1}名预览")
        if not HAS_PIL:
            self._preview_label.config(image="", text="未安装 Pillow", cursor="")
            return

        path, _, _ = resolve_question_path(image_path, update_excel=False)
        if not path.is_file():
            self._preview_label.config(image="", text="图片不存在", cursor="")
            return

        try:
            img = Image.open(path)
            img.thumbnail((210, 170), Image.LANCZOS)
            self._preview_image_ref = ImageTk.PhotoImage(img)
            self._preview_label.config(image=self._preview_image_ref, text="", cursor="hand2")
        except Exception as exc:  # noqa: BLE001
            self._preview_image_ref = None
            self._preview_label.config(image="", text=f"预览失败\n{str(exc)[:40]}", cursor="")

    def _open_current_preview(self, _event=None):
        if not self._preview_paths:
            return
        self._open_file(self._preview_paths[self._preview_index])

    def _show_preview_arrows(self, _event=None):
        if self._preview_hide_after_id:
            self.win.after_cancel(self._preview_hide_after_id)
            self._preview_hide_after_id = None
        if len(self._preview_paths) > 1:
            self._preview_prev.place(relx=0.02, rely=0.5, anchor="w")
            self._preview_next.place(relx=0.98, rely=0.5, anchor="e")
            self._preview_prev.lift()
            self._preview_next.lift()

    def _schedule_hide_preview_arrows(self, _event=None):
        if self._preview_hide_after_id:
            self.win.after_cancel(self._preview_hide_after_id)
        self._preview_hide_after_id = self.win.after(120, self._hide_preview_arrows_if_pointer_outside)

    def _hide_preview_arrows_if_pointer_outside(self):
        self._preview_hide_after_id = None
        x = self.win.winfo_pointerx()
        y = self.win.winfo_pointery()
        left = self._preview_box.winfo_rootx()
        top = self._preview_box.winfo_rooty()
        right = left + self._preview_box.winfo_width()
        bottom = top + self._preview_box.winfo_height()
        if not (left <= x <= right and top <= y <= bottom):
            self._hide_preview_arrows()

    def _hide_preview_arrows(self):
        self._preview_prev.place_forget()
        self._preview_next.place_forget()

    def _on_result_resize(self, event):
        self._sync_result_scrollregions()

    def _on_actions_canvas_resize(self, event):
        self._sync_result_scrollregions()

    def _refresh_scroll(self):
        self.result_list.update_idletasks()
        self._actions_list.update_idletasks()
        self._sync_result_scrollregions()

    def _sync_result_scrollregions(self):
        result_bbox = self._result_canvas.bbox("all")
        if result_bbox:
            self._result_canvas.configure(scrollregion=result_bbox)
        actions_bbox = self._actions_canvas.bbox("all")
        if actions_bbox:
            self._actions_canvas.configure(scrollregion=actions_bbox)

    def _on_results_yview(self, *args):
        self._result_canvas.yview(*args)
        self._actions_canvas.yview(*args)

    def _bind_result_mousewheel(self, _event=None):
        self._result_canvas.bind_all("<MouseWheel>", self._on_result_mousewheel)

    def _unbind_result_mousewheel(self, _event=None):
        self._result_canvas.unbind_all("<MouseWheel>")

    def _on_result_mousewheel(self, event):
        delta = int(-1 * (event.delta / 120))
        self._result_canvas.yview_scroll(delta, "units")
        self._actions_canvas.yview_scroll(delta, "units")

    # ----------------------------------------------------------
    # 储存
    # ----------------------------------------------------------

    def _do_store(self):
        mode = self._mode.get()
        if mode == "manual":
            messagebox.showwarning("提示", "手动模式不支持储存")
            return
        img = self._image_path.get().strip()
        if not img:
            messagebox.showwarning("提示", "请选择图片")
            return
        if not Path(img).exists():
            messagebox.showerror("错误", f"图片不存在：{img}")
            return

        # 从图片路径提取章节
        try:
            rel = Path(img).relative_to(ROOT)
            chapter = rel.parts[0]
        except (ValueError, IndexError):
            messagebox.showerror("错误", "图片路径不在题库目录下，无法确定章节")
            return

        confirm = messagebox.askyesno("确认储存", f"章节：{chapter}\n图片：{Path(img).name}\n\n确认储存到题库？")
        if not confirm:
            return

        self.btn_search.config(state="disabled")
        self.btn_store.config(state="disabled")
        self._set_status("识别中...")

        def run():
            try:
                client = ZhipuAI(api_key=ZHIPUAI_API_KEY)
                result = extract_loads(client, img)
                loads_list = result.get("loads", [])

                rel_path = str(Path(img).relative_to(ROOT)).replace("\\", "/")
                import json as _json
                loads_json = _json.dumps(result, ensure_ascii=False)
                do_store(chapter, rel_path=rel_path, loads_json=loads_json)

                df = load_chapter_excel(chapter)
                row_num = df[df["题目名称"] == rel_path].index[0] + 2  # Excel行号（含表头）
                loads_text = loads_to_display(loads_list)
                self.win.after(0, lambda: messagebox.showinfo("储存成功", f"位置：{chapter}.xlsx 第{row_num}行\n荷载：{loads_text}"))
                self.win.after(0, lambda: self._set_status("储存完成"))
            except Exception as e:
                self.win.after(0, lambda: messagebox.showerror("储存失败", str(e)))
            finally:
                self.win.after(0, self._restore_buttons)

        threading.Thread(target=run, daemon=True).start()

    # ----------------------------------------------------------
    # 一键审查补库
    # ----------------------------------------------------------

    def _do_audit_store(self):
        self.btn_search.config(state="disabled")
        self.btn_store.config(state="disabled")
        self.btn_audit_store.config(state="disabled")
        self._set_status("扫描漏存题目中...")

        def run():
            try:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                symbolic = symbolic_root(ROOT)
                special_index = DEFAULT_SPECIAL_INDEX
                special_keys = load_special_index(special_index, main_root=ROOT)

                audits = [audit_chapter(ROOT, symbolic, chapter, special_keys) for chapter in AUDIT_CHAPTERS]
                missing = sorted(
                    [rel for audit in audits for rel in audit.missing],
                    key=lambda item: item.casefold(),
                )
                special_count = sum(len(audit.ignored_special) for audit in audits)

                output_dir = Path(__file__).resolve().parent / ".tmp_audit" / f"gui_store_unindexed_{stamp}"
                backup_dir = Path(__file__).resolve().parent / "backups" / f"gui_store_unindexed_{stamp}"

                if missing:
                    self._set_status(f"识别并入库中...（{len(missing)}题）")
                    plans = classify_missing(
                        missing,
                        root=ROOT,
                        symbolic=symbolic,
                        qwen=QwenClassifier(timeout=180, use_cache=True),
                        router=RuleRouter(),
                        sleep=0.0,
                    )
                    apply_results = apply_ready_plans(plans, dry_run=False, backup_dir=backup_dir)
                else:
                    plans = []
                    apply_results = []

                write_store_unindexed_reports(
                    plans,
                    apply_results,
                    output_dir=output_dir,
                    root=ROOT,
                    symbolic=symbolic,
                    dry_run=False,
                    special_index=special_index,
                    backup_dir=backup_dir,
                )

                ready_count = sum(1 for plan in plans if plan.status == "ready")
                review_count = sum(1 for plan in plans if plan.status != "ready")
                appended_count = sum(len(result.appended) for result in apply_results)
                skipped_count = sum(len(result.skipped_existing) for result in apply_results)
                chapter_counts = _chapter_counts_from_apply_results(apply_results)

                message = (
                    f"发现未入库：{len(missing)}\n"
                    f"可自动入库：{ready_count}\n"
                    f"已入库：{appended_count}\n"
                    f"需人工复核：{review_count}\n"
                    f"特殊排除：{special_count}"
                )
                if skipped_count:
                    message += f"\n已存在跳过：{skipped_count}"
                message += "\n\n入库章节："
                if chapter_counts:
                    message += "\n" + "\n".join(f"{chapter}：{count}题" for chapter, count in chapter_counts)
                else:
                    message += "无"

                self.win.after(0, lambda: messagebox.showinfo("一键审查完成", message))
                self.win.after(0, lambda: self._set_status(f"一键审查完成：已入库 {appended_count}，需复核 {review_count}"))
                self.win.after(0, self._refresh_chapters)
            except Exception:
                import traceback
                msg = traceback.format_exc()
                self.win.after(0, lambda m=msg: messagebox.showerror("一键审查失败", m))
                self.win.after(0, lambda: self._set_status("一键审查失败"))
            finally:
                self.win.after(0, self._restore_buttons)

        threading.Thread(target=run, daemon=True).start()

    # ----------------------------------------------------------
    # 答案 / 打开文件
    # ----------------------------------------------------------

    def _open_file(self, path):
        try:
            resolved, _, _ = resolve_question_path(path, update_excel=False)
            os.startfile(str(resolved))
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _open_answer(self, rank):
        try:
            do_answer(rank)
            if ANSWER_OUTPUT.is_dir():
                imgs = sorted(
                    list(ANSWER_OUTPUT.glob("*.jpg")) +
                    list(ANSWER_OUTPUT.glob("*.jpeg")) +
                    list(ANSWER_OUTPUT.glob("*.png"))
                )
                if imgs:
                    import time
                    for img in reversed(imgs):
                        os.startfile(str(img))
                        time.sleep(0.3)
                    self._copy_images_to_clipboard(imgs)
                else:
                    messagebox.showwarning("提示", "未找到答案文件")
            else:
                messagebox.showwarning("提示", "答案输出目录不存在")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _copy_images_to_clipboard(self, img_paths):
        """把图片文件列表复制到 Windows 剪贴板（文件复制，微信可直接粘贴）"""
        import subprocess
        ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$files = New-Object System.Collections.Specialized.StringCollection
{chr(10).join(f'$files.Add("{p}")' for p in img_paths)}
[System.Windows.Forms.Clipboard]::SetFileDropList($files)
"""
        try:
            r = subprocess.run(
                ["powershell", "-STA", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if r.returncode == 0:
                self._set_status(f"已复制 {len(img_paths)} 张答案图片，可直接粘贴到微信")
            else:
                err = r.stderr.decode("utf-8", errors="replace").strip()
                self._set_status(f"剪贴板复制失败: {err[:60]}" if err else "剪贴板复制失败")
        except Exception:
            self._set_status("答案已打开（剪贴板复制失败）")

    # ----------------------------------------------------------
    # 辅助
    # ----------------------------------------------------------

    def _restore_buttons(self):
        self.btn_search.config(state="normal")
        self.btn_store.config(state="normal")
        self.btn_audit_store.config(state="normal")

    def _set_status(self, text):
        if threading.current_thread() is threading.main_thread():
            self.status_var.set(text)
        else:
            self.win.after(0, lambda: self.status_var.set(text))


# ============================================================
# 入口
# ============================================================

def main():
    if HAS_DND:
        win = TkinterDnD.Tk()
    else:
        win = tk.Tk()

    win.geometry("520x600")
    app = App(win)
    win.mainloop()


if __name__ == "__main__":
    main()
