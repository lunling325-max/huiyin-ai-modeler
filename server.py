# -*- coding: utf-8 -*-
"""参赛演示本地服务(纯标准库, 零依赖):
  GET  /               -> 前端页面 webdemo/index.html
  GET  /<file>         -> webdemo 下静态资源
  GET  /o/<...>        -> 产物区 outputs/ 下文件(预览图/下载用)
  POST /api/health     -> {"ok": true}
  POST /api/run        -> SSE 流: 收 {"message":..., "session_id":?, "image":?}
                            image = {name?, data: <base64>} 存为工作目录 ref.png
                            逐事件推送 thought/tool/tool_result/done/error + end
跨会话记忆: 每会话共享 agent/memory.db(SQLite FTS5), 大脑自动 recall/沉淀。
运行: python server.py [port]  (默认 8901)"""
import base64
import json
import mimetypes
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote, quote

ROOT = Path(__file__).resolve().parent
DOCROOT = ROOT / "webdemo"
OUTROOT = ROOT / "outputs"

_lock = threading.Lock()
_sessions = {}  # sid -> {"workdir": Path, "agent": Agent}

# 首次启动预填的三个默认大脑(走 DeepSeek 官方端点, 用 config 默认 key)。
# 之后与"添加模型"一样都存进 agent/custom_models.json, 可自由增删, 不再写死。
DEFAULT_PRESETS = [
    {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash · 快速", "model": "deepseek-v4-flash", "preset": True},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro · 更深度", "model": "deepseek-v4-pro", "preset": True},
    {"id": "agnes-2.0-flash", "label": "Agnes 2.0 Flash · 轻量", "model": "agnes-2.0-flash", "preset": True},
]
DEFAULT_MODEL = "deepseek-v4-flash"

# 用户自定义模型: 可接任意 OpenAI 兼容端点(base_url + api_key + model)。
# 持久化到 agent/custom_models.json, 服务器重启仍保留。
CUSTOM_MODELS_FILE = ROOT / "agent" / "custom_models.json"


def _load_custom_models():
    """读自动模型池; 首次(文件不存在/非法)预填默认预置并落盘, 之后以文件为准(可增删)。"""
    try:
        data = json.loads(CUSTOM_MODELS_FILE.read_text("utf-8"))
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict) and (m.get("model") or m.get("id"))]
    except (OSError, ValueError):
        pass
    # 首次: 落盘三个默认预置, 之后增删都以这份文件为准(不会自动复活已删项)
    presets = [dict(p) for p in DEFAULT_PRESETS]
    _save_custom_models(presets)
    return presets


def _save_custom_models(models):
    CUSTOM_MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_MODELS_FILE.write_text(json.dumps(models, ensure_ascii=False, indent=2), "utf-8")


def all_models():
    """菜单数据源 = 自动模型池(默认预置 + 用户添加的自定义), 均可增删。"""
    return _load_custom_models()


def _resolve_model(mid):
    """按 id 找模型定义; 返回 (base_url, api_key, model_name), 找不到返回 None。"""
    for m in all_models():
        if m["id"] == mid:
            return (m.get("base_url") or None, m.get("api_key") or None, m.get("model", mid))
    return None


def _new_agent(workdir, model=None):
    from types import SimpleNamespace
    from agent.llm_client import LLMClient
    from agent.tools import make_modeling_registry, modeling_rules, image_mode_rules
    from agent.memory import Memory
    try:
        mem = Memory()  # 项目独立记忆库 agent/memory.db(SQLite FTS5)
    except Exception as e:
        # 记忆层本来就是可选的(loop.py 每处调用都判 if self.memory), 这里补上降级:
        # 记忆库建不起来时(目录不可写等), 无记忆照常建模, 不至于整个程序起不来。
        mem = None
        print(f"[server] 记忆层不可用, 已降级为无记忆运行: {type(e).__name__}: {e}", flush=True)
    ctx = SimpleNamespace(workdir=workdir, memory=mem)
    from agent.loop import Agent
    llm = None
    if model:
        resolved = _resolve_model(model)
        if resolved:
            base_url, api_key, model_name = resolved  # 预置用默认 base/key; 自定义用其自带
            llm = LLMClient(base_url=base_url, api_key=api_key, model=model_name)
    agent = Agent(llm=llm, registry=make_modeling_registry(ctx), memory=mem)
    print(f"[server] 新会话大脑 model={model if model else '(默认)'}", flush=True)
    return agent, modeling_rules(workdir), ctx


def get_session(sid, model=None):
    if sid and sid in _sessions:
        return _sessions[sid]
    sid = (sid or uuid.uuid4().hex)[:24]
    workdir = OUTROOT / sid
    workdir.mkdir(parents=True, exist_ok=True)
    agent, rules, ctx = _new_agent(workdir, model)
    _sessions[sid] = {"workdir": workdir, "agent": agent, "rules": rules, "ctx": ctx,
                      "cancel": threading.Event()}  # 暂停/停止生成时的信号
    return _sessions[sid]


def artifacts_of(workdir):
    out = []
    for p in sorted(Path(workdir).iterdir()):
        if p.is_file() and not p.name.startswith("_") and p.name != "ref.png":
            rel = str(p.relative_to(OUTROOT)).replace("\\", "/")
            kind = "png" if p.suffix.lower() == ".png" else ("model" if p.suffix.lower() in (".blend", ".glb", ".stl") else "other")
            out.append({"name": p.name, "url": "/o/" + quote(rel), "size": p.stat().st_size, "kind": kind})
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # 静默访问日志

    # ---------- 基础 ----------
    def _send_bytes(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path: Path, ctype: str = None):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self._send_bytes(data, ctype or (mimetypes.guess_type(path.name)[0] or "application/octet-stream"))

    def _send_json(self, obj):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _route(self, path: str):
        """返回 (abs_path 或 None)"""
        if path in ("", "/"):
            return DOCROOT / "index.html"
        if path.startswith("/o/"):
            rel = path[len("/o/"):]
            return (OUTROOT / rel).resolve()
        rel = path.lstrip("/")
        target = (DOCROOT / rel).resolve()
        return target if target.is_file() else None

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        if path == "/api/models":
            self._send_json({"models": all_models(), "default": DEFAULT_MODEL})
            return
        target = self._route(path)
        if target is None or not target.is_file():
            self.send_error(404, "not found")
            return
        # 路径合法性: 产物区/前端区都必须在对应根下
        self._send_file(target)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        if path == "/api/run":
            self._handle_run()
            return
        if path == "/api/stop":
            self._handle_stop()
            return
        if path == "/api/models":
            self._add_model()
            return
        self.send_error(404, "not found")

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/models/"):
            self._del_model(path[len("/api/models/"):])
            return
        self.send_error(404, "not found")

    # ---------- 自定义模型: 添加 / 删除 ----------
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _add_model(self):
        data = self._read_body() or {}
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        model = str(data.get("model") or "").strip()
        api_key = str(data.get("api_key") or "").strip()
        label = str(data.get("label") or "").strip() or model
        if not model:
            self._send_json({"error": "请填写模型名"})
            return
        models = _load_custom_models()
        # 重复 base+model 则更新而非新增(不出 base_url 的按默认端点匹配)
        for m in models:
            if m.get("model") == model and (m.get("base_url") or None) == (base_url or None):
                m.update({"label": label, "api_key": api_key or m.get("api_key", "")})
                _save_custom_models(models)
                self._send_json({"ok": True, "models": all_models()})
                return
        mid = "custom-" + uuid.uuid4().hex[:8]
        models.append({"id": mid, "label": label, "base_url": base_url,
                       "api_key": api_key, "model": model})
        _save_custom_models(models)
        self._send_json({"ok": True, "id": mid, "models": all_models()})

    def _del_model(self, mid):
        mid = unquote(mid)
        models = _load_custom_models()
        kept = [m for m in models if m["id"] != mid]
        if len(kept) == len(models):
            self._send_json({"error": "该模型不可删除或不存在"})
            return
        _save_custom_models(kept)
        self._send_json({"ok": True, "models": all_models()})

    # ---------- SSE: /api/run ----------
    def _handle_run(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json({"error": f"bad request: {e}"})
            return
        message = str(data.get("message", "")).strip()
        if not message:
            self._send_json({"error": "message 为空"})
            return
        sid = str(data.get("session_id") or "")
        model = str(data.get("model") or "") or None
        if model and _resolve_model(model) is None:
            self._send_json({"error": f"未知模型: {model}"})
            return
        with _lock:
            session = get_session(sid, model)
            sid = next(k for k, v in _sessions.items() if v is session)

        # 参考图(可选): 解码成工作目录 ref.png, 并给大脑追加图模式纪律
        rules = session["rules"]
        img = data.get("image")
        if isinstance(img, dict) and img.get("data"):
            d = str(img["data"])
            if d.startswith("data:") and "," in d:
                d = d.split(",", 1)[1]
            try:
                (session["workdir"] / "ref.png").write_bytes(base64.b64decode(d))
                rules += image_mode_rules()
            except Exception as e:
                print("[server] 参考图保存失败:", e, flush=True)

        # --- 开流 ---
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.flush()
            self.wfile = self.connection.makefile("wb", 0)  # 无缓冲, 保证实时
        except Exception:
            pass

        def send(ev: dict):
            try:
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise

        try:
            send({"type": "meta", "session_id": sid,
                  "workdir": str(session["workdir"].relative_to(OUTROOT)).replace("\\", "/")})
            out = session["agent"].run(message, extra_rules=rules,
                                       ctx=session["ctx"], emit=send,
                                       should_stop=session["cancel"].is_set)
            send({"type": "end", "session_id": sid,
                  "artifacts": artifacts_of(session["workdir"]),
                  "answer": out.get("answer", ""),
                  "error": out.get("error")})
        except Exception as e:
            try:
                send({"type": "error", "text": f"服务端异常: {type(e).__name__}: {e}"})
                send({"type": "end", "session_id": sid, "artifacts": [],
                      "answer": "", "error": str(e)})
            except Exception:
                pass
        # 注意: 不要在这里手动 close 替换后的 self.wfile。
        # 我们已发 Connection: close, http.server 在请求结束后会自己 finish()
        # (flush + close 同一个 wfile); 手动先 close 会让框架随后 flush 已关文件
        # 抛 "ValueError: I/O operation on closed file", 每次 SSE run 都刷一条假崩溃。


    def _handle_stop(self):
        """暂停/停止生成: 置该会话 cancel 事件, 主循环下一轮检测到即中止。"""
        data = self._read_body() or {}
        sid = str(data.get("session_id") or "")
        if sid and sid in _sessions:
            _sessions[sid]["cancel"].set()
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "未找到该会话"})

def main(port: int = 8901):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"[server] http://127.0.0.1:{port}  DOCROOT={DOCROOT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8901)
