import re

import emoji


FOOTER_PHRASES = (
    "блог",
    "написати нам",
    "підтримати нас",
    "мапа",
    "написати",
    "підтримати",
    "повідомити",
    "реклама",
    "адмін",
)

AD_MARKERS = ("#реклама",)


def is_ad(text: str) -> bool:

    lowered = (text or "").lower()
    return any(marker in lowered for marker in AD_MARKERS)


def remove_emojis(text: str) -> str:
    if not text:
        return ""
    return emoji.replace_emoji(text, replace="")


def _is_footer_line(line: str) -> bool:

    if not line:
        return True
    if not any(c.isalnum() for c in line):
        return True
    lowered = line.lower()
    return any(phrase in lowered for phrase in FOOTER_PHRASES)


def strip_footer(lines: list[str]) -> list[str]:

    lines = list(lines)
    while lines and _is_footer_line(lines[-1]):
        lines.pop()
    return lines


def clean_post_text(text: str) -> str:

    if not text:
        return ""

    text = remove_emojis(text)
    lines = [line.strip() for line in text.split("\n")]
    lines = strip_footer(lines)
    lines = [line for line in lines if line]

    cleaned = "\n\n".join(lines)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned


def normalize_for_model(text: str) -> str:

    return re.sub(r"\s+", " ", (text or "").strip())