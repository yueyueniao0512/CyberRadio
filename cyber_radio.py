#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cyber_radio.py —— 赛博收音机 · 一体化主程序
============================================

把 time_greeter.py / hotsearch_monitor_03.py / tts_broadcaster.py 串成一条链路：

  任意时刻启动
    → [1] 读本机时间 → 匹配 6 时段 → LLM 生成「启动问候」→ TTS 念出来
    → [2] 热搜榜拉取 → 历史队列去重（已播报的跳过，继续取下一条）
           → 联网检索 + 抓详情正文 → 约 600 字材料投入 LLM
           → 生成两段式文稿（客观详细陈述 / 预设人格解读评价）
    → [3] 把文稿按「句」切开交给 TTS：一句话一段音频、一条字幕

★ 核心设计：三个阶段共用同一个 llama-server 进程（只加载一次 7B 权重），
  而不是像三个独立 CLI 串行那样加载三次。问候与文稿都是同一个 session 的
  /v1/chat/completions 调用。

★ 交付形态：只新增本文件，不改动、不复制三个现有程序；运行时按文件名
  import 同目录下的 hotsearch_monitor_03.py 与 tts_broadcaster.py 复用其能力。
  时段逻辑（6 段划分 / 问候提示词）从 time_greeter.py 移植为内联实现，
  这样本程序完全只用标准库，不引入 requests，也不与旧文件产生版本耦合。

常用命令（Windows PowerShell，1109001 目录下，单行可直接复制）
--------------------------------------------------------------
  # 0) 全链路自检：不加载模型、不联网、不出声（强烈建议先跑这个）
  python cyber_radio.py --selftest

  # 1) 单次完整播报（开机后运行一次，就是需求里描述的那一轮）
  python cyber_radio.py --model-dir models --llama-dir llama-bin --count 1

  # 2) 只跑文字链路、不出声（先确认模型与文稿，再开 TTS）
  python cyber_radio.py --silent --model-dir models --llama-dir llama-bin --count 1

  # 3) 常驻收音机：每 5 分钟一轮，去重队列自动跳过已播报热点，Ctrl-C 退出
  python cyber_radio.py --loop --interval 300 --model-dir models --llama-dir llama-bin

  # 4) 查看系统语音 / 输出字幕 JSON 流（供外部程序订阅）
  python cyber_radio.py --list-voices
  python cyber_radio.py --captions --captions-file captions.jsonl --model-dir models --llama-dir llama-bin

  # 5) 开启 Windows 桌面字幕悬浮窗（一句话一段字幕，与 TTS 音频逐句同步）
  python cyber_radio.py --subtitle --model-dir models --llama-dir llama-bin --count 1
  python cyber_radio.py --subtitle-demo          # 不加载模型，先弹出字幕窗看效果

说明：默认「启动问候」也会由 TTS 念出来（收音机开机音 semantics）；
      若只想念新闻文稿，加 --no-greeting-tts。
      --loop 时开机问候只在启动时播 1 次，后续各轮只播新闻。
      字幕窗由 caption_window.py 提供（标准库 tkinter，零 pip 依赖）。
★ 逐句字幕：文稿不再整段上屏，而是按中文句末标点切成一句句，
  每句单独「上屏 → TTS 念完 → 转空闲」，窗口里同时只显示当前这一句。
  句长上限 --max-sentence-chars（默认 48 字），句间停顿 --sentence-pause。
"""
import argparse
import contextlib
import datetime as _dt
import importlib.util
import io
import json
import os
import queue
import re
import sys
import threading
import time
import types
import platform

try:
    import pystray
    from PIL import Image
    TRAY_AVAILABLE = True
    TRAY_IMPORT_ERROR = ""
except Exception as _tray_err:  # noqa: BLE001 - 后端不可用会抛 ValueError 等
    TRAY_AVAILABLE = False
    TRAY_IMPORT_ERROR = "%s: %s" % (type(_tray_err).__name__, _tray_err)

VERSION = "v6-edit-mode"

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# [A] 时间感知：移植自 time_greeter.py（仅标准库，避免 requests 依赖）
# ---------------------------------------------------------------------------

# (名称, 起始小时, 结束小时)，左闭右开；夜间 [21,5) 跨零点
PERIODS = [
    ("清晨", 5, 9),
    ("上午", 9, 12),
    ("午间", 12, 14),
    ("下午", 14, 18),
    ("傍晚", 18, 21),
    ("夜间", 21, 5),
]

PERIOD_CONTEXT = {
    "清晨": "天刚亮不久，是一天的开始，人们可能刚起床或在准备出门。",
    "上午": "上午时光，人们大多开始工作或学习，精力较好。",
    "午间": "中午前后，接近或正在午休/午餐时间，适合简短问候。",
    "下午": "下午时段，工作或学习进行中，略显疲惫但仍在坚持。",
    "傍晚": "傍晚日落时分，一天的工作接近收尾，准备回家或吃晚饭。",
    "夜间": "深夜时分，大多数人已休息或准备休息，环境安静。",
}

WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

GREETING_SYSTEM = (
    "你是一个温暖、自然、得体的中文问候助手。"
    "你会根据用户告知的「当前本地时间」和「时段」，生成一句贴合当下情境的问候语。"
    "要求：只输出问候语本身，1-2 句话，不要解释、不要前缀、不要引号、不要多余空行。"
)
GREETING_USER_TMPL = (
    "现在是本地时间 {date} {weekday} {time}，属于「{period}」。\n"
    "情境：{context}\n"
    "请据此输出一句自然、真诚的中文问候语。"
)
GREETING_FALLBACK = {
    "清晨": "早安，新的一天开始了，愿你今天元气满满，一切顺心。",
    "上午": "上午好呀，祝你工作学习都专注高效，心情也舒朗。",
    "午间": "中午好，记得好好吃顿饭、稍微歇一歇，给身体充个电。",
    "下午": "下午好，离今天收工又近了一步，稳住节奏，加油。",
    "傍晚": "傍晚好，忙碌的一天渐渐落幕，愿你能轻松惬意地享受夜晚。",
    "夜间": "夜深了，辛苦一天的你也该好好休息啦，晚安，好梦。",
}


def classify_period(hour):
    """0-23 → 时段名称。"""
    for name, start, end in PERIODS:
        if start < end:
            if start <= hour < end:
                return name
        else:  # 跨零点
            if hour >= start or hour < end:
                return name
    return "夜间"


def now_info():
    """读取本机【当前本地时间】并结构化。"""
    now = _dt.datetime.now()
    period = classify_period(now.hour)
    return {
        "date_str": now.strftime("%Y-%m-%d"),
        "time_str": now.strftime("%H:%M:%S"),
        "weekday": WEEKDAY_CN[now.weekday()],
        "hour": now.hour,
        "period": period,
        "context": PERIOD_CONTEXT[period],
    }


def build_greeting_messages(info):
    user = GREETING_USER_TMPL.format(
        date=info["date_str"], weekday=info["weekday"], time=info["time_str"],
        period=info["period"], context=info["context"])
    return [{"role": "system", "content": GREETING_SYSTEM},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# [B] 依赖模块加载（import 复用，不复制代码）
# ---------------------------------------------------------------------------

def _load_module(filename, mod_name):
    path = os.path.join(HERE, filename)
    if not os.path.isfile(path):
        raise SystemExit(
            f"[错误] 找不到 {filename}\n"
            f"  期望位置：{path}\n"
            f"  请把本文件与 hotsearch_monitor_03.py、tts_broadcaster.py 放在同一目录。")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[错误] 加载 {filename} 失败：{e}")
    return mod, path


def load_deps():
    hot, hot_path = _load_module("hotsearch_monitor_03.py", "_cr_hot")
    tts, tts_path = _load_module("tts_broadcaster.py", "_cr_tts")

    need_hot = ("LlamaSession", "run_once", "build_llm_messages", "get_persona",
                "find_gguf", "resolve_llama_server", "build_llm_context",
                "split_reply", "fetch_hot_titles", "search_web", "fetch_article",
                "PERSONAS", "PART_OBJ", "PART_VIEW")
    need_tts = ("get_backend", "Broadcaster", "parse_reply", "BaseBackend")
    miss_h = [n for n in need_hot if not hasattr(hot, n)]
    miss_t = [n for n in need_tts if not hasattr(tts, n)]
    if miss_h or miss_t:
        ver = getattr(hot, "VERSION", "未知")
        print("[版本不匹配] 现有主程序不是整合所需的版本。", file=sys.stderr)
        print(f"  hotsearch_monitor_03.py 版本：{ver}（应为 v5-llm600-persona）", file=sys.stderr)
        if miss_h:
            print(f"  缺少：{', '.join(miss_h)}", file=sys.stderr)
        if miss_t:
            print(f"  tts_broadcaster.py 缺少：{', '.join(miss_t)}", file=sys.stderr)
        print("  请用最新版覆盖上述两个文件后重试。", file=sys.stderr)
        raise SystemExit(2)
    if getattr(hot, "VERSION", "") != "v5-llm600-persona":
        print(f"[warn] hotsearch_monitor_03.py 版本为 "
              f"{getattr(hot, 'VERSION', '?')}，建议用 v5-llm600-persona。",
              file=sys.stderr)
    return hot, tts, hot_path, tts_path


class NullBackend:
    """--silent：不出声，但保留完整播报编排（供自检 / 先看文字）。"""
    name = "null"

    def __init__(self, base_cls):
        self._base = base_cls

    def speak(self, text, out_wav=None):
        pass

    def list_voices(self):
        return []


def load_caption_module():
    """加载同目录 caption_window.py；缺失时给出明确的中文提示。"""
    path = os.path.join(HERE, "caption_window.py")
    if not os.path.isfile(path):
        raise SystemExit(
            f"[错误] 找不到桌面字幕模块 caption_window.py\n"
            f"  期望位置：{path}\n"
            f"  请把 caption_window.py 与 cyber_radio.py 放在同一目录。")
    spec = importlib.util.spec_from_file_location("_cr_caption", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[错误] 加载 caption_window.py 失败：{e}")
    return mod


class _NullSink:
    """ctx 未提供字幕窗时的兜底，保证播报链路不依赖 GUI。"""
    alive = False

    def show(self, *a, **k):
        pass

    def mark_idle(self):
        pass

    def schedule_hide(self, *a, **k):
        pass

    def close(self):
        pass


_NULL_SINK = _NullSink()


class CaptionSyncBackend:
    """把「字幕上屏」绑到 TTS 音频的时间轴上（单位是「一句话」）。

    arm()   —— 在 backend.speak() 之前调用：发 JSON 字幕事件 + 字幕窗上屏
    speak() —— 真实 backend.speak()（SAPI / pyttsx3 会阻塞到念完）返回后
               才 mark_idle()，于是这句字幕的显示窗口 == 这句音频的播放窗口
    逐句调用后，窗口里同时只显示当前这一句，不再整段堆在一起。
    """
    def __init__(self, inner, sink, emit, round_no, min_hold=0.0, hidden_evt=None):
        self.inner = inner
        self.sink = sink or _NULL_SINK
        self.emit = emit
        self.round_no = round_no
        self.min_hold = max(0.0, float(min_hold or 0))
        self.name = getattr(inner, "name", "?")
        # 托盘「隐藏字幕」共享标志（threading.Event）；None = 不启用门控（自检/非托盘路径）
        self.hidden_evt = hidden_evt

    def arm(self, idx, label, body, source="", sentence=None, sentence_total=None):
        self.emit(idx, label, body, self.round_no,
                  sentence=sentence, sentence_total=sentence_total)
        meta = {"index": idx, "round": self.round_no, "source": source}
        if sentence is not None:
            meta["sentence"] = sentence
            meta["sentence_total"] = sentence_total
        # 托盘隐藏字幕时：本句音频照常播，但不再上屏（JSON 字幕流始终照发）
        if self.hidden_evt is not None and self.hidden_evt.is_set():
            return
        self.sink.show(label, body, meta)

    def speak(self, text, out_wav=None):
        t0 = time.time()
        self.inner.speak(text, out_wav=out_wav)
        if self.min_hold:
            left = self.min_hold - (time.time() - t0)
            if left > 0:
                time.sleep(left)
        self.sink.mark_idle()

    def list_voices(self):
        return self.inner.list_voices()


# ---------------------------------------------------------------------------
# [C0] 句子切分：把「一整段文稿」切成「一句一句」
# ---------------------------------------------------------------------------

_SENT_END = "。！？!?；;…"          # 句末标点（切句依据）
_CLOSERS = "」』”’\"')）】》"       # 闭合引号/括号：跟随前句，不单独成句
_SOFT = "，,、：:"                  # 软标点：超长句再切时用


def _visible_len(s):
    """去掉标点/空白后的字数，用于判断残片是否过短。"""
    return len(re.sub(r"[\s" + re.escape(_SENT_END + _SOFT + _CLOSERS) + r"]",
                      "", s))


def split_sentences(text, max_chars=48, min_chars=8):
    """把一段中文播报文本切成「一句一条字幕」的句子列表。

    1. 按句末标点（。！？；…）切，标点保留在句尾；
    2. 闭合引号/括号只把「符号本身」归前句，其后的文字仍独立成句；
    3. 超长句再按逗号等软标点切，避免一条字幕塞不下；
    4. 只粘「纯标点 / 无句末标点的短碎块」，带句末标点的短句（如「第一句。」）
       是完整的一句，必须独立成一条字幕。
    保证不丢字：''.join(结果) == 折叠空白后的原文。
    """
    s = re.sub(r"\s+", " ", (text or "").strip())
    if not s:
        return []
    max_chars = max(12, int(max_chars))
    min_chars = max(0, int(min_chars))

    # 1) 按句末标点切（分隔符归前句）
    merged = []
    for piece in re.split(r"(?<=[" + re.escape(_SENT_END) + r"])", s):
        piece = piece.strip()
        if not piece:
            continue
        if merged and piece[0] in _CLOSERS:
            # 只把开头连续的闭合符号归前句，后面的文字仍是独立的一句
            j = 0
            while j < len(piece) and piece[j] in _CLOSERS:
                j += 1
            merged[-1] += piece[:j]
            rest = piece[j:].strip()
            if rest:
                merged.append(rest)
        else:
            merged.append(piece)

    # 2) 超长句按软标点再切
    out = []
    for piece in merged:
        if len(piece) <= max_chars:
            out.append(piece)
            continue
        cur = ""
        for sub in re.split(r"(?<=[" + re.escape(_SOFT) + r"])", piece):
            sub = sub.strip()
            if not sub:
                continue
            if cur and len(cur) + len(sub) > max_chars:
                out.append(cur)
                cur = sub
            else:
                cur += sub
        if cur:
            out.append(cur)

    # 3) 只粘「残片」：纯标点，或不含句末标点的短碎块。
    #    带句末标点的短句是完整句，不能合并，否则又变回整段上屏。
    def is_fragment(p):
        v = _visible_len(p)
        if v == 0:
            return True
        return (not any(ch in _SENT_END for ch in p)) and v < min_chars

    final = []
    for piece in out:
        if final and is_fragment(piece):
            final[-1] += piece
        else:
            final.append(piece)
    if len(final) >= 2 and is_fragment(final[0]):
        head = final.pop(0)
        final[0] = head + final[0]
    if len(final) >= 2 and is_fragment(final[-1]):
        final[-2] += final.pop()
    return [p for p in final if p.strip()]


# ---------------------------------------------------------------------------
# [C] 阶段输出 / 字幕流
# ---------------------------------------------------------------------------

def stage(text):
    """阶段横幅走 stderr，保持 stdout 只有「可播报内容」。"""
    print("\n" + "=" * 60, file=sys.stderr)
    print("  " + text, file=sys.stderr)
    print("=" * 60, file=sys.stderr, flush=True)


def make_emitter(args, captions_file):
    """返回 emit(idx, label, text, round_no, sentence=None, sentence_total=None)。

    只负责 JSON 字幕流（stdout / 文件），供外部程序订阅；
    桌面字幕窗由 CaptionSyncBackend.arm() 在同一时刻直接驱动。
    逐句字幕时每条记录额外带 sentence / sentence_total，便于订阅方知道
    「这是第几段的第几句、本段共几句」。
    """
    def emit(idx, label, text, round_no, sentence=None, sentence_total=None):
        rec = {"type": "segment", "round": round_no, "index": idx,
               "label": label, "text": text, "ts": round(time.time(), 3)}
        if sentence is not None:
            rec["sentence"] = sentence
            rec["sentence_total"] = sentence_total
        line = json.dumps(rec, ensure_ascii=False)
        if args.captions:
            print(line, flush=True)
        if captions_file:
            with open(captions_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    return emit


# ---------------------------------------------------------------------------
# [D] 一轮完整播报
# ---------------------------------------------------------------------------

def play_sentences(wrap, tts, args, idx, label, text, source="",
                   counter=None, clean=True, stop_evt=None):
    """把一段文本按「句」切开逐句播报 —— 一句话一条字幕、一段音频。

    每句严格走 arm() → speak() → mark_idle()：
      arm()   字幕窗上屏当前这一句 + 发一条 JSON 字幕
      speak() 真实 TTS 阻塞念完这一句
      返回后  mark_idle()，状态转待机，再进入下一句
    因此字幕与音频天然逐句对齐，窗口里不再同时堆一整段。

    counter: [n] 列表，--save-wav 时给逐句音频文件连续编号。
    """
    body = tts.clean_for_speech(text) if clean else (text or "").strip()
    max_chars = max(12, int(getattr(args, "max_sentence_chars", 48) or 48))
    sentences = split_sentences(body, max_chars=max_chars)
    if not sentences:
        sentences = [body] if body else []
    if not sentences:
        return 0

    # --speak-labels：段落名也作为该段的第一句念出来（同样占一条字幕）
    if getattr(args, "speak_labels", False):
        sentences = [label] + sentences

    spause = max(0.0, float(getattr(args, "sentence_pause", 0.15) or 0))
    total = len(sentences)
    spoken_here = 0
    for k, sent in enumerate(sentences, 1):
        # 托盘「切换下一条热点」：在当前句边界收尾，不等整段念完
        if stop_evt is not None and stop_evt.is_set():
            break
        wrap.arm(idx, label, sent, source=source,
                 sentence=k, sentence_total=total)
        out_wav = None
        if getattr(args, "save_wav", None) and counter is not None:
            counter[0] += 1
            root, ext = os.path.splitext(args.save_wav)
            out_wav = f"{root}_{counter[0]:03d}{ext}"
        wrap.speak(sent, out_wav=out_wav)
        spoken_here += 1
        if spause and k < total:
            time.sleep(spause)
    return spoken_here


def do_greeting(ctx, round_no):
    """[1] 时间感知 → LLM 启动问候 → CLI 显示 + TTS 念出来。"""
    args, hot, tts = ctx["args"], ctx["hot"], ctx["tts"]
    session, backend, emit = ctx["session"], ctx["backend"], ctx["emit"]

    info = now_info()
    stage(f"[1/3] 时间感知 · 启动问候     {info['date_str']} {info['weekday']} "
          f"{info['time_str']} → {info['period']}")
    print(f"[时段情境] {info['context']}", file=sys.stderr)

    if session is not None:
        try:
            greeting = session.chat(build_greeting_messages(info))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 问候生成失败，改用时段示例：{e}", file=sys.stderr)
            greeting = GREETING_FALLBACK[info["period"]]
    else:
        greeting = GREETING_FALLBACK[info["period"]]

    greeting = (greeting or "").strip()
    print(greeting)                         # stdout：用户看到开机问候
    wrap = CaptionSyncBackend(backend, ctx.get("sink"), emit, round_no,
                              ctx.get("min_hold", 0.0),
                              ctx.get("hidden_evt"))



    if not args.no_greeting_tts and not args.silent:
        play_sentences(wrap, tts, args, 0, "启动问候", greeting,
                       counter=ctx.get("wav_counter"))
    else:
        emit(0, "启动问候", greeting, round_no)  # 只发 JSON 流，不弹字幕
    return greeting, info


def do_news(ctx, round_no):
    """[2] 去重 → 检索 → LLM 两段式文稿；[3] 交给 TTS 逐段播报。"""
    args, hot, tts = ctx["args"], ctx["hot"], ctx["tts"]
    session, backend, emit = ctx["session"], ctx["backend"], ctx["emit"]

    skip_evt = ctx.get("skip_evt")   # 托盘「切换下一条热点」：句边界提前收尾本轮
    stage("[2/3] 热搜去重 · 联网检索 · 大模型生成两段式播报文本")

    # run_once 内部把两段式回答 print 到 stdout；这里捕获成文本，
    # 再原样显示给用户并交给 TTS，避免改动主程序。
    # —— 新增：记录调用 run_once 之前的去重队列，用于反推本轮热点标题 ——
    def _hist_titles():
        try:
            return list(hot.load_history(ctx["state"]["history"]))
        except (OSError, ValueError, TypeError):
            return []

    hist_before = set(_hist_titles())

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        summary = hot.run_once(ctx["state"], llm=session, persona=ctx["persona"])
    news = buf.getvalue().strip()

    if not news:
        print("[info] 本轮没有产出播报文稿（榜单热点可能都已播报过，"
              "或模型未返回结果）。可加 --reset 清空历史队列后重试。", file=sys.stderr)
        return None, summary

    print(news)                            # stdout：用户看到两段式文稿

    stage("[3/3] TTS 逐句播报（一句话一段字幕，与音频同步）")
    sink = ctx.get("sink")
    wrap = CaptionSyncBackend(backend, sink, emit, round_no,
                              ctx.get("min_hold", 0.0),
                              ctx.get("hidden_evt"))
    source = (summary or {}).get("src") or ""

    # —— 逐句播报：每句话单独一条字幕、单独一段音频 ——
    counter = ctx.get("wav_counter")
    print(f"[播报] 逐句播报，引擎={wrap.name}", file=sys.stderr)
    spoken = 0

    # ① 先念一遍本轮热点标题
    titles = [t for t in _hist_titles() if t not in hist_before]
    for t in titles:
        if skip_evt is not None and skip_evt.is_set():
            print("[托盘] 切换下一条热点：跳过本轮剩余内容", file=sys.stderr)
            break
        head = f"接下来播报热点：{t}。"
        print(f"[热点标题] {t}", file=sys.stderr)
        spoken += play_sentences(wrap, tts, args, -1, "热点标题", head,
                                 source=source, counter=counter,
                                 stop_evt=skip_evt)
        if args.pause:
            time.sleep(args.pause)

    # ② 两段式文稿：按段落标签拆开，每段内部再按句逐句播报
    #    （不再用 Broadcaster.play 整段上屏，否则一条字幕会塞进一整段）
    segs = tts.parse_reply(news)
    for i, (label, body) in enumerate(segs, start=1):
        if skip_evt is not None and skip_evt.is_set():
            break
        spoken += play_sentences(wrap, tts, args, i, label, body,
                                 source=source, counter=counter,
                                 stop_evt=skip_evt)
        if args.pause and i < len(segs):
            time.sleep(args.pause)

    print(f"[播报] 完成，共 {spoken} 句", file=sys.stderr)

    if sink is not None:
        sink.mark_idle()
        sink.schedule_hide(getattr(args, "subtitle_hold", 3.0))
    return news, summary


def run_round(ctx, round_no):
    greeting, info = do_greeting(ctx, round_no)
    news, summary = do_news(ctx, round_no)
    if summary:
        print(f"[本轮汇总] 来源={summary.get('src')} 新播报={summary.get('accepted')} "
              f"跳过重复={summary.get('skipped')} "
              f"历史队列={summary.get('hist_len')}/{ctx['state']['history_size']}",
              file=sys.stderr)
    return {"greeting": greeting, "period": info["period"],
            "news": news, "summary": summary}


# ---------------------------------------------------------------------------
# [E] 自检：不加载模型 / 不联网 / 不出声，跑通完整编排
# ---------------------------------------------------------------------------

def selftest(hot, tts, args):
    import tempfile

    # 1) 24 小时 → 6 时段，边界与跨零点
    expect = {0: "夜间", 4: "夜间", 5: "清晨", 8: "清晨", 9: "上午",
              11: "上午", 12: "午间", 13: "午间", 14: "下午", 17: "下午",
              18: "傍晚", 20: "傍晚", 21: "夜间", 23: "夜间"}
    for h, p in expect.items():
        assert classify_period(h) == p, f"{h}点应为{p}，得到{classify_period(h)}"
    assert set(classify_period(h) for h in range(24)) == set(p[0] for p in PERIODS)
    print("[OK] 6 时段划分（含夜间跨零点）24 小时全覆盖")

    info = now_info()
    msgs = build_greeting_messages(info)
    assert msgs[0]["role"] == "system" and "时段" in msgs[0]["content"]
    assert info["period"] in msgs[1]["content"] and info["time_str"] in msgs[1]["content"]
    print("[OK] 启动问候提示词构造（含当前时刻与时段）")

    # 2) 假网络 + 假 session，验证「去重 → 检索 → 文稿 → 播报」编排
    TITLE = "某科技公司发布新一代AI芯片"
    ARTICLE = ("据报道，该公司今日发布新一代AI芯片，官方称能效较上一代提升约40%，"
               "将于下季度量产并公布配套软件栈。报道未披露第三方实测数据与售价，"
               "业内对量产良率仍持观望。相关话题随后登上热搜榜前列。") * 8
    hot.fetch_hot_titles = lambda src: ("mock", [
        {"title": TITLE, "url": ""}, {"title": "第二条热搜", "url": ""}])
    hot.search_web = lambda q, engine="bing", timeout=15: (
        "Bing", [{"title": "报道", "url": "http://example.com/a",
                  "snippet": "该公司发布新一代AI芯片，宣称能效提升40%。"}])
    hot.fetch_article = lambda url, timeout=12, max_chars=3000: ARTICLE

    class FakeSession:
        """按 system 区分「问候」与「文稿」两次调用，并记录投喂的标题。"""

        def __init__(self):
            self.calls = 0
            self.news_prompts = []

        def chat(self, messages):
            self.calls += 1
            sys_msg = messages[0]["content"] if messages else ""
            if "问候助手" in sys_msg:
                return "上午好呀，祝你工作顺利。"
            self.news_prompts.append(messages[-1]["content"])
            return (f"{hot.PART_OBJ}\n"
                    "据材料，该公司发布新一代AI芯片，官方称能效提升约40%。"
                    "新品将于下季度量产，并公布配套软件栈。"
                    "报道未披露第三方实测数据与售价，业内对量产良率仍持观望。\n"
                    f"{hot.PART_VIEW}\n"
                    "作为理性观察员，我认为参数仍需独立实测验证。"
                    "投资者应关注量产良率与售价披露，谨慎评估风险。")

        def close(self):
            pass

    ctx_text = hot.build_llm_context(
        TITLE, [{"snippet": "能效提升40%的AI芯片发布"}], [ARTICLE], max_chars=600)
    assert 450 <= len(ctx_text) <= 620, len(ctx_text)
    print(f"[OK] 600 字上下文：{len(ctx_text)} 字")

    # 2.5) 逐句切分：整段文稿切成一句句，不丢字、单句不超长
    demo = ("据报道，该公司今日发布新一代AI芯片，官方称能效较上一代提升约40%，"
            "将于下季度量产并公布配套软件栈。报道未披露第三方实测数据与售价，"
            "业内对量产良率仍持观望。相关话题随后登上热搜榜前列。")
    sents = split_sentences(demo, max_chars=48)
    assert len(sents) >= 3, sents
    assert "".join(sents) == demo, (sents, demo)
    assert all(0 < len(x) <= 60 for x in sents), sents
    long_one = "这一句没有句末标点，只能用逗号不断往下续写，" * 8
    long_sents = split_sentences(long_one, max_chars=48)
    assert len(long_sents) >= 2 and all(len(x) <= 60 for x in long_sents), long_sents
    print(f"[OK] 逐句切分：示例切成 {len(sents)} 句、超长句切成 "
          f"{len(long_sents)} 句，且不丢字")

    tmp = tempfile.mkdtemp()
    state = {"source": "mock", "count": 1, "history_size": 50,
             "history": os.path.join(tmp, "hot_history.json"),
             "engine": "bing", "detail": 1, "timeout": 5, "context_chars": 600}

    captured = []

    class RecordingBackend:
        """假音频：把「一段语音播放」记成事件，用于验证字幕同步顺序。"""
        name = "record"

        def __init__(self, rec):
            self.rec = rec

        def speak(self, text, out_wav=None):
            self.rec.append(("audio",))

        def list_voices(self):
            return []

    class RecordingCaption:
        alive = True

        def __init__(self):
            self.events = []

        def show(self, label, text, meta=None):
            self.events.append(("show", label))

        def mark_idle(self):
            self.events.append(("idle",))

        def schedule_hide(self, seconds):
            self.events.append(("hide",))

        def close(self):
            pass

    fake_args = types.SimpleNamespace(
        captions=False, no_greeting_tts=False, silent=False, save_wav=None,
        pause=0, speak_labels=False, subtitle_hold=0,
        sentence_pause=0, max_sentence_chars=48)
    rec = RecordingCaption()
    ctx = {
        "args": fake_args, "hot": hot, "tts": tts,
        "session": FakeSession(), "backend": RecordingBackend(rec.events),
        "persona": hot.get_persona(), "state": state,
        "emit": lambda i, l, t, r, **kw: captured.append((i, l, t, kw)),
        "sink": rec, "min_hold": 0.0, "wav_counter": [0],
    }

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        r1 = run_round(ctx, 1)
    text1 = out.getvalue()
    assert "上午好呀" in text1 and hot.PART_OBJ in text1 and hot.PART_VIEW in text1, text1
    assert any(l == "启动问候" for _, l, _, _ in captured)
    assert any(l == "人格解读评价" for _, l, _, _ in captured)
    print("[OK] 完整编排跑通：问候 → 两段式文稿 → 逐句字幕 → 播报")

    # 4) 逐句同步：每句严格 show → audio → idle，且没有提前堆字幕
    kinds = [e[0] for e in rec.events]
    n_show = kinds.count("show")
    assert n_show >= 7, kinds                  # 问候1 + 标题1 + 客观3 + 人格2
    for k in range(n_show):
        assert kinds[k * 3:k * 3 + 3] == ["show", "audio", "idle"], kinds
    assert kinds[-1] == "hide", kinds
    print(f"[OK] 一句话一段字幕：show → audio → idle × {n_show}，播完自动隐藏")

    # 4.1) 每条字幕只装一句话、带句进度；不再出现整段上屏
    bodies = [t for _, _, t, _ in captured]
    assert all(0 < len(t) <= 60 for t in bodies), bodies
    assert all(isinstance(kw.get("sentence"), int)
               and kw.get("sentence_total", 0) >= 1
               for _, _, _, kw in captured), captured
    news_bodies = [t for _, l, t, _ in captured
                   if l in ("热点客观详细陈述", "人格解读评价")]
    assert len(news_bodies) >= 5, news_bodies
    print(f"[OK] 字幕内容逐句：文稿被切成 {len(news_bodies)} 条单句字幕，"
          f"每条带 sentence/sentence_total")

    # 3) 去重：已播报标题被跳过，并「继续抽取下一条」不重复热点
    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        r2 = run_round(ctx, 2)
    assert r2["summary"]["skipped"] >= 1, r2["summary"]
    assert r2["summary"]["accepted"] == 1, r2["summary"]
    prompts = ctx["session"].news_prompts
    assert len(prompts) == 2 and TITLE in prompts[0]
    assert "第二条热搜" in prompts[1] and TITLE not in prompts[1], prompts
    print("[OK] 去重校验：跳过已播报热点，自动改播下一条不重复标题")

    print(f"\n全部自检通过 ✅   版本 {VERSION}｜平台 {platform.system()}｜"
          f"共用 1 个 LlamaSession，本轮 chat 调用 {ctx['session'].calls} 次")
    return 0


# ---------------------------------------------------------------------------
# [F] CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="赛博收音机：时段问候 → 热搜去重 → 检索 → LLM 两段式文稿 → TTS 播报",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # 模型 / 推理引擎
    p.add_argument("--model-dir", default="models", help="GGUF 模型目录")
    p.add_argument("--model", default=None, help="显式指定 .gguf 路径")
    p.add_argument("--llama-dir", default="llama-bin", help="llama.cpp 构建目录")
    p.add_argument("--llama-host", default="127.0.0.1")
    p.add_argument("--llama-port", type=int, default=8080)
    p.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 4))
    p.add_argument("--llm-ctx", type=int, default=4096, help="server 上下文长度")
    p.add_argument("--n-predict", type=int, default=512, help="单次生成 token 上限")
    p.add_argument("--llm-temp", type=float, default=0.7)
    p.add_argument("--llm-timeout", type=int, default=180, help="等待 server 就绪秒数")
    # 热搜 / 文稿
    p.add_argument("--source", default="baidu",
                   help="热搜来源 baidu/toutiao/weibo/douyin/bilibili/auto")
    p.add_argument("--count", type=int, default=1, help="每轮播报的不重复热点数")
    p.add_argument("--history-size", type=int, default=50)
    p.add_argument("--history", default=os.path.join(os.getcwd(), "hot_history.json"))
    p.add_argument("--reset", action="store_true", help="运行前清空去重队列")
    p.add_argument("--search-engine", default="bing", choices=["bing", "ddg"],
                   help="联网检索搜索引擎")
    p.add_argument("--detail", type=int, default=1, help="每个热点抓取的详情正文篇数")
    p.add_argument("--no-detail", dest="detail", action="store_const", const=0)
    p.add_argument("--timeout", type=int, default=15, help="单次网络请求超时秒数")
    p.add_argument("--context-chars", type=int, default=600, help="投模型上下文字数预算")
    p.add_argument("--persona", default="default", help="预设人格名（在 PERSONAS 扩展）")
    p.add_argument("--persona-prompt", default=None, help="临时覆盖人格 system 文本")
    p.add_argument("--no-llm", action="store_true",
                   help="调试：不加载模型，问候用时段示例、文稿输出检索材料")
    # TTS
    p.add_argument("--tts-engine", default="sapi",
                   choices=["sapi", "edge", "pyttsx3", "mock"],
                   help="TTS 引擎（sapi=Windows 自带零依赖）")
    p.add_argument("--voice", default="", help="语音名，留空自动选中文")
    p.add_argument("--rate", type=int, default=0, help="语速 -10..10")
    p.add_argument("--volume", type=int, default=100, help="音量 0..100")
    p.add_argument("--pause", type=float, default=0.4, help="段间停顿秒数")
    p.add_argument("--sentence-pause", type=float, default=0.15,
                   help="句间停顿秒数（逐句字幕/音频，默认 0.15）")
    p.add_argument("--max-sentence-chars", type=int, default=48,
                   help="单句字幕最大字数；超长句按逗号再切，避免一条字幕过长")
    p.add_argument("--speak-labels", action="store_true", help="把段落标题也念出来")
    p.add_argument("--no-greeting-tts", action="store_true", help="启动问候不念，只念新闻")
    p.add_argument("--silent", action="store_true",
                   help="完全不出声（只打印文字，用于先验证链路）")
    p.add_argument("--save-wav", default=None, help="不播放，改为保存音频文件")
    p.add_argument("--captions", action="store_true", help="stdout 输出 JSON 字幕流")
    p.add_argument("--captions-file", default=None, help="字幕 JSON 流写入文件")
    # 桌面字幕窗（第 ⑥ 步）
    p.add_argument("--subtitle", action="store_true",
                   help="开启桌面字幕悬浮窗（一句话一段字幕，与 TTS 音频逐句同步）")
    p.add_argument("--subtitle-demo", action="store_true",
                   help="不加载模型，直接弹出字幕窗演示几段")
    p.add_argument("--subtitle-config", default="caption_config.json",
                   help="字幕样式 JSON 配置文件（不存在则用默认值）")
    p.add_argument("--subtitle-save-config", action="store_true",
                   help="把当前生效样式写回配置文件")
    p.add_argument("--subtitle-pos", default=None,
                   help="位置：bottom / top / center / x,y")
    p.add_argument("--subtitle-width", type=int, default=None, help="字幕窗宽度像素")
    p.add_argument("--subtitle-font", default=None, help="字体名（默认 Microsoft YaHei）")
    p.add_argument("--subtitle-font-size", type=int, default=None, help="正文字号")
    p.add_argument("--subtitle-alpha", type=float, default=None, help="不透明度 0..1")
    p.add_argument("--subtitle-hold", type=float, default=None,
                   help="整轮播完后字幕保留秒数（默认 3）")
    p.add_argument("--no-subtitle-topmost", action="store_true",
                   help="取消字幕窗置顶")
    p.add_argument("--list-voices", action="store_true", help="列出可用语音后退出")
    # 调度 / 调试
    p.add_argument("--loop", action="store_true",
                   help="常驻循环播报（Ctrl-C 退出），共用同一个 llama-server")
    p.add_argument("--interval", type=int, default=300, help="--loop 每轮间隔秒数")
    p.add_argument("--verbose", action="store_true", help="主程序过程信息输出到 stderr")
    p.add_argument("--selftest", "--mock", action="store_true",
                   help="全链路自检：不加载模型、不联网、不出声")
    return p.parse_args(argv)


def main():
    args = parse_args()
    # 只有真正用到字幕时才加载 caption_window.py，保持其它路径零耦合
    need_caption = bool(args.subtitle or args.subtitle_demo
                        or args.subtitle_save_config)
    cap = load_caption_module() if need_caption else None
    sink = cap.NullCaption() if cap else _NULL_SINK
    # --subtitle-demo：不加载模型、不依赖热搜主程序，直接演示桌面字幕窗
    if args.subtitle_demo:
        cfg = cap.merge_cli(cap.load_config(args.subtitle_config), args)
        return cap.demo(cfg)
    hot, tts, hot_path, tts_path = load_deps()
    hot._VERBOSE = args.verbose
    if args.selftest:
        return selftest(hot, tts, args)
    # TTS 后端
    if args.silent:
        backend = NullBackend(tts.BaseBackend)
    else:
        backend = tts.get_backend(args.tts_engine, args.voice, args.rate, args.volume)
    if args.list_voices:
        voices = backend.list_voices()
        if not voices:
            print("[提示] 未列出语音。SAPI 可能缺中文语音包；可试 --tts-engine edge。", file=sys.stderr)
        else:
            print(f"{'名称':<42}{'语言':<10}{'性别':<10}启用", file=sys.stderr)
            for v in voices:
                print(f"{v['name']:<42}{v.get('culture',''):<10}{v.get('gender',''):<10}{v.get('enabled','')}", file=sys.stderr)
        return 0
    # 唯一的大模型会话：问候 + 文稿共用
    session = None
    if not args.no_llm:
        try:
            model_path = hot.find_gguf(args.model_dir, args.model)
            server_bin = hot.resolve_llama_server(args.llama_dir)
            session = hot.LlamaSession(
                server_bin=server_bin, model_path=model_path,
                host=args.llama_host, port=args.llama_port,
                threads=args.threads, ctx=args.llm_ctx,
                n_predict=args.n_predict, temperature=args.llm_temp,
                server_timeout=args.llm_timeout,
                log_path=os.path.join(HERE, "llama_server.log"),
            )
            print(f"[模型] {model_path}", file=sys.stderr)
            print(f"[引擎] {server_bin}", file=sys.stderr)
            print("[启动] 加载一次 7B 权重，供「问候 + 文稿」共用", file=sys.stderr)
            session.start()
        except (FileNotFoundError, RuntimeError, OSError) as e:
            if session is not None:
                session.close()
            print(f"[error] 无法启动本地大模型：{e}", file=sys.stderr)
            print("[提示] 确认 models\\ 下有 .gguf、llama-bin\\ 下有 llama-server.exe；或加 --no-llm 仅看文字链路。", file=sys.stderr)
            return 2
    else:
        print("[调试] --no-llm：不加载模型，问候用时段示例、文稿输出检索材料", file=sys.stderr)
    if args.reset and os.path.exists(args.history):
        os.remove(args.history)
        print(f"[info] 已清空去重队列：{args.history}", file=sys.stderr)
    captions_file = args.captions_file
    if captions_file:
        with open(captions_file, "w", encoding="utf-8") as f:
            f.write("")
    # 桌面字幕窗：--subtitle 开启；--silent / 无 GUI 自动降级为 NullCaption
    cfg = cap.merge_cli(cap.load_config(args.subtitle_config), args) if cap else {}
    if cap and args.subtitle_save_config:
        cap.save_config(cfg, args.subtitle_config)
    real_subtitle_enabled = False
    if cap and args.subtitle and not args.silent:
        sink = cap.create_window(cfg)
        real_subtitle_enabled = bool(getattr(sink, "alive", False))
        print(f"[字幕] 窗口={type(sink).__name__} 就绪={real_subtitle_enabled}",
              file=sys.stderr)
        if not real_subtitle_enabled:
            print("[字幕] 警告：字幕窗未创建成功（无桌面会话/缺 tkinter/旧版模块），"
                  "托盘字幕开关不可用，播报照常。", file=sys.stderr)
    else:
        print("[字幕] 未开启：需显式传 --subtitle，且不要加 --silent。",
              file=sys.stderr)

    min_hold = 0.0 if args.silent else float(cfg.get("min_hold", 0.0) or 0)
    state = {
        "source": args.source,
        "count": max(1, args.count),
        "history_size": max(1, args.history_size),
        "history": args.history,
        "engine": args.search_engine,
        "detail": max(0, args.detail),
        "timeout": max(1, args.timeout),
        "context_chars": max(120, args.context_chars),
    }
    persona = hot.get_persona(args.persona, args.persona_prompt)
    print(f"[人格] {persona['label']}", file=sys.stderr)
    ctx = {
        "args": args, "hot": hot, "tts": tts, "session": session,
        "backend": backend, "persona": persona, "state": state,
        "emit": make_emitter(args, captions_file),
        "sink": sink, "min_hold": min_hold,
        "wav_counter": [0],
    }

    # ========= 线程/队列：把「检索 + LLM + 同步阻塞 TTS」整体移入播报工作线程 =========
    # 架构(v4)：主线程只做 ①托盘队列消费 ②字幕 GUI 显隐 ③调度，永远不被 backend.speak() 阻塞；
    # pystray 子线程只往队列丢指令；播报工作线程串行执行每一轮（含阻塞 TTS）。
    # caption_window.py 的 show/hide/mark_idle 是线程安全的队列投递，GUI 仍在独立守护线程渲染。
    tray_queue = queue.Queue()
    tray_icon = None
    tray_th = None
    shutdown_flag = [False]
    hidden_evt = threading.Event()          # set = 字幕被托盘隐藏；worker 上屏前检查
    ctx["hidden_evt"] = hidden_evt
    skip_evt = threading.Event()            # 托盘「切换下一条热点」：句边界收尾本轮
    ctx["skip_evt"] = skip_evt
    stop_evt = threading.Event()
    task_q = queue.Queue()                  # 单槽任务队列：("greeting" | "news", round_no)
    worker_done = threading.Event()
    worker_done.set()                       # 初始空闲

    # ---- 托盘菜单共享状态（回调只投递指令，主线程独占写） ----
    paused_flag = [False]                   # 暂停循环
    force_round = [False]                   # 立即播报 / 切换下一条热点
    font_size = [int(cfg.get("font_size", 20) or 20)]
    topmost_flag = [bool(cfg.get("topmost", True))]
    edit_mode = [False]                   # 字幕编辑模式（拖动 / 滚轮缩放）
    transparent = [False]                 # 字幕背景完全透明
    volume_val = [int(args.volume)]
    source_val = [str(args.source)]
    about_open = [False]
    SOURCE_CHOICES = (("baidu", "百度热搜"), ("toutiao", "头条热搜"),
                      ("weibo", "微博热搜"), ("douyin", "抖音热搜"),
                      ("bilibili", "B站热搜"),
                      ("auto", "auto（秒个位自动换榜）"))
    
    # ---------- 托盘回调：只往队列丢指令，绝不碰 Tk / backend ----------
    def _q(cmd, *args_):
        tray_queue.put((cmd,) + args_)

    def _on_toggle(icon, item):
        _q("toggle_caption")

    def _on_exit(icon, item):
        _q("exit")

    def _on_round_now(icon, item):
        _q("round_now")

    def _on_toggle_pause(icon, item):
        _q("toggle_pause")

    def _on_next_hot(icon, item):
        _q("next_hot")

    def _on_reset_hist(icon, item):
        _q("reset_history")

    def _on_voice(icon, item):
        _q("switch_voice")

    def _on_topmost(icon, item):
        _q("toggle_topmost")

    def _on_edit_mode(icon, item):
        _q("toggle_edit")

    def _on_transparent(icon, item):
        _q("toggle_transparent")

    def _on_about(icon, item):
        _q("about")

    def _on_source(key):
        return lambda icon, item: _q("set_source", key)

    def _on_volume(delta):
        return lambda icon, item: _q("volume_delta", delta)

    def _on_font(delta):
        return lambda icon, item: _q("font_delta", delta)

    # 动态文案 / 勾选：pystray 展开菜单时重新求值，勾选无需重建菜单
    def _txt_cap(item):
        return "显示字幕" if hidden_evt.is_set() else "隐藏字幕"

    def _txt_pause(item):
        return "恢复循环" if paused_flag[0] else "暂停循环"

    def _chk_pause(item):
        return bool(paused_flag[0])

    def _chk_topmost(item):
        return bool(topmost_flag[0])

    def _chk_edit_mode(item):
        return bool(edit_mode[0])

    def _chk_transparent(item):
        return bool(transparent[0])

    def _chk_source(key):
        return lambda item: source_val[0] == key

    def _txt_font_up(item):
        return "字号增大（当前 %d）" % font_size[0]

    def _txt_font_down(item):
        return "字号减小（当前 %d）" % font_size[0]

    def _txt_volume(item):
        return "音量（当前 %d）" % volume_val[0]

    def _build_tray_menu():
        src_items = [
            pystray.MenuItem(label, _on_source(key),
                             checked=_chk_source(key), radio=True)
            for key, label in SOURCE_CHOICES
        ]
        return pystray.Menu(
            pystray.MenuItem(_txt_cap, _on_toggle,
                             enabled=real_subtitle_enabled),
            pystray.MenuItem("播报控制", pystray.Menu(
                pystray.MenuItem("立即执行一轮播报", _on_round_now),
                pystray.MenuItem(_txt_pause, _on_toggle_pause,
                                 checked=_chk_pause),
                pystray.MenuItem("切换下一条热点", _on_next_hot),
            )),
            pystray.MenuItem("热搜设置", pystray.Menu(
                pystray.MenuItem("重置热搜历史队列", _on_reset_hist),
                pystray.MenuItem("热搜来源切换", pystray.Menu(*src_items)),
            )),
            pystray.MenuItem("TTS语音设置", pystray.Menu(
                pystray.MenuItem(_txt_volume, pystray.Menu(
                    pystray.MenuItem("增大 +10", _on_volume(10)),
                    pystray.MenuItem("减小 -10", _on_volume(-10)),
                )),
                pystray.MenuItem("切换语音音色（预留占位）", _on_voice),
            )),
            pystray.MenuItem("字幕样式设置", pystray.Menu(
                pystray.MenuItem(_txt_font_up, _on_font(2)),
                pystray.MenuItem(_txt_font_down, _on_font(-2)),
                pystray.MenuItem("置顶字幕", _on_topmost,
                                 checked=_chk_topmost,
                                 enabled=real_subtitle_enabled),
                pystray.MenuItem("编辑模式", _on_edit_mode,
                                 checked=_chk_edit_mode,
                                 enabled=real_subtitle_enabled),
                pystray.MenuItem("背景完全透明", _on_transparent,
                                 checked=_chk_transparent,
                                 enabled=real_subtitle_enabled),
            )),
            pystray.MenuItem("关于", _on_about),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _on_exit),
        )

    def _refresh_menu():
        if tray_icon is not None:
            try:
                tray_icon.menu = _build_tray_menu()
                tray_icon.update_menu()
            except Exception:
                pass

    def _apply_caption_toggle():
        if not real_subtitle_enabled:
            print("[托盘] 字幕未开启，忽略切换。", file=sys.stderr)
            return
        try:
            if hidden_evt.is_set():
                hidden_evt.clear()
                sink.show("赛博收音机", "字幕已开启", {})
                print("[托盘] 字幕已开启", file=sys.stderr)
            else:
                hidden_evt.set()
                sink.hide()
                print("[托盘] 字幕已隐藏", file=sys.stderr)
            _refresh_menu()
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 切换字幕失败（已忽略，不影响播报）：{e}", file=sys.stderr)

    def _apply_style(font_size_val=None, topmost_val=None):
        try:
            if hasattr(sink, "apply_style"):
                sink.apply_style(font_size=font_size_val, topmost=topmost_val)
                return
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 样式应用失败（已忽略）：{e}", file=sys.stderr)
            return
        print("[托盘] 字幕模块未含 apply_style，样式将在下次重启字幕窗后生效。",
              file=sys.stderr)

    def _save_cfg():
        try:
            if cap is not None and isinstance(cfg, dict):
                cfg["font_size"] = font_size[0]
                cfg["topmost"] = topmost_flag[0]
                cap.save_config(cfg, args.subtitle_config)
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 样式配置保存失败（已忽略）：{e}", file=sys.stderr)

    def _apply_round_now():
        force_round[0] = True
        print("[托盘] 已请求立即执行一轮播报（worker 空闲即开始）", file=sys.stderr)

    def _apply_toggle_pause():
        paused_flag[0] = not paused_flag[0]
        print(f"[托盘] 循环已{'暂停' if paused_flag[0] else '恢复'}", file=sys.stderr)

    def _apply_next_hot():
        skip_evt.set()
        force_round[0] = True
        print("[托盘] 已请求切换下一条热点（当前句念完即切）", file=sys.stderr)

    def _apply_reset_history():
        try:
            hot.save_history(state["history"], [])
            print(f"[托盘] 已重置热搜历史队列：{state['history']}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 重置历史队列失败（已忽略）：{e}", file=sys.stderr)

    def _apply_set_source(key):
        source_val[0] = key
        state["source"] = key
        args.source = key
        print(f"[托盘] 热搜来源已切换：{key}", file=sys.stderr)

    def _apply_volume_delta(delta):
        v = max(0, min(100, volume_val[0] + int(delta)))
        volume_val[0] = v
        try:
            backend.volume = v
            args.volume = v
        except Exception:
            pass
        print(f"[托盘] 音量：{v}（下一句起生效）", file=sys.stderr)

    def _apply_switch_voice():
        print("[托盘] 切换语音音色：预留占位，后续接入 --voice / --list-voices。",
              file=sys.stderr)

    def _apply_font_delta(delta):
        v = max(10, min(72, font_size[0] + int(delta)))
        font_size[0] = v
        _apply_style(font_size_val=v)
        _save_cfg()
        print(f"[托盘] 字号：{v}", file=sys.stderr)

    def _apply_toggle_topmost():
        topmost_flag[0] = not topmost_flag[0]
        _apply_style(topmost_val=topmost_flag[0])
        _save_cfg()
        print(f"[托盘] 置顶字幕：{'开' if topmost_flag[0] else '关'}", file=sys.stderr)

    def _apply_edit_mode():
        edit_mode[0] = not edit_mode[0]
        try:
            if hasattr(sink, "set_edit_mode"):
                sink.set_edit_mode(edit_mode[0])
            else:
                print("[托盘] 字幕模块未含 set_edit_mode，请更新为新版 caption_window.py。",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 编辑模式切换失败（已忽略）：{e}", file=sys.stderr)
        print(f"[托盘] 编辑模式：{'开' if edit_mode[0] else '关'}", file=sys.stderr)

    def _apply_transparent():
        transparent[0] = not transparent[0]
        try:
            if hasattr(sink, "set_transparent"):
                sink.set_transparent(transparent[0])
            else:
                print("[托盘] 字幕模块未含 set_transparent，请更新为新版 caption_window.py。",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 透明背景切换失败（已忽略）：{e}", file=sys.stderr)
        print(f"[托盘] 背景完全透明：{'开' if transparent[0] else '关'}", file=sys.stderr)

    def _apply_about():
        info = (
            f"赛博收音机 {VERSION}\n"
            f"热搜来源：{source_val[0]}    TTS：{args.tts_engine} / 音量 {volume_val[0]}\n"
            f"字幕：{'开启' if real_subtitle_enabled else '未开启'}"
            f" / 字号 {font_size[0]} / 置顶 {'是' if topmost_flag[0] else '否'}\n"
            f"编辑模式 {'开' if edit_mode[0] else '关'}"
            f" / 透明背景 {'开' if transparent[0] else '关'}\n"
            f"架构：主线程调度 · worker 播报 · 托盘线程，点击瞬时响应"
        )
        print("[关于] " + info.replace("\n", " | "), file=sys.stderr)
        try:
            if tray_icon is not None:
                tray_icon.notify(info, f"关于 · 赛博收音机 {VERSION}")
        except Exception:
            pass
        if about_open[0]:
            return
        about_open[0] = True

        def _msgbox():
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0, info, "关于 · 赛博收音机", 0x40)
            except Exception:
                pass
            finally:
                about_open[0] = False

        threading.Thread(target=_msgbox, name="about-box", daemon=True).start()
    
    def _broadcast_worker():
        # 独立工作线程：检索 + LLM + 同步阻塞 TTS 全在这里。
        # 字幕 show/mark_idle 只是 caption_window 的队列投递（非阻塞/线程安全），
        # 留在本线程即可与逐句音频对齐；Tk 对象始终只在 GUI 线程被触碰。
        while not stop_evt.is_set():
            try:
                task = task_q.get(timeout=0.2)
            except queue.Empty:
                continue
            kind, rnd = task
            try:
                if kind == "greeting":
                    do_greeting(ctx, rnd)
                elif kind == "news" and not stop_evt.is_set():
                    do_news(ctx, rnd)
            except Exception as e:  # noqa: BLE001
                print(f"[播报线程] 第 {rnd} 轮异常（已忽略，继续）：{e}",
                      file=sys.stderr)
            finally:
                if skip_evt is not None:
                    skip_evt.clear()      # 「切换下一条热点」只影响本轮
                worker_done.set()
                task_q.task_done()
    
    def _tray_thread():
        nonlocal tray_icon
        try:
            img = Image.new("RGBA", (64, 64), (40, 40, 40, 255))
            tray_icon = pystray.Icon("cyber_radio", img,
                                     title="赛博收音机｜运行中",
                                     menu=_build_tray_menu())
            tray_icon.run()
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 初始化/运行失败（不影响播报与字幕）：{e}", file=sys.stderr)
    
    worker_th = threading.Thread(target=_broadcast_worker, name="broadcast-worker",
                                 daemon=True)
    
    try:
        # -------- 托盘初始化（仅 --loop 且 pystray 可用时启用） --------
        if args.loop and TRAY_AVAILABLE:
            # daemon=True：退出时即使托盘线程未及时返回，也不会阻止进程结束（保证「退出」瞬时）
            tray_th = threading.Thread(target=_tray_thread, name="tray", daemon=True)
            tray_th.start()
        elif args.loop:
            print(f"[托盘] 未启用：{TRAY_IMPORT_ERROR}（在 .venv 里 pip install pystray pillow 后重跑）", file=sys.stderr)
        # -------- 托盘初始化结束 --------
    
        if args.loop:
            worker_th.start()
            print(f"[常驻] 每 {args.interval}s 一轮；Ctrl-C / 托盘菜单退出", file=sys.stderr)
            # 启动问候仅 1 次（保留既有改动）：作为 worker 的首个任务
            worker_done.clear()
            task_q.put(("greeting", 0))
            rnd = 0
            next_due = 0.0                  # 0 → 问候结束后立即播第一轮新闻
            while not shutdown_flag[0]:
                # ① 瞬时消费托盘队列：主线程此刻永不被 TTS 阻塞
                while not tray_queue.empty():
                    cmd = tray_queue.get()
                    op = cmd[0]
                    try:
                        if op == "exit":
                            shutdown_flag[0] = True
                        elif op == "toggle_caption":
                            _apply_caption_toggle()
                        elif op == "round_now":
                            _apply_round_now()
                        elif op == "toggle_pause":
                            _apply_toggle_pause()
                        elif op == "next_hot":
                            _apply_next_hot()
                        elif op == "reset_history":
                            _apply_reset_history()
                        elif op == "set_source":
                            _apply_set_source(cmd[1])
                        elif op == "volume_delta":
                            _apply_volume_delta(cmd[1])
                        elif op == "switch_voice":
                            _apply_switch_voice()
                        elif op == "font_delta":
                            _apply_font_delta(cmd[1])
                        elif op == "toggle_topmost":
                            _apply_toggle_topmost()
                        elif op == "toggle_edit":
                            _apply_edit_mode()
                        elif op == "toggle_transparent":
                            _apply_transparent()
                        elif op == "about":
                            _apply_about()
                    except Exception as e:  # noqa: BLE001
                        print(f"[托盘] 指令 {op} 处理失败（已忽略，不影响播报）：{e}",
                              file=sys.stderr)
                if shutdown_flag[0]:
                    break
                # ② 调度：worker 空闲才投递（单槽，绝不堆积）；暂停只顺延 next_due
                now = time.monotonic()
                if worker_done.is_set() and (force_round[0] or
                                             (not paused_flag[0] and now >= next_due)):
                    worker_done.clear()
                    rnd += 1
                    task_q.put(("news", rnd))
                    next_due = now + max(1, args.interval)
                    force_round[0] = False
                elif paused_flag[0]:
                    next_due = now + max(1, args.interval)
                # ③ 0.1s 粒度 sleep：托盘点击 ≤0.1s 内被主线程处理
                time.sleep(0.1)
        else:
            run_round(ctx, 1)
    
    except KeyboardInterrupt:
        print("\n[Ctrl-C 收到，正常退出]", file=sys.stderr)
    finally:
        print("\n[正在执行资源释放]", file=sys.stderr)
        stop_evt.set()                      # 让空闲的 worker 尽快收尾
        # 优先停托盘图标（run() 返回后托盘线程才会退出）
        if TRAY_AVAILABLE and tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass
        if tray_th is not None:
            try:
                tray_th.join(timeout=1.0)
            except Exception:
                pass
        # worker 为 daemon：不 join，主线程立即返回、进程退出即被强杀，
        # 因此「退出」绝不会被正在念的 SAPI 语音拖住。
        try:
            sink.close()
        except Exception:
            pass
        if session is not None:
            session.close()
            print("[关闭] llama-server 已回收", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
