"""同步方向（发送 / 接收）的取值与判定。

面板上文字、文件两组各有一个「双向 / 只发送 / 只接收」选择，客户端与 GUI
都按同一套取值判断某个方向是否放开，避免两边各写一份字符串比较。
"""

MODE_BOTH = "both"
MODE_SEND = "send"
MODE_RECEIVE = "receive"

VALID_MODES = (MODE_BOTH, MODE_SEND, MODE_RECEIVE)

# 面板分段按钮的显示文案，顺序即按钮顺序
MODE_LABELS = {
    MODE_BOTH: "双向",
    MODE_SEND: "只发送",
    MODE_RECEIVE: "只接收",
}

DIRECTION_SEND = "send"
DIRECTION_RECEIVE = "receive"


def normalize_mode(value, default=MODE_BOTH):
    """把配置里的值收敛成合法模式，非法值退回 default（不抛异常）。"""
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_MODES else default


def is_send_allowed(mode):
    """该模式下是否允许把本地内容推出去。"""
    return normalize_mode(mode) in (MODE_BOTH, MODE_SEND)


def is_receive_allowed(mode):
    """该模式下是否允许接收远端内容。"""
    return normalize_mode(mode) in (MODE_BOTH, MODE_RECEIVE)


def describe_mode(mode):
    """给日志用的中文描述。"""
    return MODE_LABELS.get(normalize_mode(mode), MODE_LABELS[MODE_BOTH])
