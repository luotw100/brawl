"""Sentence Runtime 跨 actor 的错误分类。"""


class FatalActorError(RuntimeError):
    """actor 会话已无法安全重试；当前运行必须 fail-stop。"""


__all__ = ["FatalActorError"]
