# 赛博收音机 · 本地 LLM 热搜播报 + 桌面字幕

在 **Windows 本机 CPU** 上用 llama.cpp 跑 GGUF 量化模型，把「按时段问候 → 热搜去重 → 联网检索详情 → LLM 两段式文稿 → TTS 逐句播报 → 桌面字幕 → 系统托盘常驻」串成一条链路。

> **核心设计**：三个阶段共用**同一个 `llama-server` 进程**（7B 权重只加载一次），问候与文稿都是同一个 session 的 `/v1/chat/completions` 调用。主程序只用 Python 标准库（`urllib`），不引入 `requests`。
>
> **逐句字幕**：文稿按中文句末标点切成一句句，每句单独「上屏 → TTS 念完 → 转空闲」，窗口里同时只显示当前这一句，字幕与音频天然对齐。

---

## 0. 当前版本

| 文件 | 版本 | 职责 |
|------|------|------|
| `cyber_radio.py` | `v6-edit-mode` | **一体化主程序**，编排全链路、托盘、常驻 loop |
| `hotsearch_monitor_03.py` | `v7-douyin-bilibili` | 热搜榜拉取、FIFO 去重、检索详情、LLM 两段式文稿 |
| `caption_window.py` | `v4-unified-bitmap-text` | 桌面字幕悬浮窗（tkinter）、透明/编辑模式、高质量位图文字 |
| `tts_broadcaster.py` | `v1-tts-sapi` | Windows SAPI / edge / pyttsx3 语音播报 |
| `time_greeter.py` | 原始版 | 最早的 6 时段问候程序；时段逻辑已内联移植进主程序 |

> 只用 `cyber_radio.py` 作为入口；其余文件**放在同一目录**即可，主程序按文件名动态 import 复用。

---

## 1. 目录结构（推荐）

```
time_greeter/
├─ cyber_radio.py                    # ★ 一体化主程序（入口）
├─ hotsearch_monitor_03.py           # 热搜 / 检索 / 文稿
├─ tts_broadcaster.py                # TTS
├─ caption_window.py                 # 桌面字幕窗
├─ caption_config.json               # 可选：字幕样式（不存在则用默认值）
├─ hot_history.json                  # 运行时生成：去重历史队列
├─ llama-b12211-bin-win-cpu-x64.zip  # Windows CPU 版 llama.cpp 构建
├─ llama-bin/                        # ← 把 zip 解压到这里（含 llama-server.exe + dll）
├─ models/                           # ← 把自己的 .gguf 模型放这里
│   └─ 参考Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf
```

---

## 2. 准备（Windows）

1. Python 3.8+，建议使用项目内的虚拟环境 `.venv`（也兼容 `venv`）。
2. 必备依赖（**托盘 + 高质量字幕文字**需要）：

   ```bat
   pip install pystray pillow
   ```

3. 可选依赖：TTS 切换 `--tts-engine edge` 需 `edge-tts`；`pyttsx3` 同理。`sapi` 是 Windows 自带，零依赖。
4. 把 `llama-b12211-bin-win-cpu-x64.zip` 解压到 `llama-bin\`，确保里面有 `llama-server.exe`。
5. 把 GGUF 模型放进 `models\`。zip 只含推理程序、**不含模型权重**，需自行准备。
6. 若报缺 DLL / `0xc000007b`，安装 [Visual C++ 运行库](https://learn.microsoft.com/zh-cn/cpp/windows/latest-supported-vc-redist) 与 OpenSSL。

---

## 3. 快速开始

以下命令均为 **PowerShell 单行**，在 `1109001\` 目录下执行。

```powershell
# 0) 全链路自检：不加载模型、不联网、不出声（建议先跑）
python cyber_radio.py --selftest

# 1) 单次完整播报
python cyber_radio.py --model-dir models --llama-dir llama-bin --count 1

# 2) 只跑文字链路、不出声（先确认模型与文稿，再开 TTS）
python cyber_radio.py --silent --model-dir models --llama-dir llama-bin --count 1

# 3) ★ 完整形态：桌面字幕 + auto 换榜 + 托盘常驻，每 5 分钟一轮
python cyber_radio.py --subtitle --source auto --loop --interval 300 --model-dir models --llama-dir llama-bin

# 4) 只看字幕效果，不加载模型、不联网
python cyber_radio.py --subtitle-demo

# 5) 列出系统语音 / 输出 JSON 字幕流供外部订阅
python cyber_radio.py --list-voices
python cyber_radio.py --captions --captions-file captions.jsonl --model-dir models --llama-dir llama-bin
```

**三个开关的硬约束**：

| 想要 | 必须 |
|------|------|
| 桌面字幕 | 显式加 `--subtitle`，且**不要**加 `--silent` |
| 系统托盘 | 加 `--loop`，且 `.venv` 里装有 `pystray` + `pillow` |
| 常驻循环 | `--loop`（开机问候只播 1 次，后续各轮只播新闻；Ctrl-C 或托盘「退出」结束） |

> `run.bat` 目前仍指向旧的 `time_greeter.py`（只做 6 时段问候），**一体化程序请用上面的命令行启动**。

---

## 4. 参数速查

完整列表以 `python cyber_radio.py --help` 为准，常用项按组如下。

**模型 / 推理**

| 参数 | 说明 | 默认 |
|------|------|------|
| `--model-dir` / `--model` | GGUF 目录 / 显式指定某个 `.gguf` | `models` / 无 |
| `--llama-dir` | llama.cpp 构建目录 | `llama-bin` |
| `--threads` | CPU 线程数 | `min(8, 核数)` |
| `--llm-ctx` | server 上下文长度 | `4096` |
| `--n-predict` | 单次生成 token 上限 | `512` |
| `--llm-temp` / `--llm-timeout` | 采样温度 / 等待 server 就绪秒数 | `0.7` / `180` |
| `--no-llm` | 调试：不加载模型，文稿直接输出检索材料 | 关 |

**热搜 / 检索 / 文稿**

| 参数 | 说明 | 默认 |
|------|------|------|
| `--source` | `baidu` / `toutiao` / `weibo` / `douyin` / `bilibili` / `auto` | `baidu` |
| `--count` | 每轮播报的不重复热点数 | `1` |
| `--history` / `--history-size` / `--reset` | 去重队列文件 / 容量 / 运行前清空 | `hot_history.json` / `50` / 关 |
| `--search-engine` | `bing` / `ddg`（失败自动回退） | `bing` |
| `--detail` / `--no-detail` | 每个热点抓取的详情正文篇数 / 不抓详情 | `1` |
| `--context-chars` | 投模型的上下文字数预算（自适应补足） | `600` |
| `--persona` / `--persona-prompt` | 预设人格名 / 临时覆盖 system 文本 | `default` |

**TTS / 字幕**

| 参数 | 说明 | 默认 |
|------|------|------|
| `--tts-engine` | `sapi` / `edge` / `pyttsx3` / `mock` | `sapi` |
| `--voice` / `--rate` / `--volume` | 语音名 / 语速 `-10..10` / 音量 `0..100` | 自动 / `0` / `100` |
| `--max-sentence-chars` | 单句字幕最大字数，超长按逗号再切 | `48` |
| `--sentence-pause` / `--pause` | 句间 / 段间停顿秒数 | `0.15` / `0.4` |
| `--speak-labels` / `--no-greeting-tts` | 连段落标题也念 / 启动问候不念 | 均关 |
| `--subtitle` / `--subtitle-demo` | 开启字幕窗 / 纯演示 | 均关 |
| `--subtitle-pos` / `--subtitle-width` / `--subtitle-font-size` / `--subtitle-alpha` / `--subtitle-hold` | 位置、宽、字号、不透明度、播完保留秒数 | 见 `caption_config.json` |
| `--subtitle-config` / `--subtitle-save-config` | 样式文件 / 把当前样式写回文件 | `caption_config.json` |
| `--captions` / `--captions-file` | stdout 输出 JSON 字幕流 / 写文件 | 关 |

**常驻 / 调试**：`--loop`、`--interval`（默认 `300` 秒）、`--verbose`、`--selftest`（别名 `--mock`）。

---

## 5. 字幕窗与样式配置

字幕窗由 `caption_window.py` 提供，跑在**独立 GUI 线程**；主线程只投递队列事件，TTS 的阻塞式 `speak()` 不会卡住窗口刷新。

**行为约定**

- 一句话一段字幕，播完按 `--subtitle-hold` 自动隐藏；无桌面会话 / 非 Windows / 缺 tkinter 时自动降级为无字幕，播报照常。
- **编辑模式**（托盘菜单开启）：可拖动字幕、滚轮整体缩放（字号 + 窗口宽度），编辑期间不自动隐藏。
- **拖动位置在本次运行内持久**：之后每句新字幕都固定在同一位置，**只有重启程序才回默认位置**，不写回配置文件。
- **背景完全透明**：Windows 用 `-transparentcolor` 颜色键穿透。`v4` 起透明与非透明共用同一套位图渲染，**字号、折行、窗口位置严格一致**，不再出现「透明后字体缩小」；文字用灰度抗锯齿 + 深色描边，避免 ClearType 彩边与颜色键脏晕。

`caption_config.json`（可选，缺省即用默认值）：

| 键 | 默认 | 说明 |
|----|------|------|
| `position` | `bottom` | `bottom` / `top` / `center` / `"x,y"` |
| `width` / `font_size` / `font` | `900` / `20` / `Microsoft YaHei` | 宽、正文字号（**点**）、字体名 |
| `label_size` | `12` | 段落标签字号 |
| `fg` / `label_fg` / `bg` | `#FFFFFF` / `#7FD7FF` / `#101418` | 正文色 / 标签色 / 底色 |
| `alpha` | `0.86` | 整体不透明度；透明模式自动锁 `1.0` |
| `topmost` / `hold` | `true` / `3.0` | 置顶 / 播完保留秒数 |
| `show_corner` / `show_status` | `true` / `true` | 角落「来源 · 第N轮」/ 顶部播报状态 |
| `transparent_hq` | `true` | 高质量位图文字总开关；设 `false` 时两种背景一起回退 Tk 原生文字 |

> 命令行显式传入的样式项优先于配置文件；`--subtitle-save-config` 可把当前生效样式写回 json。

---

## 6. 系统托盘菜单（`--loop` 时出现）

```
显示/隐藏字幕                    ← 一级动作项，文案随状态变化
播报控制
  ├ 立即执行一轮播报
  ├ 暂停循环 / 恢复循环 ✓
  └ 切换下一条热点             ← 在当前句边界收尾
热搜设置
  ├ 重置热搜历史队列
  └ 热搜来源切换 ○              ← 百度/头条/微博/auto/抖音/B站 单选
TTS语音设置
  ├ 音量增大 +10 / 减小 -10
  └ 切换语音音色（预留占位）
字幕样式设置
  ├ 字号增大 +2 / 减小 -2
  ├ 置顶字幕 ✓
  ├ 编辑模式 ✓
  └ 背景完全透明 ✓
关于
────────
退出
```

> 托盘点击瞬时生效：播报跑在 daemon worker 线程，主线程只消费队列；pystray 子线程只投递指令，绝不碰 Tk 或 backend。「退出」不会被正在念的 SAPI 语音拖住。

---

## 7. 热搜来源与 `--source auto`

`SOURCES` 注册 5 个榜单：百度、今日头条、微博、**抖音**（`iesdouyin` billboard 接口）、**B 站**（`search/square`，空则回退热门视频标题）。抓不到时由 `fetch_hot_titles` 故障回退。

`--source auto` 按**当前 Unix 时间戳秒的个位数**硬绑定，每一轮 `run_once()` 重新取秒，所以 `--loop` 下每轮可能换榜：

| 秒个位 | 来源 |
|--------|------|
| 0 / 1 / 2 | 百度 / 头条 / 微博 |
| 3 / 4 | 抖音 / B 站 |
| 5–9 | 按已注册来源队列取模轮换 |

> 热搜榜只有标题，详细内容来自「联网检索 → 抓详情页正文 → 约 600 字材料投模型」；页脚备案与非正文域名会被过滤。

---

## 8. 6 个时段划分规则

按小时左闭右开划分，跨零点的「夜间」单独处理：

| 时段 | 范围 | 时段 | 范围 |
|------|------|------|------|
| 清晨 | 05:00–09:00 | 下午 | 14:00–18:00 |
| 上午 | 09:00–12:00 | 傍晚 | 18:00–21:00 |
| 午间 | 12:00–14:00 | 夜间 | 21:00–05:00 |

边界示例：`09:00` → 上午，`14:00` → 下午，`21:00` → 夜间，`05:00` → 清晨。

---

## 9. 自检与测试

```powershell
python cyber_radio.py --selftest       # 全链路：不加载模型、不联网、不出声
python caption_window.py --selftest    # 字幕配置合并 / 同步契约 / 透明一致性
python test_time_greeter.py            # 6 时段划分、模型查找、提示词
python test_orchestration.py           # 推理编排全链路（fake_llama_server.py）
python test_hotsearch_llm.py           # 热搜 + 文稿链路
```

测试均不依赖真实模型权重；编排类测试用 `fake_llama_server.py` 模拟 OpenAI 兼容接口。Windows 控制台若中文乱码，设 `PYTHONIOENCODING=utf-8`。

---

## 10. 常见问题

- **托盘不显示**：必须带 `--loop`，且 `.venv` 里装了 `pystray` + `pillow`；stderr 会打印「未启用」及具体原因。
- **字幕不出现**：需显式 `--subtitle`，且**不要**加 `--silent`；另确认有桌面会话、tkinter 可用。
- **stderr 出现 `[warn] hotsearch_monitor_03.py 版本为 v7-… 建议用 v5-llm600-persona`**：这是主程序里遗留的旧版本提示，当前 `v7` 属正常，功能不受影响，可忽略。
- **透明底文字发糊 / 想退回原生文字**：`v4` 已统一渲染路径；若仍不满意，在 `caption_config.json` 设 `"transparent_hq": false`。
- **SAPI 提示缺中文语音包**：`python cyber_radio.py --list-voices` 查看；可改用 `--tts-engine edge`（需联网与 `edge-tts`）。
- **输出是英文或格式乱**：换用带 `instruct` 的对话模型（如 Qwen2.5 的 GGUF 版）。
- **模型加载慢**：首次加载几秒到几十秒属正常；可减小 `--llm-ctx`、`--n-predict`。
- **`run.bat` 跑出来只有问候**：它仍指向旧的 `time_greeter.py`，一体化程序请用第 3 节命令行。

---

## 11. 已验证情况与边界

- ✅ **Windows 真机**：时间问候、热搜 + LLM 两段式文稿输出。
- ✅ **沙箱验证**：TTS、一体化编排、句级字幕同步、托盘菜单与瞬时响应、抖音/B 站抓取、字幕编辑模式/透明开关，以及 `v4` 透明与非透明的位图尺寸与窗口几何完全一致。
- ✅ 多副本 `py_compile`、`--selftest`、字幕逻辑单测、Xvfb GUI 整链与队列投递测试均通过、零异常。
- ⚠️ 真实权重推理需在 Windows 用 `llama-server.exe` 运行；本仓库测试机为 Linux 且无 Windows 运行时，未在此执行真实模型推理。
- ⚠️ Windows `-transparentcolor` 颜色键在浅色桌面上的最终观感仍需真机确认；字幕位置、编辑模式、透明开关均为**运行期状态**，不写回配置文件，重启回默认。
