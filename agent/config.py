# -*- coding: utf-8 -*-
"""智能体配置: 大脑(DeepSeek 官方, OpenAI 兼容)+ 看图(DeepSeek 多模态)。"""
import os

def _load_key():
    """密钥不写进代码。优先环境变量 BRAIN_API_KEY; 其次本机私有文件 agent/_local_key.py
    (该文件被 .gitignore 排除, 不随仓库发布)。两处都没有就返回空串, 由调用方报错。"""
    k = os.environ.get("BRAIN_API_KEY")
    if k:
        return k
    try:
        from . import _local_key
        return getattr(_local_key, "BRAIN_API_KEY", "")
    except Exception:
        return ""


# 大脑(文本推理 + ReAct JSON 决策): DeepSeek
API_BASE = os.environ.get("BRAIN_API_BASE", "https://api.deepseek.com")
API_KEY = _load_key()
MODEL = os.environ.get("BRAIN_MODEL", "deepseek-v4-flash")

# 看图(多模态): DeepSeek vision-exp, 同 base/key。纯文本 chat 模型不能看图, 故单独指定。
VISION_MODEL = os.environ.get("BRAIN_VISION_MODEL", "deepseek-v4-flash-vision-exp")

# 协议模式: 默认原生 function-calling(根治"推理模型空回复卡死"——空 content 也能出 tool_calls 的动作)。
# 环境变量 BRAIN_TOOLS_MODE 可切; 保留 "json_text" 作回退(不依赖模型原生工具调用, 由正文吐严格 JSON)。
TOOLS_MODE = os.environ.get("BRAIN_TOOLS_MODE", "function_calling")

# 用量统计开关: 开启后 llm_client 在流式请求里带 stream_options.include_usage,
# 把每次调用的 token 用量(含缓存命中/未命中)累加进 llm_client._USAGE。
# 默认关闭 —— 演示链路零改动; 只在"测单次建模成本"时置 1。
TRACK_USAGE = os.environ.get("BRAIN_TRACK_USAGE", "0") == "1"

MAX_ROUNDS = 20   # ReAct 最大循环轮数(留足"建模+≤2次自审+导出"的余量)
TIMEOUT = 180             # 兼容旧引用(实际单次尝试超时见 LLM_TIMEOUT_PER_TRY)
LLM_TIMEOUT_PER_TRY = 180  # 单次大脑 HTTP 尝试超时(健康时 ~1.5s, 推理模型大提示词可达 60~120s):
                            # 让一次慢推理跑完, 而不是 60s 就掐断 -> 从头重试再超, 造成"半天不出内容"
BRAIN_MAX_TOKENS = 16000   # 默认输出 token 上限: 推理模型先吐 reasoning_content, 预算太小会被思考吃光、
                            # 留给动作 JSON 的为 0 -> content 空回复 -> "飞机"这类卡死。抬到 16000 给思考+动作留足。
LLM_RETRIES = 3           # 网络失败最多尝试次数(直连 <-> 代理 交替)

# 大脑请求网络策略: 默认绕过系统/环境代理直连(requests/urllib 在 Windows 会默认吃
# 系统代理, 代理一抽风整场演示就断; DeepSeek 国内服务直连即可)。
# 若必须走代理, 设环境变量 BRAIN_PROXY=http://127.0.0.1:7897。
BRAIN_PROXY = os.environ.get("BRAIN_PROXY") or None  # None=默认直连(另有兜底通道, 见下)
# 直连失败/卡住时的代理兜底通道(本机 Clash 7897); 请求失败会自动交替两条通道重试
BRAIN_FALLBACK_PROXY = os.environ.get("BRAIN_FALLBACK_PROXY", "http://127.0.0.1:7897")

# Blender 5.1 无头执行(宿主)
BLENDER_EXE = r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
BLENDER_TIMEOUT = 240  # 单次 blender -b 超时(秒)
