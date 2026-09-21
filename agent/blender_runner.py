# -*- coding: utf-8 -*-
"""Blender 无头子进程执行器:宿主侧收 bpy 代码字符串 -> 落临时 .py -> blender -b -P 跑。
Blender 侧不 import 本模块(它没有 bpy); 宿主侧也没有 bpy, 只有 python。"""
import subprocess
import time
from pathlib import Path

from . import config


def run_blender_code(code: str, workdir, tag: str = "gen") -> dict:
    """返回 {returncode, ok, log_tail}。ok=False 时 log_tail 带错误线索。"""
    workdir = Path(workdir).resolve()  # 一律绝对路径, 避免 Blender cwd 拼出双重路径
    workdir.mkdir(parents=True, exist_ok=True)
    py = workdir / f"_{tag}_{int(time.time() * 1000)}.py"
    py.write_text(code, encoding="utf-8")
    try:
        cp = subprocess.run(
            [config.BLENDER_EXE, "-b", "-P", str(py)],
            cwd=str(workdir), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=config.BLENDER_TIMEOUT)
        tail = (cp.stdout or "") + "\n[STDERR]\n" + (cp.stderr or "")
        return {"returncode": cp.returncode, "ok": cp.returncode == 0,
                "log_tail": tail[-4000:]}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "ok": False,
                "log_tail": f"[Blender 超时 >{config.BLENDER_TIMEOUT}s]"}


def list_files(workdir) -> str:
    """工作目录现有文件清单(给大脑确认产物落盘用)。"""
    workdir = Path(workdir).resolve()
    if not workdir.exists():
        return "(工作目录为空)"
    items = []
    for p in sorted(workdir.iterdir()):
        if p.is_file() and not p.name.startswith("_"):  # 过滤我们的临时脚本
            items.append(f"- {p.name} ({p.stat().st_size} B)")
    return "\n".join(items) if items else "(工作目录暂无文件)"
