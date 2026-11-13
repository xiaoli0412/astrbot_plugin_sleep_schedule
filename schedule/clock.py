"""纯函数时段判断，不依赖 AstrBot，便于单元测试。

窗口以「当天第几分钟」表示：0 = 00:00，1439 = 23:59。
(start, end) 中 end < start 表示跨午夜，例如 (1410, 450) = 23:30 → 次日 07:30。
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo

Window = tuple[int, int]

_SPLIT = re.compile(r"[,，;；\n\r\t ]+")
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")

DEFAULT_WINDOW = "23:30-07:30"


def parse_hhmm(token: str) -> int:
    """把 "07:30" 解析成当天第几分钟（450）。"""
    m = _HHMM.match(token.strip())
    if not m:
        raise ValueError(f"时间格式应为 HH:MM，收到 {token!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23):
        raise ValueError(f"小时越界（0-23）：{token!r}")
    if not (0 <= minute <= 59):
        raise ValueError(f"分钟越界（0-59）：{token!r}")
    return hour * 60 + minute


def minutes_to_hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_windows(spec: str) -> list[Window]:
    """解析 "23:30-07:30,13:00-14:00" -> [(1410, 450), (780, 840)]。

    支持 , ， ; ； 换行 空格 分隔多个窗口；非法条目抛 ValueError。
    """
    if spec is None:
        raise ValueError("作息窗口为空")
    text = str(spec).strip()
    if not text:
        raise ValueError("作息窗口为空")

    windows: list[Window] = []
    for chunk in _SPLIT.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("-")
        if len(parts) != 2:
            raise ValueError(f"窗口格式应为 HH:MM-HH:MM，收到 {chunk!r}")
        start = parse_hhmm(parts[0])
        end = parse_hhmm(parts[1])
        windows.append((start, end))

    if not windows:
        raise ValueError(f"没有解析到任何窗口：{spec!r}")
    return windows


def windows_str(windows: list[Window]) -> str:
    """把窗口列表还原成可读字符串。"""
    if not windows:
        return "（未配置）"
    return "、".join(f"{minutes_to_hhmm(s)}-{minutes_to_hhmm(e)}" for s, e in windows)


def minutes_of_day(now_ts: float, tz: tzinfo | None = None) -> int:
    """时间戳 -> 当天第几分钟（默认本地时区）。"""
    dt = datetime.fromtimestamp(float(now_ts), tz)
    return dt.hour * 60 + dt.minute


def is_asleep(now_ts: float, windows: list[Window], *, tz: tzinfo | None = None) -> bool:
    """当前时刻是否落在任一睡眠窗口内。

    左闭右开：窗口 "23:30-07:30" 在 23:30 算睡着、在 07:30 算已醒。
    start == end 视为整天都在睡。
    """
    if not windows:
        return False
    minute = minutes_of_day(now_ts, tz)
    for start, end in windows:
        if start == end:
            return True
        if start < end:
            if start <= minute < end:
                return True
        else:  # 跨午夜
            if minute >= start or minute < end:
                return True
    return False


def next_change(
    now_ts: float, windows: list[Window], *, tz: tzinfo | None = None
) -> tuple[float, bool]:
    """返回 (下次状态切换的时间戳, 切换后是否在睡)。

    没有窗口时返回 (now_ts, False)。
    逐分钟前扫，最多 24 小时，逻辑简单且不会算错跨午夜。
    """
    now_ts = float(now_ts)
    current = is_asleep(now_ts, windows, tz=tz)
    if not windows:
        return now_ts, current
    step = 60
    for i in range(1, 24 * 60 + 1):
        ts = now_ts + i * step
        if is_asleep(ts, windows, tz=tz) != current:
            return ts, not current
    return now_ts, current


def sleep_state_label(asleep: bool) -> str:
    return "睡眠中 🌙" if asleep else "清醒 ☀️"
