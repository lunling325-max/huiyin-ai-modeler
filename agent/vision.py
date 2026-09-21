# -*- coding: utf-8 -*-
"""视觉看图(DeepSeek 多模态 deepseek-v4-flash-vision-exp)。图闭环的"眼睛"。

- describe_image(path, ask): 打开一张本地图 -> 限制尺寸 -> base64 直传 ->
  让模型用中文回答 ask(默认面向 3D 建模还原的详细描述)。
- 参考图描述 和 渲染图对照自审 都用它; 单次调用是独立 HTTP, 不走记忆/会话。
"""
import base64
import io
import json
import urllib.request

from PIL import Image

from . import config

_DEFAULT_ASK = ("请仔细看这张图, 用中文详细描述, 目的是在 Blender 里做 3D 建模尽量还原。请说明: "
                "1) 画面里是什么物体/角色; "
                "2) 整体几何结构与比例(主体、头/身/肢体等部件, 大致形状是球/椭圆体/锥/柱/方块, 相互位置关系); "
                "3) 各部件相对大小与朝向; "
                "4) 颜色与材质(每个部件大致色名或 RGB, 亮面/哑光等); "
                "5) 其它建模能参考的细节(对称性、特征点、别忽略的小部件)。尽量具体, 分条回答。")


def _read_b64(path, max_px=1024):
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_px, max_px))  # 限制体积避免超限
    buf = io.BytesIO()
    im.save(buf, "PNG")
    im.close()
    return base64.b64encode(buf.getvalue()).decode()


def describe_image(path, ask=None, timeout=120):
    """看图 -> 返回中文文本描述。抛异常由调用方兜底。"""
    b64 = _read_b64(path)
    question = ask or _DEFAULT_ASK
    payload = {
        "model": config.VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
    }
    req = urllib.request.Request(
        f"{config.API_BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.API_KEY}"})
    # 网络失败自动交替 直连<->代理 重试(与 llm_client 同策略); ProxyHandler({})=直连
    last = None
    for attempt in range(config.LLM_RETRIES):
        if config.BRAIN_PROXY:
            cfgs = [{"http": config.BRAIN_PROXY, "https": config.BRAIN_PROXY}, {}]
        else:
            cfgs = [{}, {"http": config.BRAIN_FALLBACK_PROXY, "https": config.BRAIN_FALLBACK_PROXY}]
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(cfgs[attempt % 2]))
            with opener.open(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
        except Exception as e:
            last = e  # 超时/断连/HTTP 错误 -> 换通道再试
    raise RuntimeError(f"看图请求失败(直连/代理共尝试{config.LLM_RETRIES}次): {last}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("用法: python -m agent.vision <图片路径> [问题]")
    p = sys.argv[1]
    a = sys.argv[2] if len(sys.argv) > 2 else None
    print(describe_image(p, ask=a)[:2000])
