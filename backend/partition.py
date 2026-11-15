"""分区切面：一个业务日只能按事件码切或按通道切，一条扫描的切面名决定它落在哪个分区。"""

FACETS = {
    "event": "按事件码切开后落库",
    "channel": "按通道切开后落库",
}

FACET_SHORTS = {
    "event": "按事件码切",
    "channel": "按通道切",
}

BATCH_CUTS = {
    "event": "这一批按事件码切开",
    "channel": "这一批按通道切开",
}

AXIS_NAMES = {
    "event": "事件码切面",
    "channel": "通道切面",
    "other": "不属这两种切法的切面名",
    "empty": "切面名为空",
}

OUTCOME_NAMES = {
    "pending": "待处理",
    "adopted": "已采纳",
    "deviation": "切面偏了",
    "contention": "混切争用",
}

# 建层员可能把办法写成一句话，也可能直接给内部代码。
METHOD_ALIASES = {
    "event": {
        "event",
        "事件",
        "事件码",
        "事件码切",
        "事件码切面",
        "按事件码切",
        "按事件码切开",
        "按事件码切开后落库",
    },
    "channel": {
        "channel",
        "通道",
        "通道切",
        "通道切面",
        "按通道切",
        "按通道切开",
        "按通道切开后落库",
    },
}


def normalize_method(value) -> str | None:
    """把请求里带的办法归一成 event / channel；认不出返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for code, aliases in METHOD_ALIASES.items():
        if text.lower() == code or text in aliases:
            return code
    return None


def facet_name(payload: dict) -> str:
    """清洗请求给这条扫描带的切面名；空串表示没带。"""
    value = payload.get("facet")
    if value is None:
        return ""
    return str(value).strip()


def event_code(payload: dict) -> str:
    """扫描自己的事件码，事件码切法拿它当切面值。"""
    return str(payload.get("scan") or "").strip().upper()


def channel_code(terminal) -> str:
    """扫描所在的闸口通道，通道切法拿它当切面值。"""
    return str(terminal or "").strip().upper()


def facet_axis(payload: dict, terminal) -> str:
    """这个切面名属于哪一面：事件码 / 通道 / 别的名字 / 空。"""
    name = facet_name(payload)
    if not name:
        return "empty"
    upper = name.upper()
    if upper == event_code(payload):
        return "event"
    if upper == channel_code(terminal):
        return "channel"
    return "other"


def partition_key(axis: str, payload: dict, terminal):
    """按某一切法，这条扫描落到哪个分区。"""
    if axis == "event":
        code = event_code(payload)
        return f"event={code}" if code else None
    if axis == "channel":
        code = channel_code(terminal)
        return f"channel={code}" if code else None
    return None


def candidate_keys(payload: dict, terminal):
    """切面详情里的对照：事件码切落在哪，通道切落在哪。"""
    return {
        "event_key": partition_key("event", payload, terminal),
        "channel_key": partition_key("channel", payload, terminal),
    }


def aligned(axis: str, policy) -> bool:
    """切面名是否与已封存的切法相符。"""
    return bool(policy) and axis == policy


def is_mixed(axis: str, policy) -> bool:
    """切面名不为空、又跟所选切法对不上，就是混切。"""
    return axis != "empty" and not aligned(axis, policy)
