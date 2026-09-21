# -*- coding: utf-8 -*-
"""领域工具面:建模工具 + 给大脑的建模纪律(注入 sysprompt 的 extra_rules)。

工具(全部在无头 Blender 子进程里干活, 每步独立进程):
- blender_exec   : 跑一段"自包含" bpy 脚本(建模型并存 .blend)。
- render_preview : 打开某 .blend -> 自动加相机/灯框住模型 -> EEVEE 渲染 PNG(自动翻转修正)。
- export_glb_stl : 打开某 .blend -> 导出 .glb 和/或自写毫米 .stl。
- memory_remember: 把可复用经验存一句到跨会话记忆。
- memory_recall  : 按关键词召回相关历史记忆。
- see_image      : 用视觉模型看图(参考图/渲染图), 返回中文描述(图闭环用)。

文件都在本回合工作目录(ctx.workdir)。大脑在各轮之间靠"文件名"引用产物。
"""
from pathlib import Path

from PIL import Image

from .blender_runner import run_blender_code, list_files
from .bpy_templates import render_preview_code, export_glb_stl_code


def _find_blend(ctx):
    blends = sorted(Path(ctx.workdir).resolve().glob("*.blend")) if ctx else []
    return blends[0] if blends else None


def blender_exec(args, ctx):
    """执行一段自包含 bpy 脚本, 返回日志 + 工作目录文件清单。"""
    code = (args or {}).get("code", "")
    if not code.strip():
        return "[错误] code 为空。请给出完整 bpy 脚本。"
    if ctx is None:
        return "[错误] 缺少 ctx.workdir。"
    r = run_blender_code(code, ctx.workdir, tag="gen")
    head = "OK" if r["ok"] else f"FAILED rc={r['returncode']}"
    return f"[{head}]\n{r['log_tail']}\n\n[工作目录现有文件]\n{list_files(ctx.workdir)}"


def render_preview(args, ctx):
    """对工作目录里某个 .blend 渲染 PNG(自动翻转), 返回日志 + 文件清单。"""
    args = args or {}
    wd = Path(ctx.workdir).resolve()
    name = args.get("file")
    blend = (wd / name) if name else _find_blend(ctx)
    if blend is None or not blend.exists():
        return f"[错误] 找不到要渲染的 .blend(给了 '{name}')。工作目录: {wd}"
    out_name = args.get("out") or (blend.stem + "_preview.png")
    out_png = wd / out_name
    view = args.get("view", "iso")
    res = args.get("res", [720, 540])
    code = render_preview_code(blend, out_png, view=view, res=res)
    r = run_blender_code(code, wd, tag="render")
    if not r["ok"]:
        return f"[FAILED rc={r['returncode']}]\n{r['log_tail']}"
    if out_png.exists():
        try:
            # 2026-09-08 实测: 无头 Blender(bpy.ops.render.render(write_still=True)) 原生就是正立的,
            # 之前"上下颠倒需 FLIP_TOP_BOTTOM"是误判 —— 无谓翻转会把已成图翻成上下颠倒。
            size = out_png.stat().st_size
        except Exception as e:
            return f"[渲染完成但统计失败] {e}\n{r['log_tail']}"
        return (f"[渲染完成] {out_name} {size} B view={view}\n"
                f"[工作目录现有文件]\n{list_files(wd)}")
    return f"[渲染未产出文件] 可能渲染报错。\n{r['log_tail']}\n{list_files(wd)}"


def export_glb_stl(args, ctx):
    """导出 .glb / .stl(毫米) 到工作目录, 返回日志 + 文件清单。"""
    args = args or {}
    wd = Path(ctx.workdir).resolve()
    name = args.get("file")
    blend = (wd / name) if name else _find_blend(ctx)
    if blend is None or not blend.exists():
        return f"[错误] 找不到要导出的 .blend(给了 '{name}')。工作目录: {wd}"
    base = (args.get("base") or blend.stem)
    formats = args.get("formats") or ["glb", "stl"]
    glb_out = (wd / (base + ".glb")) if "glb" in formats else None
    stl_out = (wd / (base + ".stl")) if "stl" in formats else None
    if glb_out is None and stl_out is None:
        return "[错误] formats 里没有 glb/stl 任一。"
    code = export_glb_stl_code(blend, glb_out, stl_out)
    r = run_blender_code(code, wd, tag="export")
    made = []
    for p in (glb_out, stl_out):
        if p is not None and p.exists():
            made.append(f"- {p.name} ({p.stat().st_size} B)")
    if not r["ok"]:
        return f"[FAILED rc={r['returncode']}]\n{r['log_tail']}"
    if not made:
        return f"[导出未产出文件]\n{r['log_tail']}\n{list_files(wd)}"
    return "[导出完成]\n" + "\n".join(made) + f"\n\n[工作目录现有文件]\n{list_files(wd)}"


def memory_remember(args, ctx):
    """存一条跨会话记忆(经验/结论)。"""
    text = ((args or {}).get("text") or "").strip()
    mem = getattr(ctx, "memory", None) if ctx else None
    if mem is None:
        return "[记忆不可用: 本会话未开启记忆库]"
    if not text:
        return "[错误] text 为空。想存什么?一句经验/结论即可。"
    r = mem.remember(text, source="agent")
    if r is not True:
        return str(r)
    return f"[已记住] {text[:140]}"


def memory_recall(args, ctx):
    """按关键词召回相关历史记忆。"""
    q = ((args or {}).get("query") or "").strip()
    mem = getattr(ctx, "memory", None) if ctx else None
    if mem is None:
        return "[记忆不可用: 本会话未开启记忆库]"
    if not q:
        return "[错误] query 为空。想查什么?给 2~5 个关键词即可。"
    hits = mem.recall(q, limit=6)
    if not hits:
        return "[无相关记忆]"
    return "[相关记忆]\n" + "\n".join(f"- {h}" for h in hits)


def see_image(args, ctx):
    """视觉看图(DeepSeek 多模态): 参考图/渲染图 -> 中文描述/对照结论。"""
    from . import vision  # 延迟导入, 避免循环
    args = args or {}
    wd = Path(ctx.workdir).resolve()
    name = (args.get("file") or "").strip()
    png = (wd / name) if name else None
    if png is None or not png.exists():
        # 没给名字时优先工作目录里的 ref.png
        ref = wd / "ref.png"
        png = ref if ref.exists() else None
        if png is None:
            return f"[错误] 找不到图(给了 '{name}')。工作目录: {wd}"
    ask = (args.get("ask") or "").strip()
    try:
        return vision.describe_image(png, ask=ask or None)
    except Exception as e:
        return f"[看图失败] {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 给大脑的建模纪律/环境说明(注入 system prompt)
# ---------------------------------------------------------------------------
_MODEL_RULES = """## 3D 建模环境(你必须遵守)
- 你在驱动无头 Blender 5.1。没有窗口; 每次工具调用都是**全新进程**(上一步的物体/变量不保留)。
- 本回合工作目录(绝对路径, 所有文件都放这里):
__WORK__
- 文件在轮与轮之间靠**文件名**流转: 你在 blender_exec 里存成 `chair.blend`, 之后用 `chair.blend` 这个名字渲染/导出。

## 可用工具(契约)
1. `blender_exec`: args {code: <完整自包含 bpy 脚本>, note: <这步在做什么>}
   脚本自己清场、自己建全部物体、自己存 .blend 到工作目录。返回执行日志 + 目录文件清单。
   典型骨架(照抄, 在"建模代码"段发挥, 结尾存盘):
```python
import bpy, os
bpy.ops.wm.read_factory_settings(use_empty=True)
WORK = r"__WORK__"
def _srgb(c):
    # 若有人误传 0-255 的值, 自动转回 0-1(否则会过曝成白色)
    def f(x):
        if x > 1.0:
            x = x / 255.0
        return x/12.92 if x <= 0.04045 else ((x+0.055)/1.055)**2.4
    return (f(c[0]), f(c[1]), f(c[2]), 1.0)
def mat(name, color, rough=0.8, met=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    p = m.node_tree.nodes.get("Principled BSDF")
    if p:
        p.inputs["Base Color"].default_value = _srgb(color)
        p.inputs["Roughness"].default_value = rough
        p.inputs["Metallic"].default_value = met
    return m
# ---------- 你的建模代码(建全部物体, 上材质) ----------

# ---------- 结束: 存盘 + 材质预览 + 打标 ----------
for scr in bpy.data.screens:
    for area in scr.areas:
        if area.type == 'VIEW_3D':
            for sp in area.spaces:
                if sp.type == 'VIEW_3D':
                    sp.shading.type = 'MATERIAL'
out = os.path.join(WORK, "model.blend")   # ← 文件名你自定(如 duck.blend), 之后要用它渲染/导出
bpy.ops.wm.save_as_mainfile(filepath=out)
print("DONE", out, flush=True)
```
2. `render_preview`: args {file: "<刚才存的 .blend 名>", out?: "<png 名>", view?: iso/front/side/top}
   自动加相机和灯光框住整个模型, EEVEE 渲染 PNG(已修正上下颠倒), 放进工作目录。用它"看图"自我检查比例/外观。
3. `export_glb_stl`: args {file: "<.blend 名>", base?: "<输出前缀>", formats?: ["glb","stl"]}
   默认同前缀导出 .glb + .stl(毫米, 3D 打印友好)。
4. `see_image`: args {file: "<工作目录里某张 png>", ask?: "<要它回答什么/对照什么>"}
   用视觉模型看图, 返回中文分析。**参考图**(上传到工作目录的 ref.png)和**你的渲染图**都靠它"看见"。
5. `memory_remember`: args {text} 把一条可复用经验/结论存进跨会话记忆。
6. `memory_recall`: args {query} 按关键词召回之前做类似任务的历史记忆。

## 工作纪律(快路径优先, 别空转)
- **需求没给细节就自行拍板(通用原则, 不依赖任何预设清单)**: 用户只说了物体大类、没给型号/尺寸/涂装等细节时, **立刻**按"这类东西最常见的通用样子"自己定一个构型直接建, 不要停在"做哪种"上追问或反复斟酌。所谓"常见样子"用通用常识判断即可, **不列清单、不查表**——任何物体都能凭常识选一个典型形态。第一轮就直接调用 blender_exec, 并注明"我用默认的<这类东西>形态来建"。
- **每回合必须推进到能出文件**: 任何情况下, 一次任务至少要有一个工具调用、最终产出一个模型文件; 禁止连续多轮只在思考里打转却不调用工具。凡是上一轮已经思考过"选哪种", 这一轮就必须落到工具。
- **默认流程(尽量一次到位)**: ① blender_exec 建模并存 .blend → ② render_preview 渲染一张 →
  ③ 简短自评(看渲染图, 两三句话说结构/比例/配色即可) → ④ export_glb_stl 导出 → ⑤ done 交付。
- **自审克制**: 无参考图时自评**最多 1 次**。只有发现**明显问题**(缺件 / 穿模 / 整体不像 / 比例离谱)才改脚本重跑①(每次全新进程, 改整段代码); 改完直接导出, 不要反复"自审到满意"。结果干净就不要带节奏回去再看。
- **see_image 按需用**: 只当①附了参考图(ref.png), 或 ②你对渲染结果拿不准时才调用视觉模型看图。常见物体(椅/桌/杯/笔等)凭渲染轮廓 + 你的建模意图判断即可, **不必每次都用视觉模型空转**。
- 渲染/导出前确认 `.blend` 真落盘(工具返回会列文件), 先认文件名再引用。
- **参考图模式**(有 ref.png): 才走细致路径 —— 先 see_image(ref.png) 理解结构/比例/配色, 渲染后用 see_image 对照自审, 那条路径允许至多 2 轮(见额外图模式纪律)。
- Blender 5 易错速查: UV 球 = `primitive_uv_sphere_add(segments=…, ring_count=…)`; 二十面体 =
  `primitive_ico_sphere_add(subdivisions=…)`; 细分修改器 = `obj.modifiers.new(..., type='SUBSURF')`
  (类型名不是 SUBSURFACE/SUBSUBSURF); 建球体后设 scale/rotate 记得切回 OBJECT 模式。名字/参数一次写对。
- 颜色一律写 0.0~1.0(如黄 `(0.95, 0.8, 0.15)`); 参考图视觉给的是 0-255 就**除以 255** 再用。
  传 0-255(如 (255,210,50))会让材质过曝成白色 —— 这是"渲染出来白花花"最常见的原因。
- 用球体堆角色时, 小部件(嘴/眼/肚皮/脚)务必放在主体**外表面外侧并明显外凸**, 埋进主体 = 看不见。
- 只渲染/导出已经真实落盘的 .blend; 工具返回里会列出文件, 先确认文件名再引用。
- 模型要"可打印/可编辑"意识: 物体别互相穿插太离谱即可, 不必合成单网格。
- 值得复用的经验(某类物体的高效建法/配色/踩坑)交付前用 `memory_remember` 存一句, 下次用得上。
- 用户没指定格式时, 默认交付 .blend + .glb + .stl 三样(这就是"模型文件")。"""


_IMAGE_MODE_RULES = """
## 本次附了参考图(图模式纪律)
用户上传的参考图 = 工作目录里的 ref.png。它只是"目标参照", 不是交付物。
1. 动手前: see_image(file='ref.png') 看清结构/比例/配色, 在脑内压缩成 ≤6 条"目标清单"(主体形状、部件清单、每部件颜色与大致相对大小)。
2. 一次就建"部件齐全"的初版, 别省略小部件。
3. 渲染 → 自审(至多 2 次) → 修完立即做第 4 步。
4. 导出交付: export_glb_stl + done。任何情况下模型文件(.blend/.glb/.stl)必须落盘才算完成。

### 卡通角色(吉祥物/鸭/动物)的立体摆放铁律
"渲染出来像两个光球 / 看不到五官" 的根因几乎总是同一个:**小部件被埋进了主体里面, 外壳把细节全盖住了**。请强制遵守:
- 方向: 竖直朝上 = +Z, 地面在 z≈0; 前脸朝 **+Y**; 左右 = ±X。
- 每个部件都要**从主体外表面明显鼓出去**。统一判定法: 设你放置它的那个主球半径为 R、小件自身最大半径 r, 小件中心到主球球心的距离必须 **> R + r×0.6**, 否则必埋。小件越大、靠得越里越看不见。
- 头常比身大, 脸部件(眼/嘴)要照**头的半径**算, 不是照身。参考数值示例(站姿小鸭/吉祥物, 身半径≈1、头半径≈1.2~1.4、头中心 z≈身中心+0.7): 嘴中心 y ≈ 头半径×1.25(明显凸在头前表面外); 眼睛=小扁球, 眼中心 y ≈ 头半径×1.15 且 z ≈ 头中心; 肚皮在身体**前侧下段**表面外(+Y 明显凸出轮廓); 腿从身体底部向下伸, **脚中心 z≈0.08** 且略朝 +Y(x=±0.5 这种别藏进身子里)—— 所以身体要整体抬到 z≥1.0。从侧面(+X)与正面(+Y)两个方向看, 轮廓都应能看见脚、嘴、肚皮。
- 每次渲染后若某个部件"没看见", 第一反应是"埋了或朝向不对", 把它沿朝外方向外移 0.3~0.6 再渲染一次确认, 而不是凭空整段重写。

### 图模式轮数预算(硬约束, 保证交得出文件)
- 工作流压缩到: see_image(ref) → blender_exec(初版) → render_preview → 自审#1 → blender_exec(只修被埋的坐标) → render_preview → 自审#2 → export_glb_stl → done。
- 自审 ask 按模板写: "这是我 3D 渲染的<目标>, 我做的部件有: <逐条列出 身/头/肚/嘴/眼/脚/…>。请逐条回答每个部件在画面里是否可见、在哪个方位、形状颜色对不对; 哪个看不到, 就说明它疑似埋在主体里, 应朝哪个方向外移多少。" 不要让它只给一句"缺少细节"。
- **最多自审 2 次**, 第 2 次之后无论是否满意都立刻 export + done, 不许再重建。
- 你数着: 工具调用 ≥9 次还没导出时, 立刻把手头 .blend 导出交付。宁可交付"部件齐全但略糙", 也不要空转到超轮数交不出文件。
"""


def image_mode_rules() -> str:
    return _IMAGE_MODE_RULES


def modeling_rules(workdir) -> str:
    return _MODEL_RULES.replace("__WORK__", str(Path(workdir)))


def _params(props, required=None):
    """造一个 OpenAI JSON-schema 参数对象。"""
    return {"type": "object", "properties": props,
            "required": required or list(props)}


def make_modeling_registry(ctx):
    from .loop import ToolRegistry
    reg = ToolRegistry()
    reg.register("blender_exec",
                 "把一段自包含 bpy 脚本交给无头 Blender 执行(建模型并存 .blend); 返回日志和文件清单。args: {code: 完整 bpy 脚本, note: 这步在做什么}",
                 blender_exec,
                 _params({"code": {"type": "string", "description": "完整自包含 bpy 脚本"},
                          "note": {"type": "string", "description": "这一步在做什么(一句话)"}},
                         required=["code"]))
    reg.register("render_preview",
                 "对工作目录里的某个 .blend 渲染 PNG 预览(自动加相机/灯光/翻转), 供你查看模型外观。args: {file: .blend名, out?: png名, view?: iso/front/side/top}",
                 render_preview,
                 _params({"file": {"type": "string", "description": "要渲染的 .blend 文件名"},
                          "out": {"type": "string", "description": "输出 png 文件名(默认 <名字>_preview.png)"},
                          "view": {"type": "string", "enum": ["iso", "front", "side", "top"],
                                   "description": "相机视角(默认 iso)"}}))
    reg.register("export_glb_stl",
                 "把 .blend 导出为 .glb 与/或 .stl(毫米)。args: {file: .blend名, base?: 输出前缀, formats?: [glb, stl]}",
                 export_glb_stl,
                 _params({"file": {"type": "string", "description": "要导出的 .blend 文件名"},
                          "base": {"type": "string", "description": "输出文件名前缀(默认同 .blend 名)"},
                          "formats": {"type": "array", "items": {"type": "string"},
                                      "description": "导出格式列表, 可选 glb/stl(默认都导)"}}))
    reg.register("see_image",
                 "用视觉模型看图并返回中文分析: 看参考图(默认工作目录 ref.png)或看某张渲染图。args: {file?: 图片文件名, ask?: 要它回答/对照的问题}",
                 see_image,
                 _params({"file": {"type": "string", "description": "图片文件名(默认工作目录 ref.png)"},
                          "ask": {"type": "string", "description": "要它回答/对照的问题"}}))
    reg.register("memory_remember",
                 "把一条可复用经验/结论存进跨会话记忆, 以后做同类任务能想起。args: {text: 一句经验}",
                 memory_remember,
                 _params({"text": {"type": "string", "description": "一句可复用经验/结论"}},
                         required=["text"]))
    reg.register("memory_recall",
                 "按关键词召回之前做类似任务的历史记忆。args: {query: 关键词}",
                 memory_recall,
                 _params({"query": {"type": "string", "description": "2~5 个关键词"}},
                         required=["query"]))
    return reg
