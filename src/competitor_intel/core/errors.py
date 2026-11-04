"""业务错误：机器可读 code / hint / retryable。

core 不认识 mcp；翻译成工具错误只发生在 adapters/mcp/server.py 的边界。
"""

from __future__ import annotations

import json


class CiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable

    def to_json(self) -> str:
        return json.dumps(
            {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
                "retryable": self.retryable,
            },
            ensure_ascii=False,
        )
