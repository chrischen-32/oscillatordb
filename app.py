import os
import re
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "oscillators.db"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024

# -------------------------
# Database
# -------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_safe_value(value):
    """Convert local-LLM outputs into values SQLite can store.

    Ollama/local models sometimes return lists or dictionaries for fields like
    authors, notes, evidence_quote, or platform. SQLite cannot bind those
    directly, so we convert them to readable strings.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, list):
        cleaned = [sqlite_safe_value(v) for v in value if v not in (None, "")]
        # Join simple lists as text; JSON-encode nested structures.
        if all(isinstance(v, (str, int, float)) for v in cleaned):
            return "; ".join(str(v) for v in cleaned)
        return json.dumps(cleaned, ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def prepare_sqlite_row(row):
    return {k: sqlite_safe_value(v) for k, v in row.items()}


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oscillator_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                authors TEXT,
                year INTEGER,
                doi TEXT,
                source_url TEXT,
                filename TEXT,
                oscillator_type TEXT,
                platform_type TEXT,
                material TEXT,
                platform TEXT,
                frequency_hz REAL,
                quality_factor REAL,
                fq_product REAL,
                t1_seconds REAL,
                temperature_k REAL,
                evidence_quote TEXT,
                notes TEXT,
                validation_status TEXT DEFAULT 'pending',
                extraction_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Lightweight migrations for users updating from older ZIPs.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(oscillator_records)").fetchall()}
        if "platform_type" not in cols:
            conn.execute("ALTER TABLE oscillator_records ADD COLUMN platform_type TEXT")
        count = conn.execute("SELECT COUNT(*) FROM oscillator_records").fetchone()[0]
        if count == 0:
            seed_examples(conn)


def seed_examples(conn):
    row = {
        "title": "Example nanomechanical oscillator record",
        "authors": "Example Author",
        "year": 2024,
        "doi": "",
        "source_url": "",
        "filename": "",
        "oscillator_type": "Optomechanical / nanomechanical",
        "platform_type": "Optomechanical",
        "material": "Silicon",
        "platform": "Phononic crystal",
        "frequency_hz": 5.0e9,
        "quality_factor": 1.0e5,
        "fq_product": 5.0e14,
        "t1_seconds": 3.18e-6,
        "temperature_k": 0.02,
        "evidence_quote": "Seed example only. Replace with values extracted from a real paper.",
        "notes": "Example public record. Delete or edit this after uploading real papers.",
        "validation_status": "approved",
        "extraction_json": "{}",
    }
    cols = ", ".join(row.keys())
    qs = ", ".join([":" + k for k in row.keys()])
    conn.execute(f"INSERT INTO oscillator_records ({cols}) VALUES ({qs})", prepare_sqlite_row(row))

# -------------------------
# Text extraction
# -------------------------
def extract_text_from_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("pypdf is not installed. Run: pip install -r requirements.txt") from exc
        text_parts = []
        reader = PdfReader(str(path))
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            text_parts.append(f"\n\n--- PAGE {i+1} ---\n{page_text}")
        return normalize_scientific_text("\n".join(text_parts))
    return normalize_scientific_text(path.read_text(errors="ignore"))

# -------------------------
# Physics/unit helpers
# -------------------------
UNIT_MULTIPLIERS = {"hz": 1, "khz": 1e3, "mhz": 1e6, "ghz": 1e9, "thz": 1e12}
TIME_MULTIPLIERS = {"ns": 1e-9, "µs": 1e-6, "us": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1.0}


def normalize_scientific_text(text: str) -> str:
    """Normalize PDF/OCR scientific notation before extraction.

    Handles common research-paper forms that break simple parsers:
    - (214 ± 2) × 10^6 -> 214e6
    - (1.66 ± 0.02) × 10^14 -> 1.66e14
    - 3 × 10−7 -> 3e-7
    - 10−7 / 10–7 -> 10^-7
    - Q = 2,070 × 10^6 -> Q = 2070e6
    """
    if not text:
        return text
    t = text
    # Unicode cleanup from PDFs.
    t = (t.replace('−', '-')
           .replace('–', '-')
           .replace('—', '-')
           .replace('×', 'x')
           .replace('·', 'x')
           .replace('μ', 'µ'))
    # Remove spaces inside powers like 10 ^ 6.
    t = re.sub(r"10\s*\^\s*([+-]?\d+)", r"10^\1", t)
    # Convert unicode-style superscript-ish OCR minus is handled above.
    # (214 ± 2) x 10^6 -> 214e6. Also handles +/-, ±, ∓.
    t = re.sub(
        r"\(\s*([+-]?\d+(?:[\.,]\d+)?)\s*(?:±|\+/-|\+-|∓)\s*[+-]?\d+(?:[\.,]\d+)?\s*\)\s*(?:x|\*)\s*10\^?\s*([+-]?\d+)",
        lambda m: f"{m.group(1).replace(',', '')}e{m.group(2)}",
        t,
        flags=re.I,
    )
    # 214 ± 2 x 10^6 -> 214e6 (no parentheses)
    t = re.sub(
        r"\b([+-]?\d+(?:[\.,]\d+)?)\s*(?:±|\+/-|\+-|∓)\s*[+-]?\d+(?:[\.,]\d+)?\s*(?:x|\*)\s*10\^?\s*([+-]?\d+)",
        lambda m: f"{m.group(1).replace(',', '')}e{m.group(2)}",
        t,
        flags=re.I,
    )
    # Plain a x 10^b -> ae b.
    t = re.sub(
        r"\b([+-]?\d+(?:[\.,]\d+)?)\s*(?:x|\*)\s*10\^?\s*([+-]?\d+)\b",
        lambda m: f"{m.group(1).replace(',', '')}e{m.group(2)}",
        t,
        flags=re.I,
    )
    # 10-7 in contexts where PDF extraction dropped the caret: convert only when exponent is small.
    t = re.sub(r"\b10\s*([-+]\d{1,2})\b", r"10^\1", t)
    # Compact units separated by weird spaces remain okay; keep readable whitespace.
    return t



def parse_float(s: Any) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    raw = str(s).replace(",", "").strip()
    raw = raw.replace("×", "x").replace("·", "x").replace("–", "-")
    raw = re.sub(r"\s+", " ", raw)

    # Handles PDF/OCR forms: 10 5 -> 10^5; 4 x 10 5 -> 4e5; 4 3 10 5 -> 4e5.
    m = re.match(r"^10\s*([+-]?\d{1,2})$", raw)
    if m:
        return 10 ** int(m.group(1))
    m = re.match(r"^([+-]?[0-9]*\.?[0-9]+)\s*(?:x|\*)\s*10\s*\^?\s*([+-]?\d+)$", raw, re.I)
    if m:
        return float(m.group(1)) * (10 ** int(m.group(2)))
    m = re.match(r"^([+-]?[0-9]*\.?[0-9]+)\s+3\s+10\s*([+-]?\d{1,2})$", raw)
    if m:
        return float(m.group(1)) * (10 ** int(m.group(2)))

    compact = re.sub(r"\s+", "", raw)
    sci = re.match(r"^([+-]?[0-9]*\.?[0-9]+)(?:x|\*)?10\^?([+-]?\d+)$", compact, re.I)
    if sci:
        return float(sci.group(1)) * (10 ** int(sci.group(2)))
    sci2 = re.match(r"^10\^?([+-]?\d+)$", compact, re.I)
    if sci2:
        return 10 ** int(sci2.group(1))
    try:
        return float(compact)
    except ValueError:
        return None


def normalize_nullable_number(v: Any) -> Optional[float]:
    val = parse_float(v)
    if val is None or not math.isfinite(val):
        return None
    return val


def nearby_quote(text: str, start: int, end: int, window: int = 260) -> str:
    q = text[max(0, start - window): min(len(text), end + window)]
    q = re.sub(r"\s+", " ", q).strip()
    return q[:900]


def basic_metadata(text: str) -> Dict[str, Any]:
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 4]
    title = "Uploaded paper"
    # Pick first line that is not a page marker or copyright line.
    for l in lines[:25]:
        if not l.startswith("--- PAGE") and "copyright" not in l.lower() and len(l) > 12:
            title = l[:200]
            break
    doi = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.I)
    year = re.search(r"\b(19|20)\d{2}\b", text)
    return {
        "title": title,
        "authors": "",
        "year": int(year.group(0)) if year else None,
        "doi": doi.group(0).rstrip(".,;)") if doi else "",
    }


def infer_type_material(text: str) -> Dict[str, str]:
    low = " " + text.lower() + " "
    types = [
        ("SAW", ["surface acoustic wave", " saw "]),
        ("BAW", ["bulk acoustic wave", " baw "]),
        ("HBAR", ["hbar", "high-overtone bulk acoustic"]),
        ("FBAR", ["fbar", "film bulk acoustic"]),
        ("MEMS", ["mems", "microelectromechanical"]),
        ("Optomechanical crystal", ["optomechanical crystal"]),
        ("Phononic crystal", ["phononic crystal"]),
        ("Nanomechanical", ["nanomechanical"]),
        ("Optomechanical", ["optomechanical"]),
    ]
    platform = "Mechanical oscillator"
    for label, needles in types:
        if any(n in low for n in needles):
            platform = label
            break
    material_names = [
        ("Silicon nitride", ["silicon nitride", "si3n4"]),
        ("Silicon", ["silicon", "si "]),
        ("Sapphire", ["sapphire"]),
        ("Quartz", ["quartz"]),
        ("Aluminum nitride", ["aluminum nitride", "aln"]),
        ("Diamond", ["diamond"]),
        ("Gallium arsenide", ["gallium arsenide", "gaas"]),
        ("Lithium niobate", ["lithium niobate", "linbo3"]),
        ("Silica", ["sio2", "silica"]),
    ]
    found = []
    for label, needles in material_names:
        if any(n in low for n in needles):
            found.append(label)
    platform_type = infer_platform_type_from_text(text, platform)
    return {"oscillator_type": platform, "platform_type": platform_type, "platform": platform, "material": ", ".join(dict.fromkeys(found[:4]))}


PLATFORM_TYPES = [
    "MEMS",
    "NEMS / Nanomechanical",
    "Optomechanical",
    "Phononic crystal",
    "SAW",
    "BAW",
    "FBAR",
    "HBAR",
    "Levitated",
    "Quantum acoustic",
    "Other",
]


def infer_platform_type_from_text(text: str, fallback: str = "") -> str:
    low = " " + (text or "").lower() + " "
    # Order matters: choose the most specific category first.
    rules = [
        ("FBAR", ["fbar", "film bulk acoustic"]),
        ("HBAR", ["hbar", "high-overtone bulk acoustic", "high overtone bulk acoustic"]),
        ("BAW", ["bulk acoustic wave", " baw "]),
        ("SAW", ["surface acoustic wave", " saw ", "rayleigh surface acoustic"]),
        ("Levitated", ["levitated", "optical trap", "optically trapped", "trapped nanoparticle"]),
        ("Optomechanical", ["optomechanical", "cavity optomechanics", "radiation pressure"]),
        ("Phononic crystal", ["phononic crystal", "phononic bandgap", "soft clamping"]),
        ("MEMS", ["mems", "microelectromechanical", "microresonator", "micromechanical"]),
        ("NEMS / Nanomechanical", ["nems", "nanomechanical", "nanoelectromechanical"]),
        ("Quantum acoustic", ["quantum acoustic", "spin-acoustic", "surface acoustic wave resonator", "superconducting qubit"]),
    ]
    for label, needles in rules:
        if any(n in low for n in needles):
            return label
    fb = (fallback or "").strip()
    return fb if fb in PLATFORM_TYPES else "Other"


def normalize_platform_type(value: str, source_text: str = "", fallback: str = "") -> str:
    raw = (value or "").strip()
    aliases = {
        "nanomechanical": "NEMS / Nanomechanical",
        "nems": "NEMS / Nanomechanical",
        "optomechanical crystal": "Optomechanical",
        "surface acoustic wave": "SAW",
        "bulk acoustic wave": "BAW",
        "film bulk acoustic resonator": "FBAR",
        "levitated nanoparticle": "Levitated",
        "mechanical oscillator": "Other",
    }
    if raw in PLATFORM_TYPES:
        return raw
    low = raw.lower()
    if low in aliases:
        return aliases[low]
    return infer_platform_type_from_text((raw + " " + source_text), fallback=fallback)


def select_relevant_chunks(text: str, max_chunks: int = 10, chunk_size: int = 4200, overlap: int = 500) -> List[str]:
    compact = re.sub(r"\n{3,}", "\n\n", text)
    chunks = []
    i = 0
    while i < len(compact):
        chunks.append(compact[i:i+chunk_size])
        i += chunk_size - overlap
    keywords = [
        "mechanical", "resonance", "resonator", "frequency", "quality", "Q-factor", "Qm", "linewidth",
        "damping", "gamma", "γ", "T1", "lifetime", "ringdown", "GHz", "MHz", "kHz", "phononic",
        "optomechanical", "bath temperature", "temperature", "fQ", "frequency-quality"
    ]
    scored = []
    for idx, ch in enumerate(chunks):
        low = ch.lower()
        score = sum(low.count(k.lower()) for k in keywords)
        # Strongly prefer chunks that contain numbers + units
        score += 5 * len(re.findall(r"\b\d+(?:\.\d+)?\s*(?:khz|mhz|ghz|thz|hz|mk|k|µs|us|ms)\b", low))
        if idx == 0:
            score += 8
        scored.append((score, idx, ch))
    scored.sort(reverse=True)
    selected = [ch for score, idx, ch in scored[:max_chunks] if score > 0]
    return selected or chunks[:max_chunks]



def strip_reference_noise(text: str) -> str:
    """Remove citation/reference markers that often get mistaken for measurements."""
    cleaned = re.sub(r"\[[0-9,\s\-]+\]", " ", text)
    cleaned = re.sub(r"\brefs?\.?\s*\d+\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def high_value_q_candidates(text: str) -> List[Tuple[float, str]]:
    """Find plausible quality-factor values with strict context.

    The old extractor was too permissive and could treat citation numbers such as
    [41], Qudi, Q-circle method references, equation numbers, or figure labels as Q.
    This accepts a Q only when the nearby wording explicitly refers to measured
    resonator quality factor.
    """
    compact = strip_reference_noise(text.replace("×", "x").replace("·", "x"))
    number = r"(?:[0-9][0-9,.]*(?:e[+-]?\d+)|[0-9][0-9,.]*\s*(?:x|\*)\s*10\s*\^?\s*[+-]?\d+|[0-9][0-9,.]*\s*x\s*10[+-]?\d+|[0-9][0-9,.]*|10\s*\^?\s*[-+]?\d+)"
    patterns = [
        rf"(?:quality\s+factors?|q[-\s]?factors?|mechanical\s+q[-\s]?factor|resonator\s+q)[^.;:]{{0,160}}?(?:Q\s*)?(?:=|≈|~|of|reach(?:es|ed)?|up\s+to|as\s+high\s+as|approach(?:es|ing)?|exceed(?:s|ing)?)\s*({number})",
        rf"\bQ\s*(?:max|m)?\s*(?:=|≈|~|of)\s*({number})",
        rf"\bQ\s*(?:values?\s*)?(?:of|around|approximately|about)\s*({number})",
    ]
    out: List[Tuple[float, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, compact, flags=re.I):
            ctx = nearby_quote(compact, m.start(), m.end(), window=180)
            low = ctx.lower()
            if ("qudi" in low or "grant" in low or "equation" in low) and not any(w in low for w in ["quality", "q-factor", "resonance", "resonator"]):
                continue
            val = parse_float(m.group(1))
            if val and 100 <= val <= 1e12:
                out.append((val, ctx))
            # Capture list continuation after first Q, e.g. "Q ≈ 5.5x10^3, 7.8x10^3, and 8.5x10^3".
            tail = compact[m.end():m.end()+140]
            for nm in re.finditer(number, tail, flags=re.I):
                v2 = parse_float(nm.group(0))
                if v2 and 100 <= v2 <= 1e12:
                    out.append((v2, nearby_quote(compact, m.start(), m.end()+nm.end(), window=180)))
    dedup: List[Tuple[float, str]] = []
    seen = set()
    for v, c in out:
        key = round(v, 6)
        if key not in seen:
            seen.add(key)
            dedup.append((v, c))
    return dedup


def relevant_frequency_candidates(text: str) -> List[Tuple[float, str]]:
    compact = strip_reference_noise(text.replace("Ω", "omega").replace("ω", "omega"))
    number = r"[0-9][0-9,.]*(?:e[+-]?\d+|\s*(?:x|\*)\s*10\s*\^?\s*[+-]?\d+)?"
    patterns = [
        rf"(?:mechanical|acoustic|SAW|surface acoustic wave|resonance|resonator|mode|transition)[^.;:]{{0,140}}?({number})\s*(Hz|kHz|MHz|GHz|THz)",
        rf"(?:omega_m|vm|f_m)\s*/\s*2\s*(?:pi|π|p)\s*(?:=|≈|~|of|is)?\s*({number})\s*(Hz|kHz|MHz|GHz|THz)",
        rf"({number})\s*(Hz|kHz|MHz|GHz|THz)[^.;:]{{0,100}}?(?:mechanical|acoustic|SAW|surface acoustic wave|resonance|resonator|mode|transition)",
    ]
    out: List[Tuple[float, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, compact, flags=re.I):
            val = parse_float(m.group(1))
            unit = m.group(2).lower()
            if val:
                hz = val * UNIT_MULTIPLIERS[unit]
                if 1 <= hz <= 1e13:
                    out.append((hz, nearby_quote(compact, m.start(), m.end(), window=180)))
    dedup=[]; seen=set()
    for v,c in out:
        key=round(v, 3)
        if key not in seen:
            seen.add(key); dedup.append((v,c))
    return dedup



def strip_references_section(text: str) -> str:
    """Drop bibliography/reference sections before extraction.

    Reference lists contain many distracting temperatures from other papers
    (for example "below 5 mK" in a cited title). Those are not the experiment
    temperature for the uploaded paper.
    """
    if not text:
        return text
    markers = [
        r"\n\s*references?\s*\n",
        r"\n\s*reference\s*\n",
        r"\n\s*bibliography\s*\n",
        r"\n\s*works cited\s*\n",
    ]
    cut = len(text)
    for pat in markers:
        m = re.search(pat, text, flags=re.I)
        if m:
            cut = min(cut, m.start())
    return text[:cut]


def robust_temperature_from_text(text: str) -> Tuple[Optional[float], str]:
    """Return the best experiment temperature in kelvin, with evidence.

    Core rule:
    - exact experiment temperatures beat generic room-temperature mentions
    - room temperature becomes 300 K only when no better experiment temp exists
    - references/comparison literature are downranked or removed
    """
    if not text:
        return None, ""

    full = normalize_scientific_text(text)
    # Join words split by PDF line wrapping, e.g. "experi- ments" -> "experiments".
    full = re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1\2", full)
    compact = re.sub(r"\s+", " ", strip_references_section(full))
    low_all = compact.lower()

    def convert_temp(value: str, unit: str) -> Optional[float]:
        val = parse_float(value)
        if val is None:
            return None
        u = unit.lower().replace("°", "")
        if u == "mk":
            return val / 1000.0
        if u in ("k", "kelvin"):
            return val
        if u in ("c", "°c"):
            return val + 273.15
        return None

    candidates: List[Tuple[int, float, int, int, str]] = []

    # These are high-confidence contexts for the actual experiment/sample.
    patterns = [
        (260, r"(?:all\s+)?reported\s+experiments?\s+were\s+conducted[^.;]{0,180}?\(?\s*T\s*[≈=~]?\s*([0-9][0-9,.]*)\s*(mK|K)"),
        (245, r"experiments?\s+were\s+conducted[^.;]{0,180}?\(?\s*T\s*[≈=~]?\s*([0-9][0-9,.]*)\s*(mK|K)"),
        (235, r"temperature\s+of\s+this\s+experiment[^.;]{0,120}?\(?\s*T\s*[≈=~]?\s*([0-9][0-9,.]*)\s*(mK|K)"),
        (255, r"\bT\s*[≈=~]\s*([0-9][0-9,.]*)\s*(mK|K)[^.;]{0,220}?(?:all\s+reported\s+experiments?|reported\s+experiments?|experiments?)\s+were\s+conducted"),
        (225, r"(?:environment|environmental|bath|thermal\s+bath|sample|sample\s+mount|cryogenic)\s+temperature[^.;]{0,140}?\(?\s*T?\s*[≈=~:=]?\s*([0-9][0-9,.]*)\s*(mK|K)"),
        (215, r"(?:cooled\s+to|cooling\s+to|down\s+to|held\s+at|operated\s+at|measured\s+at|at)\s*([0-9][0-9,.]*)\s*(mK|K)\b[^.;]{0,120}?(?:experiment|measurement|resonator|device|sample|mode|membrane|cryostat|temperature)"),
        (195, r"(?:experiment|measurement|resonator|device|sample|mode|membrane|system)[^.;]{0,160}?\b(?:at|to|T\s*[≈=~])\s*([0-9][0-9,.]*)\s*(mK|K)"),
        (165, r"\bT\s*[≈=~]\s*([0-9][0-9,.]*)\s*(mK|K)"),
        (130, r"(?:temperature\s+ranging\s+from|temperature\s+range\s+from)\s*([0-9][0-9,.]*)\s*(?:to|-)\s*([0-9][0-9,.]*)\s*(°?C|C|K)"),
    ]

    bad_context = [
        "reference", " ref.", " refs", "previous", "prior", "other work", "other works",
        "state-of-the-art", "reported in ref", "cited", "citation", "literature",
        "prospect", "could", "would", "future", "outlook", "if our system", "were made",
        "ultracold atoms", "trapped ion", "cantilever", "nanospheres", "comparison",
    ]
    good_context = [
        "our experiment", "this experiment", "our device", "our resonator", "our oscillator",
        "our system", "reported experiments were conducted", "experiments were conducted",
        "measured", "measurement", "sample", "device", "resonator", "membrane",
        "thermal bath", "environment", "cryogenic", "lab environment", "open lab environment",
        "peltier", "temperature coefficient",
    ]

    for base_score, pat in patterns:
        for m in re.finditer(pat, compact, flags=re.I):
            # Temperature range pattern: use approximate room temp, not low endpoint.
            if len(m.groups()) == 3:
                v1 = parse_float(m.group(1)); v2 = parse_float(m.group(2)); unit = m.group(3)
                if v1 is None or v2 is None:
                    continue
                mid = (v1 + v2) / 2.0
                temp_k = convert_temp(str(mid), unit)
            else:
                temp_k = convert_temp(m.group(1), m.group(2))
            if temp_k is None or not math.isfinite(temp_k) or temp_k <= 0:
                continue

            ctx = compact[max(0, m.start() - 260): min(len(compact), m.end() + 260)]
            ctx_low = ctx.lower()
            score = base_score

            if any(w in ctx_low for w in good_context):
                score += 60
            if any(w in ctx_low for w in bad_context):
                score -= 130

            # Avoid grabbing temperatures from formulas/constants rather than sample conditions.
            if any(w in ctx_low for w in ["kbt", "boltzmann", "noise temperature reference"]):
                score -= 40

            # Strong special cases.
            if "all reported experiments were conducted" in ctx_low:
                score += 260
            elif "reported experiments were conducted" in ctx_low:
                score += 220
            if "temperature of this experiment" in ctx_low:
                score += 90
            if "open lab environment" in ctx_low or "temperature coefficient" in ctx_low:
                score += 50

            candidates.append((score, temp_k, m.start(), m.end(), ctx))

    # Room temperature is a candidate, but not allowed to beat a strong exact experimental temperature.
    room_match = re.search(r"\b(room[-\s]?temperature|at\s+room\s+temperature|ambient\s+temperature|\bRT\b)\b", compact, re.I)
    if room_match:
        ctx = compact[max(0, room_match.start() - 260): min(len(compact), room_match.end() + 260)]
        ctx_low = ctx.lower()
        score = 185
        # Title/abstract/main result room-temperature papers should become 300 K.
        if room_match.start() < min(len(compact), 4500):
            score += 90
        if any(w in ctx_low for w in ["our", "device", "resonator", "oscillator", "experiment", "measurement", "at room temperature"]):
            score += 70
        if any(w in ctx_low for w in bad_context):
            score -= 120
        candidates.append((score, 300.0, room_match.start(), room_match.end(), ctx))

    if not candidates:
        return None, ""

    # Pick highest contextual score, not smallest numerical temperature.
    best = max(candidates, key=lambda x: x[0])
    # If the best evidence is weak and only cryogenic-looking from unrelated context, don't force it.
    if best[0] < 100:
        return None, ""
    return best[1], nearby_quote(compact, best[2], best[3], window=240)


def validate_against_source(record: Dict[str, Any], source_text: str) -> Dict[str, Any]:
    """Correct common extraction mistakes by checking source-supported candidates."""
    source_text = normalize_scientific_text(source_text or "")
    if not source_text:
        return record
    notes = record.get("notes") or ""
    q_cands = high_value_q_candidates(source_text)
    f_cands = relevant_frequency_candidates(source_text)

    q = record.get("quality_factor")
    if q_cands:
        best_q, q_ev = max(q_cands, key=lambda x: x[0])
        if (q is None) or (q < 100 and best_q >= 1000) or (best_q >= 1000 and q and abs(q - best_q) / best_q > 0.75):
            record["quality_factor"] = best_q
            if q and q != best_q:
                notes += f" Rejected extracted Q={q:g} as likely citation/notation noise; replaced with source-supported Q={best_q:g}."
            else:
                notes += f" Q selected from strict quality-factor context: {best_q:g}."
            if not record.get("evidence_quote") or "No reliable" in record.get("evidence_quote", ""):
                record["evidence_quote"] = q_ev

    f = record.get("frequency_hz")
    if f_cands:
        plausible = [x for x in f_cands if 1e3 <= x[0] <= 1e11]
        chosen_f, f_ev = plausible[0] if plausible else f_cands[0]
        if f is None or f < 1e3 or f > 1e13:
            record["frequency_hz"] = chosen_f
            notes += f" Frequency selected from strict acoustic/mechanical context: {chosen_f:g} Hz."
            if not record.get("evidence_quote") or "No reliable" in record.get("evidence_quote", ""):
                record["evidence_quote"] = f_ev

    f = record.get("frequency_hz")
    q = record.get("quality_factor")
    if f and q:
        record["fq_product"] = f * q
        record["t1_seconds"] = q / (2 * math.pi * f)
    record["notes"] = notes.strip()
    return record
def rule_based_extract(text: str) -> Dict[str, Any]:
    text = normalize_scientific_text(text)
    compact = re.sub(r"\s+", " ", text)
    result: Dict[str, Any] = {
        **basic_metadata(text),
        **infer_type_material(compact),
        "frequency_hz": None,
        "quality_factor": None,
        "fq_product": None,
        "t1_seconds": None,
        "temperature_k": None,
        "evidence_quote": "",
        "notes": "Rule-based fallback extraction. Review carefully before approving.",
        "confidence": "low",
        "extraction_method": "rule_fallback",
    }
    evidence_spans: List[Tuple[int, int]] = []

    freq_patterns = [
        r"(?:mechanical\s+)?(?:resonance|resonant|mode)?\s*(?:frequency|f_m|f\s*=|f\s+|[vωΩ][mM]?\s*/\s*2\s*(?:pi|π|p)|omega_m\s*/\s*2\s*(?:pi|π|p))\s*(?:of|occurs at|is|=|:|5)?\s*([0-9][0-9,\.]*(?:e[+-]?\d+)?|[0-9][0-9,\.]*\s*(?:x|\*|×)?\s*10\^?\s*[+-]?\d+)\s*(kHz|MHz|GHz|THz|Hz)",
        r"(?:at|near|around)\s*([0-9][0-9,\.]*(?:e[+-]?\d+)?)\s*(kHz|MHz|GHz|THz|Hz)\s*(?:mechanical|acoustic|breathing|SAW|resonance|mode|transition)",
        r"([0-9][0-9,\.]*(?:e[+-]?\d+)?)\s*(kHz|MHz|GHz|THz|Hz)\s*(?:mechanical|acoustic|breathing|SAW|surface acoustic wave)\s*(?:resonance|mode|frequency|oscillator|transition)",
    ]
    for pat in freq_patterns:
        m = re.search(pat, compact, re.I)
        if m:
            val = parse_float(m.group(1))
            if val:
                result["frequency_hz"] = val * UNIT_MULTIPLIERS[m.group(2).lower()]
                evidence_spans.append((m.start(), m.end()))
                break

    # Mechanical damping gamma/2pi can imply Q = f / linewidth when Q text was mangled by PDF extraction.
    linewidth_hz = None
    damp = re.search(r"(?:intrinsic\s+)?mechanical\s+damping\s+rate\s+of\s*(?:c|γ|gamma)?i?\s*/\s*2\s*(?:pi|π|p)\s*(?:5|=|is)?\s*([0-9][0-9,\.]*)\s*(kHz|MHz|GHz|Hz)", compact, re.I)
    if damp:
        dv = parse_float(damp.group(1))
        if dv:
            linewidth_hz = dv * UNIT_MULTIPLIERS[damp.group(2).lower()]
            evidence_spans.append((damp.start(), damp.end()))

    q_num = r"(?:[0-9][0-9,.]*(?:e[+-]?\d+)|[0-9][0-9,.]*\s*(?:x|\*|×)?\s*10\s*\^?\s*[+-]?\d+|[0-9][0-9,.]*\s+3\s+10\s*[+-]?\d+|10\s*\^?\s*[-+]?\d+|10\s+\d+|[0-9][0-9,.]*)"
    q_patterns = [
        rf"mechanical Q-factor[^0-9]{{0,100}}({q_num})",
        rf"Qm\s*(?:<|>|≈|~|=|5)?\s*({q_num})",
        rf"Q-factor[^0-9]{{0,60}}({q_num})",
        rf"quality factor[^0-9]{{0,60}}({q_num})",
    ]
    for pat in q_patterns:
        m = re.search(pat, compact, re.I)
        if m:
            val = parse_float(m.group(1))
            if val and val > 1:
                result["quality_factor"] = val
                evidence_spans.append((m.start(), m.end()))
                break
    # If Q looked like 105 but frequency/linewidth imply 1e5, fix it and note.
    if result.get("frequency_hz") and linewidth_hz:
        q_from_linewidth = result["frequency_hz"] / linewidth_hz
        if not result.get("quality_factor") or result["quality_factor"] < 1000:
            result["quality_factor"] = q_from_linewidth
            result["notes"] += f" Q inferred from f/linewidth = {q_from_linewidth:.3g} because direct Q text may be mangled by PDF extraction."

    temp_val, temp_ev = robust_temperature_from_text(text)
    if temp_val is not None:
        result["temperature_k"] = temp_val
        if temp_ev:
            # Put temp evidence into the general evidence window later.
            pos = compact.lower().find(temp_ev[:40].lower())
            if pos >= 0:
                evidence_spans.append((pos, min(len(compact), pos + len(temp_ev))))


    t_patterns = [
        r"(?:energy relaxation time|T_?1|lifetime|ringdown time|decay time)\s*(?:of|is|=|:|≈|~)?\s*([0-9][0-9,\.]*)(?:\s*)(ns|µs|μs|us|ms|s)",
        r"([0-9][0-9,\.]*)(?:\s*)(ns|µs|μs|us|ms|s)\s*(?:energy relaxation time|T_?1|lifetime|ringdown time|decay time)",
    ]
    for pat in t_patterns:
        m = re.search(pat, compact, re.I)
        if m:
            val = parse_float(m.group(1))
            if val:
                result["t1_seconds"] = val * TIME_MULTIPLIERS[m.group(2).lower()]
                evidence_spans.append((m.start(), m.end()))
                break

    # Mechanical ringdown papers sometimes state amplitude ringdown time as 2τ.
    m2tau = re.search(r"(?:amplitude\s+)?ringdown\s+time\s*2\s*(?:τ|tau)\s*(?:=|is|of)?\s*([0-9][0-9,.]*(?:e[+-]?\d+)?)\s*(ns|µs|μs|us|ms|s)", compact, re.I)
    if m2tau and not result.get("t1_seconds"):
        val = parse_float(m2tau.group(1))
        if val:
            result["t1_seconds"] = 0.5 * val * TIME_MULTIPLIERS[m2tau.group(2).lower()]
            evidence_spans.append((m2tau.start(), m2tau.end()))
            result["notes"] += " T1 inferred as τ from reported amplitude ringdown time 2τ."

    if result.get("frequency_hz") and result.get("quality_factor"):
        result["fq_product"] = result.get("fq_product") or result["frequency_hz"] * result["quality_factor"]
        result["t1_seconds"] = result.get("t1_seconds") or result["quality_factor"] / (2 * math.pi * result["frequency_hz"])
        result["notes"] += " f·Q and/or T1 calculated from f and Q when not explicitly stated."

    if evidence_spans:
        start = min(s for s, e in evidence_spans)
        end = max(e for s, e in evidence_spans)
        result["evidence_quote"] = nearby_quote(compact, start, end, window=550)
    else:
        result["evidence_quote"] = "No reliable supporting quote found. Manual review required."
    filled = sum(result[k] is not None for k in ["frequency_hz", "quality_factor", "fq_product", "t1_seconds"])
    result["confidence"] = "medium" if filled >= 3 and evidence_spans else "low"
    return postprocess_record(result, text)


def postprocess_record(record: Dict[str, Any], source_text: str = "") -> Dict[str, Any]:
    defaults = {**basic_metadata(source_text), **infer_type_material(source_text)} if source_text else {}
    for k, v in defaults.items():
        if not record.get(k):
            record[k] = v
    for k in ["frequency_hz", "quality_factor", "fq_product", "t1_seconds", "temperature_k"]:
        record[k] = normalize_nullable_number(record.get(k))
    # Temperature must come from the source text, not from unrelated references or model guesses.
    # This intentionally overrides local-LLM output when a source-supported temperature is found.
    if source_text:
        robust_temp, robust_temp_ev = robust_temperature_from_text(source_text)
        if robust_temp is not None:
            old_temp = record.get("temperature_k")
            if old_temp is None or abs(float(old_temp) - robust_temp) / max(abs(robust_temp), 1e-12) > 0.05:
                record["temperature_k"] = robust_temp
                existing_notes = record.get("notes") or ""
                record["notes"] = (existing_notes + f" Temperature set/overridden from source-supported context: {robust_temp:g} K.").strip()
                if robust_temp_ev and not record.get("evidence_quote"):
                    record["evidence_quote"] = robust_temp_ev
    if record.get("year"):
        try:
            record["year"] = int(float(record["year"]))
        except Exception:
            record["year"] = defaults.get("year")
    # Physics consistency checks.
    notes = record.get("notes") or ""
    f = record.get("frequency_hz")
    q = record.get("quality_factor")
    if f and q:
        computed_fq = f * q
        if not record.get("fq_product") or abs(record["fq_product"] - computed_fq) / max(computed_fq, 1) > 0.25:
            record["fq_product"] = computed_fq
            notes += " f·Q calculated/overridden using f × Q."
        computed_t1 = q / (2 * math.pi * f)
        if not record.get("t1_seconds") or abs(record["t1_seconds"] - computed_t1) / max(computed_t1, 1e-30) > 0.50:
            record["t1_seconds"] = computed_t1
            notes += " T1 calculated/overridden using Q/(2πf)."
    # Avoid one-character weird values.
    record["platform_type"] = normalize_platform_type(record.get("platform_type") or record.get("oscillator_type") or record.get("platform"), source_text, record.get("platform") or "")
    for k in ["title", "authors", "doi", "oscillator_type", "platform_type", "material", "platform", "evidence_quote"]:
        if record.get(k) is None:
            record[k] = ""
    record["notes"] = notes.strip()
    record = validate_against_source(record, source_text) if source_text else record
    return record

# -------------------------
# Free local AI extraction through Ollama
# -------------------------
OSC_SCHEMA_HELP = """
Return ONLY valid JSON with these fields:
{
  "title": string|null,
  "authors": string|null,
  "year": integer|null,
  "doi": string|null,
  "oscillator_type": string|null,
  "platform_type": "MEMS"|"NEMS / Nanomechanical"|"Optomechanical"|"Phononic crystal"|"SAW"|"BAW"|"FBAR"|"HBAR"|"Levitated"|"Quantum acoustic"|"Other"|null,
  "material": string|null,
  "platform": string|null,
  "frequency_hz": number|null,
  "quality_factor": number|null,
  "fq_product": number|null,
  "t1_seconds": number|null,
  "temperature_k": number|null,
  "evidence_quote": string,
  "notes": string,
  "confidence": "low"|"medium"|"high"
}
"""


def local_llm_extract_if_available(text: str) -> Optional[Dict[str, Any]]:
    """Use a local Ollama model if it is running. No paid API required."""
    import urllib.request

    ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    chunks = select_relevant_chunks(text, max_chunks=8, chunk_size=3600, overlap=450)
    context = "\n\n".join(f"[CHUNK {i+1}]\n{c}" for i, c in enumerate(chunks))
    metadata = basic_metadata(text)
    prompt = f"""
You are an expert scientific extractor for mechanical harmonic oscillator papers.

Extract the BEST primary mechanical/acoustic oscillator record from the supplied text.
This is for a database of mechanical harmonic oscillators.

{OSC_SCHEMA_HELP}

Rules:
1. Extract only mechanical/acoustic oscillator values, not optical cavity Q, laser frequencies, reference numbers, equation numbers, or unrelated literature citations.
2. Convert all units to SI: frequency_hz in Hz, temperature_k in K, t1_seconds in seconds.
3. Understand notation: Ωm/2π, omega_m/2pi, f_m, resonance frequency, mode frequency, SAW frequency, acoustic frequency all can mean mechanical frequency.
3b. Choose a standardized platform_type such as MEMS, NEMS / Nanomechanical, Optomechanical, Phononic crystal, SAW, BAW, FBAR, HBAR, Levitated, Quantum acoustic, or Other. Use platform for the more specific device description.
4. Understand Q notation: Q, Qm, Q-factor, quality factor, ringdown Q. Parse uncertainty notation like (214 ± 2) × 10^6 as 214e6. Do NOT confuse Qudi, Q-circle method references, citation [41], figure numbers, or equation numbers with quality factor.
5. If a paper says room temperature but gives no exact temperature, set temperature_k = 300 and note it was assumed from room temperature.
6. If f and Q are present, calculate fq_product = f*Q and t1_seconds = Q/(2*pi*f), unless explicit values are clearly provided. If a paper reports amplitude ringdown time 2τ, use τ as energy relaxation time.
7. For papers with many devices, choose the best/highest-Q measured device unless the text clearly identifies a primary device.
8. Use null for genuinely missing values. Do not invent.
9. evidence_quote must quote or closely quote the supplied text that supports the values.
10. Return JSON only. No markdown. No explanation outside JSON.

Weak metadata guess from parser:
{json.dumps(metadata)}

PAPER TEXT CHUNKS:
{context}
"""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": 8192}
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            ollama_url + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=int(os.environ.get("OLLAMA_TIMEOUT", "180"))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data.get("response", "{}").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.I).strip()
        if not raw.startswith("{"):
            m = re.search(r"\{.*\}", raw, flags=re.S)
            raw = m.group(0) if m else "{}"
        extracted = json.loads(raw)
        extracted["extraction_method"] = f"local_ollama:{model}"
        return postprocess_record(extracted, text)
    except Exception as e:
        print("Local Ollama extraction unavailable/failed; using rule-based extractor:", e)
        return None


def free_extract(text: str) -> Dict[str, Any]:
    """No-paid strategy: local LLM + rule extraction + strict validator."""
    text = normalize_scientific_text(text)
    local = local_llm_extract_if_available(text)
    if local:
        rules = rule_based_extract(text)
        for k in ["frequency_hz", "quality_factor", "fq_product", "t1_seconds", "temperature_k"]:
            if local.get(k) is None and rules.get(k) is not None:
                local[k] = rules[k]
        if not local.get("evidence_quote") or "No reliable" in str(local.get("evidence_quote")):
            local["evidence_quote"] = rules.get("evidence_quote", "")
        local["notes"] = ((local.get("notes") or "") + " Free local extraction using Ollama plus rule validation. Review/edit values before relying on them.").strip()
        return postprocess_record(local, text)
    return rule_based_extract(text)

def extract_record(text: str) -> Dict[str, Any]:
    return free_extract(text)

# -------------------------
# Routes
# -------------------------
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    platform_filter = request.args.get("platform_type", "").strip()
    params = []
    where = []
    if q:
        where.append("(title LIKE ? OR oscillator_type LIKE ? OR platform_type LIKE ? OR material LIKE ? OR platform LIKE ? OR doi LIKE ?)")
        like = f"%{q}%"
        params.extend([like] * 6)
    if platform_filter:
        where.append("platform_type = ?")
        params.append(platform_filter)
    sql = "SELECT * FROM oscillator_records"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with db() as conn:
        records = conn.execute(sql, params).fetchall()
        platform_rows = conn.execute(
            "SELECT DISTINCT platform_type FROM oscillator_records WHERE platform_type IS NOT NULL AND platform_type != '' ORDER BY platform_type"
        ).fetchall()
    available_platforms = [row[0] for row in platform_rows]
    for ptype in PLATFORM_TYPES:
        if ptype not in available_platforms:
            available_platforms.append(ptype)
    return render_template("index.html", records=records, q=q, platform_filter=platform_filter, platform_types=available_platforms)


@app.route("/visualize")
def visualize():
    platform_filter = request.args.get("platform_type", "").strip()
    params = []
    where = ["frequency_hz IS NOT NULL"]
    if platform_filter:
        where.append("platform_type = ?")
        params.append(platform_filter)
    sql = "SELECT id, title, year, platform_type, platform, material, frequency_hz, quality_factor, fq_product, t1_seconds, temperature_k FROM oscillator_records"
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY frequency_hz ASC"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        platform_rows = conn.execute(
            "SELECT DISTINCT platform_type FROM oscillator_records WHERE platform_type IS NOT NULL AND platform_type != '' ORDER BY platform_type"
        ).fetchall()
    platform_types = [row[0] for row in platform_rows]
    for ptype in PLATFORM_TYPES:
        if ptype not in platform_types:
            platform_types.append(ptype)
    chart_data = []
    for r in rows:
        chart_data.append({
            "id": r["id"],
            "title": r["title"] or "Untitled paper",
            "year": r["year"],
            "platform_type": r["platform_type"] or "Other",
            "platform": r["platform"] or "",
            "material": r["material"] or "",
            "frequency_hz": r["frequency_hz"],
            "quality_factor": r["quality_factor"],
            "fq_product": r["fq_product"],
            "t1_seconds": r["t1_seconds"],
            "temperature_k": r["temperature_k"],
        })
    return render_template(
        "visualize.html",
        records=rows,
        chart_data=chart_data,
        platform_filter=platform_filter,
        platform_types=platform_types,
    )


@app.route("/record/<int:record_id>", methods=["GET", "POST"])
def record(record_id):
    if request.method == "POST":
        fields = [
            "title", "authors", "year", "doi", "source_url", "oscillator_type", "platform_type", "material", "platform",
            "frequency_hz", "quality_factor", "fq_product", "t1_seconds", "temperature_k", "evidence_quote", "notes", "validation_status"
        ]
        data = {f: request.form.get(f) or None for f in fields}
        for numeric in ["year", "frequency_hz", "quality_factor", "fq_product", "t1_seconds", "temperature_k"]:
            if data[numeric] not in (None, ""):
                try:
                    data[numeric] = float(data[numeric]) if numeric != "year" else int(float(data[numeric]))
                except ValueError:
                    data[numeric] = None
        if data.get("frequency_hz") and data.get("quality_factor"):
            data["fq_product"] = data.get("fq_product") or data["frequency_hz"] * data["quality_factor"]
            data["t1_seconds"] = data.get("t1_seconds") or data["quality_factor"] / (2 * math.pi * data["frequency_hz"])
        set_clause = ", ".join([f"{f} = :{f}" for f in fields])
        data["id"] = record_id
        with db() as conn:
            conn.execute(f"UPDATE oscillator_records SET {set_clause} WHERE id = :id", data)
        flash("Record saved.")
        return redirect(url_for("record", record_id=record_id))

    with db() as conn:
        row = conn.execute("SELECT * FROM oscillator_records WHERE id = ?", (record_id,)).fetchone()
    if not row:
        return "Record not found", 404
    return render_template("record.html", r=row)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        file = request.files.get("paper")
        source_url = ""
        if not file or not file.filename:
            flash("Choose a PDF or text file first.")
            return redirect(url_for("upload"))
        filename = secure_filename(file.filename)
        path = UPLOAD_DIR / f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
        file.save(path)
        try:
            text = extract_text_from_file(path)
            if len(text.strip()) < 100:
                raise RuntimeError("Very little text was extracted. Try uploading a text version or OCR'd PDF.")
            extracted = extract_record(text)
        except Exception as e:
            flash(f"Extraction failed: {e}")
            return redirect(url_for("upload"))

        row = {
            "title": extracted.get("title") or "Uploaded paper",
            "authors": extracted.get("authors") or "",
            "year": extracted.get("year"),
            "doi": extracted.get("doi") or "",
            "source_url": source_url,
            "filename": path.name,
            "oscillator_type": extracted.get("oscillator_type") or "",
            "platform_type": extracted.get("platform_type") or normalize_platform_type(extracted.get("oscillator_type") or extracted.get("platform"), text, extracted.get("platform") or ""),
            "material": extracted.get("material") or "",
            "platform": extracted.get("platform") or "",
            "frequency_hz": extracted.get("frequency_hz"),
            "quality_factor": extracted.get("quality_factor"),
            "fq_product": extracted.get("fq_product"),
            "t1_seconds": extracted.get("t1_seconds"),
            "temperature_k": extracted.get("temperature_k"),
            "evidence_quote": extracted.get("evidence_quote") or "",
            "notes": extracted.get("notes") or "",
            "validation_status": "approved",
            "extraction_json": json.dumps(extracted, indent=2),
        }
        with db() as conn:
            cols = ", ".join(row.keys())
            qs = ", ".join([":" + k for k in row.keys()])
            cur = conn.execute(f"INSERT INTO oscillator_records ({cols}) VALUES ({qs})", prepare_sqlite_row(row))
            new_id = cur.lastrowid
        flash("Paper uploaded and extracted. Edit the values below if needed.")
        return redirect(url_for("record", record_id=new_id))
    return render_template("upload.html", llm_enabled=True, model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        record_id = int(request.form["id"])
        fields = [
            "title", "authors", "year", "doi", "source_url", "oscillator_type", "platform_type", "material", "platform",
            "frequency_hz", "quality_factor", "fq_product", "t1_seconds", "temperature_k", "evidence_quote", "notes", "validation_status"
        ]
        data = {f: request.form.get(f) or None for f in fields}
        for numeric in ["year", "frequency_hz", "quality_factor", "fq_product", "t1_seconds", "temperature_k"]:
            if data[numeric] not in (None, ""):
                try:
                    data[numeric] = float(data[numeric]) if numeric != "year" else int(float(data[numeric]))
                except ValueError:
                    data[numeric] = None
        # Recalculate if reviewer edited f or Q and left derived fields blank.
        if data.get("frequency_hz") and data.get("quality_factor"):
            if not data.get("fq_product"):
                data["fq_product"] = data["frequency_hz"] * data["quality_factor"]
            if not data.get("t1_seconds"):
                data["t1_seconds"] = data["quality_factor"] / (2 * math.pi * data["frequency_hz"])
        set_clause = ", ".join([f"{f} = :{f}" for f in fields])
        data["id"] = record_id
        with db() as conn:
            conn.execute(f"UPDATE oscillator_records SET {set_clause} WHERE id = :id", data)
        flash("Record saved.")
        return redirect(url_for("admin"))

    with db() as conn:
        records = conn.execute("SELECT * FROM oscillator_records ORDER BY validation_status='pending' DESC, created_at DESC").fetchall()
    return render_template("admin.html", records=records)


@app.route("/delete/<int:record_id>", methods=["POST"])
def delete_record(record_id):
    with db() as conn:
        row = conn.execute("SELECT filename FROM oscillator_records WHERE id = ?", (record_id,)).fetchone()
        conn.execute("DELETE FROM oscillator_records WHERE id = ?", (record_id,))
    # Best-effort cleanup of uploaded file; ignore failures.
    try:
        if row and row["filename"]:
            path = UPLOAD_DIR / row["filename"]
            if path.exists():
                path.unlink()
    except Exception:
        pass
    flash("Record deleted.")
    return redirect(url_for("index"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)



@app.route("/debug/<int:record_id>")
def debug(record_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM oscillator_records WHERE id = ?", (record_id,)).fetchone()
    if not row:
        return "Record not found", 404
    upload_text = ""
    if row["filename"]:
        path = UPLOAD_DIR / row["filename"]
        if path.exists():
            try:
                upload_text = extract_text_from_file(path)[:12000]
            except Exception as e:
                upload_text = f"Could not re-read file: {e}"
    return render_template("debug.html", r=row, upload_text=upload_text)

@app.route("/extract-text", methods=["GET", "POST"])
def extract_text_direct():
    if request.method == "POST":
        text = request.form.get("paper_text", "")
        source_url = ""
        if len(text.strip()) < 100:
            flash("Paste more paper text first.")
            return redirect(url_for("extract_text_direct"))
        extracted = extract_record(text)
        row = {
            "title": extracted.get("title") or "Pasted paper",
            "authors": extracted.get("authors") or "",
            "year": extracted.get("year"),
            "doi": extracted.get("doi") or "",
            "source_url": source_url,
            "filename": "",
            "oscillator_type": extracted.get("oscillator_type") or "",
            "platform_type": extracted.get("platform_type") or normalize_platform_type(extracted.get("oscillator_type") or extracted.get("platform"), text, extracted.get("platform") or ""),
            "material": extracted.get("material") or "",
            "platform": extracted.get("platform") or "",
            "frequency_hz": extracted.get("frequency_hz"),
            "quality_factor": extracted.get("quality_factor"),
            "fq_product": extracted.get("fq_product"),
            "t1_seconds": extracted.get("t1_seconds"),
            "temperature_k": extracted.get("temperature_k"),
            "evidence_quote": extracted.get("evidence_quote") or "",
            "notes": extracted.get("notes") or "",
            "validation_status": "approved",
            "extraction_json": json.dumps(extracted, indent=2),
        }
        with db() as conn:
            cols = ", ".join(row.keys())
            qs = ", ".join([":" + k for k in row.keys()])
            cur = conn.execute(f"INSERT INTO oscillator_records ({cols}) VALUES ({qs})", prepare_sqlite_row(row))
            new_id = cur.lastrowid
        flash("Text extracted. Edit the values below if needed.")
        return redirect(url_for("record", record_id=new_id))
    return render_template("extract_text.html", llm_enabled=True, model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct"))

@app.template_filter("sci")
def sci(v):
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.3e}"
    except Exception:
        return str(v)


@app.template_filter("jsonpretty")
def jsonpretty(v):
    try:
        return json.dumps(json.loads(v), indent=2)
    except Exception:
        return v or ""


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
