"""业务级内容指纹（TDD 6.1 第二层缓存）。

只取目标容器的可见文本，归一化空白后 sha256。
不拼接整页 HTML，避免广告位、时间戳、推荐模块造成"伪变更"。

对外契约：compute_fingerprint(html, selector) -> str（纯函数）
"""

import hashlib
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser


def compute_fingerprint(html: str, selector: str) -> str:
    node = HTMLParser(html).css_first(selector)
    raw = node.text() if node else ""
    norm = re.sub(r"\s+", " ", raw).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
