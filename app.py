"""
Hermes Rechnung PDF Parser Server
==================================
Micro-server Flask care extrage corect pozițiile din PDF-urile Hermes
folosind pdfplumber (detectare reală a tabelului din liniile desenate
în PDF, nu aproximare din coordonate text) — soluție 100% determinstă.

Endpoint principal: POST /parse
  - Body: multipart/form-data cu fișierul PDF la cheia 'file'
  - Returnează: JSON cu belegNr, dataEmitere, suma, pozitii[]
"""

import io
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber

app = Flask(__name__)
CORS(app)  # permite request-uri din browser (GitHub Pages etc.)


def clean_atg(raw):
    """Curăță textul ATG, păstrând doar numele brandului/firmei."""
    if not raw:
        return ""
    raw = raw.replace("\n", " ")
    parts = raw.split()
    brand = [p for p in parts if not re.match(r"^\d{4,}$", p) and p != "0000"]
    # elimina duplicate pastrand ordinea
    seen = []
    for w in brand:
        if w not in seen:
            seen.append(w)
    return " ".join(seen).strip(" ,")


def clean_name(raw):
    if not raw:
        return ""
    return raw.replace("\n", " ").strip()


def clean_strasse(raw):
    if not raw:
        return ""
    s = raw.replace("\n", " ").strip()
    # Fix "17 Schreinerbauerweg" -> "Schreinerbauerweg 17"
    m = re.match(r"^(\d+[a-z]?)\s+(.+)$", s, re.IGNORECASE)
    if m:
        return f"{m.group(2)} {m.group(1)}"
    return s


def parse_money(raw):
    if not raw:
        return 0.0
    raw = raw.strip().replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_hermes_pdf(file_bytes):
    result = {
        "belegNr": None,
        "dataEmitere": None,
        "suma": None,
        "pozitii": [],
    }

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        # ── Header info din prima pagina (text simplu) ──
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        m = re.search(r"Beleg-Nr[.\s]+(\d+)", full_text)
        if m:
            result["belegNr"] = m.group(1)

        m = re.search(r"Hamburg,\s*den\s*(\d{2}\.\d{2}\.\d{4})", full_text)
        if m:
            result["dataEmitere"] = m.group(1)

        m = re.search(r"Forderung\s+gesamt\*?\s*€?\s*([\d.]+,[\d]+)", full_text)
        if m:
            result["suma"] = m.group(1).replace(".", "").replace(",", ".")

        # ── Tabel pozitii: cautam pe toate paginile ──
        for page in pdf.pages:
            tables = page.find_tables()
            for table in tables:
                rows = table.extract()
                if not rows or len(rows) < 2:
                    continue

                header = [h.strip().lower() if h else "" for h in rows[0]]
                # Verificam ca e tabelul corect (are coloanele asteptate)
                if not any("identnummer" in h for h in header):
                    continue
                if not any("tour" in h for h in header):
                    continue

                # Gasim indexul fiecarei coloane dupa numele din header
                def col_idx(*names):
                    for i, h in enumerate(header):
                        for n in names:
                            if n in h:
                                return i
                    return None

                idx_ident = col_idx("identnummer")
                idx_atg = col_idx("atg")
                idx_schad = col_idx("schadenart")
                idx_tour = col_idx("tour")
                idx_name = col_idx("name")
                idx_str = col_idx("strasse")
                idx_plz = col_idx("plz")
                idx_ort = col_idx("wohnort")
                idx_datum = col_idx("datum")
                idx_ford = col_idx("forderung")

                for row in rows[1:]:
                    if not row or len(row) < 5:
                        continue

                    def get(idx):
                        if idx is None or idx >= len(row):
                            return ""
                        return (row[idx] or "").strip()

                    tour = get(idx_tour).replace("\n", " ").strip()
                    name = clean_name(get(idx_name))
                    ident = get(idx_ident).replace("\n", "")

                    # Skip randuri goale / footer (ex: "Forderung gesamt")
                    if not name and not tour:
                        continue
                    if "forderung gesamt" in (get(idx_ident) or "").lower():
                        continue

                    pozitie = {
                        "tour": tour,
                        "identnummer": ident,
                        "atg": clean_atg(get(idx_atg)),
                        "schadenart": get(idx_schad).replace("\n", " ").strip() or "Totalverlust",
                        "name": name,
                        "strasse": clean_strasse(get(idx_str)),
                        "plz": get(idx_plz).strip(),
                        "ort": get(idx_ort).replace("\n", " ").strip(),
                        "datum": get(idx_datum).strip(),
                        "forderung": parse_money(get(idx_ford)),
                    }
                    result["pozitii"].append(pozitie)

        # Sortam dupa data descrescator
        def sort_key(p):
            try:
                d, mo, y = p["datum"].split(".")
                return (int(y), int(mo), int(d))
            except Exception:
                return (0, 0, 0)

        result["pozitii"].sort(key=sort_key, reverse=True)

    return result


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Hermes Rechnung PDF Parser",
        "usage": "POST /parse cu fisierul PDF la cheia 'file'"
    })


@app.route("/parse", methods=["POST"])
def parse_endpoint():
    if "file" not in request.files:
        return jsonify({"error": "Niciun fisier trimis (cheia 'file' lipseste)"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Fisierul trebuie sa fie PDF"}), 400

    try:
        file_bytes = file.read()
        result = parse_hermes_pdf(file_bytes)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Eroare la parsare: {str(e)}"}), 500


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
