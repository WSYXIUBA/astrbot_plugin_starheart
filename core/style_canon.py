# -*- coding: utf-8 -*-
"""表达样本 canon 化：把一条语料洗成可注入的样本。

纯函数、无 DB / 无 astrbot 依赖，S0 盘点与运行时共用同一套口径。
纪律：
- 好感标签格式以 core/engine.py 的 FAVOUR_TAG_PAT 为准（`[好感:+3]`），本文件自带同款正则，
  不改 engine（避免把 astrbot 依赖拉进纯函数层），两边必须保持一致。
- 不在这里做「值不值得学」的判断，那交给 LLM；这里只做清洗。
"""
from __future__ import annotations

import re

# 与 engine.FAVOUR_TAG_PAT 同款：允许 [好感:+3] / [好感度:-1] / [favor: 2] / [好感:持平]
FAVOUR_TAG = re.compile(
    r"\[\s*(?:好感度?|favou?r)\s*[:：]?\s*(?:持平|没有?变化|unchanged|[+-]?\d+(?:\.\d+)?)\s*\]",
    re.I,
)
# 平台占位与 CQ 码
PLACEHOLDER = re.compile(r"\[(?:表情|图片|语音|视频|文件|at|@)[^\]]*\]|\[CQ:[^\]]+\]", re.I)
# 命令残骸
CMD = re.compile(
    r"^\s*(?:/hf\b|hf\s+(?:mute|conf|v2|on|off|expr|mem|vec|graph|recall|sid|status|style)\b|"
    r"/help\b|/reset\b)",
    re.I,
)
# 纯符号/纯表情（没有任何文字）
NO_TEXT = re.compile(r"^[\W_]+$", re.U)

_WS = re.compile(r"[ \t\u3000]+")
_PUNCT = re.compile(r"[，。！？、；：「」『』（）()\[\]【】<>《》~`!@#$%^&*_+=|\\/:;\"',.?\-—…]")


def norm(text: str) -> str:
    """归一化：小写、去空白、去标点。用于去重键与相似度。"""
    t = str(text or "").lower()
    t = _WS.sub("", t)
    return _PUNCT.sub("", t)


def clean(text: str) -> str:
    """单条消息清洗（不拆泡）：剥标签/占位/命令，压缩空白。"""
    t = str(text or "")
    t = FAVOUR_TAG.sub("", t)
    t = PLACEHOLDER.sub("", t)
    t = t.replace("\r", "")
    return _WS.sub(" ", t).strip()


def canon(text: str, *, min_len: int = 2, max_len: int = 80) -> list[str]:
    """把一条语料洗成气泡列表；不合格的气泡丢弃。

    规则（与方案 v1.3 §2.2 一致）：
      1. 剥好感标签 / 平台占位 / CQ 码
      2. 按空行拆气泡（插件用空行分段发送，这是真实边界）
      3. 去掉命令残骸、纯符号、过短/过长
    """
    t = FAVOUR_TAG.sub("", str(text or ""))
    t = PLACEHOLDER.sub("", t)
    t = t.replace("\r", "")
    out: list[str] = []
    for raw in re.split(r"\n\s*\n", t):
        b = _WS.sub(" ", raw).strip()
        if not b or CMD.match(b) or NO_TEXT.match(b):
            continue
        if not (min_len <= len(b) <= max_len):
            continue
        out.append(b)
    return out


def norm_key(trigger: str, reply: str) -> str:
    """样本去重键：trigger + reply 归一化后拼接。"""
    return f"{norm(trigger)}|{norm(reply)}"


def bigrams(text: str) -> set[str]:
    t = norm(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar(a: str, b: str) -> float:
    """2-gram Jaccard。用于 trigger 重合闸门。"""
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def structure(text: str) -> dict:
    """结构标注（给模型看「为什么这样接」，也给排序用）。"""
    bubbles = canon(text)
    body = bubbles[0] if bubbles else clean(text)
    tail = body[-1:] if body else ""
    return {
        "len": len(body),
        "bubbles": max(1, len(bubbles)),
        "question": body.endswith(("?", "？", "吗", "呢", "吧")),
        "ends_with": tail if tail in "啊嘛呢吧哦嗯呀哈" else "",
        "bangs": body.count("!"),
        "ellipsis": body.count("…") + body.count("..."),
    }


def looks_like_bot_meta(text: str) -> bool:
    """明显的元操作 / 自我描述，不该当表达样本（蒸馏时兜底过滤）。"""
    t = str(text or "")
    return bool(re.search(
        r"(我是(?:一个)?(?:AI|人工智能|助手|模型)|作为(?:一个)?(?:AI|助手)|"
        r"希望(?:能)?帮到你|有什么(?:可以|能)帮|请(?:稍等|放心)|抱歉[，,]?\s*我|"
        r"根据(?:你的|您)|综上所述|首先[，,]|其次[，,]|最后[，,])",
        t,
    ))
