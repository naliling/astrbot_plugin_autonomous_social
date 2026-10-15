"""跨模块共用的文本小工具。

现在只装一件东西：天气原句的方位前缀。

来由：Humanoid Core 的 `weather.env` 交过来时本身往往就是一整句
（「外面在下雨」/「窗外在下雪」），所以谁要在前面补一个「外头」，
都得先看一眼原句是不是已经交代过方位。原来 `anchors._outside_lead` 与
`groupflow.icebreak_topics` 各自硬写了一遍 `f"外头{text}"`，
于是同一个毛病在两条链路上各犯一次（实测产出「外头外面在下雨」）。
判据与词表放在这里，两边共用一份。
"""

from __future__ import annotations

from typing import Any

#: 已经带了方位信息的词——原句里出现任意一个就不再补「外头」。
OUTSIDE_WORDS = ("外头", "外面", "外边", "户外", "窗外")


def outside_lead(text: Any) -> str:
    """给天气这类整句补一个「外头」——原句自己已经说了方位就不补。

    空输入返回空串（调用点普遍把它拼进提示词，别拼出一个孤零零的「外头」）。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    if any(word in body for word in OUTSIDE_WORDS):
        return body
    return f"外头{body}"
