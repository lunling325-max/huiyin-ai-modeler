# -*- coding: utf-8 -*-
"""主循环:function-calling ReAct 的 JSON-in-text 版(薄壳, 注册表驱动)。

消息流:
  msg = [system(角色+工具目录+输出格式) , user(需求)]
  for <= MAX_ROUNDS:
     raw = llm.chat(msg)
     act = extract_json(raw)
     if act.done  -> 交付, 返回 {"answer", "rounds", "history"}
     elif act.tool in registry -> 跑真实工具, 结果以 user 消息回填
     else -> 纠偏消息(未知工具/非法格式), 连续失败上限后放弃
"""

import json
from pathlib import Path

from . import config
from . import llm_client
from .llm_client import extract_json, LLMError


ROLE_PROMPT = """你是一个在本地 3D 建模工作台里自主干活的智能体。
用户用中文提出建模需求, 你要自己决定调哪些工具、按什么顺序调, 直到产出可交付的模型文件。
你的能力边界和当前可用的工具见下方工具目录。真正动手的动作(写脚本/执行/渲染/导出/查记忆)必须通过工具完成, 不要假装已经完成。
遇到脚本报错就读取错误、修正后重试; 用户要求有歧义就按常识合理推断, 不要反复追问。
最后确认产物齐全(模型文件已落盘)后再交付。

注意区分:
- 用户给出的是"具体建模任务"(要做什么物体/改哪里)时, 才需要调用工具序列。
- 用户只是打招呼、问你会什么、闲聊、或说一句与当前任务无关的话时, 不要调用任何工具, 直接用 done 交付一段简短的中文回答即可。
- 同一工具不要无意义地重复调用; 没有任何需要执行的动作时, 说明当前状态并 done。"""

ACTION_FORMAT = """你的每一条回复必须严格是下面两种 JSON 之一, 不得输出任何 JSON 以外的文字(不要用 ``` 围栏):

直接给出 JSON, 不要长篇推理, 更不要输出 ``` 围栏或多余文字。调用工具时保持简短:

调用工具:
{"reason": "<一句话说明这步为什么>", "tool": "<工具名>", "args": {<该工具要求的参数>}}

交付(所有产物已就绪):
{"reason": "<一句话总结做了什么>", "done": true, "answer": "<给用户的中文交付说明>", "files": ["<产物相对路径或说明>"]}

若某工具需要的前置条件不满足(例如还没生成脚本就去渲染), 先调用能获得前置条件的工具。
"""

FC_ACTION_FORMAT = """你通过原生工具调用(function-calling)干活, 不要输出任何包裹动作的 JSON 文本。
- 需要调工具(写脚本/执行/渲染/导出/查记忆/看图)时, 直接用工具调用, 一句话说明这步在做什么。
- 当所有产物齐备、要交付时, 直接用中文给用户一个自然段总结(做了什么、产物有哪些), 不要再发起任何工具调用; 我会把它当作交付。
- 模型文件(.blend/.glb/.stl)是真实产物, 由工具写进工作目录; 在它们真正落盘前不要宣称完成。
"""

DONE_KEYS = ("done",)
ERROR_LIMIT = 3  # 连续非法回复/未知工具的上限


class ToolRegistry:
    """name -> {desc, run(args)->str}。run 返回的字符串会成为下一轮 user 消息。"""

    def __init__(self):
        self._tools = {}

    def register(self, name, desc, run, params=None):
        # run(args: dict, ctx: object|None) -> str
        # params: 可选 OpenAI JSON-schema, function_calling 模式会据此生成原生 tools。
        self._tools[name] = {"desc": desc, "run": run, "params": params or {}}

    def names(self):
        return list(self._tools)

    def catalog(self):
        """生成给模型的工具目录文本。"""
        lines = []
        for name, t in self._tools.items():
            lines.append(f"- {name}: {t['desc']}")
        return "\n".join(lines)

    def schema(self):
        """生成 OpenAI 原生 tools 列表(function_calling 模式用)。"""
        out = []
        for name, t in self._tools.items():
            out.append({"type": "function",
                        "function": {"name": name,
                                     "description": t["desc"],
                                     "parameters": t["params"]}})
        return out


def build_system_prompt(registry: ToolRegistry, extra_rules: str = "",
                        memory_prelude: str = "", mode: str = "json_text") -> str:
    sys_p = (ROLE_PROMPT
             + "\n\n## 当前可用工具\n" + registry.catalog())
    # 协议不同, 输出格式指令也不同: json_text 要求正文吐严格 JSON; function_calling 用原生工具、交付用纯文本。
    sys_p += "\n\n" + (FC_ACTION_FORMAT if mode == "function_calling" else ACTION_FORMAT)
    if extra_rules:
        sys_p += "\n\n## 额外纪律\n" + extra_rules
    if memory_prelude:
        sys_p += "\n\n" + memory_prelude
    return sys_p


class Agent:
    def __init__(self, llm=None, registry=None, max_rounds=None, memory=None):
        self.llm = llm or llm_client.LLMClient()
        self.registry = registry or ToolRegistry()
        self.max_rounds = max_rounds or config.MAX_ROUNDS
        self.memory = memory  # Memory|None(S3a 跨会话记忆)

    def run(self, user_request: str, extra_rules: str = "", ctx=None, emit=None, verbose=True,
            should_stop=None):
        """跑一轮任务。ctx: 传给工具的上下文(工作目录等)。
        should_stop: 可选回调 ()->bool, 每轮开始前检查, 为真则中止(供"暂停/停止生成"用)。
        emit: 可选回调 emit(event: dict), 逐事件推送(S2 SSE 用)。event 类型:
          thought(大脑一句话) / tool(要调工具) / tool_result(工具返回) / done / error。
        返回 dict: {answer, rounds, files, transcript}。"""
        # 协议由 config.TOOLS_MODE 决定; function_calling 是根治"空回复卡死"的主路径, json_text 保留走原路。
        if config.TOOLS_MODE == "function_calling":
            return self._run_function_calling(user_request, extra_rules, ctx, emit, verbose, should_stop)
        self._ctx = ctx

        def _emit(ev):
            if emit:
                try:
                    emit(ev)
                except Exception:
                    pass

        # 流式透传: 大脑边生成边推 reasoning/content 给前端(llm_token 事件), 用户不再盯着三点干等。
        def _stok(text, kind):
            _emit({"type": "llm_token", "kind": kind, "text": text})

        system, msgs, transcript = self._prep(user_request, extra_rules, _emit)

        consecutive_err = 0
        for rnd in range(1, self.max_rounds + 1):
            if should_stop and should_stop():
                _emit({"type": "error", "text": "已暂停, 本次生成被中止。可再次发送继续。"})
                return {"stopped": True, "answer": "", "files": [], "rounds": rnd,
                        "transcript": transcript}
            try:
                raw = self.llm.chat(msgs, on_token=_stok)
            except LLMError as e:
                err = str(e)
                # 推理模型间歇性空回复 / 通道耗尽: 一次次追加"别空回复、立刻出动作"提示多蹭几次,
                # 空回复本身是偶发的, 多试能极大提高成功概率; 全部落空才中止(而不是判死成"服务端异常")。
                if "空内容" in err or "请求失败" in err:
                    last_err = e
                    recovered = False
                    for _ in range(3):
                        _emit({"type": "thought", "text": "(大脑一时没反应, 正在再试…)"})
                        msgs.append({"role": "user",
                                     "content": "你上一条回复是空/无法解析。请立即只输出本回合要求的合法 JSON 动作(见系统提示), 不要输出任何其他文字。"})
                        try:
                            raw = self.llm.chat(msgs, on_token=_stok)
                            recovered = True
                            break
                        except LLMError as e2:
                            last_err = e2
                    if not recovered:
                        _emit({"type": "error",
                               "text": "大脑多次没能产出可执行动作, 本次生成中止。请稍后重发。"})
                        return {"error": f"大脑请求失败: {last_err}", "rounds": rnd,
                                "transcript": transcript}
                else:
                    raise
            transcript.append({"role": "assistant", "content": raw})
            if verbose:
                print(f"\n----- 轮 {rnd}: 大脑 -----\n{raw[:1500]}")

            try:
                act = extract_json(raw)
            except LLMError as e:
                consecutive_err += 1
                _emit({"type": "thought", "text": "(输出无法解析, 正在纠正…)"})
                if consecutive_err > ERROR_LIMIT:
                    _emit({"type": "error", "text": f"大脑多次输出无法解析: {e}"})
                    return {"error": f"大脑多次输出无法解析: {e}", "rounds": rnd,
                            "transcript": transcript}
                msgs.append({"role": "user",
                             "content": "你的上一条输出不是合法 JSON。请只输出本回合要求的 JSON(见系统提示), 不要任何多余文字。"})
                continue

            reason = act.get("reason", "")

            # 交付
            if act.get("done"):
                _emit({"type": "thought", "text": reason or "完成, 交付。"})
                _emit({"type": "done", "answer": act.get("answer", ""),
                       "files": act.get("files", []), "rounds": rnd})
                # 完成即自动沉淀一条记忆(供之后"它记得上次做了什么")
                # 只记真正产出模型文件的: 打招呼/闲聊这类空产物不记, 免得把废话攒成"相似经验"
                if self.memory:
                    try:
                        _files = act.get("files") or []
                        if _files:
                            self.memory.remember(
                                f"任务完成: {user_request} | {act.get('answer', '')} | 产物: {', '.join(map(str, _files))}",
                                source="auto")
                    except Exception:
                        pass
                return {"answer": act.get("answer", ""),
                        "files": act.get("files", []),
                        "rounds": rnd,
                        "transcript": transcript}

            # 工具调用
            name, args = act.get("tool"), act.get("args")
            if name in self.registry.names():
                consecutive_err = 0
                _emit({"type": "thought", "text": reason or f"调用 {name}"})
                _emit({"type": "tool", "name": name, "args": (args or {}),
                       "raw": raw})
                try:
                    result = self.registry._tools[name]["run"](args or {}, self._ctx)
                except Exception as e:  # 工具自身出错 -> 也回填给大脑修
                    result = f"[工具 {name} 执行异常] {type(e).__name__}: {e}"
                _emit({"type": "tool_result", "name": name, "text": result})
                msgs.append({"role": "assistant", "content": raw})
                msgs.append({"role": "user", "content": f"[工具 {name} 返回]\n{result}"})
                transcript.append(msgs[-1])
                if verbose:
                    print(f"----- 工具 {name} 返回 -----\n{str(result)[:800]}")
            else:
                consecutive_err += 1
                _emit({"type": "thought", "text": f"(想调用未知工具 {name}, 已纠正)"})
                if consecutive_err > ERROR_LIMIT:
                    _emit({"type": "error", "text": f"大脑连续调用未知工具 '{name}'"})
                    return {"error": f"大脑连续调用未知工具 '{name}'", "rounds": rnd,
                            "transcript": transcript}
                msgs.append({"role": "user",
                             "content": f"工具 '{name}' 不存在。可用工具: {', '.join(self.registry.names())}。请重新输出合法动作。"})

        _emit({"type": "error", "text": f"超过最大轮数 {self.max_rounds} 仍未交付"})
        return {"error": f"超过最大轮数 {self.max_rounds} 仍未交付", "rounds": self.max_rounds,
                "transcript": transcript}

    def _prep(self, user_request, extra_rules, emit):
        """公共前置: 记忆前奏 + 组装 system(按协议选 format) + 初始化消息与 transcript。
        返回 (system, msgs, transcript)。"""
        prelude = ""
        if self.memory:
            try:
                hits = self.memory.recall(user_request, limit=5)
                if hits:
                    prelude = ("## 你过往解决过类似任务的记忆(仅作参考: 相关的用, 不相关的忽略)\n"
                               + "\n".join(f"- {h}" for h in hits))
                    # 让 UI 看到"它记得": 只推给界面, 不进大脑上下文
                    emit({"type": "thought",
                          "text": "📚 想起之前做过类似的: " + hits[0][:140]})
            except Exception:
                pass
        system = build_system_prompt(self.registry, extra_rules, prelude,
                                     mode=config.TOOLS_MODE)
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user_request}]
        transcript = [msgs[0], msgs[1]]
        return system, msgs, transcript

    def _workspace_files(self):
        """枚举工作目录里真实落盘的交付物(.blend/.glb/.stl + *_preview.png)。
        用于 function_calling 交付时自动收集产物, 不依赖模型诚实自报文件名。"""
        wd = getattr(self._ctx, "workdir", None) if self._ctx else None
        if not wd:
            return []
        p = Path(wd).resolve()
        if not p.exists():
            return []
        files = []
        for ext in ("*.blend", "*.glb", "*.stl"):
            files.extend(sorted(f.name for f in p.glob(ext)))
        files.extend(sorted(f.name for f in p.glob("*_preview.png")))
        return files

    def _run_function_calling(self, user_request, extra_rules, ctx, emit, verbose, should_stop):
        """原生 function-calling 主循环(根治空回复卡死):
        - 模型回调产生 tool_calls -> 逐个分发真实工具, 结果以 role=tool 回填。
        - 模型回调不带 tool_calls -> 视为交付: 自动收集工作目录真实产物, 不依赖模型自报文件名。
        依赖 config.TOOLS_MODE == 'function_calling'。"""
        self._ctx = ctx

        def _emit(ev):
            if emit:
                try:
                    emit(ev)
                except Exception:
                    pass

        def _stok(text, kind):
            _emit({"type": "llm_token", "kind": kind, "text": text})

        system, msgs, transcript = self._prep(user_request, extra_rules, _emit)
        fc_tools = self.registry.schema()
        consecutive_err = 0
        any_tool_ran = False
        for rnd in range(1, self.max_rounds + 1):
            if should_stop and should_stop():
                _emit({"type": "error", "text": "已暂停, 本次生成被中止。可再次发送继续。"})
                return {"stopped": True, "answer": "", "files": [], "rounds": rnd,
                        "transcript": transcript}
            try:
                reply = self.llm.chat(msgs, on_token=_stok, tools=fc_tools)
            except LLMError as e:
                err = str(e)
                if "空内容" not in err and "请求失败" not in err:
                    raise
                last_err = e
                recovered = False
                for _ in range(3):
                    _emit({"type": "thought", "text": "(大脑一时没反应, 正在再试…)"})
                    msgs.append({"role": "user",
                                 "content": "你上一条回复是空/无法解析。请立刻调用一个需要的工具推进; 若确已全部完成就用一句中文交付。"})
                    try:
                        reply = self.llm.chat(msgs, on_token=_stok, tools=fc_tools)
                        recovered = True
                        break
                    except LLMError as e2:
                        last_err = e2
                if not recovered:
                    _emit({"type": "error",
                           "text": "大脑多次没能产出可执行动作, 本次生成中止。请稍后重发。"})
                    return {"error": f"大脑请求失败: {last_err}", "rounds": rnd,
                            "transcript": transcript}
            content = reply.get("content")
            tool_calls = reply.get("tool_calls") or []
            # 归一化成 OpenAI 原生的 assistant.tool_calls(让下一轮消息格式合法)
            oai_tcs = [{"id": tc.get("id") or f"call_{rnd}_{i}", "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments": json.dumps(tc.get("args") or {}, ensure_ascii=False)}}
                       for i, tc in enumerate(tool_calls)]
            if tool_calls:
                consecutive_err = 0
                transcript.append({"role": "assistant", "content": content or "", "tool_calls": oai_tcs})
                msgs.append({"role": "assistant", "content": content or "", "tool_calls": oai_tcs})
                for i, tc in enumerate(tool_calls):
                    name = tc.get("name")
                    args = tc.get("args") or {}
                    tid = oai_tcs[i]["id"]
                    if name in self.registry.names():
                        any_tool_ran = True
                        _emit({"type": "thought", "text": f"调用 {name}"})
                        _emit({"type": "tool", "name": name, "args": args, "raw": ""})
                        try:
                            result = self.registry._tools[name]["run"](args, self._ctx)
                        except Exception as e:
                            result = f"[工具 {name} 执行异常] {type(e).__name__}: {e}"
                        _emit({"type": "tool_result", "name": name, "text": result})
                        msgs.append({"role": "tool", "tool_call_id": tid, "content": result})
                        transcript.append({"role": "tool", "tool_call_id": tid, "content": result})
                        if verbose:
                            print(f"----- 工具 {name} 返回 -----\n{str(result)[:800]}")
                    else:
                        consecutive_err += 1
                        _emit({"type": "thought", "text": f"(想调用未知工具 {name}, 已纠正)"})
                        if consecutive_err > ERROR_LIMIT:
                            _emit({"type": "error", "text": f"大脑连续调用未知工具 '{name}'"})
                            return {"error": f"大脑连续调用未知工具 '{name}'", "rounds": rnd,
                                    "transcript": transcript}
                        bad = f"工具 '{name}' 不存在。可用工具: {', '.join(self.registry.names())}"
                        msgs.append({"role": "tool", "tool_call_id": tid, "content": bad})
                        transcript.append({"role": "tool", "tool_call_id": tid, "content": bad})
                continue

            # 没有 tool_calls -> 视为交付(或退化空转)
            files = self._workspace_files()
            deliverable = bool(files) or (not any_tool_ran and str(content or "").strip())
            if not deliverable:
                consecutive_err += 1
                if consecutive_err > ERROR_LIMIT:
                    _emit({"type": "error", "text": "大脑连续空转(既没调工具、也没产出)。"})
                    return {"error": "大脑连续空转", "rounds": rnd, "transcript": transcript}
                _emit({"type": "thought", "text": "(这轮没动作, 已纠正)"})
                msgs.append({"role": "user",
                             "content": "你这条回复没有任何动作: 既没调用工具, 也没有实际产出或描述的成果。请调用一个工具推进(写脚本并执行 / 渲染 / 导出), 或简短中文交付。"})
                continue
            _emit({"type": "thought", "text": "完成, 交付。"})
            _emit({"type": "done", "answer": content or "", "files": files, "rounds": rnd})
            if self.memory and files:
                try:
                    self.memory.remember(
                        f"任务完成: {user_request} | {content} | 产物: {', '.join(files)}",
                        source="auto")
                except Exception:
                    pass
            return {"answer": content or "", "files": files, "rounds": rnd,
                    "transcript": transcript}

        _emit({"type": "error", "text": f"超过最大轮数 {self.max_rounds} 仍未交付"})
        return {"error": f"超过最大轮数 {self.max_rounds} 仍未交付", "rounds": self.max_rounds,
                "transcript": transcript}
