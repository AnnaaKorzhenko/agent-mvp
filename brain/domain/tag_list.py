"""The pipeline's fixed tag list: every post's tags come from it (older posts were re-tagged).

A copy, taken 2026-10-29 from the list the local-model track uses. Request #9 in
docs/REQUESTS_TO_LOCAL_MODEL_TRACK.md asks for one shared source; until then, update this copy when the list
changes. Used by the learning job: the model maps each goal to tags from this list (the broadest match level).
"""

TAG_LIST: tuple[str, ...] = (
    "продуктова розробка", "інновації", "інвестиції", "грантове фінансування", "державні закупівлі",
    "виробничі потужності", "масштабування бізнесу", "ринкова локалізація", "галузева аналітика", "ринкові огляди",
    "стратегічні партнерства", "комерціалізація розробок", "аналітика", "партнерства", "стартапи", "виробництво",
    "гранти", "прогнози", "тренди", "промисловість", "тендер", "закупівлі", "субпідряд", "експорт", "імпорт",
    "постачання", "тестування", "автоматизація", "стратегія", "оборонні технології", "мілтех",
    "бойове застосування", "нова тактика", "комплектуючі",
)
