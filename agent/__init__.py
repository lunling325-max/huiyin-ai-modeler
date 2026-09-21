# -*- coding: utf-8 -*-
"""agent 包。导入本包时统一把出站 DNS 解析偏好改为 IPv4 优先。

背景: 曾用大脑 API 挂在 Cloudflare 上, 国内网络到其 IPv6 偶发"半死路径":
TCP 能建连(ESTABLISHED)但数据卡住不返回——连 requests 的 read timeout 都可能
不触发, 表现为整场演示卡几十秒到几分钟。把 getaddrinfo 结果按 IPv4 优先排序
(仍有 v6 兜底)可绕开这一类问题, 对现在国内直连的 DeepSeek 同样是无害的稳健默认。

覆盖面:
- urllib3/requests 建连时调用 socket.getaddrinfo(属性访问) -> 命中;
- 标准库 socket.create_connection 内部按函数 globals 查 getaddrinfo
  (LOAD_GLOBAL 每次调用时查 socket 模块 dict) -> 替换属性即命中;
  因此 vision.py 的 urllib 路径也一并生效。
"""
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    """调用方不限协议族(family==0)时把 IPv4 排到最前; 其余原样透传。"""
    if family == 0:
        try:
            results = _orig_getaddrinfo(host, port, 0, type, proto, flags)
            v4 = [r for r in results if r[0] == _socket.AF_INET]
            if v4:
                v6 = [r for r in results if r[0] != _socket.AF_INET]
                return v4 + v6
            return results  # 完全没有 v4, 用原结果(v6 兜底)
        except Exception:
            pass
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


_socket.getaddrinfo = _ipv4_first
import socket as _s
if _s.getaddrinfo is not _ipv4_first:
    _s.getaddrinfo = _ipv4_first  # 覆盖 socket 模块级名字(含 create_connection 内部引用)
