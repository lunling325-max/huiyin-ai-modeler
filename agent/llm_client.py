# -*- coding: utf-8 -*-
"""大脑客户端:OpenAI 兼容 /chat/completions。

协议两种模式(见 config.TOOLS_MODE):
- "json_text"(默认): 不依赖模型 function-calling, 要求模型在正文里吐严格 JSON 动作。
- "function_calling": 原生 tools 参数(真 API 支持时切过去)。
"""
import json
import re

import requests

from . import config


class LLMError(Exception):
    pass


def _proxy_candidates():
    """请求通道候选: 主通道在前, 另一条兜底在后, 网络失败重试时交替使用。
    requests 的 proxies={"http": None, "https": None} 表示对应协议直连
    (绕过 Windows 系统代理, 避免 requests 默认吃 Clash)。"""
    if config.BRAIN_PROXY:
        return [{"http": config.BRAIN_PROXY, "https": config.BRAIN_PROXY},
                {"http": None, "https": None}]
    return [{"http": None, "https": None},
            {"http": config.BRAIN_FALLBACK_PROXY, "https": config.BRAIN_FALLBACK_PROXY}]


# ---------------- 用量统计(默认关闭, 见 config.TRACK_USAGE) ----------------
# 用途: 量出"一次建模到底烧多少 token", 据此算单次成本(商业计划书财务页)。
# 开启后流式请求会带 stream_options.include_usage, 由服务端在末块回传 usage。
_USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
          "cache_hit_tokens": 0, "cache_miss_tokens": 0}

# DeepSeek 官方价, 元/百万 tokens(2026-09-13 取自 api-docs.deepseek.com/quick_start/pricing)。
# 高峰时段 = 北京时间 周一至周五 9:00-12:00、14:00-18:00; 其余为空闲时段(价格减半)。
PRICES = {
    "flash": {"hit": (0.02, 0.04), "miss": (1.0, 2.0), "out": (4.0, 8.0)},
    "pro":   {"hit": (0.15, 0.30), "miss": (4.5, 9.0), "out": (13.5, 27.0)},
}


def usage_reset():
    for k in _USAGE:
        _USAGE[k] = 0


def usage_snapshot():
    return dict(_USAGE)


def estimate_cost_yuan(usage=None, family="flash", peak=False):
    """按官方单价把用量折成人民币。返回 (总额, 明细dict)。
    缓存命中/未命中分开计价; usage 里没有 hit/miss 字段时, 全部按"未命中"(最贵)保守计。"""
    u = usage if usage is not None else usage_snapshot()
    p = PRICES[family]
    i = 1 if peak else 0
    hit = u.get("cache_hit_tokens") or 0
    miss = u.get("cache_miss_tokens") or 0
    if not hit and not miss:  # 服务端没给缓存明细 -> 保守全按未命中
        miss = u.get("prompt_tokens") or 0
    cost_in_hit = hit / 1e6 * p["hit"][i]
    cost_in_miss = miss / 1e6 * p["miss"][i]
    cost_out = (u.get("completion_tokens") or 0) / 1e6 * p["out"][i]
    detail = {"命中输入": cost_in_hit, "未命中输入": cost_in_miss, "输出": cost_out,
              "命中tokens": hit, "未命中tokens": miss}
    return cost_in_hit + cost_in_miss + cost_out, detail


def _accumulate(usage):
    """把一次响应的 usage 累加进 _USAGE。服务端未回传时静默跳过。"""
    if not usage:
        return
    _USAGE["calls"] += 1
    _USAGE["prompt_tokens"] += usage.get("prompt_tokens") or 0
    _USAGE["completion_tokens"] += usage.get("completion_tokens") or 0
    _USAGE["cache_hit_tokens"] += usage.get("prompt_cache_hit_tokens") or 0
    _USAGE["cache_miss_tokens"] += usage.get("prompt_cache_miss_tokens") or 0


def extract_json(text: str) -> dict:
    """从模型输出里抠出一个 JSON 对象:容忍 ```json 围栏和前后废话。"""
    if not text:
        raise LLMError("模型返回空内容")
    s = text.strip()
    # 剥 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if fence:
        s = fence.group(1).strip()
    # 找第一个 { 到与之平衡的 }
    start = s.find("{")
    if start == -1:
        raise LLMError(f"输出里没有 JSON 对象: {text[:200]}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError as e:
                    raise LLMError(f"JSON 解析失败: {e}")
    raise LLMError(f"JSON 括号不平衡: {text[:200]}")


class LLMClient:
    def __init__(self, base_url=None, api_key=None, model=None, timeout=None):
        self.base_url = (base_url or config.API_BASE).rstrip("/")
        self.api_key = api_key or config.API_KEY
        self.model = model or config.MODEL
        self.timeout = timeout or config.LLM_TIMEOUT_PER_TRY

    def chat(self, messages, max_tokens=None, temperature=None, on_token=None,
             tools=None, tool_choice=None):
        """messages: [{'role': 'user'|'assistant'|'system'|'tool', 'content': str, ...}, ...]
        on_token: 可选回调 (text: str, kind: 'reasoning'|'content') -> None, 用于流式逐字推给前端。
        tools: 可选 OpenAI function schema 列表, 仅 function_calling 模式用到。
        tool_choice: 可选, 默认 auto(不传)。注意推理模型若被强制指定 tool_choice 会返回 400, 故平时别传。
        返回:
          - function_calling 模式: dict {"content": str|None, "tool_calls": [{"id","name","args":dict}, ...]}
          - json_text 模式: str (即 content)
        """
        is_stream = on_token is not None
        is_fc = config.TOOLS_MODE == "function_calling"
        payload = {"model": self.model, "messages": messages, "stream": is_stream}
        if is_stream and config.TRACK_USAGE:
            # 流式响应默认不带 usage, 须显式索要; 服务端在末块回传。
            payload["stream_options"] = {"include_usage": True}
        if max_tokens is None:
            max_tokens = config.BRAIN_MAX_TOKENS  # 默认上限, 约束推理+动作不跑飞
        payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if is_fc:
            if tools:
                payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        # 网络层失败(超时/断连/代理抖动)自动交替 直连<->代理 重试; 正常 ~1.5s 一次, 只有上游不健康才多花时间
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        last = None
        for attempt in range(config.LLM_RETRIES):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     timeout=self.timeout, stream=is_stream,
                                     proxies=_proxy_candidates()[attempt % 2])
                if resp.status_code != 200:
                    last = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                    continue  # 换通道再试
                if is_stream:
                    raw, tool_calls, usage = self._read_stream(
                        resp, on_token, collect_tool_calls=is_fc)
                else:
                    data = resp.json()
                    try:
                        msg = data["choices"][0]["message"]
                    except (KeyError, IndexError):
                        last = LLMError(f"响应格式异常: {str(data)[:300]}")
                        continue  # 换通道再试
                    raw = msg.get("content")
                    tool_calls = self._normalize_tool_calls(msg.get("tool_calls")) if is_fc else []
                    usage = data.get("usage")
                if config.TRACK_USAGE:
                    _accumulate(usage)
                if is_fc:
                    # 关键: 空回复的根修 —— 只要 tool_calls 非空, content 为空也能出动作, 不算空回复。
                    if not str(raw or "").strip() and not tool_calls:
                        raise LLMError("模型返回空内容")
                    return {"content": raw, "tool_calls": tool_calls}
                if raw is None or not str(raw).strip():
                    # 推理模型间歇性把预算全花在 reasoning_content 上、content 为空。这与通道(网络)无关,
                    # 换通道重试只白烧时间; 立即抛出, 交给上层 loop 用"别空回复、只出动作"的提示多试几次蹭过去。
                    raise LLMError("模型返回空内容")
                return raw
            except requests.exceptions.RequestException as e:
                last = e  # 纯网络层问题 -> 换通道再试
        # 到这里说明 LLM_RETRIES 次都没成功; 空回复造成的 last 直接带出, 让上层判定解析失败而非静默吞掉
        raise LLMError(f"大脑请求失败(直连/代理共尝试{config.LLM_RETRIES}次): {last}")

    @staticmethod
    def _normalize_tool_calls(tool_calls):
        """把 OpenAI 原生 tool_calls(非流式)归一化成 loop 用的 {"id","name","args":dict}。"""
        out = []
        for tc in (tool_calls or []):
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            argstr = (fn.get("arguments") or "").strip()
            args = {}
            if argstr:
                try:
                    args = json.loads(argstr)
                except ValueError:
                    args = {"_raw": argstr}
            out.append({"id": tc.get("id"), "name": name, "args": args})
        return out

    def _read_stream(self, resp, on_token, collect_tool_calls=False):
        """读取流式 SSE。reasoning_content/content 经 on_token(text, kind) 推出。
        collect_tool_calls=True 时还按 index 累积 tool_calls 碎片(流式里 name/arguments 分片到)。
        返回 (raw_content, tool_calls, usage): tool_calls 为 [{"id","name","args":dict}] 顺序按 index;
        usage 为末块的用量字典(仅 include_usage 时有), 否则 None。"""
        raw = ""
        acc = {}
        usage = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                obj = json.loads(d)
            except ValueError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]  # 末块 choices 为空, 须先取 usage 再取 delta
            try:
                delta = obj["choices"][0]["delta"]
            except (KeyError, IndexError):
                continue
            rc = delta.get("reasoning_content")
            if rc:
                try:
                    on_token(rc, "reasoning")
                except Exception:
                    pass
            c = delta.get("content")
            if c:
                raw += c
                try:
                    on_token(c, "content")
                except Exception:
                    pass
            if collect_tool_calls and delta.get("tool_calls"):
                for tc in delta["tool_calls"]:
                    try:
                        idx = tc.get("index", 0)
                    except Exception:
                        idx = 0
                    slot = acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
        tool_calls = []
        if collect_tool_calls:
            for idx in sorted(acc):
                slot = acc[idx]
                argstr = slot["arguments"].strip()
                args = {}
                if argstr:
                    try:
                        args = json.loads(argstr)
                    except ValueError:
                        args = {"_raw": argstr}
                tool_calls.append({"id": slot["id"], "name": slot["name"], "args": args})
        return raw, tool_calls, usage

    def chat_json(self, messages, **kw) -> dict:
        """聊天并要求返回严格 JSON, 自动重试几次纠偏。"""
        last = None
        for attempt in range(3):
            try:
                txt = self.chat(messages, **kw)
                return extract_json(txt)
            except (LLMError, json.JSONDecodeError) as e:
                last = e
                messages = messages + [{
                    "role": "user",
                    "content": f"你上一条输出不是可解析的 JSON({e})。请只输出一个合法 JSON 对象, 不要任何多余文字。",
                }]
        raise LLMError(f"多次尝试后仍无法得到合法 JSON: {last}")
