"""
gui.py - 结构力学题库检索桌面端
"""

import os
import json
import re
import sys
import threading
from pathlib import Path

os.environ['no_proxy'] = '*'
os.environ['NO_PROXY'] = '*'

# ============================================================
# 读取 config.json（优先同目录，兼容打包后的路径）
# ============================================================

def _config_path():
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "config.json"

def load_config():
    p = _config_path()
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}

cfg = load_config()
ROOT = Path(cfg.get("root", r"D:\桌面\答疑、帮做\结构力学\帮做"))
ANSWER_OUTPUT = Path(cfg.get("answer_output", r"D:\桌面\答疑、帮做\答案输出"))
ZHIPUAI_API_KEY = cfg.get("zhipuai_api_key", "")
TOP_K = cfg.get("top_k", 5)

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
    answer as do_answer, load_chapter_excel
)
from zhipuai import ZhipuAI

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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

        # 右侧固定面板（%+按钮，不随滚动移动）
        self._right_panel = tk.Frame(result_frame)
        self._right_panel.pack(side="right", fill="y")

        # 分隔线
        tk.Frame(result_frame, width=2, bg="#cccccc").pack(side="right", fill="y", padx=2)

        # 左侧：只放路径，带水平滚动条
        left = tk.Frame(result_frame)
        left.pack(side="left", fill="both", expand=True)

        self._result_canvas = tk.Canvas(left)
        xscroll = tk.Scrollbar(left, orient="horizontal",
                               command=self._result_canvas.xview)
        self._result_canvas.configure(xscrollcommand=xscroll.set)
        xscroll.pack(side="bottom", fill="x")
        self._result_canvas.pack(side="top", fill="both", expand=True)

        self.result_list = tk.Frame(self._result_canvas)
        self._result_canvas.create_window((0, 0), window=self.result_list, anchor="nw")
        self.result_list.bind("<Configure>", self._on_result_resize)

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
        self._ch_cb["values"] = chapters
        if chapters and not self._chapter.get():
            self._chapter.set(chapters[0])

    # ----------------------------------------------------------
    # 检索
    # ----------------------------------------------------------

    def _do_search(self):
        chapter = self._chapter.get().strip()
        if not chapter:
            messagebox.showwarning("提示", "请选择章节")
            return

        self.btn_search.config(state="disabled")
        self.btn_store.config(state="disabled")
        self._set_status("识别中...")
        self._clear_results()

        def run():
            try:
                query_loads = self._get_query_loads()
                if query_loads is None:
                    return

                self._update_loads_display(query_loads)
                self._set_status("检索中...")

                # 捕获 search() 的输出结果
                results = self._run_search(query_loads, chapter)
                if self._mode.get() == "manual":
                    self.win.after(0, self._clear_loads)
                self.win.after(0, lambda: self._show_results(results))
            except Exception as e:
                import traceback
                msg = traceback.format_exc()
                self.win.after(0, lambda m=msg: messagebox.showerror("错误", m))
            finally:
                self.win.after(0, self._restore_buttons)

        threading.Thread(target=run, daemon=True).start()

    def _get_query_loads(self):
        mode = self._mode.get()
        if mode == "image":
            img = self._image_path.get().strip()
            if not img:
                self.win.after(0, lambda: messagebox.showwarning("提示", "请选择或拖入题目图片"))
                return None
            if not Path(img).exists():
                self.win.after(0, lambda: messagebox.showerror("错误", f"图片不存在：{img}"))
                return None
            client = ZhipuAI(api_key=ZHIPUAI_API_KEY)
            result = extract_loads(client, img)
            return result.get("loads", [])
        else:
            if not self._manual_loads:
                self.win.after(0, lambda: messagebox.showwarning("提示", "请至少添加一条荷载"))
                return None
            return list(self._manual_loads)

    def _update_loads_display(self, loads_list):
        text = "识别荷载：" + loads_to_display(loads_list)
        self.win.after(0, lambda: self.loads_result_label.config(text=text))

    def _run_search(self, query_loads, chapter):
        """直接调 search 逻辑，返回结果列表"""
        from search import fix_load_types, compute_similarity, load_chapter_excel
        import json as _json

        query_loads = fix_load_types(query_loads)
        df = load_chapter_excel(chapter)
        if df is None:
            return []

        results = []
        for _, row in df.iterrows():
            try:
                db_loads = _json.loads(row["荷载"]).get("loads", [])
            except Exception:
                db_loads = []
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
        paths = [{"rank": i+1, "path": str(ROOT / name), "score": score}
                 for i, (score, name) in enumerate(top)]
        last_file = ROOT / "_last_search.json"
        last_file.write_text(_json.dumps(paths, ensure_ascii=False), encoding="utf-8")

        return top

    def _show_results(self, results):
        self._clear_results()
        self._last_results = results

        if not results:
            tk.Label(self.result_list, text="无匹配结果", fg="gray").pack(anchor="w")
            self._set_status("无结果")
            return

        ROW_PAD = 4

        for rank, (score, name) in enumerate(results, 1):
            pct = round(score * 100)
            full_path = str(ROOT / name)

            # 左侧：路径 Entry
            text = f"{rank}.  {full_path}"
            e = tk.Entry(self.result_list, font=("Consolas", 9),
                         relief="flat", bd=0, readonlybackground="#f0f0f0")
            e.insert(0, text)
            e.config(state="readonly")
            e.pack(anchor="w", pady=ROW_PAD, ipady=6, fill="x", expand=True)

            # 右侧：% + 按钮
            r = rank
            right_row = tk.Frame(self._right_panel)
            right_row.pack(anchor="e", pady=ROW_PAD)
            tk.Label(right_row, text=f"{pct}%", width=5, anchor="center",
                     font=("", 9, "bold"),
                     fg="#27AE60" if pct == 100 else "#333").pack(side="left", padx=2)
            tk.Button(right_row, text="打开图片", width=8,
                      command=lambda p=full_path: self._open_file(p)).pack(side="left", padx=2)
            tk.Button(right_row, text="打开答案", width=8,
                      command=lambda rk=r: self._open_answer(rk)).pack(side="left", padx=(2, 4))

        self._set_status("检索完成")
        # 延迟刷新，等 Tkinter 完成像素级布局
        self.win.after(50, self._refresh_scroll)

    def _clear_results(self):
        for w in self.result_list.winfo_children():
            w.destroy()
        for w in self._right_panel.winfo_children():
            w.destroy()

    def _on_result_resize(self, event):
        self._result_canvas.configure(scrollregion=self._result_canvas.bbox("all"))

    def _refresh_scroll(self):
        self.result_list.update_idletasks()
        bbox = self._result_canvas.bbox("all")
        if bbox:
            self._result_canvas.configure(scrollregion=bbox)

    # ----------------------------------------------------------
    # 储存
    # ----------------------------------------------------------

    def _do_store(self):
        chapter = self._chapter.get().strip()
        if not chapter:
            messagebox.showwarning("提示", "请选择章节")
            return

        mode = self._mode.get()
        if mode == "image":
            img = self._image_path.get().strip()
            if not img:
                messagebox.showwarning("提示", "请选择图片")
                return
            if not Path(img).exists():
                messagebox.showerror("错误", f"图片不存在：{img}")
                return
        else:
            if not self._manual_loads:
                messagebox.showwarning("提示", "手动模式下请先添加荷载，储存需要同时指定图片路径")
                return
            img = self._image_path.get().strip()
            if not img:
                messagebox.showwarning("提示", "手动模式储存需要填写图片路径（作为题目名称）")
                return

        self.btn_search.config(state="disabled")
        self.btn_store.config(state="disabled")
        self._set_status("储存中...")

        # 确认弹窗
        chapter = self._chapter.get().strip()
        img = self._image_path.get().strip()
        confirm = messagebox.askyesno(
            "确认储存",
            f"章节：{chapter}\n图片：{Path(img).name if img else '（手动荷载）'}\n\n确认储存到题库？"
        )
        if not confirm:
            self._restore_buttons()
            self._set_status("已取消")
            return

        def run():
            try:
                if mode == "image":
                    do_store(chapter, image_path=img)
                else:
                    import json as _json
                    loads_json = _json.dumps({"loads": self._manual_loads}, ensure_ascii=False)
                    try:
                        rel = str(Path(img).relative_to(ROOT)).replace("\\", "/")
                    except ValueError:
                        rel = Path(img).name
                    do_store(chapter, rel_path=rel, loads_json=loads_json)

                self.win.after(0, lambda: self._set_status("储存完成"))
                self.win.after(0, lambda: messagebox.showinfo("完成", f"已储存到 {chapter}.xlsx"))
            except Exception as e:
                self.win.after(0, lambda: messagebox.showerror("储存失败", str(e)))
            finally:
                self.win.after(0, self._restore_buttons)

        threading.Thread(target=run, daemon=True).start()

    # ----------------------------------------------------------
    # 答案 / 打开文件
    # ----------------------------------------------------------

    def _open_file(self, path):
        try:
            os.startfile(path)
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
        paths = "\n".join(f'"{p}"' for p in img_paths)
        ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$files = New-Object System.Collections.Specialized.StringCollection
{chr(10).join(f'$files.Add("{p}")' for p in img_paths)}
[System.Windows.Forms.Clipboard]::SetFileDropList($files)
"""
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            self._set_status(f"已复制 {len(img_paths)} 张答案图片，可直接粘贴到微信")
        except Exception:
            self._set_status("答案已打开（剪贴板复制失败）")

    # ----------------------------------------------------------
    # 辅助
    # ----------------------------------------------------------

    def _restore_buttons(self):
        self.btn_search.config(state="normal")
        self.btn_store.config(state="normal")

    def _set_status(self, text):
        self.status_var.set(text)


# ============================================================
# 入口
# ============================================================

def main():
    if HAS_DND:
        win = TkinterDnD.Tk()
    else:
        win = tk.Tk()

    win.geometry("780x600")
    app = App(win)
    win.mainloop()


if __name__ == "__main__":
    main()
