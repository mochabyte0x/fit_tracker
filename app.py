"""Personal metrics tracker — days in SQLite, charts in the browser."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "tracker.db"
PASSWORD = os.environ.get("PASSWORD", "")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Metrics Tracker")
security = HTTPBasic(auto_error=False)


def _password_ok(password: str) -> bool:
    # hash so compare_digest tolerates different lengths
    a = hashlib.sha256(password.encode()).hexdigest()
    b = hashlib.sha256(PASSWORD.encode()).hexdigest()
    return secrets.compare_digest(a, b)


def require_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> None:
    # ponytail: HTTP Basic if PASSWORD set; put Tailscale in front for real security
    if not PASSWORD:
        return
    if credentials is None or not _password_ok(credentials.password or ""):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


RANGE_METRICS = ("calories", "protein", "carbs", "fat")


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS days (
                date TEXT PRIMARY KEY,
                weight REAL,
                calories INTEGER,
                calories_min INTEGER,
                calories_max INTEGER,
                protein REAL,
                protein_min REAL,
                protein_max REAL,
                carbs REAL,
                carbs_min REAL,
                carbs_max REAL,
                fat REAL,
                fat_min REAL,
                fat_max REAL,
                sleep_hours REAL,
                sleep_quality INTEGER,
                training TEXT,
                notes TEXT
            )
            """
        )
        # ponytail: cheap migrate for DBs created before range columns
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(days)").fetchall()
        }
        for col, typ in (
            ("calories_min", "INTEGER"),
            ("calories_max", "INTEGER"),
            ("protein_min", "REAL"),
            ("protein_max", "REAL"),
            ("carbs_min", "REAL"),
            ("carbs_max", "REAL"),
            ("fat_min", "REAL"),
            ("fat_max", "REAL"),
        ):
            if col not in existing:
                conn.execute(f"ALTER TABLE days ADD COLUMN {col} {typ}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


class DayIn(BaseModel):
    weight: Optional[float] = None
    calories: Optional[int] = None
    calories_min: Optional[int] = None
    calories_max: Optional[int] = None
    protein: Optional[float] = None
    protein_min: Optional[float] = None
    protein_max: Optional[float] = None
    carbs: Optional[float] = None
    carbs_min: Optional[float] = None
    carbs_max: Optional[float] = None
    fat: Optional[float] = None
    fat_min: Optional[float] = None
    fat_max: Optional[float] = None
    sleep_hours: Optional[float] = None
    sleep_quality: Optional[int] = Field(default=None, ge=1, le=5)
    training: Optional[str] = None
    notes: Optional[str] = None


def normalize_ranges(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure min/max ordered; midpoint fills the main field used by charts."""
    for key in RANGE_METRICS:
        lo_k, hi_k = f"{key}_min", f"{key}_max"
        lo, hi = data.get(lo_k), data.get(hi_k)
        if lo is not None and hi is not None:
            if lo > hi:
                lo, hi = hi, lo
            mid = (lo + hi) / 2
            data[lo_k] = int(round(lo)) if key == "calories" else round(float(lo), 1)
            data[hi_k] = int(round(hi)) if key == "calories" else round(float(hi), 1)
            data[key] = int(round(mid)) if key == "calories" else round(mid, 1)
        else:
            data[lo_k] = None
            data[hi_k] = None
    return data


class DayTargets(BaseModel):
    calories: Optional[int] = None
    protein: Optional[float] = None


class Phase(BaseModel):
    id: str
    name: str
    weight: Optional[float] = None
    training: DayTargets = Field(default_factory=DayTargets)
    rest: DayTargets = Field(default_factory=DayTargets)


class PhasesConfig(BaseModel):
    active_id: Optional[str] = None
    phases: list[Phase] = Field(default_factory=list)


class ParseIn(BaseModel):
    text: str


def _setting_get(key: str) -> Any | None:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def _setting_set(key: str, value: Any) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, json.dumps(value)),
        )


def _new_phase_id() -> str:
    return secrets.token_hex(4)


def get_phases() -> PhasesConfig:
    raw = _setting_get("phases")
    if raw is not None:
        try:
            cfg = PhasesConfig.model_validate(raw)
            if cfg.phases and (
                not cfg.active_id
                or cfg.active_id not in {p.id for p in cfg.phases}
            ):
                cfg.active_id = cfg.phases[0].id
            return cfg
        except ValueError:
            pass

    # migrate legacy flat goals → one phase
    legacy = _setting_get("goals")
    if isinstance(legacy, dict) and any(legacy.get(k) is not None for k in ("calories", "protein", "weight")):
        targets = DayTargets(
            calories=legacy.get("calories"),
            protein=legacy.get("protein"),
        )
        phase = Phase(
            id=_new_phase_id(),
            name="Default",
            weight=legacy.get("weight"),
            training=targets,
            rest=targets.model_copy(),
        )
        cfg = PhasesConfig(active_id=phase.id, phases=[phase])
        save_phases(cfg)
        return cfg

    return PhasesConfig()


def save_phases(cfg: PhasesConfig) -> PhasesConfig:
    if cfg.phases and (
        not cfg.active_id or cfg.active_id not in {p.id for p in cfg.phases}
    ):
        cfg.active_id = cfg.phases[0].id
    if not cfg.phases:
        cfg.active_id = None
    _setting_set("phases", cfg.model_dump())
    return cfg


def active_phase(cfg: PhasesConfig | None = None) -> Phase | None:
    cfg = cfg or get_phases()
    if not cfg.active_id:
        return None
    for phase in cfg.phases:
        if phase.id == cfg.active_id:
            return phase
    return cfg.phases[0] if cfg.phases else None


def targets_for_day(phase: Phase | None, training: str | None) -> DayTargets:
    if phase is None:
        return DayTargets()
    trained = bool(training and str(training).strip())
    return phase.training if trained else phase.rest


# number token: 2100 | 2,100 | 78.2 | 78,2
_NUM = r"(\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+|\d+(?:[.,]\d+)?)"
_SEP = r"\s*(?:[-–—]|to)\s*"


def parse_number(raw: str) -> float:
    s = raw.strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
        return float(s.replace(",", ""))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        return float(s.replace(".", ""))
    return float(s.replace(",", "."))


def _set_range(
    out: dict[str, Any], key: str, lo: str, hi: str, *, as_int: bool
) -> None:
    a, b = parse_number(lo), parse_number(hi)
    if a > b:
        a, b = b, a
    mid = (a + b) / 2
    if as_int:
        out[key] = int(round(mid))
        out[f"{key}_min"] = int(round(a))
        out[f"{key}_max"] = int(round(b))
    else:
        out[key] = round(mid, 1)
        out[f"{key}_min"] = round(a, 1)
        out[f"{key}_max"] = round(b, 1)


def parse_metrics_text(text: str) -> dict[str, Any]:
    """Pull weight/macros/sleep out of messy AI paste. Supports 2000-2200 ranges."""
    out: dict[str, Any] = {}

    def first(*patterns: str) -> re.Match[str] | None:
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                return m
        return None

    m = first(
        r"(?:weight|körpergewicht|koerpergewicht)\s*[:=]?\s*" + _NUM + r"\s*kg?",
        _NUM + r"\s*kg\b",
    )
    if m:
        out["weight"] = round(parse_number(m.group(1)), 2)

    m = first(
        rf"(?:calories|calorie|kcal|cals?)\s*[:=]?\s*{_NUM}{_SEP}{_NUM}",
        rf"{_NUM}{_SEP}{_NUM}\s*(?:kcal|calories|cals?)\b",
    )
    if m:
        _set_range(out, "calories", m.group(1), m.group(2), as_int=True)
    else:
        m = first(
            rf"(?:calories|calorie|kcal|cals?)\s*[:=]?\s*{_NUM}",
            rf"{_NUM}\s*(?:kcal|calories|cals?)\b",
        )
        if m:
            out["calories"] = int(round(parse_number(m.group(1))))

    m = first(
        rf"(?:protein|proteins|eiweiß|eiweiss)\s*[:=]?\s*{_NUM}{_SEP}{_NUM}\s*g?",
        rf"{_NUM}{_SEP}{_NUM}\s*g\s*(?:protein|proteins|eiweiß|eiweiss)\b",
    )
    if m:
        _set_range(out, "protein", m.group(1), m.group(2), as_int=False)
    else:
        m = first(
            rf"(?:protein|proteins|eiweiß|eiweiss)\s*[:=]?\s*{_NUM}\s*g?",
            rf"{_NUM}\s*g\s*(?:protein|proteins|eiweiß|eiweiss)\b",
        )
        if m:
            out["protein"] = round(parse_number(m.group(1)), 1)

    m = first(
        rf"(?:carbs?|carbohydrates?|kohlehydrate?|kohlenhydrate?)\s*[:=]?\s*{_NUM}{_SEP}{_NUM}\s*g?",
        rf"{_NUM}{_SEP}{_NUM}\s*g\s*(?:carbs?|carbohydrates?|kohlehydrate?|kohlenhydrate?)\b",
    )
    if m:
        _set_range(out, "carbs", m.group(1), m.group(2), as_int=False)
    else:
        m = first(
            rf"(?:carbs?|carbohydrates?|kohlehydrate?|kohlenhydrate?)\s*[:=]?\s*{_NUM}\s*g?",
            rf"{_NUM}\s*g\s*(?:carbs?|carbohydrates?|kohlehydrate?|kohlenhydrate?)\b",
        )
        if m:
            out["carbs"] = round(parse_number(m.group(1)), 1)

    m = first(
        rf"(?:fat|fats|fett)\s*[:=]?\s*{_NUM}{_SEP}{_NUM}\s*g?",
        rf"{_NUM}{_SEP}{_NUM}\s*g\s*(?:fat|fats|fett)\b",
    )
    if m:
        _set_range(out, "fat", m.group(1), m.group(2), as_int=False)
    else:
        m = first(
            rf"(?:fat|fats|fett)\s*[:=]?\s*{_NUM}\s*g?",
            rf"{_NUM}\s*g\s*(?:fat|fats|fett)\b",
        )
        if m:
            out["fat"] = round(parse_number(m.group(1)), 1)

    m = first(
        rf"(?:sleep(?:\s*hours?)?|slept|schlaf(?:dauer)?)\s*[:=]?\s*{_NUM}\s*(?:h|hrs?|hours?)?",
        rf"{_NUM}\s*(?:h|hrs?|hours?)\s*(?:sleep|slept|schlaf)?",
    )
    if m:
        out["sleep_hours"] = round(parse_number(m.group(1)), 2)

    return out


def parse_day(day: str) -> str:
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date, use YYYY-MM-DD") from exc


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/days", dependencies=[Depends(require_auth)])
def list_days(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
) -> list[dict[str, Any]]:
    end = parse_day(to_date) if to_date else date.today().isoformat()
    start = parse_day(from_date) if from_date else (date.today() - timedelta(days=90)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM days
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/days/{day}", dependencies=[Depends(require_auth)])
def get_day(day: str) -> dict[str, Any]:
    day = parse_day(day)
    with db() as conn:
        row = conn.execute("SELECT * FROM days WHERE date = ?", (day,)).fetchone()
    if not row:
        return {"date": day}
    return dict(row)


@app.put("/api/days/{day}", dependencies=[Depends(require_auth)])
def upsert_day(day: str, body: DayIn) -> dict[str, Any]:
    day = parse_day(day)
    data = normalize_ranges(body.model_dump())
    if all(v is None or v == "" for v in data.values()):
        with db() as conn:
            conn.execute("DELETE FROM days WHERE date = ?", (day,))
        return {"date": day, "deleted": True}

    cols = list(data.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols)
    values = [data[c] for c in cols]

    with db() as conn:
        conn.execute(
            f"""
            INSERT INTO days (date, {", ".join(cols)})
            VALUES (?, {placeholders})
            ON CONFLICT(date) DO UPDATE SET {updates}
            """,
            [day, *values],
        )
        row = conn.execute("SELECT * FROM days WHERE date = ?", (day,)).fetchone()
    return dict(row)


@app.delete("/api/days/{day}", dependencies=[Depends(require_auth)])
def delete_day(day: str) -> Response:
    day = parse_day(day)
    with db() as conn:
        conn.execute("DELETE FROM days WHERE date = ?", (day,))
    return Response(status_code=204)


@app.get("/api/stats", dependencies=[Depends(require_auth)])
def stats(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
) -> dict[str, Any]:
    end = parse_day(to_date) if to_date else date.today().isoformat()
    start = parse_day(from_date) if from_date else (date.today() - timedelta(days=30)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM days
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (start, end),
        ).fetchall()

    def avg(key: str) -> float | None:
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    trained = sum(1 for r in rows if r["training"] and str(r["training"]).strip())
    weights = [r["weight"] for r in rows if r["weight"] is not None]
    weight_delta = (
        round(weights[-1] - weights[0], 2) if len(weights) >= 2 else None
    )

    cfg = get_phases()
    phase = active_phase(cfg)
    avg_cal = avg("calories")
    avg_pro = avg("protein")
    avg_w = avg("weight")

    target_cals: list[float] = []
    target_pros: list[float] = []
    for r in rows:
        t = targets_for_day(phase, r["training"])
        if r["calories"] is not None and t.calories is not None:
            target_cals.append(t.calories)
        if r["protein"] is not None and t.protein is not None:
            target_pros.append(t.protein)

    avg_target_cal = round(sum(target_cals) / len(target_cals), 2) if target_cals else None
    avg_target_pro = round(sum(target_pros) / len(target_pros), 2) if target_pros else None

    def vs_goal(avg_val: float | None, goal: float | None) -> float | None:
        if avg_val is None or goal is None:
            return None
        return round(avg_val - goal, 2)

    return {
        "from": start,
        "to": end,
        "days_logged": len(rows),
        "avg_calories": avg_cal,
        "avg_weight": avg_w,
        "avg_sleep": avg("sleep_hours"),
        "avg_protein": avg_pro,
        "training_days": trained,
        "weight_change": weight_delta,
        "phase": phase.model_dump() if phase else None,
        "avg_target_calories": avg_target_cal,
        "avg_target_protein": avg_target_pro,
        "calories_vs_goal": vs_goal(avg_cal, avg_target_cal),
        "protein_vs_goal": vs_goal(avg_pro, avg_target_pro),
        "weight_vs_goal": vs_goal(avg_w, phase.weight if phase else None),
    }


@app.get("/api/phases", dependencies=[Depends(require_auth)])
def read_phases() -> PhasesConfig:
    return get_phases()


@app.put("/api/phases", dependencies=[Depends(require_auth)])
def write_phases(body: PhasesConfig) -> PhasesConfig:
    # ensure every phase has an id
    fixed: list[Phase] = []
    for phase in body.phases:
        if not phase.id:
            phase = phase.model_copy(update={"id": _new_phase_id()})
        if not phase.name.strip():
            raise HTTPException(status_code=400, detail="Phase name required")
        fixed.append(phase)
    return save_phases(PhasesConfig(active_id=body.active_id, phases=fixed))


@app.post("/api/parse", dependencies=[Depends(require_auth)])
def parse_paste(body: ParseIn) -> dict[str, Any]:
    return parse_metrics_text(body.text)


@app.get("/api/export.csv", dependencies=[Depends(require_auth)])
def export_csv() -> StreamingResponse:
    with db() as conn:
        rows = conn.execute("SELECT * FROM days ORDER BY date ASC").fetchall()

    buf = io.StringIO()
    fields = [
        "date",
        "weight",
        "calories",
        "calories_min",
        "calories_max",
        "protein",
        "protein_min",
        "protein_max",
        "carbs",
        "carbs_min",
        "carbs_max",
        "fat",
        "fat_min",
        "fat_max",
        "sleep_hours",
        "sleep_quality",
        "training",
        "notes",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r[k] for k in fields})

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=metrics.csv"},
    )


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _self_check() -> None:
    sample = "2100 kcal, 140g protein, 180g carbs, 70g fat, weight 78.2 kg, sleep 7.5h"
    got = parse_metrics_text(sample)
    assert got["calories"] == 2100
    assert got["protein"] == 140
    assert got["carbs"] == 180
    assert got["fat"] == 70
    assert got["weight"] == 78.2
    assert got["sleep_hours"] == 7.5
    assert parse_metrics_text("Calories: 2,100\nProtein: 140g")["calories"] == 2100
    rng = parse_metrics_text("2000-2200 kcal, protein 130-150g, carbs 170-190g, fat 60-80g")
    assert rng["calories"] == 2100
    assert rng["calories_min"] == 2000 and rng["calories_max"] == 2200
    assert rng["protein"] == 140
    assert rng["protein_min"] == 130 and rng["protein_max"] == 150
    assert normalize_ranges(
        {"calories": None, "calories_min": 2200, "calories_max": 2000,
         "protein": None, "protein_min": None, "protein_max": None,
         "carbs": None, "carbs_min": None, "carbs_max": None,
         "fat": None, "fat_min": None, "fat_max": None}
    )["calories"] == 2100
    print("self-check ok")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "check":
        _self_check()
    else:
        import uvicorn

        uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
