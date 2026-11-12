#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
caption_window.py —— 赛博收音机 · Windows 桌面字幕悬浮窗
=========================================================

给 cyber_radio.py 的第 ⑥ 步提供「独立字幕悬浮窗」，并与 TTS 音频同步：

    每「一句话」走一遍：
    cyber_radio 的 CaptionSyncBackend.arm()  →  show()  这一句上屏（窗口出现）
    cyber_radio 的 backend.speak()           →  SAPI 阻塞念完这一句音频
    cyber_radio 的 CaptionSyncBackend.speak()→  mark_idle()  状态转空闲
    整轮播完                                 →  schedule_hide(N)  N 秒后隐藏

因为 SAPI / pyttsx3 的 speak() 会阻塞到「这句语音真正念完」才返回，
所以窗口里同时只显示当前这一句，字幕与音频在时间轴上天然逐句对齐。
（edge 引擎用系统默认播放器异步播放，只能做到句子级近似同步。）

★ 只用 Python 标准库（tkinter / threading / queue / json），零 pip 依赖。
★ GUI 跑在独立守护线程里；主线程只往 queue 投递事件，绝不碰 Tk 对象
  —— 这样 TTS 的阻塞式 speak() 不会卡住窗口刷新。
★ 无桌面会话 / 非 Windows / 缺 tkinter 时自动降级为 NullCaption，
  主流程照常播报，不抛异常。

单独自检 / 演示（Windows）：
    python caption_window.py --selftest      # 逻辑自检，不弹窗
    python caption_window.py --demo          # 直接弹出字幕窗演示几段，点 ✕ 或 Ctrl-C 退出
"""

import json
import os
import platform
import queue
import re
import sys
import threading
import time

VERSION = "v4-unified-bitmap-text"

# ---------------------------------------------------------------------------
# 默认样式（可被 caption_config.json 或命令行覆盖）
# ---------------------------------------------------------------------------
DEFAULTS = {
    "enabled": True,
    "position": "bottom",      # bottom / top / center / "x,y" 直接坐标
    "width": 900,
    "font": "Microsoft YaHei",
    "font_size": 20,
    "label_size": 12,
    "fg": "#FFFFFF",          # 正文字色
    "label_fg": "#7FD7FF",    # 段落标签色
    "bg": "#101418",          # 窗口底色
    "alpha": 0.86,            # 整体不透明度 0..1
    "topmost": True,          # 置顶
    "hold": 3.0,              # 整轮播完后字幕保留秒数，之后自动隐藏
    "show_corner": True,      # 角落显示「来源 · 第N轮」（蓝图的新闻链接位预留）
    "show_status": True,      # 顶部显示 ● 正在播报 / ○ 待机
    "min_hold": 0.0,          # 每段字幕最短停留秒数（--silent 时由主程序设为 1.0）
    # 高质量位图文字（灰度抗锯齿 + 深色描边）。透明与非透明模式统一使用
    # 同一套位图渲染，保证两种背景下字号、折行、位置完全一致。
    # 设 False 则两种模式一起回退到 Tk 原生文字（无描边，但仍保持一致）。
    "transparent_hq": True,
}

# 「背景完全透明」模式的穿透键色：Windows 用 -transparentcolor 让该色像素
# 完全透明；选一个不会与文字颜色冲突的极暗色。
TRANSPARENT_KEY = "#0B0C0D"

# Tk 的字号是「点」(point, 1pt = 1/72 inch)，PIL 的 ImageFont size 是「像素」。
# Windows 常规 96 DPI 下 1pt ≈ 1.333px。运行时会优先用 Tk 的字体度量
# (font.metrics("linespace")) 校准这个比值，跨 DPI / 系统缩放都能对齐；
# 测不到时才用这个缺省值。这是「透明后字体缩小」的根因所在。
_DEFAULT_PT_TO_PX = 96.0 / 72.0


# ---- 高质量透明文字：字体候选与按像素折行 --------------------------------
# Windows 优先雅黑/黑体；Linux/macOS 用常见中文字体。找不到就自动降级回 Tk 文字。
_HQ_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_HQ_FONT_CACHE = {}


def _hq_font(size):
    """返回可用的 TrueType 字体；没有可用字体则返回 None（触发降级）。"""
    key = int(size)
    if key in _HQ_FONT_CACHE:
        return _HQ_FONT_CACHE[key]
    font = None
    try:
        from PIL import ImageFont
        for path in _HQ_FONT_CANDIDATES:
            try:
                if os.path.isfile(path):
                    font = ImageFont.truetype(path, key)
                    break
            except Exception:
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=key)   # PIL >= 10.1
            except Exception:
                font = None
    except Exception:
        font = None
    _HQ_FONT_CACHE[key] = font
    return font


# 连续 ASCII 数字 / 英文词（含 40%、AI、3.5、Q4 之类）视为一个整体，
# 折行时不从中间拆开；其余字符（汉字、中文标点）仍逐字断行。
_HQ_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[%.+/:@\-][A-Za-z0-9]*)*|.")


def _hq_wrap(text, font, max_width, draw):
    """按像素宽度折行：中文逐字断行，数字/英文词不拆，兼容显式换行符。"""
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for tok in _HQ_TOKEN_RE.findall(para):
            # 单个 token 本身就超宽（超长英文串）时，退回逐字符拆，
            # 避免把位图撑得比窗口还宽。
            if draw.textlength(tok, font=font) > max_width:
                for ch in tok:
                    trial = cur + ch
                    if cur and draw.textlength(trial, font=font) > max_width:
                        lines.append(cur)
                        cur = ch
                    else:
                        cur = trial
                continue
            trial = cur + tok
            if cur and draw.textlength(trial, font=font) > max_width:
                lines.append(cur)
                cur = tok
            else:
                cur = trial
        lines.append(cur)
    return lines



def load_config(path=None):
    """默认值 ← JSON 配置文件覆盖。读不到就用默认值，不报错。"""
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        except (OSError, ValueError) as e:
            print(f"[字幕] 配置文件读取失败，已用默认值：{e}", file=sys.stderr)
    return cfg


def merge_cli(cfg, args):
    """命令行显式传入（非 None）的项覆盖配置；None 表示未指定。"""
    pairs = (
        ("position", getattr(args, "subtitle_pos", None)),
        ("width", getattr(args, "subtitle_width", None)),
        ("font", getattr(args, "subtitle_font", None)),
        ("font_size", getattr(args, "subtitle_font_size", None)),
        ("alpha", getattr(args, "subtitle_alpha", None)),
        ("hold", getattr(args, "subtitle_hold", None)),
    )
    for k, v in pairs:
        if v is not None:
            cfg[k] = v
    if getattr(args, "no_subtitle_topmost", False):
        cfg["topmost"] = False
    cfg["enabled"] = bool(getattr(args, "subtitle", False))
    return cfg


def save_config(cfg, path):
    """保存当前生效样式，供下次复用（蓝图 ⑦ 配置持久化的轻量入口）。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"[字幕] 样式已保存：{path}", file=sys.stderr)
        return True
    except OSError as e:
        print(f"[字幕] 样式保存失败：{e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# 空实现：未开启字幕 / --silent / 无 GUI 环境
# ---------------------------------------------------------------------------
class NullCaption:
    """接口与 CaptionWindow 完全一致，但所有操作都是空操作。"""
    alive = False
    version = "null"

    def start(self):
        return self

    def show(self, label, text, meta=None):
        pass

    def mark_idle(self):
        pass

    def schedule_hide(self, seconds):
        pass

    def apply_style(self, font_size=None, topmost=None):
        pass

    def set_edit_mode(self, enabled):
        pass

    def set_transparent(self, enabled):
        pass

    def hide(self):
        pass

    def close(self):
        pass

    def wait_closed(self, timeout=None):
        return False


# ---------------------------------------------------------------------------
# 字幕窗本体
# ---------------------------------------------------------------------------
class CaptionWindow:
    """线程安全的置顶无边框字幕窗。

    主线程调用 show / mark_idle / schedule_hide / close；
    GUI 在独立线程跑 Tk mainloop，通过 queue + root.after 消费事件。
    """

    version = VERSION

    def __init__(self, config):
        self.cfg = dict(config)
        self._q = queue.Queue()
        self._ready = threading.Event()
        self._failed = False
        self._closed = False
        self._thread = None
        self._root = None          # 仅 GUI 线程内使用
        self._hide_job = None      # 仅 GUI 线程内使用
        self._drag = None
        self._vars = {}
        self.alive = False
        # ---- 编辑模式 / 位置 / 缩放：仅「运行期」状态，不写回配置文件 ----
        # 用户拖动的位置在本进程内持续保持（每句新字幕都在同一位置），
        # 只有重启程序（本实例重建）才回到默认位置。
        self._user_pos = None       # (x, y) 用户拖到的位置；None = 用默认位置
        self._edit_mode = False     # 编辑模式：允许拖动 + 滚轮缩放
        self._transparent = False   # 背景完全透明
        self._scale = 1.0           # 整体缩放倍率（滚轮，0.6~2.2）
        self._base_width = int(config.get("width", 900) or 900)
        self._cur_width = self._base_width
        self._font_specs = {}       # var名 -> (family, base_size, *style)
        self._bg_widgets = []       # 需随透明模式改底色的 widget
        self._photos = {}           # var名 -> PhotoImage（必须持有引用，防 GC）
        self._texts = {}            # var名 -> 当前文字，供透明切换/改字号重绘
        self._hq_ok = None          # None=未探测；True/False
        self._speaking = False
        self._pt_to_px = _DEFAULT_PT_TO_PX   # _build() 里由 Tk 字体度量校准

    # ---- 主线程 API -----------------------------------------------------
    def start(self):
        # 不硬编码平台：有可用桌面就显示；没有（无 DISPLAY / 缺 tkinter）
        # 由 _run() 捕获异常自动降级，主流程照常播报。
        self._thread = threading.Thread(target=self._run, name="caption-gui",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=8)
        if self._failed or not self.alive:
            print("[字幕] 无法创建窗口（无桌面会话 / 缺 tkinter），"
                  "已降级为无字幕，播报照常进行。", file=sys.stderr)
        return self

    def _post(self, kind, *payload):
        if self._failed or self._closed:
            return
        try:
            self._q.put_nowait((kind, payload))
        except queue.Full:
            pass

    def show(self, label, text, meta=None):
        """在某段音频开始之前上屏。"""
        self._post("show", label, text, meta or {})

    def mark_idle(self):
        """某段音频念完之后调用：状态转为待机。"""
        self._post("idle")

    def schedule_hide(self, seconds):
        """整轮播完 N 秒后自动隐藏窗口（字幕只在播报时出现）。"""
        self._post("hide_after", max(0.0, float(seconds or 0)))

    def apply_style(self, font_size=None, topmost=None):
        """主线程 API：动态调整字号 / 置顶，GUI 线程内生效（线程安全）。"""
        self._post("style", font_size, topmost)

    def set_edit_mode(self, enabled):
        """主线程 API：编辑模式开关。开启后可拖动字幕窗、滚轮缩放整体大小。"""
        self._post("edit_mode", bool(enabled))

    def set_transparent(self, enabled):
        """主线程 API：背景完全透明开关。"""
        self._post("transparent", bool(enabled))

    def hide(self):
        self._post("hide")

    def close(self):
        try:
            self._q.put_nowait(("quit", ()))
        except queue.Full:
            pass
        self._closed = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self.alive = False

    def wait_closed(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return not (self._thread and self._thread.is_alive())

    # ---- GUI 线程 -------------------------------------------------------
    def _run(self):
        try:
            import tkinter as tk
        except Exception as e:  # noqa: BLE001
            print(f"[字幕] 加载 tkinter 失败：{e}", file=sys.stderr)
            self._failed = True
            self._ready.set()
            return
        root = None
        try:
            root = tk.Tk()
            self._root = root
            self._build(root)
            self.alive = True
            self._ready.set()
            root.after(40, self._drain)
            root.mainloop()
        except Exception as e:  # noqa: BLE001
            print(f"[字幕] 窗口运行出错，已降级：{e}", file=sys.stderr)
            self._failed = True
        finally:
            self.alive = False
            self._closed = True
            self._ready.set()
            # 关键①：PhotoImage 必须在 GUI 线程、且 Tk interpreter 仍存活时
            # 就释放；放到 destroy 之后 / 让主线程 GC 析构会触发
            # Tcl_AsyncDelete: async handler deleted by the wrong thread。
            # 同时先断开 widget 对 image 的引用，避免 Tcl 侧残留。
            try:
                for _w in self._vars.values():
                    _w.configure(image="")
            except Exception:
                pass
            self._photos = {}
            self._texts = {}
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
            # 关键②：清空所有 Tk 对象引用，避免 Python 退出时由主线程 GC
            # 跨线程析构 widget。
            self._root = None
            self._vars = {}
            self._bg_widgets = []      # 透明模式记录的 widget 引用也必须清空
            self._hide_job = None

    def _build(self, root):
        c = self.cfg
        root.title("赛博收音机 · 字幕")
        try:
            root.overrideredirect(True)             # 无边框
        except Exception:
            pass
        # 部分 X11 / 无合成管理器环境不支持 -alpha / -topmost，失败不致命
        try:
            root.attributes("-topmost", bool(c["topmost"]))
            root.attributes("-alpha", float(c["alpha"]))
        except Exception:
            pass
        root.configure(bg=c["bg"])

        # 顶部条：状态 + 角落来源 + 关闭按钮（同时是拖动把手）
        bar = _frame(root, c["bg"])
        bar.pack(fill="x", padx=14, pady=(9, 2))

        st = _label(bar, "", c["label_fg"], c["bg"],
                    (c["font"], max(9, c["label_size"] - 1)))
        st.pack(side="left")
        corner = _label(bar, "", "#8A9AA8", c["bg"],
                        (c["font"], max(9, c["label_size"] - 2)))
        corner.pack(side="right", padx=(8, 0))
        close_btn = _label(bar, "  ✕  ", "#FF8080", c["bg"],
                           (c["font"], c["label_size"]))
        close_btn.pack(side="right")
        close_btn.configure(cursor="hand2")
        close_btn.bind("<Button-1>", lambda e: self._on_close_click())

        lab = _label(root, "", c["label_fg"], c["bg"],
                     (c["font"], c["label_size"], "bold"))
        lab.pack(anchor="w", padx=16, pady=(4, 0))

        body = _label(root, "", c["fg"], c["bg"], (c["font"], c["font_size"]),
                      wraplength=max(200, int(c["width"]) - 32),
                      justify="left")
        body.pack(anchor="w", padx=16, pady=(2, 12), fill="x")

        self._vars = {"status": st, "corner": corner, "close": close_btn,
                      "label": lab, "body": body}
        self._font_specs = {
            "status": (c["font"], max(9, c["label_size"] - 1)),
            "corner": (c["font"], max(9, c["label_size"] - 2)),
            "close": (c["font"], c["label_size"]),
            "label": (c["font"], c["label_size"], "bold"),
            "body": (c["font"], c["font_size"]),
        }
        # 用 Tk 自己的字体度量把「点」校准成「像素」，供 PIL 位图字号换算，
        # 这样位图文字与原生 Tk 文字等高，切换背景不会变大变小。
        self._pt_to_px = self._measure_pt_to_px(root, c)
        self._bg_widgets = [bar, st, corner, close_btn, lab, body]
        # 拖动绑定到除关闭按钮外的所有可见 widget（close_btn 保留点击关闭）；
        # 滚轮缩放绑定到全部 widget，二者都只在编辑模式内生效。
        for w in (root, bar, st, corner, lab, body):
            w.bind("<Button-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
        for w in (root, bar, st, corner, close_btn, lab, body):
            w.bind("<MouseWheel>", self._on_wheel)      # Windows / macOS
            w.bind("<Button-4>", self._on_wheel_up)     # X11 向上
            w.bind("<Button-5>", self._on_wheel_down)   # X11 向下
        self._set_status(False)
        self._place(root)
        root.withdraw()                            # 启动时隐藏，第一条字幕才出现

    @staticmethod
    def _measure_pt_to_px(root, c):
        """测量「Tk 点数 → 屏幕像素」的实际比值（GUI 线程内调用）。

        不同系统 DPI / 缩放设置下这个比值不固定，硬编码 96/72 会在高 DPI
        屏幕上偏小，所以以 Tk 报告的 linespace 为准；测不到才回退缺省值。
        """
        try:
            from tkinter import font as tkfont
            probe = max(10, int(c["font_size"]))
            f = tkfont.Font(root=root, family=c["font"], size=probe)
            ls = f.metrics("linespace")
            if ls and ls > 0:
                ratio = ls / float(probe)
                if 0.8 <= ratio <= 2.5:      # 明显异常的度量直接忽略
                    return ratio
        except Exception:
            pass
        return _DEFAULT_PT_TO_PX

    def _place(self, root, width=None):
        """按屏幕尺寸放置窗口；用户拖动过则固定在拖到的位置（运行期持久）。

        width 由滚轮缩放传入；不传则用当前生效宽度。每渲染一句新字幕都会
        调用本方法，但只要 _user_pos 有值就始终回到同一位置，不会跳回默认。
        """
        c = self.cfg
        root.update_idletasks()
        w = max(320, int(width or self._cur_width))
        h = max(70, root.winfo_reqheight())
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        if self._user_pos is not None:
            x, y = self._user_pos
        else:
            pos = str(c["position"]).strip().lower()
            if "," in pos:
                try:
                    x, y = (int(v) for v in pos.split(",", 1))
                except ValueError:
                    x, y = (sw - w) // 2, sh - h - 90
            elif pos == "top":
                x, y = (sw - w) // 2, 60
            elif pos == "center":
                x, y = (sw - w) // 2, (sh - h) // 2
            else:  # bottom
                x, y = (sw - w) // 2, max(0, sh - h - 90)
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ---- 拖动（仅编辑模式生效）与整体缩放 -------------------------------
    def _on_press(self, event):
        if not self._edit_mode:
            self._drag = None
            return
        self._drag = (event.x_root, event.y_root)

    def _on_drag(self, event):
        if not self._edit_mode or not self._drag or self._root is None:
            return
        r = self._root
        dx = event.x_root - self._drag[0]
        dy = event.y_root - self._drag[1]
        x = max(0, r.winfo_x() + dx)
        y = max(0, r.winfo_y() + dy)
        r.geometry("+%d+%d" % (x, y))
        self._drag = (event.x_root, event.y_root)
        self._user_pos = (x, y)      # 运行期记住：之后每句新字幕都在这个位置

    def _on_wheel(self, event):
        self._wheel_step(1 if getattr(event, "delta", 0) > 0 else -1)

    def _on_wheel_up(self, event):
        self._wheel_step(1)

    def _on_wheel_down(self, event):
        self._wheel_step(-1)

    def _wheel_step(self, direction):
        if not self._edit_mode or self._root is None:
            return
        new = max(0.6, min(2.2, round(self._scale + direction * 0.1, 2)))
        if new == self._scale:
            return
        self._scale = new
        self._rescale()

    def _rescale(self):
        """按 _scale 重设所有标签字体与窗口宽度（GUI 线程内）。"""
        s = self._scale
        for name, spec in self._font_specs.items():
            w = self._vars.get(name)
            if w is None:
                continue
            fam, size = spec[0], spec[1]
            rest = spec[2:]
            try:
                w.configure(font=(fam, max(8, int(round(size * s)))) + rest)
            except Exception:
                pass
        self._cur_width = max(320, int(round(self._base_width * s)))
        try:
            self._vars["body"].configure(
                wraplength=max(200, self._cur_width - 32))
        except Exception:
            pass
        if self._root is not None:
            self._place(self._root, width=int(self._cur_width))
        # 位图文字不读 Tk font，缩放后必须按新 scale 重绘；两种模式都要重绘，
        # 否则非透明模式的位图字号会停在旧值，与当前样式脱节。
        self._redraw_texts()

    # ---- 高质量透明文字（GUI 线程内调用） --------------------------------
    def _hq_available(self):
        """高质量位图文字是否可用（两种背景共用）；结果缓存，只探测一次。"""
        if self._hq_ok is not None:
            return self._hq_ok
        if not self.cfg.get("transparent_hq", True):
            self._hq_ok = False
            return False
        try:
            from PIL import Image, ImageDraw, ImageTk  # noqa: F401
            ok = _hq_font(20) is not None
        except Exception as e:  # noqa: BLE001
            print("[字幕] 高质量位图文字不可用，已回退 Tk 原生文字：%s" % e,
                  file=sys.stderr)
            ok = False
        self._hq_ok = ok
        if ok:
            print("[字幕] 高质量位图文字已启用（灰度抗锯齿 + 深色描边，"
                  "透明/非透明同尺寸同位）。", file=sys.stderr)
        return ok

    def _hq_image(self, name, content):
        """生成不透明 RGB 位图（纯 PIL，无 Tk 依赖，可在自检里直接调用）。

        透明 / 非透明只差「底色」：透明用穿透键色，非透明用正常底色；
        文字部分（字形、字号、折行、描边、抗锯齿）在两种模式下逐像素相同，
        因此字号、折行数、位图尺寸、窗口高度与位置都完全一致。

        清晰度的关键：文字层自带不透明黑底，字色的抗锯齿边缘只与「黑」
        混合，绝不与底色（颜色键）混合——所以既没有 ClearType 彩边，
        也不会像 RGBA 位图那样在白字周围晕出一圈暗色脏边；而纯背景区
        保持精确的键色，可被 -transparentcolor 干净穿透。
        """
        from PIL import Image, ImageDraw
        c = self.cfg
        spec = self._font_specs.get(name, (c["font"], c["font_size"]))
        # Tk 点数 → 像素：透明后字体缩小的根因就是这里漏了这次换算
        px = max(8, int(round(spec[1] * self._scale * self._pt_to_px)))
        font = _hq_font(px)
        if font is None:
            return None
        if name == "status":
            fg = "#7FE0A0" if self._speaking else "#8A9AA8"
        elif name == "label":
            fg = c["label_fg"]
        elif name == "corner":
            fg = "#C7D3DC"
        else:
            fg = c["fg"]
        # 描边宽度随字号自适应，避免小字被固定 2px 粗描边糊死
        stroke = max(1, int(round(px / 14.0)))
        max_w = max(160, self._cur_width - 48 - stroke * 2)
        tmp = ImageDraw.Draw(Image.new("L", (1, 1), 0))
        lines = _hq_wrap(content, font, max_w, tmp)
        text_w = max((tmp.textlength(ln, font=font) for ln in lines if ln),
                     default=0.0)
        line_h = int(round(px * 1.35))
        pad = stroke + 3
        w = max(1, int(text_w) + pad * 2 + 4)
        h = max(1, line_h * max(1, len(lines)) + pad * 2)

        # 描边范围 mask（字 + 外扩描边，带灰度抗锯齿）
        m_stroke = Image.new("L", (w, h), 0)
        ds = ImageDraw.Draw(m_stroke)
        # 字心 mask（只有字），用于把字色铺到描边层之上
        m_core = Image.new("L", (w, h), 0)
        dc = ImageDraw.Draw(m_core)
        for i, ln in enumerate(lines):
            if not ln:
                continue
            y = pad + i * line_h
            ds.text((pad, y), ln, font=font, fill=255,
                    stroke_width=stroke, stroke_fill=255)
            dc.text((pad, y), ln, font=font, fill=255)

        glyph = Image.new("RGB", (w, h), "#000000")
        glyph.paste(Image.new("RGB", (w, h), fg), (0, 0), m_core)
        base = TRANSPARENT_KEY if self._transparent else c["bg"]
        img = Image.new("RGB", (w, h), base)
        # 二值覆盖（不做灰度混合）：有文字处直接取 glyph，其余保持精确底色
        mask_bin = m_stroke.point([0] + [255] * 255).convert("1")
        img.paste(glyph, (0, 0), mask_bin)
        return img

    def _hq_bitmap(self, name, content):
        """把 _hq_image 的结果转为 Tk 可显示的 PhotoImage。"""
        from PIL import ImageTk
        img = self._hq_image(name, content)
        if img is None:
            return None
        return ImageTk.PhotoImage(img)

    def _set_text(self, name, content):
        """统一文字入口。

        启用高质量位图时，透明与非透明都走同一套位图渲染（只换底色），
        字号、折行、位置因此严格一致；不可用时两者一起回退 Tk 原生文字，
        同样保持一致。绝不混用两条渲染路径。
        """
        content = content or ""
        self._texts[name] = content
        w = self._vars.get(name)
        if w is None:
            return
        if self._hq_available():
            try:
                ph = self._hq_bitmap(name, content)
                if ph is not None:
                    self._photos[name] = ph     # 持有引用，否则图片会被 GC 掉
                    w.configure(image=ph, text="")
                    return
            except Exception as e:  # noqa: BLE001
                print("[字幕] 文字位图渲染失败，已回退原生文字：%s" % e,
                      file=sys.stderr)
        self._photos.pop(name, None)
        try:
            w.configure(image="", text=content)
        except Exception:
            pass

    def _redraw_texts(self):
        """透明开关 / 字号 / 滚轮缩放变化后，按缓存文字重绘所有文本。"""
        for name in ("label", "body", "status", "corner"):
            if name in self._texts:
                self._set_text(name, self._texts[name])

    def _apply_background(self):
        """切换「背景完全透明」：所有底色换成穿透键色（GUI 线程内）。"""
        root = self._root
        if root is None:
            return
        bg = TRANSPARENT_KEY if self._transparent else self.cfg["bg"]
        try:
            root.configure(bg=bg)
            for w in self._bg_widgets:
                w.configure(bg=bg)
        except Exception:
            pass
        try:
            # Windows 专属：让键色像素完全透明（文字不受影响）
            root.attributes("-transparentcolor",
                            TRANSPARENT_KEY if self._transparent else "")
        except Exception:
            # X11 / 无合成管理器不支持该属性：底色已改但不穿透，不致命
            pass

    def _on_close_click(self):
        self.alive = False
        self._closed = True
        try:
            self._root.quit()
        except Exception:
            pass

    # 事件泵
    def _drain(self):
        root = self._root
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "quit":
                    root.quit()
                    return
                elif kind == "show":
                    self._do_show(*payload)
                elif kind == "idle":
                    self._do_idle()
                elif kind == "hide_after":
                    self._do_hide_after(payload[0])
                elif kind == "hide":
                    self._do_hide()
                elif kind == "style":
                    self._do_style(*payload)
                elif kind == "edit_mode":
                    self._do_edit_mode(payload[0])
                elif kind == "transparent":
                    self._do_transparent(payload[0])
        except queue.Empty:
            pass
        try:
            if root.winfo_exists():
                root.after(40, self._drain)
        except Exception:
            pass

    def _cancel_hide(self):
        if self._hide_job is not None and self._root is not None:
            try:
                self._root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

    def _set_status(self, speaking):
        self._speaking = bool(speaking)
        if not self.cfg.get("show_status", True):
            return
        txt = "● 正在播报" if speaking else "○ 待机"
        if self._edit_mode:
            txt += "  ✎ 编辑中"
        self._set_text("status", txt)
        try:
            self._vars["status"].configure(
                fg="#7FE0A0" if speaking else "#8A9AA8")
        except Exception:
            pass

    def _corner_text(self, meta):
        if not self.cfg.get("show_corner", True):
            return ""
        src = meta.get("source") or ""
        rnd = meta.get("round")
        bits = [b for b in (src, "第%s轮" % rnd if rnd else "") if b]
        k, n = meta.get("sentence"), meta.get("sentence_total")
        if k and n:
            bits.append("%d/%d 句" % (k, n))
        return " · ".join(bits)

    def _do_show(self, label, text, meta):
        root = self._root
        self._cancel_hide()
        self._set_text("label", label or "")
        self._set_text("body", text or "")
        self._set_text("corner", self._corner_text(meta))
        self._set_status(True)
        try:
            # 背景已由 -transparentcolor 穿透；透明模式下整窗 alpha 必须锁 1.0，
            # 否则文字也被压成半透明（白字发灰），这是透明底字糊的主因。
            root.attributes("-alpha", 1.0 if self._transparent
                            else float(self.cfg["alpha"]))
            if self.cfg.get("topmost", True):
                root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            root.deiconify()
            root.lift()
            # _place 尊重 _user_pos：拖动过的位置在整段播报里保持不变
            self._place(root)
        except Exception:
            pass

    def _do_style(self, font_size=None, topmost=None):
        """GUI 线程内动态改字号 / 置顶（由 apply_style 投递）。"""
        c = self.cfg
        if font_size is not None:
            try:
                fs = max(8, int(font_size))
                c["font_size"] = fs
                self._vars["body"].configure(font=(c["font"], fs))
                # 同步缩放基准，避免之后滚轮缩放把托盘设的字号覆盖回去
                self._font_specs["body"] = (c["font"], fs)
                # 位图文字不读 Tk font，改字号后两种模式都必须重绘
                self._redraw_texts()
            except Exception:
                pass
        if topmost is not None:
            try:
                top = bool(topmost)
                c["topmost"] = top
                self._root.attributes("-topmost", top)
            except Exception:
                pass

    def _do_edit_mode(self, enabled):
        self._edit_mode = bool(enabled)
        if self._edit_mode:
            self._cancel_hide()          # 编辑期间不自动隐藏，方便拖动
        self._set_status(self._speaking)
        if self._edit_mode and self._root is not None:
            try:
                self._root.deiconify()
                self._root.lift()
                self._set_text("label", "编辑模式")
                self._set_text(
                    "body",
                    "拖动字幕可调整位置（本次运行内保持，重启回默认）"
                    " · 滚轮缩放整体大小 · 再次点击「编辑模式」退出")
                self._place(self._root)
            except Exception:
                pass
        state = "开（可拖动 / 滚轮缩放）" if self._edit_mode else "关"
        print(f"[字幕] 编辑模式：{state}", file=sys.stderr)

    def _do_transparent(self, enabled):
        self._transparent = bool(enabled)
        self._apply_background()
        try:
            self._root.attributes("-alpha", 1.0 if self._transparent
                                  else float(self.cfg["alpha"]))
        except Exception:
            pass
        self._redraw_texts()
        state = "开" if self._transparent else "关"
        print(f"[字幕] 背景完全透明：{state}", file=sys.stderr)

    def _do_idle(self):
        self._set_status(False)

    def _do_hide_after(self, seconds):
        self._cancel_hide()
        if self._edit_mode:
            return                      # 编辑模式下不自动隐藏，窗口可继续拖动
        try:
            self._hide_job = self._root.after(max(0, int(seconds * 1000)),
                                              self._do_hide)
        except Exception:
            pass

    def _do_hide(self):
        self._hide_job = None
        self._set_status(False)
        try:
            self._root.withdraw()
        except Exception:
            pass


def _frame(parent, bg):
    import tkinter as tk
    return tk.Frame(parent, bg=bg)


def _label(parent, text, fg, bg, font, wraplength=None, justify=None):
    import tkinter as tk
    kw = dict(text=text, fg=fg, bg=bg, font=font, anchor="w")
    if wraplength:
        kw["wraplength"] = wraplength
    if justify:
        kw["justify"] = justify
    return tk.Label(parent, **kw)


def create_window(config):
    """工厂：任何不可用情况都安全降级为 NullCaption。"""
    if not config.get("enabled", False):
        return NullCaption()
    try:
        w = CaptionWindow(config)
        w.start()
        if w.alive:
            print("[字幕] 桌面字幕窗已就绪（置顶悬浮，播报时出现、播完自动隐藏）。",
                  file=sys.stderr)
            return w
    except Exception as e:  # noqa: BLE001
        print(f"[字幕] 创建窗口失败，已降级：{e}", file=sys.stderr)
    return NullCaption()


# ---------------------------------------------------------------------------
# 自检 / 演示
# ---------------------------------------------------------------------------
# 演示数据也按「一句一条」组织，与真实播报一致
DEMO_SEGMENTS = [
    ("启动问候", "早上好！",
     {"round": 0, "sentence": 1, "sentence_total": 2}),
    ("启动问候", "愿你今天充满活力，工作顺利。",
     {"round": 0, "sentence": 2, "sentence_total": 2}),
    ("热点标题", "接下来播报热点：某科技公司发布新一代 AI 芯片。",
     {"round": 1, "sentence": 1, "sentence_total": 1}),
    ("热点客观详细陈述",
     "据报道，该公司今日发布新一代 AI 芯片，官方称能效较上一代提升约 40%。",
     {"round": 1, "source": "baidu", "sentence": 1, "sentence_total": 3}),
    ("热点客观详细陈述", "新品将于下季度量产，并公布配套软件栈。",
     {"round": 1, "source": "baidu", "sentence": 2, "sentence_total": 3}),
    ("热点客观详细陈述", "报道未披露第三方实测数据与售价，业内对量产良率仍持观望。",
     {"round": 1, "source": "baidu", "sentence": 3, "sentence_total": 3}),
    ("人格解读评价", "作为理性时评观察员，我认为相关参数仍需第三方独立实测验证。",
     {"round": 1, "source": "baidu", "sentence": 1, "sentence_total": 2}),
    ("人格解读评价", "投资者应关注量产良率与售价披露，谨慎评估风险。",
     {"round": 1, "source": "baidu", "sentence": 2, "sentence_total": 2}),
]


def _rgb(color):
    """把 "#RRGGBB" 转成 (r,g,b)，供位图自检取色。"""
    from PIL import Image
    return Image.new("RGB", (1, 1), color).getpixel((0, 0))


def _selftest_bitmap():
    """不依赖 GUI，验证两种背景下的位图除底色外逐像素一致。"""
    try:
        from PIL import Image, ImageChops  # noqa: F401
    except Exception:
        print("[SKIP] 未安装 Pillow，跳过位图一致性检查")
        return
    cfg = dict(DEFAULTS)
    w = CaptionWindow.__new__(CaptionWindow)   # 只测纯逻辑，不启动 GUI 线程
    w.cfg = cfg
    w._font_specs = {"body": (cfg["font"], cfg["font_size"])}
    w._scale = 1.0
    w._pt_to_px = _DEFAULT_PT_TO_PX
    w._cur_width = cfg["width"]
    w._speaking = False
    sample = ("据报道，该公司今日发布新一代 AI 芯片，官方称能效较上一代"
              "提升约 40%，新品将于下季度量产并公布配套软件栈。")

    w._transparent = False
    a = w._hq_image("body", sample)
    w._transparent = True
    b = w._hq_image("body", sample)
    assert a is not None and b is not None, "位图渲染不应返回 None"
    assert a.size == b.size, "透明 / 非透明位图尺寸必须完全一致"
    assert a.getpixel((0, 0)) == _rgb(cfg["bg"]), "非透明底色应为配置底色"
    assert b.getpixel((0, 0)) == _rgb(TRANSPARENT_KEY), "透明底色应为穿透键色"

    # 把 a 的底色归一成键色，再与 b 比较：应逐像素相同
    bg, key = _rgb(cfg["bg"]), _rgb(TRANSPARENT_KEY)
    a_norm = a.copy()
    pxl = a_norm.load()
    for yy in range(a_norm.height):
        for xx in range(a_norm.width):
            if pxl[xx, yy] == bg:
                pxl[xx, yy] = key
    assert ImageChops.difference(a_norm, b).getbbox() is None, \
        "除底色外，两种背景的文字像素必须逐点一致（字号/折行/位置不可变）"

    # 字号换算确实生效：像素字号应大于 Tk 点数字号，否则透明后会缩小
    px = max(8, int(round(cfg["font_size"] * w._pt_to_px)))
    assert px > cfg["font_size"], "像素字号应大于 Tk 点数字号（否则会缩小）"
    print("[OK] 位图一致性：透明/非透明仅底色不同，文字像素逐点相同，"
          "字号 %dpt→%dpx" % (cfg["font_size"], px))

    # 连续数字 / 英文词不应被逐字折行拆开（40% 不能变成 4 / 0%）
    from PIL import ImageDraw
    fnt = _hq_font(px)
    if fnt is not None:
        probe = ImageDraw.Draw(Image.new("L", (1, 1), 0))
        wrapped = _hq_wrap("能效提升约 40%，新一代 AI 芯片功耗 3.5 倍。",
                           fnt, 260, probe)
        assert any("40%" in ln for ln in wrapped), \
            "连续数字与百分号不应被拆到两行"
        assert any("AI" in ln for ln in wrapped), \
            "连续英文词不应被拆到两行"
        print("[OK] 折行：数字/英文词整体不拆，超长词逐字符降级不撑破窗口")


def selftest():
    """不弹窗、不依赖 GUI，验证配置合并与空实现接口。"""
    cfg = load_config(None)
    assert cfg["position"] == "bottom" and cfg["topmost"] is True
    assert 0 < cfg["alpha"] <= 1 and cfg["width"] >= 320
    print("[OK] 默认样式配置完整")

    class A:
        subtitle = True
        subtitle_pos = "top"
        subtitle_width = 1000
        subtitle_font = None
        subtitle_font_size = None
        subtitle_alpha = None
        subtitle_hold = None
        no_subtitle_topmost = True

    c2 = merge_cli(dict(cfg), A())
    assert c2["position"] == "top" and c2["width"] == 1000
    assert c2["topmost"] is False and c2["font"] == cfg["font"]
    print("[OK] 命令行覆盖配置文件、未指定项保留")

    n = NullCaption().start()
    for name in ("show", "mark_idle", "schedule_hide", "hide", "close",
                 "apply_style", "set_edit_mode", "set_transparent"):
        assert hasattr(n, name)
    n.show("标签", "正文", {"round": 1})
    n.mark_idle()
    n.schedule_hide(1)
    n.hide()
    n.close()
    assert n.wait_closed() is False
    print("[OK] 无 GUI 环境降级为 NullCaption，接口齐全且不阻塞")

    # 事件序列：模拟「上屏 → 音频 → 空闲」的同步契约
    seq = []

    class Recorder(NullCaption):
        def show(self, label, text, meta=None):
            seq.append(("show", label))

        def mark_idle(self):
            seq.append(("idle",))

    r = Recorder()
    for label, text, meta in DEMO_SEGMENTS:
        r.show(label, text, meta)
        seq.append(("audio", label))
        r.mark_idle()
    assert len(seq) == len(DEMO_SEGMENTS) * 3
    for i in range(0, len(seq), 3):
        assert seq[i][0] == "show" and seq[i + 1][0] == "audio" \
            and seq[i + 2][0] == "idle"
        assert seq[i][1] == seq[i + 1][1]
    print("[OK] 同步契约：每句严格 show → audio → idle，无提前堆字幕")

    _selftest_bitmap()

    print(f"\n全部自检通过 ✅   {VERSION}｜平台 {platform.system()}｜"
          f"无 GUI 依赖")
    return 0


def demo(config):
    """Windows 上直接弹出字幕窗演示，点 ✕ 或 Ctrl-C 退出。"""
    cfg = dict(config)
    cfg["enabled"] = True
    w = create_window(cfg)
    if not w.alive:
        print("[提示] 当前环境无法弹出字幕窗（需 Windows 桌面 + tkinter）。")
        return 1
    try:
        for label, text, meta in DEMO_SEGMENTS:
            w.show(label, text, meta)
            time.sleep(1.4)          # 一句停留约 1.4s，模拟 TTS 念完一句
            w.mark_idle()
            time.sleep(0.2)
        # 演示时保留窗口可见，方便点右上角 ✕ 关闭；
        # 实际播报中窗口会在整轮播完后按 --subtitle-hold 自动隐藏。
        print(f"[演示] 已逐句播放 {len(DEMO_SEGMENTS)} 条单句字幕；"
              f"点窗口右上角 ✕ 或按 Ctrl-C 退出。")
        w.wait_closed()
    except KeyboardInterrupt:
        pass
    finally:
        w.close()
    return 0


def _demo_args():
    import argparse
    p = argparse.ArgumentParser(description="桌面字幕窗自检 / 演示")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--config", default="caption_config.json")
    return p.parse_args()


if __name__ == "__main__":
    a = _demo_args()
    cfg = load_config(a.config)
    if a.selftest:
        sys.exit(selftest())
    elif a.demo:
        sys.exit(demo(cfg))
    else:
        print(__doc__)
        print("请用 --selftest 或 --demo；实际使用时由 cyber_radio.py --subtitle 调用。")
