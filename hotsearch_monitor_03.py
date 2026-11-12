#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热搜去重抽取 + 联网检索 + 本地大模型两段式解读
==============================================

流程：
  1) 抽取热搜标题（百度/头条/微博/抖音/B站；--source auto 按当前秒个位
     自动选榜，失败自动回退）+ FIFO 历史队列去重；
  2) Bing / DuckDuckGo 联网检索，抓取详情网页正文（含页脚备案清理）；
  3) 把「热搜标题 + 详细网页文本 + 检索摘要」压成约 600 字上下文，投入本地
     llama.cpp（llama-server）大模型，生成两段式回复：
        · 【热点客观详细陈述】—— 客观复述事实，不带人格色彩
        · 【人格解读评价】  —— 按预设人格口吻给出解读与评价
     人格通过 PERSONAS / --persona / --persona-prompt 配置（当前仅 default，
     已预留后续扩展接口）。一次运行共用一个 server 进程，避免重复加载权重。

CLI 默认只输出「最后的模型回答」；过程信息可用 --verbose 打开（走 stderr）。

仅依赖 Python 标准库，无需 pip 安装；模型推理需另备 llama-server 与 GGUF。

用法示例：
  python hotsearch_monitor_03.py --model-dir models --llama-dir llama-bin --count 1
  python hotsearch_monitor_03.py --source auto --count 1   # 按当前秒个位自动切换榜单
  python hotsearch_monitor_03.py --source douyin --count 1  # 抖音热搜榜
  python hotsearch_monitor_03.py --source bilibili --count 1 # B站热搜榜
  python hotsearch_monitor_03.py --context-chars 600 --persona-prompt "你是一位犀利的科技评论员"
  python hotsearch_monitor_03.py --verbose        # 额外查看过程信息
  python hotsearch_monitor_03.py --no-llm         # 不投模型（调试原流程）
  python hotsearch_monitor_03.py --reset          # 清空历史队列后重新抽取
"""

import argparse
import glob
import gzip
import html
import json
import os
import platform
import re
import shutil
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.parse

# 版本标记：--verbose 时会打印，用于确认本地文件是否是最新版
VERSION = "v7-douyin-bilibili"

# ---------------------------------------------------------------------------
# 基础网络工具
# ---------------------------------------------------------------------------

# 关闭证书校验（部分聚合接口证书链在沙箱里不完整，避免误报 SSL 错误）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def http_get(url, timeout=15):
    """发起 GET 请求，自动处理 gzip，返回解码后的文本字符串。"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        data = resp.read()
    # 部分服务返回 gzip 压缩体
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    # 尝试 utf-8，失败再尝试 gbk（中文站点常见）
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def strip_tags(raw):
    """极简 HTML -> 文本（去掉标签并解码 HTML 实体）。"""
    return html.unescape(re.sub(r"<[^>]+>", "", raw))


def clean_text(text):
    """折叠空白，便于阅读。"""
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# 热搜榜单抽取
# ---------------------------------------------------------------------------

def fetch_baidu_hot():
    """从百度实时热点榜抽取标题，返回 [{title, url}] 列表。"""
    url = "https://top.baidu.com/board?tab=realtime"
    html = http_get(url)
    results = []
    # 真实 HTML 里标题在 class="title_xxxx" 的 div 内、由 <a> 包裹
    for m in re.finditer(r'class="title_[^"]*"[^>]*>(.*?)</div>', html, re.S):
        block = m.group(1)
        a = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if a:
            href = a.group(1)
            title = clean_text(strip_tags(a.group(2)))
        else:
            href = ""
            title = clean_text(strip_tags(block))
        if title:
            results.append({"title": title, "url": href})
    return results


def fetch_weibo_hot():
    """微博热搜（需要 UA，沙箱里可能 403，失败时由调用方回退）。"""
    url = "https://weibo.com/ajax/side/hotSearch"
    html = http_get(url)
    data = json.loads(html)
    items = data.get("data", {}).get("realtime", [])
    results = []
    for it in items:
        t = it.get("word", "").strip()
        if t:
            results.append({"title": t, "url": it.get("url", "")})
    return results


def fetch_toutiao_hot():
    """头条热榜（JSON 接口，返回 data[].Title）。"""
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    data = json.loads(http_get(url))
    results = []
    for it in data.get("data", []):
        t = (it.get("Title") or "").strip()
        if t:
            results.append({"title": t, "url": it.get("Url", "")})
    return results


def fetch_douyin_hot():
    """抖音热搜榜（iesdouyin 公开接口，返回 word_list[].word，无需签名）。"""
    url = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
    data = json.loads(http_get(url))
    if data.get("status_code", 0) != 0:
        raise RuntimeError("抖音榜返回异常 status_code=%s" % data.get("status_code"))
    results = []
    for it in data.get("word_list") or []:
        t = (it.get("word") or "").strip()
        if t:
            results.append({"title": t, "url": ""})
    if not results:
        raise RuntimeError("抖音榜返回为空")
    return results


def fetch_bilibili_hot():
    """B站热搜榜（search/square 的 trending.list[].keyword，无需登录）。

    主接口为空时回退到「热门视频」榜的 title，尽量保证有标题可用。
    """
    results = []
    try:
        url = "https://api.bilibili.com/x/web-interface/search/square?limit=20"
        data = json.loads(http_get(url))
        if data.get("code") == 0:
            trend = ((data.get("data") or {}).get("trending") or {})
            for it in trend.get("list") or []:
                t = (it.get("keyword") or it.get("show_name") or "").strip()
                if t:
                    results.append({"title": t, "url": ""})
    except Exception:
        pass
    if not results:
        url = "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1"
        data = json.loads(http_get(url))
        for it in (data.get("data") or {}).get("list") or []:
            t = (it.get("title") or "").strip()
            if t:
                results.append({"title": t, "url": ""})
    if not results:
        raise RuntimeError("B站榜返回为空")
    return results


SOURCES = {
    "baidu": fetch_baidu_hot,
    "toutiao": fetch_toutiao_hot,
    "weibo": fetch_weibo_hot,
    "douyin": fetch_douyin_hot,
    "bilibili": fetch_bilibili_hot,
}

# ---------------------------------------------------------------------------
# --source auto：按「当前时间戳秒的个位数」硬绑定热搜来源
# ---------------------------------------------------------------------------
# 个位数 -> 来源名的硬绑定表。0/1/2 百度/头条/微博，3 抖音、4 B站均已就绪；
# 用 mapped in SOURCES 判断：抓取函数只要注册进 SOURCES，对应个位即自动生效。
AUTO_DIGIT_MAP = {
    0: "baidu",
    1: "toutiao",
    2: "weibo",
    3: "douyin",     # 抖音热搜
    4: "bilibili",   # B站热搜
}


def resolve_source(source_name):
    """把 --source 解析成本轮真正要用的来源名。

    仅当 source_name == "auto" 时生效：取当前时间戳「秒的个位数」，
    命中 AUTO_DIGIT_MAP 且该来源已在 SOURCES 注册 -> 直接用；
    其余个位数（5-9 等未配置的个位）没有配置映射关系，
    复用「已配置来源队列」= SOURCES 当前注册顺序，按个位取模选一个。
    每次调用都重新读秒，故放在 run_once 内 = 每一轮重新选源；
    --loop 下每轮播报都可能切换榜单。故障回退仍由 fetch_hot_titles 负责。
    """
    if source_name != "auto":
        return source_name
    digit = int(time.time()) % 10
    mapped = AUTO_DIGIT_MAP.get(digit)
    if mapped and mapped in SOURCES:
        _vprint(f"[auto] 秒个位={digit} → {mapped}（硬绑定）")
        return mapped
    # 未配置映射关系的个位数：复用已经配置过的热点来源队列
    queue = list(SOURCES) or ["baidu"]
    slot = digit % len(queue)
    _vprint(f"[auto] 秒个位={digit} 无映射 → 复用已配置队列[{slot}] → {queue[slot]}")
    return queue[slot]


def fetch_hot_titles(source_name):
    """按名称抽取热搜标题，失败时自动回退到下一个可用源。"""
    order = [source_name] + [s for s in SOURCES if s != source_name]
    last_err = None
    for name in order:
        try:
            titles = SOURCES[name]()
            if titles:
                return name, titles
            last_err = f"{name} 返回为空"
        except Exception as e:  # noqa: BLE001
            last_err = f"{name} 失败: {e}"
    raise RuntimeError(f"所有热搜源均不可用（{last_err}）")


# ---------------------------------------------------------------------------
# 历史队列（去重记录）
# ---------------------------------------------------------------------------

def load_history(path):
    """读取历史队列（JSON 列表），文件不存在则返回空列表。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(path, history):
    """将历史队列写回文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def normalize(title):
    """归一化标题用于去重比较：保留字母/数字/汉字，去空格与标点，转小写。"""
    kept = re.sub(r"[\W_]+", "", title, flags=re.UNICODE)
    return kept.lower()


# ---------------------------------------------------------------------------
# 联网检索
# ---------------------------------------------------------------------------

def parse_bing(html, max_results=6):
    """解析 Bing 搜索结果，返回 [{title, url, snippet}]。"""
    out = []
    for part in html.split('<li class="b_algo"')[1:]:
        chunk = part[:6000]
        hm = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        sm = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        title = clean_text(strip_tags(hm.group(2))) if hm else ""
        url = hm.group(1) if hm else ""
        snippet = clean_text(strip_tags(sm.group(1))) if sm else ""
        text = title + ("  —  " + snippet if snippet else "")
        if text.strip():
            out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= max_results:
            break
    return out


def parse_ddg(html, max_results=6):
    """解析 DuckDuckGo HTML 版搜索结果，返回 [{title, url, snippet}]。"""
    out = []
    for part in html.split('class="result__a"')[1:]:
        chunk = part[:4000]
        am = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', chunk, re.S)
        title = clean_text(strip_tags(am.group(2))) if am else ""
        url = am.group(1) if am else ""
        snippet = clean_text(strip_tags(sm.group(1))) if sm else ""
        text = title + ("  —  " + snippet if snippet else "")
        if text.strip():
            out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= max_results:
            break
    return out


def search_web(query, engine="bing", timeout=15):
    """联网检索，返回 (来源名, [结果字典])。优先 Bing，失败回退 DuckDuckGo。"""
    q = urllib.parse.quote_plus(query)
    if engine in ("bing", "auto"):
        try:
            html = http_get("https://www.bing.com/search?q=" + q, timeout=timeout)
            res = parse_bing(html)
            if res:
                return "Bing", res
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[warn] Bing 检索失败: {e}\n")
    # 回退
    try:
        html = http_get("https://html.duckduckgo.com/html/?q=" + q, timeout=timeout)
        res = parse_ddg(html)
        if res:
            return "DuckDuckGo", res
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[warn] DuckDuckGo 检索失败: {e}\n")
    return "无结果", []


# 非文章类站点（视频/直播/音乐/社交），没有可读正文，直接跳过
_NON_ARTICLE_HOSTS = (
    "v.qq.com", "v.youku.com", "www.bilibili.com/video", "player.bilibili.com",
    "live.", "tv.", "weibo.com", "m.weibo.cn", "music.", "open.spotify.com",
    "youtube.com", "youtu.be",
    "baike.baidu.com",   #新增，直接不抓取百度百科页面，它经常403而且很多不是新闻
)


def is_article_url(url):
    """判断链接是否可能是可读的文章页（过滤视频/直播等无效源）。"""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return True
    return not any(h in host for h in _NON_ARTICLE_HOSTS)


def _block_text(block):
    """返回 (纯文本, 链接内文本长度)，用于剔除导航/广告块。"""
    link_texts = re.findall(r"<a[^>]*>(.*?)</a>", block, re.S | re.I)
    link_len = sum(len(clean_text(strip_tags(x))) for x in link_texts)
    text = clean_text(strip_tags(block))
    return text, link_len


# 页脚版权 / ICP 备案 / 经营许可证 / 举报信息等「强特征」：正常正文几乎不会出现
_BOILERPLATE_STRONG = (
    r"Copyright", r"All\s*rights\s*reserved", r"[©Ⓒ]", r"ICP备", r"ICP证",
    r"公网安备", r"网登网视备", r"增值电信业务经营许可证",
    r"广播电视节目制作经营许可证", r"互联网新闻信息服务许可证",
    r"互联网宗教信息服务许可证", r"信息网络传播视听节目许可证",
)
# 「弱特征」：单独出现可能是新闻内容（如"获得经营许可证"），累计命中多个才判为页脚
_BOILERPLATE_WEAK = (
    r"增值电信业务", r"广播电视节目制作经营", r"互联网新闻信息服务许可",
    r"互联网宗教信息服务许可", r"违法及不良信息举报", r"不良信息举报",
    r"举报中心", r"举报电话", r"经营许可证", r"服务许可证",
    r"许可证编号", r"许可证号", r"备案号",
)
# 页脚区块的「起始标记」：备案信息总是位于文末，从这里开始整段切掉
_FOOTER_START = _BOILERPLATE_STRONG + (
    r"主管[:：]", r"主办[:：]", r"承办[:：]", r"投稿邮箱", r"举报中心",
    r"举报电话", r"违法及不良信息举报", r"不良信息举报",
)


def _is_boilerplate(text):
    """判断文本是否为页脚版权 / 备案 / 许可证等无效信息。"""
    if any(re.search(p, text, re.I) for p in _BOILERPLATE_STRONG):
        return True
    return sum(1 for p in _BOILERPLATE_WEAK if re.search(p, text, re.I)) >= 2


def _tail_is_boilerplate(tail):
    """判断从某位置到文末是否确实是「备案密集区」。"""
    if any(re.search(p, tail, re.I) for p in _BOILERPLATE_STRONG):
        return True
    return sum(1 for p in _BOILERPLATE_WEAK if re.search(p, tail, re.I)) >= 2


def _strip_boilerplate(text):
    """切除文本尾部的页脚/备案片段，保留前面的正文。

    备案信息永远出现在网页末尾，且自身特征密集；因此从最早的「页脚起始标记」
    处切断即可。用 tail 长度上限保护正文里偶发提到"许可证/举报电话"的情况。
    """
    if not text:
        return ""
    positions = []
    for p in _FOOTER_START:
        m = re.search(p, text, re.I)
        if m:
            positions.append(m.start())
    for pos in sorted(set(positions)):
        tail = text[pos:]
        # 页脚一般不超过千字；过长的 tail 多半是正文里偶发提及，不切
        if len(tail) <= 1000 and _tail_is_boilerplate(tail):
            return text[:pos].strip()
    if _is_boilerplate(text):
        return ""
    return text


def _usable_text(block, min_len):
    """抽取块文本：过滤外链过多的导航块，切除页脚备案片段后返回正文。"""
    t, lt = _block_text(block)
    if not t or (lt / max(1, len(t))) > 0.5:
        return ""
    t = _strip_boilerplate(t)          # 先切掉尾部备案，尽量保住前面的正文
    if len(t) < min_len or _is_boilerplate(t):
        return ""
    return t


def extract_main_text(html, max_chars=3000):
    """从网页 HTML 中提取正文文本，剔除导航/广告/页脚备案等低信息块。

    策略：
      1) 优先抽取 <p> 段落，逐段过滤导航块并切除尾部备案片段；
      2) 仅当完全没有有效段落时，才回退按整页块做文本密度筛选；
      3) 逐行兜底：切除行内备案片段、丢弃整行为备案的行；
      4) 整体兜底：备案跨行拼接时，从首个页脚标记处再切一次。
    """
    html = re.sub(r"<(script|style|noscript|svg|head|nav|header|footer|aside|form)[^>]*>.*?</\1>",
                  "", html, flags=re.S | re.I)
    # 1) 段落级提取
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)
    kept = []
    for p in paras:
        t = _usable_text(p, 40)
        if t:
            kept.append(t)
    body = "\n".join(kept)
    # 2) 仅当完全没有有效段落时才回退块级，避免粗粒度块把正文连同噪声一起丢掉
    if not kept:
        blocks = re.split(r"</(?:div|section|article|li|td)|<br\s*/?>", html, flags=re.I)
        scored = []
        for b in blocks:
            t = _usable_text(b, 80)
            if t:
                scored.append(t)
        scored.sort(key=len, reverse=True)
        body = "\n".join(scored[:8])
    # 3) 逐行兜底：切除行内备案片段，并丢弃整行为备案的行
    lines = []
    for ln in body.split("\n"):
        ln = _strip_boilerplate(ln.strip())
        if not ln or _is_boilerplate(ln):
            continue
        lines.append(ln)
    body = "\n".join(lines)
    # 4) 整体兜底：备案信息被拼到同一行、跨段时再切一次
    body = _strip_boilerplate(body)
    return body[:max_chars]


def fetch_article(url, timeout=12, max_chars=3000):
    """抓取单篇详情网页的正文文本，返回纯文本（失败返回空串）。"""
    if not url or url.startswith("javascript:") or not is_article_url(url):
        return ""
    try:
        html = http_get(url, timeout=timeout)
        return extract_main_text(html, max_chars=max_chars)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[warn] 抓取网页正文失败({url}): {e}\n")
        return ""


# ---------------------------------------------------------------------------
# 本地大模型生成（llama.cpp / llama-server）
# ---------------------------------------------------------------------------
# 与「按时段问候」小程序一致：通过本地 llama-server 的 OpenAI 兼容接口推理。
# 本文件坚持「仅标准库」，HTTP 用 urllib 实现，不引入 requests。
#
# 人格接口（预留）：PERSONAS 目前只有 default；后续新增人格只需往字典里
# 加一项（--persona 的 choices 会自动同步），或用 --persona-prompt 临时覆盖。

PERSONAS = {
    "default": {
        "label": "理性时评观察员",
        "system": (
            "你是一位理性、克制、就事论事的热点观察员。"
            "说话客观平实、逻辑清楚，不煽情、不站队、不堆砌夸张修辞，"
            "习惯先把事实讲明白，再给出有分寸的判断。"
        ),
    },
    # 预留示例（取消注释即可启用，--persona 可选项会自动带上）：
    # "witty": {
    #     "label": "幽默吐槽员",
    #     "system": "你是一位机智幽默、爱用轻松比喻点评热点的观察者……",
    # },
    # "analyst": {
    #     "label": "产业分析师",
    #     "system": "你是一位关注行业结构与长期影响的产业分析师……",
    # },
}

DEFAULT_PERSONA = "default"

# 两段式回复的固定段标
PART_OBJ = "【热点客观详细陈述】"
PART_VIEW = "【人格解读评价】"

OUTPUT_FORMAT = (
    "请严格按下面两段式结构输出，除这两段外不要输出任何前言、说明或总结：\n"
    f"{PART_OBJ}\n"
    "基于给定材料，客观、中立、完整地复述这个热点：发生了什么、涉及谁、"
    "关键事实与数据、来龙去脉。只写材料里确有的信息，材料没有的不要编造；"
    "不带情绪、不做评价。约 250 字。\n"
    f"{PART_VIEW}\n"
    "以你被设定的人格身份与口吻，对上述热点给出解读与评价：可谈成因、影响、"
    "值得关注之处，或有分寸的判断。必须明显体现该人格的语气与立场。约 150 字。"
)


def get_persona(name=None, override=None):
    """取人格设定（预留接口）：按名称取预设，override 文本可临时覆盖。"""
    p = PERSONAS.get(name or DEFAULT_PERSONA, PERSONAS[DEFAULT_PERSONA])
    system = override.strip() if override and override.strip() else p["system"]
    return {"name": name or DEFAULT_PERSONA, "label": p["label"], "system": system}


def find_gguf(model_dir, model=None):
    """定位 GGUF 模型：优先用显式路径，否则扫描目录中体积最大的 .gguf。"""
    if model:
        if not os.path.isfile(model):
            raise FileNotFoundError(f"指定的模型文件不存在：{model}")
        return model
    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"模型目录不存在：{model_dir}")
    files = sorted(glob.glob(os.path.join(model_dir, "*.gguf")))
    if not files:
        raise FileNotFoundError(f"模型目录下没有 .gguf 文件：{model_dir}")
    files.sort(key=os.path.getsize, reverse=True)
    return files[0]


def resolve_llama_server(llama_dir):
    """跨平台定位 llama-server（Windows 用 .exe）。"""
    is_win = platform.system().lower().startswith("win")
    exe = "llama-server.exe" if is_win else "llama-server"
    base = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(llama_dir, exe),
                 os.path.join(base, "llama-bin", exe),
                 os.path.join(base, exe)):
        if os.path.isfile(cand):
            return cand
    found = shutil.which(exe)
    if found:
        return found
    raise FileNotFoundError(
        f"找不到 {exe}；请用 --llama-dir 指定 llama.cpp 构建目录，或用 --no-llm 关闭模型生成")


def _http_request_text(method, url, payload=None, timeout=15):
    """标准库实现的 HTTP 请求，返回解码文本（自动处理 gzip）。"""
    data = None
    headers = dict(HEADERS)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


class LlamaSession:
    """一次运行共用一个 llama-server 进程，避免每个热点都重新加载大权重。"""

    def __init__(self, server_bin, model_path, host="127.0.0.1", port=8080,
                 threads=8, ctx=4096, n_predict=512, temperature=0.7,
                 server_timeout=180, req_timeout=180, log_path="llama_server.log"):
        self.server_bin = server_bin
        self.model_path = model_path
        self.base_url = f"http://{host}:{port}"
        self.host = host
        self.port = port
        self.threads = threads
        self.ctx = ctx
        self.n_predict = n_predict
        self.temperature = temperature
        self.server_timeout = server_timeout
        self.req_timeout = req_timeout
        self.log_path = log_path
        self.proc = None
        self._log = None

    def start(self):
        cmd = [self.server_bin, "-m", self.model_path,
               "--host", self.host, "--port", str(self.port),
               "-ngl", "0", "-t", str(self.threads), "-c", str(self.ctx),
               "--no-warmup"]
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        _vprint(f"[模型] 启动 llama-server：{' '.join(cmd)}")
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP
                 if platform.system().lower().startswith("win") else 0)
        self.proc = subprocess.Popen(cmd, stdout=self._log,
                                     stderr=subprocess.STDOUT, creationflags=flags)
        try:
            if not self._wait_health():
                raise RuntimeError(
                    f"llama-server {self.server_timeout}s 内未就绪，日志：{self.log_path}")
        except Exception:
            self.close()
            raise

    def _wait_health(self):
        deadline = time.time() + self.server_timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(self.base_url + "/health", headers=HEADERS)
                with urllib.request.urlopen(req, timeout=2, context=_SSL_CTX) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def chat(self, messages):
        """调用 OpenAI 兼容接口，返回模型回复文本。"""
        payload = {
            "model": os.path.basename(self.model_path),
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.n_predict,
            "stream": False,
        }
        raw = _http_request_text("POST", self.base_url + "/v1/chat/completions",
                                 payload, timeout=self.req_timeout)
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"].strip()

    def close(self):
        if self.proc is not None:
            try:
                if platform.system().lower().startswith("win"):
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=15)
            except Exception:
                self.proc.kill()
            self.proc = None
        if self._log is not None:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None


# ---------------------------------------------------------------------------
# 上下文构造 / 提示词 / 回复拆分
# ---------------------------------------------------------------------------

def _clip(text, limit):
    text = clean_text(text)
    if limit <= 0:
        return ""
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def build_llm_context(title, results, articles, max_chars=600):
    """把「热搜标题 + 详细网页文本 + 检索摘要」裁剪成约 600 字的模型输入上下文。
    优先级：标题 > 详情网页文本 > 检索摘要；正文用不完的额度回补给摘要，尽量填满预算。"""
    head = f"【热点标题】{clean_text(title)}"
    budget = max(80, max_chars - len(head) - 10)

    d_txt = clean_text("\n".join(a for a in articles if a))
    s_txt = clean_text(" / ".join(r.get("snippet", "")
                                  for r in results if r.get("snippet")))

    # 自适应分配：先给摘要留约 1/4（不超过其自身长度），其余给正文；
    # 任何一方用不满，剩余额度自动回补给另一方，尽量贴近预算上限。
    s_cap = min(len(s_txt), budget // 4)
    d = _clip(d_txt, budget - s_cap) if d_txt else ""
    s = _clip(s_txt, budget - len(d)) if s_txt else ""

    sections = [head]
    if d:
        sections.append("【详情网页文本】" + d)
    if s:
        sections.append("【检索摘要】" + s)
    return "\n".join(sections)


def build_llm_messages(persona, context):
    """system = 人格设定 + 格式硬约束；user = 两段式要求 + 约 600 字材料。"""
    system = (
        persona["system"]
        + "\n\n输出格式硬要求：回复必须且只能由两段组成，依次以"
        + f"「{PART_OBJ}」和「{PART_VIEW}」开头。"
        + "第一段必须保持客观中立，不得带入你的人格色彩；第二段才体现你的人格。"
    )
    user = f"{OUTPUT_FORMAT}\n\n以下是本次热点材料（约 {len(context)} 字）：\n{context}"
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def split_reply(text):
    """把模型回复拆成（客观陈述, 人格解读）；格式不符时整体作为原文返回。"""
    t = text.replace("**", "").strip()
    i = t.find(PART_OBJ)
    j = t.find(PART_VIEW)
    if i >= 0 and j > i:
        return t[i + len(PART_OBJ):j].strip(), t[j + len(PART_VIEW):].strip()
    return "", t


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# 过程信息开关：默认关闭，CLI 只输出「最后的模型回答」；--verbose 时打印到 stderr
_VERBOSE = False


def _vprint(*args, **kwargs):
    """过程/诊断输出：仅 --verbose 时打印，且走 stderr 以免污染最终回答。"""
    if _VERBOSE:
        kwargs.setdefault("file", sys.stderr)
        print(*args, **kwargs)


def _eprint(*args, **kwargs):
    """错误/警告：始终打印到 stderr（不影响 stdout 里唯一的模型回答）。"""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def banner(text):
    _vprint("\n" + "=" * 64)
    _vprint(text)
    _vprint("=" * 64)


def run_once(state, llm=None, persona=None, count=None):
    """执行一次「抽取 → 去重 → 检索 → 投模型 → 输出回答」，返回汇总 dict。

    默认只在 stdout 输出「最后的模型两段式回答」；过程信息由 --verbose 打开。
    llm     : LlamaSession 实例（None = --no-llm 调试模式）
    persona : get_persona() 返回的人格字典
    """
    persona = persona or get_persona()
    count = count or state["count"]

    # 1) 抽取热搜标题
    try:
        src_name, hot_list = fetch_hot_titles(resolve_source(state["source"]))
    except RuntimeError as e:
        _vprint(f"[error] {e}")
        return None
    _vprint(f"来源：{src_name}    共获取热搜条目：{len(hot_list)} 条")

    # 2) 去重校验（队列式）
    history = load_history(state["history"])
    seen = {normalize(t) for t in history}
    accepted, skipped = [], 0
    for item in hot_list:
        key = normalize(item["title"])
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        accepted.append(item)
        if len(accepted) >= count:
            break

    _vprint(f"跳过重复热点：{skipped} 条；本次命中不重复热点：{len(accepted)} 条")
    if not accepted:
        _vprint("[info] 当前榜单的热点都已在历史记录里，可 --reset 清空后再试。")
        return {"src": src_name, "accepted": 0, "skipped": skipped,
                "hist_len": len(history)}

    # 3) 逐条：联网检索 → 抓详情正文（数据收集，默认不打印）→ 投模型 → 输出回答
    engine = "ddg" if state["engine"] == "ddg" else "bing"
    for idx, item in enumerate(accepted, 1):
        title = item["title"]
        _vprint(f"\n【{idx}/{len(accepted)}】热点标题：{title}")
        src, results = search_web(title, engine=engine, timeout=state["timeout"])
        _vprint(f"检索来源：{src}    命中结果：{len(results)} 条")

        # 3.1) 详情网页文本：逐条尝试，直到拿到 state["detail"] 篇有效正文
        articles = []
        if state["detail"] > 0:
            shown = 0
            for r in results:
                if shown >= state["detail"]:
                    break
                url = r.get("url")
                if not url or not is_article_url(url):
                    continue
                art = fetch_article(url, timeout=state["timeout"])
                if art and len(art) >= 80:
                    shown += 1
                    articles.append(art)
                    _vprint(f"  ▸ 详情正文：{len(art)} 字 ← {url}")
            if not articles:
                _vprint("  （未能从检索结果中获取到有效详情正文）")
                _eprint(f"[警告]热点《{title}》：全部详情网页抓取失败，放弃本热点，尝试下一条")
                continue
        # 3.2) 构造约 600 字上下文，投入模型，生成两段式回复
        ctx = build_llm_context(title, results, articles,
                                max_chars=state.get("context_chars", 600))

        _vprint(f"【投入模型】上下文约 {len(ctx)} 字 ｜ 人格：{persona['label']}")
        _vprint(ctx)

        if llm is None:
            # --no-llm 调试模式：没有模型回答，输出收集到的材料供检查
            print(ctx)                         # ctx 本身已含【热点标题】
            continue

        messages = build_llm_messages(persona, ctx)
        try:
            reply = llm.chat(messages)
        except Exception as e:  # noqa: BLE001
            _eprint(f"[error] 模型生成失败：{e}")
            continue

        obj, view = split_reply(reply)
        # ★ 最终 CLI 输出：只有模型的两段式回答
        if obj:
            print(f"{PART_OBJ}\n{obj}\n\n{PART_VIEW}\n{view}")
        else:
            print(reply)                       # 格式不符时原文兜底
        if idx < len(accepted):
            print()                            # 多条热点之间空行分隔

    # 写入历史队列（FIFO：超出容量从队头挤掉最早的）
    for item in accepted:
        history.append(item["title"])
    if len(history) > state["history_size"]:
        history = history[-state["history_size"]:]
    save_history(state["history"], history)

    return {"src": src_name, "accepted": len(accepted), "skipped": skipped,
            "hist_len": len(history)}


def main():
    global _VERBOSE

    parser = argparse.ArgumentParser(
        description="热搜去重抽取 + 联网检索 + 本地大模型两段式解读",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", default="baidu", choices=list(SOURCES) + ["auto"],
                        help="热搜来源（默认 baidu；可选 toutiao / weibo / auto；"
                             "auto=按当前秒个位自动选榜，失败自动回退）")
    parser.add_argument("--count", type=int, default=1,
                        help="抽取的不重复热点数量（默认 1）")
    parser.add_argument("--history-size", type=int, default=50,
                        help="历史队列容量：保留最近 N 条标题（默认 50）")
    parser.add_argument("--history", default=os.path.join(os.getcwd(), "hot_history.json"),
                        help="历史队列文件路径（默认 ./hot_history.json）")
    parser.add_argument("--engine", default="bing", choices=["bing", "ddg", "auto"],
                        help="检索搜索引擎（默认 bing）")
    parser.add_argument("--detail", type=int, default=1,
                        help="每个热点抓取前 N 条检索结果对应网页的正文（默认 1，0=关闭）")
    parser.add_argument("--no-detail", dest="detail", action="store_const", const=0,
                        help="关闭详情网页正文抓取")
    parser.add_argument("--reset", action="store_true",
                        help="运行前清空历史队列")
    parser.add_argument("--timeout", type=int, default=15,
                        help="单次网络请求超时（秒，默认 15）")
    parser.add_argument("--verbose", action="store_true",
                        help="打印过程信息到 stderr（默认关闭，只输出模型回答）")
    # ---- 本地大模型 / 人格 ----
    parser.add_argument("--no-llm", action="store_true",
                        help="不投模型，仅输出收集到的材料（调试用）")
    parser.add_argument("--model-dir", default="models",
                        help="存放 .gguf 量化模型的目录（默认 ./models）")
    parser.add_argument("--model", default=None,
                        help="直接指定 .gguf 模型路径（优先于 --model-dir）")
    parser.add_argument("--llama-dir", default="llama-bin",
                        help="llama.cpp 构建目录，含 llama-server(.exe)（默认 ./llama-bin）")
    parser.add_argument("--llama-host", default="127.0.0.1",
                        help="llama-server 监听地址")
    parser.add_argument("--llama-port", type=int, default=8080,
                        help="llama-server 监听端口")
    parser.add_argument("--persona", default=DEFAULT_PERSONA, choices=list(PERSONAS),
                        help="预设人格（当前仅 default；在 PERSONAS 里新增即可扩展）")
    parser.add_argument("--persona-prompt", default=None,
                        help="自定义人格 system 文本，覆盖 --persona（预留扩展接口）")
    parser.add_argument("--context-chars", type=int, default=600,
                        help="每条热点投入模型上下文的字数预算（默认 600）")
    parser.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 4),
                        help="llama-server CPU 线程数")
    parser.add_argument("--llm-ctx", type=int, default=4096,
                        help="llama-server 上下文长度")
    parser.add_argument("--n-predict", type=int, default=512,
                        help="模型单次生成 token 上限")
    parser.add_argument("--llm-temp", type=float, default=0.7,
                        help="模型采样温度")
    parser.add_argument("--llm-timeout", type=int, default=180,
                        help="等待 llama-server 就绪的最长秒数")
    args = parser.parse_args()

    _VERBOSE = args.verbose
    banner(f"热搜去重抽取 + 联网检索 + 模型两段式解读  [{VERSION}]")

    state = {
        "source": args.source,
        "count": max(1, args.count),
        "history_size": max(1, args.history_size),
        "history": args.history,
        "engine": "ddg" if args.engine == "ddg" else "bing",
        "detail": max(0, args.detail),
        "timeout": max(1, args.timeout),
        "context_chars": max(120, args.context_chars),
    }
    persona = get_persona(args.persona, args.persona_prompt)

    if args.reset and os.path.exists(args.history):
        os.remove(args.history)
        _vprint(f"[info] 已清空历史队列：{args.history}")

    # 启动本地大模型：一次运行共用一个 server；缺模型/缺二进制则自动跳过
    llm = None
    if not args.no_llm:
        try:
            model_path = find_gguf(args.model_dir, args.model)
            server_bin = resolve_llama_server(args.llama_dir)
            llm = LlamaSession(
                server_bin=server_bin,
                model_path=model_path,
                host=args.llama_host,
                port=args.llama_port,
                threads=args.threads,
                ctx=args.llm_ctx,
                n_predict=args.n_predict,
                temperature=args.llm_temp,
                server_timeout=args.llm_timeout,
                log_path=os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "llama_server.log"),
            )
            _vprint(f"[模型] {model_path}")
            _vprint(f"[引擎] {server_bin}")
            _vprint(f"[人格] {persona['label']}（--persona / --persona-prompt 可调整）")
            llm.start()
        except (FileNotFoundError, RuntimeError, OSError) as e:
            # 拿不到模型回答属于致命错误：明确报错并以非零码退出，
            # 不把原始材料误当作「模型回答」输出。调试请显式加 --no-llm。
            if llm is not None:
                llm.close()
            _eprint(f"[error] 无法启动本地大模型：{e}")
            _eprint("[提示] 请确认 models\\ 下有 .gguf、llama-bin\\ 下有 llama-server.exe；"
                    "或加 --no-llm 仅查看材料，加 --verbose 查看过程。")
            sys.exit(2)

    try:
        summary = run_once(state, llm=llm, persona=persona)
        if summary:
            _vprint(f"[汇总] 来源={summary['src']} 新抽取={summary['accepted']} "
                    f"跳过重复={summary['skipped']} "
                    f"历史队列={summary['hist_len']}/{state['history_size']}")
    finally:
        if llm is not None:
            llm.close()


if __name__ == "__main__":
    main()
