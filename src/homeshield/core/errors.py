"""领域异常。"""


class HomeshieldError(Exception):
    pass


class DegradeError(HomeshieldError):
    """可预期的降级:图片转写失败提示粘贴文字;引用校验耗尽提示人工确认。"""

    def __init__(self, user_message: str, reason: str):
        super().__init__(reason)
        self.user_message = user_message
        self.reason = reason


class DuplicateMessage(HomeshieldError):
    """同一 msg_id 的重复提交。"""

    def __init__(self, msg_id: str):
        super().__init__(f"duplicate msg_id={msg_id}")
        self.msg_id = msg_id


class ValidationError(HomeshieldError):
    pass
