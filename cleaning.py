import re
from collections import Counter

import emoji


FOOTER_PHRASES = (
    "блог",
    "мапа",
    "написати нам",
    "написати",
    "підтримати нас",
    "підтримати",
    "повідомити новину",
    "надіслати новину",
    "підписатися",
    "підписуйтесь",
    "наш чат",
    "реклама",
    "адмін",
    "співпраця",
)
MAX_FOOTER_LINE_LEN = 40

AD_MARKERS = (
    "#реклама",
    "#промо",
    "#partner",
    "#ad ",
    "на правах реклами",
    "партнерський матеріал",
    "erid",
)

_LINK_ONLY = re.compile(r"(@\w+|https?://\S+|t\.me/\S+)")
_STRIP_CHARS = " .,:;|•·-—–>»«\"'()[]"


def is_ad(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in AD_MARKERS)


def remove_emojis(text: str) -> str:
    if not text:
        return ""
    return emoji.replace_emoji(text, replace="")


def _norm_line(line: str) -> str:
    return line.lower().strip(_STRIP_CHARS)


def _is_footer_line(line: str, channel_footers: set[str] = frozenset()) -> bool:
    if not line:
        return True
    if not any(c.isalnum() for c in line):
        return True
    norm = _norm_line(line)
    if norm in channel_footers:
        return True
    if len(norm) > MAX_FOOTER_LINE_LEN:
        return False
    if _LINK_ONLY.fullmatch(norm):
        return True
    if norm in FOOTER_PHRASES:
        return True
    return len(norm.split()) <= 3 and any(norm.startswith(p) for p in FOOTER_PHRASES)


def strip_footer(lines: list[str], channel_footers: set[str] = frozenset()) -> list[str]:
    lines = list(lines)
    while lines and _is_footer_line(lines[-1], channel_footers):
        lines.pop()
    return lines


def learn_channel_footers(texts: list[str], tail: int = 6, min_share: float = 0.3) -> set[str]:

    texts = [t for t in texts if t]
    if len(texts) < 5:
        return set()
    counter: Counter[tuple[str, ...]] = Counter()
    for t in texts:
        raw_lines = [l.strip() for l in remove_emojis(t).split("\n")]
        raw_lines = [l for l in raw_lines if any(c.isalnum() for c in l)]
        for k in range(1, min(tail, len(raw_lines) - 1) + 1):
            suffix = raw_lines[-k:]

            if suffix[0][-1] in ".!?…":
                break
            counter[tuple(_norm_line(l) for l in suffix)] += 1
    threshold = max(3, int(len(texts) * min_share))
    footers: set[str] = set()
    for suffix, n in counter.items():
        if n >= threshold and all(len(l) <= 60 for l in suffix):
            footers.update(suffix)
    return footers


def clean_post_text(text: str, channel_footers: set[str] = frozenset()) -> str:
    if not text:
        return ""
    text = remove_emojis(text)
    lines = [line.strip() for line in text.split("\n")]
    lines = strip_footer(lines, channel_footers)
    lines = [line for line in lines if line]
    cleaned = "\n\n".join(lines)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def normalize_for_model(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())
