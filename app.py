"""
Hermes Parser Server
====================
Endpoints:
  GET  /          — health check
  POST /parse     — parsează PDF Hermes Rechnung → JSON pozitii
  POST /parse-ods — parsează ODS REKLAMATIE → JSON reclamatii cu statusuri din culori
"""

import io
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
from odf.opendocument import load as odf_load
from odf.table import Table, TableRow, TableCell
from odf.text import P as OdfP

app = Flask(__name__)
CORS(app)


# ══════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════

def clean_atg(raw):
    if not raw:
        return ""
    parts = raw.replace("\n", " ").split()
    brand = [p for p in parts if not re.match(r"^\d{4,}$", p) and p != "0000"]
    seen = []
    for w in brand:
        if w not in seen:
            seen.append(w)
    return " ".join(seen).strip(" ,")


def clean_strasse(raw):
    if not raw:
        return ""
    s = raw.replace("\n", " ").strip()
    m = re.match(r"^(\d+[a-z]?)\s+(.+)$", s, re.IGNORECASE)
    if m:
        return f"{m.group(2)} {m.group(1)}"
    return s


def parse_money(raw):
    if not raw:
        return 0.0
    try:
        return float(raw.strip().replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


# ══════════════════════════════════════════════════════
# PARSER: Hermes Rechnung PDF
# ══════════════════════════════════════════════════════

def parse_hermes_pdf(file_bytes):
    result = {"belegNr": None, "dataEmitere": None, "suma": None, "pozitii": []}

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        m = re.search(r"Beleg-Nr[.\s]+(\d+)", full_text)
        if m: result["belegNr"] = m.group(1)

        m = re.search(r"Hamburg,\s*den\s*(\d{2}\.\d{2}\.\d{4})", full_text)
        if m: result["dataEmitere"] = m.group(1)

        m = re.search(r"Forderung\s+gesamt\*?\s*€?\s*([\d.]+,[\d]+)", full_text)
        if m: result["suma"] = m.group(1).replace(".", "").replace(",", ".")

        for page in pdf.pages:
            for table in page.find_tables():
                rows = table.extract()
                if not rows or len(rows) < 2:
                    continue
                header = [h.strip().lower() if h else "" for h in rows[0]]
                if not any("identnummer" in h for h in header):
                    continue
                if not any("tour" in h for h in header):
                    continue

                def col_idx(*names):
                    for i, h in enumerate(header):
                        for n in names:
                            if n in h:
                                return i
                    return None

                idx = {
                    "ident": col_idx("identnummer"),
                    "atg":   col_idx("atg"),
                    "schad": col_idx("schadenart"),
                    "tour":  col_idx("tour"),
                    "name":  col_idx("name"),
                    "str":   col_idx("strasse"),
                    "plz":   col_idx("plz"),
                    "ort":   col_idx("wohnort"),
                    "datum": col_idx("datum"),
                    "ford":  col_idx("forderung"),
                }

                for row in rows[1:]:
                    if not row or len(row) < 5:
                        continue

                    def get(key):
                        i = idx.get(key)
                        if i is None or i >= len(row):
                            return ""
                        return (row[i] or "").strip()

                    name = get("name").replace("\n", " ").strip()
                    tour = get("tour").replace("\n", " ").strip()
                    if not name and not tour:
                        continue
                    if "forderung gesamt" in get("ident").lower():
                        continue

                    result["pozitii"].append({
                        "tour":       tour,
                        "identnummer": get("ident").replace("\n", ""),
                        "atg":        clean_atg(get("atg")),
                        "schadenart": get("schad").replace("\n", " ").strip() or "Totalverlust",
                        "name":       name,
                        "strasse":    clean_strasse(get("str")),
                        "plz":        get("plz"),
                        "ort":        get("ort").replace("\n", " ").strip(),
                        "datum":      get("datum"),
                        "forderung":  parse_money(get("ford")),
                    })

        def sort_key(p):
            try:
                d, mo, y = p["datum"].split(".")
                return (int(y), int(mo), int(d))
            except Exception:
                return (0, 0, 0)

        result["pozitii"].sort(key=sort_key, reverse=True)

    return result


# ══════════════════════════════════════════════════════
# PARSER: REKLAMATIE ODS
# ══════════════════════════════════════════════════════

def parse_ods_reclamatii(file_bytes):
    doc = odf_load(io.BytesIO(file_bytes))

    # Map style name -> background color
    styles_bg = {}
    for s in doc.automaticstyles.childNodes:
        if s.qname and s.qname[1] == "style":
            name = s.getAttribute("name")
            for child in s.childNodes:
                if hasattr(child, "qname") and child.qname and "table-cell-properties" in child.qname[1]:
                    bg = child.getAttribute("backgroundcolor")
                    if bg and bg != "transparent":
                        styles_bg[name] = bg.lower()

    GREEN = {"#66ff00", "#99ff33", "#66ff66", "#99ff66"}
    RED   = {"#ff9999", "#ff8080"}

    def get_status(sname):
        c = styles_bg.get(sname or "", "")
        if c in GREEN: return "rezolvata"
        if c in RED:   return "nerezolvata"
        return "neprelucrat"

    def fix_date(d):
        if not d or "1899" in d or "1900" in d:
            return ""
        return d

    reclamatii = []
    for sheet in doc.spreadsheet.getElementsByType(Table):
        if sheet.getAttribute("name") != "DATE":
            continue
        for ri, row in enumerate(sheet.getElementsByType(TableRow)):
            if ri == 0:
                continue
            cells = row.getElementsByType(TableCell)

            def get_cell(i):
                if i >= len(cells):
                    return ("", None)
                cell = cells[i]
                texts = [str(p) for p in cell.getElementsByType(OdfP)]
                return (" ".join(texts).strip(), cell.getAttribute("stylename"))

            tour, s0 = get_cell(0)
            if not tour or not re.match(r"^\d+$", tour.strip()):
                continue

            vorname,   s1 = get_cell(1)
            nachname,  s2 = get_cell(2)
            datum,     s3 = get_cell(3)
            versender, s4 = get_cell(4)
            se_nr,     s5 = get_cell(5)
            termin,    s6 = get_cell(6)

            row_status = "neprelucrat"
            for sname in [s0, s1, s2, s3, s4, s5]:
                st = get_status(sname)
                if st != "neprelucrat":
                    row_status = st
                    break

            reclamatii.append({
                "turaSofer":      tour.strip(),
                "numeClient":     f"{nachname.strip()} {vorname.strip()}".strip(),
                "vorname":        vorname.strip(),
                "nachname":       nachname.strip(),
                "numePachet":     versender.strip(),
                "numarPachet":    se_nr.strip(),
                "dataReclamatie": fix_date(datum),
                "termin":         fix_date(termin),
                "status":         row_status,
                "locatie":        "Ruhstorf",
                "adaugatDe":      "import-ods",
                "adaugatDeNume":  "Import ODS",
            })

    return {
        "total":        len(reclamatii),
        "rezolvate":    sum(1 for r in reclamatii if r["status"] == "rezolvata"),
        "nerezolvate":  sum(1 for r in reclamatii if r["status"] == "nerezolvata"),
        "neprelucrate": sum(1 for r in reclamatii if r["status"] == "neprelucrat"),
        "reclamatii":   reclamatii,
    }


# ══════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Hermes Parser Server",
        "endpoints": {
            "POST /parse":     "Parsează PDF Hermes Rechnung → JSON pozitii",
            "POST /parse-ods": "Parsează ODS REKLAMATIE → JSON reclamatii cu statusuri"
        }
    })


@app.route("/parse", methods=["POST"])
def parse_endpoint():
    if "file" not in request.files:
        return jsonify({"error": "Lipsește cheia 'file'"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Fișierul trebuie să fie PDF"}), 400
    try:
        return jsonify(parse_hermes_pdf(f.read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/parse-ods", methods=["POST"])
def parse_ods_endpoint():
    if "file" not in request.files:
        return jsonify({"error": "Lipsește cheia 'file'"}), 400
    try:
        return jsonify(parse_ods_reclamatii(request.files["file"].read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
