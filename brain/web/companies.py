
from __future__ import annotations

import re

from bson import ObjectId
from fastapi.encoders import jsonable_encoder
from pymongo.asynchronous.database import AsyncDatabase

COMPANIES = "defence_companies"
COMPANY_OK = {"collection_status": "success"}
COMPANY_CARD = {"_id": 0, "name": 1, "slug": 1, "description": 1, "location": 1, "country": 1, "sector": 1,
                "card_metadata": 1, "website": 1, "facts.website": 1}
COMPANY_HIDDEN = {"_id": 0, "data_name": 0}
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")
SECTOR_LABELS = ("Advanced Materials", "Autonomy & AI", "Counter-Drone", "Cyber", "Drones", "Electronic Warfare",
                 "Ground Robotics", "Hypersonics & Propulsion", "Loitering Munitions", "Maritime", "Space & ISR",
                 "Training & Simulation")


def titled(s: str | None) -> str:
    """«united-kingdom» → «United Kingdom», «ai» → «AI»."""
    return " ".join(w.upper() if len(w) <= 2 else w.capitalize() for w in re.split(r"[-_\s]+", s or "") if w)


def split_card_metadata(meta: str | None) -> tuple[str | None, str]:
    """«Advanced Materials Seed / Jun 2025» → («Advanced Materials», «Seed / Jun 2025»)."""
    m = " ".join((meta or "").split())
    for label in sorted(SECTOR_LABELS, key=len, reverse=True):
        if m.lower().startswith(label.lower()):
            return label, m[len(label):].strip()
    return None, m


def company_out(doc: dict) -> dict:
    """Документ для браузера: + sector_label і stage з card_metadata; ObjectId і дати — у рядки."""
    label, stage = split_card_metadata(doc.get("card_metadata"))
    doc["sector_label"] = label or titled(doc.get("sector"))
    doc["stage"] = stage
    return jsonable_encoder(doc, custom_encoder={ObjectId: str})


async def facets(db: AsyncDatabase) -> dict:
    """Країни й категорії для фільтрів, з кількістю компаній: [{id, label, count}]."""
    sectors: dict[str, dict] = {}
    countries: dict[str, int] = {}
    async for d in db[COMPANIES].find(COMPANY_OK, {"_id": 0, "sector": 1, "country": 1, "card_metadata": 1}):
        if isinstance(d.get("sector"), str) and d["sector"]:
            s = sectors.setdefault(d["sector"], {"id": d["sector"], "label": None, "count": 0})
            s["count"] += 1
            s["label"] = s["label"] or split_card_metadata(d.get("card_metadata"))[0]
        if isinstance(d.get("country"), str) and d["country"]:
            countries[d["country"]] = countries.get(d["country"], 0) + 1
    for s in sectors.values():
        s["label"] = s["label"] or titled(s["id"])
    return {"sectors": sorted(sectors.values(), key=lambda s: s["label"]),
            "countries": sorted(({"id": c, "label": titled(c), "count": n} for c, n in countries.items()),
                                key=lambda c: c["label"])}


async def list_companies(db: AsyncDatabase, q: str = "", country: str = "", sector: str = "",
                         skip: int = 0, limit: int = 24) -> dict:
    """{total, items}; на першій сторінці (skip=0) ще й списки для фільтрів — з усієї колекції."""
    flt = dict(COMPANY_OK)
    if country:
        flt["country"] = country
    if sector:
        flt["sector"] = sector
    q = q.strip()
    if q:                               # пошук за назвою, описом і містом; re.escape: текст, а не регулярний вираз
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"name": rx}, {"description": rx}, {"location": rx}]
    col = db[COMPANIES]
    total = await col.count_documents(flt)
    items = [company_out(c) async for c in col.find(flt, COMPANY_CARD).sort("name", 1).skip(skip).limit(limit)]
    out = {"total": total, "items": items}
    if skip == 0:
        out |= await facets(db)
    return out


async def get_company(db: AsyncDatabase, slug: str) -> dict | None:
    """Увесь документ компанії, крім службових полів; None — немає такої (або slug не схожий на slug)."""
    if not SLUG.match(slug or ""):
        return None
    doc = await db[COMPANIES].find_one({"slug": slug, **COMPANY_OK}, COMPANY_HIDDEN)
    return company_out(doc) if doc else None
