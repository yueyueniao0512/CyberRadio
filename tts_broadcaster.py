#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTS 播报模块（蓝图第 ⑤ 模块） / 播报入口
========================================

职责：把 hotsearch_monitor_03.py 输出的两段式文稿，逐段合成语音并播放。

设计原则
--------
* 单文件、自包含：不改动主程序，不引入第二个自研模块。
* 零依赖优先：Windows 用系统自带 System.Speech（SAPI），无需 pip 装任何东西。
* 可选更好音色：--engine edge（edge-tts，需 pip + 联网）/ --engine pyttsx3。
* 逐段播报 + JSON 字幕流：给蓝图第 ⑥ 步「独立字幕悬浮窗」预留订阅接口。
* 坚持仅标准库实现主链路（subprocess / tempfile / json / re）。

常用命令（Windows PowerShell，1109001 目录下，均为单行可直接复制）
------------------------------------------------------------------
  # 0) 逻辑自检（不出声、不联网、不加载模型）
  python tts_broadcaster.py --selftest

  # 1) 查看系统已装语音，确认有没有中文语音包
  python tts_broadcaster.py --list-voices

  # 2) 立刻试听一句话（秒出声，不用等 7B 模型）
  python tts_broadcaster.py --text "你好，这是热点播报测试。"

  # 3) 完整链路：跑热搜主程序 → 拿两段式文稿 → 逐段念出来
  python tts_broadcaster.py --run --model-dir models --llama-dir llama-bin --count 1

  # 4) 指定语音 / 语速 / 音量，并把字幕写成 JSON 流（供 UI 订阅）
  python tts_broadcaster.py --engine sapi --voice "Microsoft Huihui Desktop" --rate 0 --volume 100 --captions --captions-file captions.jsonl --run --model-dir models --llama-dir llama-bin --count 1

参数分界：--run 之前的参数属于本模块，--run 之后的全部原样透传给主程序。
其他输入方式：--text "..." / --text-file file.txt / 管道 stdin / --main-script 指定主程序。
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = "v1-tts-sapi"

# 与主程序保持一致的段落标记
PART_OBJ = "【热点客观详细陈述】"
PART_VIEW = "【人格解读评价】"
LABELS = (PART_OBJ, PART_VIEW)

# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------

_MD_NOISE = re.compile(r"[*_`#>]|\|")


def clean_for_speech(text):
    """去掉不适合念出来的 markdown 符号，折叠空白，保留中文标点。"""
    text = _MD_NOISE.sub("", text or "")
    text = re.sub(r"https?://\S+", "", text)          # URL 不念（由字幕/UI 展示）
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_reply(text):
    """把两段式文稿拆成 [(label, body), ...]；格式不符时整段兜底为一段。"""
    text = (text or "").strip()
    if not text:
        return []
    found = []
    for lab in LABELS:
        m = re.search(re.escape(lab), text)
        if m:
            found.append((m.start(), lab))
    if len(found) < 2:
        return [("全文", clean_for_speech(re.sub(r"|".join(re.escape(x) for x in LABELS), "", text)))]

    found.sort()
    segs = []
    for i, (pos, lab) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(text)
        body = text[pos + len(lab):end]
        body = clean_for_speech(body)
        if body:
            segs.append((lab.strip("【】"), body))
    return segs


# ---------------------------------------------------------------------------
# PowerShell / SAPI 脚本（全部 ASCII，避免中文脚本编码问题）
# ---------------------------------------------------------------------------

_PS_SPEAK = r'''
param(
  [string]$TextPath,
  [int]$Rate = 0,
  [int]$Volume = 100,
  [string]$Voice = "",
  [string]$OutWav = ""
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $s.Rate = $Rate
  $s.Volume = $Volume
  if ($Voice -ne "") {
    try { $s.SelectVoice($Voice) }
    catch { Write-Output ("WARN_VOICE_NOT_FOUND: " + $Voice) }
  } else {
    $zh = $s.GetInstalledVoices() |
          Where-Object { $_.VoiceInfo.Culture.Name -like "zh*" } |
          Select-Object -First 1
    if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }
    else { Write-Output "WARN_NO_ZH_VOICE" }
  }
  $text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
  if ($OutWav -ne "") {
    $s.SetOutputToWaveFile($OutWav)
    $s.Speak($text)
    $s.SetOutputToNull()
  } else {
    $s.Speak($text)
  }
} finally {
  $s.Dispose()
}
'''

_PS_VOICES = r'''
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $s.GetInstalledVoices() | ForEach-Object {
    $v = $_.VoiceInfo
    Write-Output ("VOICE|" + $v.Name + "|" + $v.Culture.Name + "|" + $v.Gender + "|" + $_.Enabled)
  }
} finally {
  $s.Dispose()
}
'''


def _powershell_exe():
    return shutil.which("powershell") or shutil.which("pwsh")


def _run_ps(script, args=None, timeout=180, capture=False):
    """把 PS 脚本写到临时文件再执行，避免命令行引号/编码地狱。"""
    exe = _powershell_exe()
    if not exe:
        raise RuntimeError("未找到 powershell / pwsh，SAPI 引擎仅支持 Windows")
    fd, ps1 = tempfile.mkstemp(suffix=".ps1", prefix="tts_")
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
            f.write(script)
        cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", ps1] + (args or [])
        return subprocess.run(cmd, timeout=timeout, capture_output=capture,
                              text=True, encoding="utf-8", errors="replace")
    finally:
        try:
            os.remove(ps1)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# TTS 引擎
# ---------------------------------------------------------------------------

class BaseBackend:
    name = "base"

    def __init__(self, voice="", rate=0, volume=100):
        self.voice = voice
        self.rate = rate
        self.volume = volume

    @classmethod
    def available(cls):
        return True

    def speak(self, text, out_wav=None):
        raise NotImplementedError

    def list_voices(self):
        return []


class SapiBackend(BaseBackend):
    """Windows 自带 System.Speech，零 pip 依赖、离线。"""

    name = "sapi"

    @classmethod
    def available(cls):
        return platform.system() == "Windows" and bool(_powershell_exe())

    def speak(self, text, out_wav=None):
        fd, txt = tempfile.mkstemp(suffix=".txt", prefix="tts_text_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            args = ["-TextPath", txt, "-Rate", str(self.rate),
                    "-Volume", str(self.volume), "-Voice", self.voice or "",
                    "-OutWav", out_wav or ""]
            r = _run_ps(_PS_SPEAK, args, capture=True)
            if r.returncode != 0:
                raise RuntimeError("SAPI 合成失败：" + (r.stderr or "").strip()[:300])
            if r.stdout and "WARN_VOICE_NOT_FOUND" in r.stdout:
                print("[warn] 指定语音不存在，已回退系统默认：" + self.voice, file=sys.stderr)
            if r.stdout and "WARN_NO_ZH_VOICE" in r.stdout:
                print("[warn] 系统未安装中文语音包，中文可能无法正常朗读。\n"
                      "       建议：设置→时间和语言→语音→添加中文语音包；"
                      "或改用 --engine edge（pip install edge-tts，需联网，音色更好）。",
                      file=sys.stderr)
            if out_wav and os.path.isfile(out_wav):
                print(f"[音频] 已保存：{out_wav}", file=sys.stderr)
        finally:
            try:
                os.remove(txt)
            except OSError:
                pass

    def list_voices(self):
        r = _run_ps(_PS_VOICES, capture=True, timeout=40)
        out = []
        for line in (r.stdout or "").splitlines():
            if line.startswith("VOICE|"):
                _, name, culture, gender, enabled = (line.split("|") + [""] * 5)[:5]
                out.append({"name": name, "culture": culture,
                            "gender": gender, "enabled": enabled})
        return out


class EdgeBackend(BaseBackend):
    """edge-tts：微软在线神经语音，中文音色更好。需 pip install edge-tts + 联网。"""

    name = "edge"

    def __init__(self, voice="", rate=0, volume=100):
        super().__init__(voice or "zh-CN-XiaoxiaoNeural", rate, volume)

    @classmethod
    def available(cls):
        return bool(shutil.which("edge-tts"))

    def speak(self, text, out_wav=None):
        fd, txt = tempfile.mkstemp(suffix=".txt", prefix="tts_text_")
        if out_wav:
            media, play = out_wav, False
        else:
            outdir = os.path.join(os.getcwd(), "tts_audio")
            os.makedirs(outdir, exist_ok=True)
            media = os.path.join(outdir, "seg_%d_%04d.mp3" %
                                 (int(time.time() * 1000), abs(hash(text)) % 10000))
            play = True
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            rate = f"{self.rate:+d}%" if self.rate else "+0%"
            cmd = ["edge-tts", "--voice", self.voice, "--rate", rate,
                   "--file", txt, "--write-media", media]
            r = subprocess.run(cmd, timeout=180, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise RuntimeError("edge-tts 失败：" + (r.stderr or "").strip()[:300])
            print(f"[音频] 已生成：{media}", file=sys.stderr)
            if play:
                _open_with_default_player(media)
                time.sleep(0.8)
        finally:
            try:
                os.remove(txt)
            except OSError:
                pass
            # 注意：不删除 media —— 默认播放器是异步读取的，删掉会中断播放

    def list_voices(self):
        return [{"name": "zh-CN-XiaoxiaoNeural (晓晓)", "culture": "zh-CN",
                 "gender": "Female", "enabled": "True"},
                {"name": "zh-CN-YunxiNeural (云希)", "culture": "zh-CN",
                 "gender": "Male", "enabled": "True"}]


class Pyttsx3Backend(BaseBackend):
    """pyttsx3：跨平台离线兜底。需 pip install pyttsx3。"""

    name = "pyttsx3"

    @classmethod
    def available(cls):
        try:
            import pyttsx3  # noqa: F401
            return True
        except ImportError:
            return False

    def speak(self, text, out_wav=None):
        import pyttsx3
        eng = pyttsx3.init()
        eng.setProperty("volume", max(0.0, min(1.0, self.volume / 100.0)))
        eng.setProperty("rate", max(50, 200 + self.rate * 15))
        if self.voice:
            eng.setProperty("voice", self.voice)
        if out_wav:
            eng.save_to_file(text, out_wav)
            eng.runAndWait()
            print(f"[音频] 已保存：{out_wav}", file=sys.stderr)
        else:
            eng.say(text)
            eng.runAndWait()

    def list_voices(self):
        try:
            import pyttsx3
            eng = pyttsx3.init()
            return [{"name": v.name, "culture": "", "gender": "", "enabled": "True"}
                    for v in eng.getProperty("voices")]
        except Exception:
            return []


class MockBackend(BaseBackend):
    """无声自检：只打印将要播放的分段，用于验证流程 / Linux 沙箱。"""

    name = "mock"

    def speak(self, text, out_wav=None):
        print(f"[mock TTS] ({len(text)} 字) {text[:60]}{'…' if len(text) > 60 else ''}")

    def list_voices(self):
        return [{"name": "mock-voice", "culture": "zh-CN", "gender": "Neutral",
                 "enabled": "True"}]


BACKENDS = {c.name: c for c in
            (SapiBackend, EdgeBackend, Pyttsx3Backend, MockBackend)}


def get_backend(name, voice="", rate=0, volume=100):
    cls = BACKENDS.get(name)
    if not cls:
        raise SystemExit(f"[错误] 未知引擎 {name}，可选：{', '.join(BACKENDS)}")
    if not cls.available():
        hint = {
            "sapi": "SAPI 需要 Windows + PowerShell；非 Windows 请改用 --engine mock 或 pyttsx3",
            "edge": "请先 pip install edge-tts（并确认可联网）",
            "pyttsx3": "请先 pip install pyttsx3",
        }.get(name, "")
        raise SystemExit(f"[错误] 引擎 {name} 当前不可用。{hint}")
    return cls(voice=voice, rate=rate, volume=volume)


def _open_with_default_player(path):
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # noqa: S606
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 无法自动打开播放器（{e}），音频在：{path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 播报会话
# ---------------------------------------------------------------------------

class Broadcaster:
    """逐段播报；on_segment 回调给 UI/字幕层订阅。"""

    def __init__(self, backend, pause=0.4, speak_labels=False,
                 captions=False, captions_file=None, no_play=False,
                 save_wav=None):
        self.backend = backend
        self.pause = max(0.0, pause)
        self.speak_labels = speak_labels
        self.captions = captions
        self.captions_file = captions_file
        self.no_play = no_play
        self.save_wav = save_wav
        if captions_file:
            with open(captions_file, "w", encoding="utf-8") as f:
                f.write("")

    def _emit(self, idx, label, body):
        rec = {"type": "segment", "index": idx, "label": label,
               "text": body, "ts": round(time.time(), 3)}
        line = json.dumps(rec, ensure_ascii=False)
        if self.captions:
            print(line, flush=True)
        if self.captions_file:
            with open(self.captions_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def play(self, text):
        segs = parse_reply(text)
        if not segs:
            print("[warn] 没有可播报的文本（主程序输出为空？）", file=sys.stderr)
            return 0
        print(f"[播报] 共 {len(segs)} 段，引擎={self.backend.name}", file=sys.stderr)
        for i, (label, body) in enumerate(segs):
            self._emit(i, label, body)
            to_say = f"{label}。{body}" if self.speak_labels else body
            if self.no_play:
                print(f"[{i + 1}/{len(segs)}] {label}｜{body}")
            else:
                out_wav = None
                if self.save_wav:
                    out_wav = self.save_wav if len(segs) == 1 else \
                        f"{os.path.splitext(self.save_wav)[0]}_{i + 1}{os.path.splitext(self.save_wav)[1]}"
                self.backend.speak(to_say, out_wav=out_wav)
            if self.pause and i < len(segs) - 1:
                time.sleep(self.pause)
        print("[播报] 完成", file=sys.stderr)
        return len(segs)


def run_main_program(main_script, main_args, timeout=600):
    """调用热搜主程序，捕获它的 stdout 作为播报文稿。"""
    if not os.path.isfile(main_script):
        raise SystemExit(f"[错误] 找不到主程序：{main_script}\n"
                         "请用 --main-script 指定，或把 tts_broadcaster.py 与主程序放同一目录。")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"   # 关键：避免 Windows 管道中文乱码
    env["PYTHONUTF8"] = "1"
    cmd = [sys.executable, main_script] + list(main_args or [])
    print(f"[调度] {' '.join(cmd)}", file=sys.stderr)
    try:
        r = subprocess.run(cmd, timeout=timeout, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"[错误] 主程序超时（{timeout}s），可减小 --count 或检查模型加载")
    if r.stderr:
        sys.stderr.write(r.stderr)
    if r.returncode != 0:
        raise SystemExit(f"[错误] 主程序退出码 {r.returncode}")
    return r.stdout


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def selftest():
    """纯逻辑自检：不发声、不联网、不加载模型。"""
    sample = (PART_OBJ + "\n消费三姐妹股价较高点回落，市场对其增长预期重新定价。\n"
              + PART_VIEW + "\n作为理性观察员，我认为应关注业绩基本面与估值匹配。")
    segs = parse_reply(sample)
    assert len(segs) == 2, segs
    assert segs[0][0] == "热点客观详细陈述" and "消费三姐妹" in segs[0][1]
    assert segs[1][0] == "人格解读评价" and "理性观察员" in segs[1][1]
    assert parse_reply("一段没有标记的普通文本") == [("全文", "一段没有标记的普通文本")]
    assert parse_reply("") == []

    b = Broadcaster(MockBackend(), captions=True, no_play=True, pause=0)
    n = b.play(sample)
    assert n == 2
    print("[OK] 分段解析 / 两段兜底 / 字幕流 / 无声播报 全部通过")
    print(f"[OK] 版本 {VERSION}｜平台 {platform.system()}｜"
          f"SAPI 可用={SapiBackend.available()}｜"
          f"edge-tts 可用={EdgeBackend.available()}｜"
          f"pyttsx3 可用={Pyttsx3Backend.available()}")
    return 0


def main():
    # 参数分界：--run 之前归本模块，之后原样透传给主程序
    argv = sys.argv[1:]
    if "--run" in argv:
        i = argv.index("--run")
        own, main_args, do_run = argv[:i], argv[i + 1:], True
    else:
        own, main_args, do_run = argv, [], False

    ap = argparse.ArgumentParser(
        description="把两段式热点文稿逐段 TTS 播报（Windows SAPI 零依赖优先）")
    ap.add_argument("--engine", default="sapi", choices=list(BACKENDS),
                    help="TTS 引擎（默认 sapi：Windows 自带、离线、免安装）")
    ap.add_argument("--voice", default="", help="语音名；留空自动选中文语音")
    ap.add_argument("--rate", type=int, default=0, help="语速 -10..10（默认 0）")
    ap.add_argument("--volume", type=int, default=100, help="音量 0..100（默认 100）")
    ap.add_argument("--pause", type=float, default=0.4, help="段间停顿秒数（默认 0.4）")
    ap.add_argument("--speak-labels", action="store_true", help="把段落标题也念出来")
    ap.add_argument("--captions", action="store_true",
                    help="stdout 输出 JSON 字幕流（供字幕窗订阅）")
    ap.add_argument("--captions-file", default=None,
                    help="把同样的 JSON 字幕流写入文件（供 UI tail）")
    ap.add_argument("--no-play", action="store_true",
                    help="只解析并打印分段，不发声（调试用）")
    ap.add_argument("--save-wav", default=None,
                    help="不直接播放，改为保存音频文件（SAPI/edge/pyttsx3）")
    ap.add_argument("--text", default=None, help="直接试听一段文字")
    ap.add_argument("--text-file", default=None, help="从文件读取文稿")
    ap.add_argument("--main-script", default=None,
                    help="主程序路径（默认：本脚本同目录的 hotsearch_monitor_03.py）")
    ap.add_argument("--timeout", type=int, default=600, help="主程序超时秒数")
    ap.add_argument("--list-voices", action="store_true", help="列出可用语音后退出")
    ap.add_argument("--selftest", action="store_true", help="逻辑自检后退出")
    args = ap.parse_args(own)

    if args.selftest:
        return selftest()

    backend = get_backend(args.engine, args.voice, args.rate, args.volume)

    if args.list_voices:
        voices = backend.list_voices()
        if not voices:
            print("[提示] 未列出任何语音。SAPI 中文语音包可能未安装；"
                  "可试 --engine edge（需 pip install edge-tts + 联网）。")
        else:
            print(f"{'名称':<42}{'语言':<10}{'性别':<10}启用")
            for v in voices:
                print(f"{v['name']:<42}{v['culture']:<10}{v['gender']:<10}{v['enabled']}")
        return 0

    # 取文稿：--run > --text > --text-file > stdin
    if do_run:
        main_script = args.main_script or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hotsearch_monitor_03.py")
        text = run_main_program(main_script, main_args, timeout=args.timeout)
    elif args.text is not None:
        text = args.text
    elif args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        ap.print_help()
        print("\n[提示] 未指定文稿。试听用 --text \"你好\"；"
              "完整链路用 --run --model-dir models --llama-dir llama-bin --count 1",
              file=sys.stderr)
        return 1

    b = Broadcaster(backend, pause=args.pause, speak_labels=args.speak_labels,
                    captions=args.captions, captions_file=args.captions_file,
                    no_play=args.no_play, save_wav=args.save_wav)
    b.play(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
