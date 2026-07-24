"""Sentence Runtime 跨 actor 的错误分类。"""


class FatalActorError(RuntimeError):
    """actor 会话已无法安全重试；当前运行必须 fail-stop。"""


class RetryableActorError(RuntimeError):
    """actor 边界明确判定为瞬态失败；loop 会先回滚会话再重试。"""
