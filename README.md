# 回音 —— AI 建模智能体

在对话框里发一句「做一把简洁的木椅，坐着舒服一点」，它会自己写 Blender 脚本、在后台跑一遍、把渲染出来的图拿回来看，觉得不像就改，最后交出 `.blend`、`.glb`、`.stl` 三个文件。都是真几何，能打开、能编辑、丢进切片软件能直接打印。

参赛作品｜队伍「永远的启明星」｜2026 年 iCAN 大学生创新创业大赛 · AI 应用创新挑战赛

先说清楚一个容易误会的地方：它不是「文字生成模型效果图」那一类工具，仓库里也没有预置模板库，找不到任何现成的模型文件。每一次的建模脚本，都是大模型针对你这句话当场写的。

## 需要装什么

Windows 机器一台，Python 3.11，另外还得有 Blender 5.1。Blender 不在 pip 里，要单独装。再准备一个 OpenAI 兼容的模型服务端点和密钥，它靠这个思考。浏览器随便什么都行，Chrome、Edge 都可以。

## 怎么起

```bash
pip install -r requirements.txt

set BRAIN_API_KEY=你的密钥        # CMD
# export BRAIN_API_KEY=你的密钥   # Git Bash；PowerShell 用 $env:BRAIN_API_KEY

python server.py
```

终端会打印一行 `[server] http://127.0.0.1:8901`，浏览器打开这个地址就是界面。不用注册也不用登录，数据全在本机。端口想换就在后面跟个数字，比如 `python server.py 9000`。

Blender 的安装路径写在 `agent/config.py` 的 `BLENDER_EXE`，默认指 `C:\Program Files\Blender Foundation\Blender 5.1\blender.exe`。装在别处就改这一行。

### 密钥放哪

不写进代码。按顺序找两个地方，哪个先找到用哪个。

一是环境变量 `BRAIN_API_KEY`，推荐这么干。

二是在项目里自己建个 `agent/_local_key.py`，内容就一行：

```python
BRAIN_API_KEY = "你的密钥"
```

这个文件名以下划线开头，`.gitignore` 已经把它排除了，不会跟着进仓库。

下面几个环境变量可以调，不调就用默认：

| 变量 | 默认 | 干什么的 |
|---|---|---|
| `BRAIN_API_BASE` | `https://api.deepseek.com` | 任何 OpenAI 兼容端点 |
| `BRAIN_API_KEY` | 没有，得自己给 | 密钥 |
| `BRAIN_MODEL` | `deepseek-v4-flash` | 主大脑，管推理和决策 |
| `BRAIN_VISION_MODEL` | `deepseek-v4-flash-vision-exp` | 看图自审的那个 |

换模型只动环境变量，业务代码一行都不用碰。

## 跑起来是什么样

发完那句话，左边会实时往外滚它的思考过程，工具调用一张张卡片上屏，点开能看到这次调用的理由和返回结果。模型把当前状态渲成图，再把图喂回给自己看，判断轮廓、朝向、比例对不对，有没有缺件。不行就改代码重渲，行了才交付。

预览图下面会出现三个下载链接，右侧栏汇总这次会话产出的全部文件。产物落在 `outputs/<会话 id>/`。

在同一个会话里接着提修改要求，它是在上一次的成果上继续改，不是推倒重来。

## 代码在哪

```
server.py              纯标准库的 HTTP + SSE 桥，每个会话开一个工作目录
agent/
  loop.py              主循环，注册表驱动的 ReAct（思考—调工具—看结果—修正）
  tools.py             工具面，以及注入大脑的建模纪律
  llm_client.py        模型客户端，流式、原生 function-calling、失败重试
  blender_runner.py    无头 Blender 子进程执行器
  bpy_templates.py     渲染和导出用的 bpy 代码模板
  vision.py            看图自审
  memory.py            跨会话记忆，SQLite FTS5 检索
  config.py            配置，密钥从环境变量或 _local_key.py 读
webdemo/               Vue 3 单文件前端，vendor 放在本地，没有构建工具链
outputs/               运行时产物（.blend/.glb/.stl 和预览图），不进仓库
```

## 可能踩到的坑

**改了代码不生效，或者端口起不来。** Windows 的端口复用会让旧进程不被自动回收，可能同时有好几个 `server.py` 活着。先把遗留进程找出来杀干净，再重启。

**渲染出来一片黑。** 多半是缺光，或者世界背景没设。布光写在每次运行时生成的临时脚本里，展开工具卡能看到。

**记忆没生效。** 记忆库就是 `agent/` 目录下一个 SQLite 文件，走 FTS5 全文检索。这个文件建不起来（比如目录不可写）的时候，程序会降级成无记忆运行，终端打一行提示，其他功能照常。

## 说明

主循环、工具面、前后端的业务逻辑都是自己写的，跨会话记忆那层也是 —— 用标准库 `sqlite3` 建了个 FTS5 索引，没有引入任何智能体框架。界面视觉是基于一份参考 UI 改写的，交互和业务逻辑是自研。代码规模（不含测试和调试脚本）后端约 1,800 行 Python，前端约 1,280 行。
