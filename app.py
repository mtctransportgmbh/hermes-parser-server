"""
Hermes Parser Server
====================
Endpoints:
  GET  /          — health check
  POST /parse     — parsează PDF Hermes Rechnung → JSON pozitii
  POST /parse-ods — parsează ODS REKLAMATIE → JSON reclamatii cu statusuri din culori
"""

import io
import os
import re
import hmac
import hashlib
import base64
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
from odf.opendocument import load as odf_load
from odf.table import Table, TableRow, TableCell
from odf.text import P as OdfP

app = Flask(__name__)
# CORS: doar domeniul aplicației (poți adăuga altele, separate prin virgulă, în ALLOWED_ORIGINS pe Render)
_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "https://reclamatii.mtctransportgmbh.de").split(",") if o.strip()]
CORS(app, origins=_ALLOWED_ORIGINS)
# Limită de mărime pentru orice cerere (PDF/ODS/atașamente Mailgun) — protecție împotriva fișierelor uriașe
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

# ── Firebase Admin SDK (pentru webhook-ul de import automat din email) ──
# Necesita variabila de mediu FIREBASE_SERVICE_ACCOUNT_JSON pe Render
# (continutul JSON al cheii de service account, descarcata din Firebase Console)
_firebase_app = None
_firestore_db = None

def get_firestore():
    global _firebase_app, _firestore_db
    if _firestore_db is not None:
        return _firestore_db
    import firebase_admin
    from firebase_admin import credentials, firestore
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON nu e setat pe server")
    cred_dict = json.loads(sa_json)
    cred = credentials.Certificate(cred_dict)
    _firebase_app = firebase_admin.initialize_app(cred)
    _firestore_db = firestore.client()
    return _firestore_db


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

        # ── VALIDARE: Hermes declara pe pagina 1 numarul de pozitii pe categorie ──
        # "Transportschäden (Anzahl: X) €", "Verluste (Anzahl: Y) €", "Sonstige (Anzahl: Z) €"
        # Acest text apare de obicei DE DOUA ORI in PDF (o data in scrisoare, o data
        # repetat pe pagina Anlage) — luam doar primele 3 aparitii (Transportschaden,
        # Verluste, Sonstige = cele 3 categorii standard Hermes), nu tot ce gasim in document.
        anzahl_matches = re.findall(r"\(Anzahl:\s*(\d+)\)", full_text)
        if anzahl_matches:
            result["declaredPositionCount"] = sum(int(x) for x in anzahl_matches[:3])
        else:
            result["declaredPositionCount"] = None

        # Schema fixa Hermes (ordinea coloanelor e mereu aceasta pe toate paginile)
        FIXED_SCHEMA = ["ident","atg","schad","kst","tour","name","str","plz","ort","datum","ford"]

        def extract_row_as_position(row, idx_map):
            def get(key):
                i = idx_map.get(key)
                if i is None or i >= len(row):
                    return ""
                return (row[i] or "").strip()

            name = get("name").replace("\n", " ").strip()
            tour = get("tour").replace("\n", " ").strip()
            if not name and not tour:
                return None
            if "forderung gesamt" in get("ident").lower():
                return None

            return {
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
            }

        for page in pdf.pages:
            for table in page.find_tables():
                rows = table.extract()
                if not rows:
                    continue

                header = [h.strip().lower() if h else "" for h in rows[0]]
                has_header = any("identnummer" in h for h in header) and any("tour" in h for h in header)

                if has_header and len(rows) >= 2:
                    # Cazul normal: tabel cu header explicit, parsam rows[1:]
                    def col_idx(*names):
                        for i, h in enumerate(header):
                            for n in names:
                                if n in h:
                                    return i
                        return None

                    idx = {
                        "ident": col_idx("identnummer"), "atg": col_idx("atg"),
                        "schad": col_idx("schadenart"), "tour": col_idx("tour"),
                        "name": col_idx("name"), "str": col_idx("strasse"),
                        "plz": col_idx("plz"), "ort": col_idx("wohnort"),
                        "datum": col_idx("datum"), "ford": col_idx("forderung"),
                    }
                    for row in rows[1:]:
                        if not row or len(row) < 5:
                            continue
                        poz = extract_row_as_position(row, idx)
                        if poz: result["pozitii"].append(poz)

                elif len(rows[0]) == 11:
                    # Tabel FARA header — apare cand tabelul de pozitii se rupe intre
                    # pagini si Hermes NU repeta header-ul pe pagina noua (doar randul
                    # de date continua direct). Recunoastem dupa numarul de coloane
                    # identic cu schema fixa Hermes (11) si folosim pozitii FIXE.
                    idx_fixed = {k: i for i, k in enumerate(FIXED_SCHEMA)}
                    for row in rows:
                        if not row or len(row) < 5:
                            continue
                        poz = extract_row_as_position(row, idx_fixed)
                        if poz: result["pozitii"].append(poz)

        def sort_key(p):
            try:
                d, mo, y = p["datum"].split(".")
                return (int(y), int(mo), int(d))
            except Exception:
                return (0, 0, 0)

        result["pozitii"].sort(key=sort_key, reverse=True)

        # ── VERIFICARE FINALA: comparam ce am extras cu ce declara Hermes ──
        extracted_count = len(result["pozitii"])
        extracted_sum = round(sum(p["forderung"] for p in result["pozitii"]), 2)
        declared_count = result.get("declaredPositionCount")
        declared_sum = float(result["suma"]) if result.get("suma") else None

        warnings = []
        if declared_count is not None and declared_count != extracted_count:
            warnings.append(
                f"Hermes declară {declared_count} poziții, dar am extras {extracted_count}. "
                f"{'Lipsesc ' + str(declared_count - extracted_count) + ' poziții!' if declared_count > extracted_count else 'Sunt poziții în plus, posibil duplicate!'}"
            )
        if declared_sum is not None and abs(declared_sum - extracted_sum) > 0.02:
            warnings.append(
                f"Suma declarată de Hermes este €{declared_sum:.2f}, dar suma pozițiilor extrase este €{extracted_sum:.2f} "
                f"(diferență €{abs(declared_sum-extracted_sum):.2f})."
            )

        result["validation"] = {
            "ok": len(warnings) == 0,
            "declaredCount": declared_count,
            "extractedCount": extracted_count,
            "declaredSum": declared_sum,
            "extractedSum": extracted_sum,
            "warnings": warnings,
        }

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


# ══════════════════════════════════════════════════════════
# PARSER: Sachverhalt PDF (Sendungsauskunft) — pentru import automat
# Recunoaste toate 3 variantele de header Hermes: "Zustelladresse",
# "Ablageort", "An Haushalt übergeben"
# ══════════════════════════════════════════════════════════

def parse_sachverhalt_pdf(file_bytes):
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page = pdf.pages[0]
        words = page.extract_words()
        # Cuvintele de pe TOATE paginile — necesar pentru tabelul de tracking,
        # care se poate intinde pe 2+ pagini (mai ales cand sunt multiple
        # tentative esuate "Rücklauf nicht Angetroffen" inainte de livrarea reala)
        all_pages_words = []
        for p in pdf.pages:
            all_pages_words.append(p.extract_words())

    def at_top(top_val, xmin=None, xmax=None, tol=2):
        m = [w for w in words if abs(w["top"] - top_val) <= tol]
        if xmin is not None:
            m = [w for w in m if w["x0"] >= xmin]
        if xmax is not None:
            m = [w for w in m if w["x0"] <= xmax]
        return sorted(m, key=lambda w: w["x0"])

    result = {}

    ruck_word = next((w for w in words if w["text"] == "Rückinfo"), None)
    if ruck_word:
        result["ruckinfo"] = " ".join(w["text"] for w in at_top(ruck_word["top"], 200))

    ag_word = next((w for w in words if w["text"] == "Auftraggeber"), None)
    sid_word = next((w for w in words if w["text"] == "Sendungs-ID"), None)
    if ag_word:
        l1 = " ".join(w["text"] for w in at_top(ag_word["top"], 200, 600))
        l2 = ""
        if sid_word:
            candidate_top = ag_word["top"] + 12
            if candidate_top < sid_word["top"] - 2:
                l2 = " ".join(w["text"] for w in at_top(candidate_top, 200, 600))
        result["auftraggeber"] = (l1 + " " + l2).strip()

    if sid_word:
        result["sendungsId"] = " ".join(w["text"] for w in at_top(sid_word["top"], 200))

    def extract_address_block(xmin, xmax, yh, y_bottom):
        """Extrage nume/strada/plz/ort dintr-o coloana de adresa (stanga=originala, dreapta=livrare)."""
        row_tops = sorted(set(round(w["top"]) for w in words if xmin <= w["x0"] <= xmax and yh < w["top"] < y_bottom))
        lines = []
        for rt in row_tops:
            text = " ".join(w["text"] for w in at_top(rt, xmin, xmax)).strip()
            if text:
                lines.append(text)
        plz_idx = next((i for i, l in enumerate(lines) if re.match(r"^\d{5}\s+", l)), len(lines) - 1)
        plz_line = lines[plz_idx] if plz_idx < len(lines) else ""
        plz_m = re.match(r"^(\d{5})\s*(.+)", plz_line)
        plz = plz_m.group(1) if plz_m else ""
        ort = plz_m.group(2) if plz_m else ""
        name = lines[0] if lines else ""
        candidate_lines = [l for l in lines[1:plz_idx] if not re.match(r"^\d+$", l.strip())]
        strasse = candidate_lines[-1] if candidate_lines else ""
        return {"name": name, "strasse": strasse, "plz": plz, "ort": ort}

    def normalize_strada(s):
        """Normalizeaza prescurtari (Straße/Strasse/Str./Str -> str) pentru comparatie corecta."""
        if not s:
            return ""
        s = s.lower()
        s = re.sub(r"stra(ss|ß)e", "str", s)
        s = re.sub(r"str\.", "str", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    header_word = next((w for w in words if w["text"] in ("Zustelladresse", "Ablageort")), None)
    if not header_word:
        header_word = next((w for w in words if w["text"] == "übergeben"), None)

    # "Ablageort" = pachetul a fost lasat la un loc de depozitare stabilit prin
    # acord (nu predat direct), spre deosebire de "Zustelladresse"/"übergeben"
    result["esteAblageort"] = bool(header_word and header_word["text"] == "Ablageort")

    if header_word:
        yh = header_word["top"]
        quelle_word = next((w for w in words if w["text"].startswith("Quelle") and 250 <= w["x0"] <= 420 and w["top"] > yh), None)
        y_bottom = quelle_word["top"] if quelle_word else yh + 60

        deliv = extract_address_block(250, 420, yh, y_bottom)
        result["name"] = deliv["name"]
        result["strasse"] = deliv["strasse"]
        result["plz"] = deliv["plz"]
        result["ort"] = deliv["ort"]

        # Adresa ORIGINALA (unde trebuia livrat pachetul) — coloana din stanga,
        # aceleasi limite verticale (yh/y_bottom) ca si adresa de livrare
        orig = extract_address_block(50, 230, yh, y_bottom)
        result["origStrasse"] = orig["strasse"]
        result["origPlz"] = orig["plz"]
        result["origOrt"] = orig["ort"]

        # Comparam adresele NORMALIZATE — daca strada difera dupa normalizare
        # (nu doar o prescurtare diferita a aceleiasi strazi), semnalam alerta
        norm_orig = normalize_strada(orig["strasse"])
        norm_deliv = normalize_strada(deliv["strasse"])
        result["adresaLivrareDiferita"] = bool(norm_orig and norm_deliv and norm_orig != norm_deliv)

        if result.get("name"):
            parts = [p.strip() for p in result["name"].split(",")]
            if len(parts) == 2:
                result["nachname"] = parts[0]
                result["vorname"] = parts[1]

    # Cautam textul "Zugestellt" (livrare reusita) pe TOATE paginile, nu doar prima —
    # tabelul de tracking poate contine multe tentative esuate ("Rücklauf nicht
    # Angetroffen") inainte de livrarea reala, si acestea pot depasi o pagina.
    # Parcurgem paginile in ordine si folosim PRIMA aparitie gasita (evenimentele
    # sunt cronologice, iar Hermes scrie "Zugestellt" doar la livrarea efectiva).
    for page_words in all_pages_words:
        def at_top_page(top_val, xmin=None, xmax=None, tol=2, _words=page_words):
            m = [w for w in _words if abs(w["top"] - top_val) <= tol]
            if xmin is not None:
                m = [w for w in m if w["x0"] >= xmin]
            if xmax is not None:
                m = [w for w in m if w["x0"] <= xmax]
            return sorted(m, key=lambda w: w["x0"])

        zug_word = next((w for w in page_words if "Zugestellt" in w["text"]), None)
        if zug_word:
            yk = zug_word["top"]
            result["deliveryDate"] = " ".join(w["text"] for w in at_top_page(yk, 50, 120))
            result["tour"] = " ".join(w["text"] for w in at_top_page(yk, 500, 560)).strip()
            result["deliveryStatus"] = " ".join(w["text"] for w in at_top_page(yk, 230, 420)).strip()
            time_row = at_top_page(yk - 10, 50, 120, 4)
            if not time_row:
                time_row = at_top_page(yk + 10, 50, 120, 4)
            result["deliveryTime"] = " ".join(w["text"] for w in time_row)
            break  # gasit — nu mai cautam pe alte pagini

    return result


def _fix_squished_spacing(s):
    """pdfplumber lipeste uneori cuvinte alaturate fara spatiu intre ele
    (coloane apropiate vizual). Reintroducem spatiul acolo unde e clar un
    nume nou incepe (graniță minuscula->majuscula), ex: 'MoritzGammel' ->
    'Moritz Gammel'."""
    if not s:
        return s
    return re.sub(r"([a-zà-öø-ÿ])([A-ZÀ-ÖØ-Ý])", r"\1 \2", s)


def parse_sachverhalt_dpd_pdf(file_bytes):
    """
    Parseaza PDF-ul "Paketdaten" (Sachverhalt) de la curierul DPD — format
    diferit de cel Hermes (parse_sachverhalt_pdf de mai sus), cu propriile
    sectiuni: LIEFERADRESSE (adresa reala de livrare), AUFTRAGSDATEN, si
    PAKETLEBENSLAUF (istoricul livrarii, unde gasim linia "Zustellung an:
    <Nume> ... Tour: <numar>" cu data/ora pe doua randuri consecutive).
    Folosit DOAR pentru spatiul Corinei — nu afecteaza fluxul Hermes existent.
    """
    result = {"numeClient": "", "strada": "", "plz": "", "oras": "", "tour": "", "dataOraLivrare": "", "numarPachet": ""}

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    # ── Numarul de urmarire al pachetului — mereu din "VerknüpftePakete" ──
    # (e acelasi numar care apare si langa "Paketdaten" in capul paginii, dar
    # "VerknüpftePakete" e eticheta oficiala si stabila in AUFTRAGSDATEN)
    for l in lines:
        m = re.match(r"^Verkn[uü]pftePakete\s+(.+)$", l)
        if m:
            result["numarPachet"] = m.group(1).split(";")[0].strip()
            break

    # ── Blocul LIEFERADRESSE (adresa unde a fost livrat efectiv pachetul) ──
    lief_idx = next((i for i, l in enumerate(lines) if "LIEFERADRESSE" in l), None)
    if lief_idx is not None:
        end_idx = next((j for j in range(lief_idx + 1, len(lines)) if "AUFTRAGSDATEN" in lines[j]), None)
        block = lines[lief_idx + 1 : end_idx] if end_idx else lines[lief_idx + 1 : lief_idx + 8]
        cleaned = []
        for b in block:
            if b.startswith("Tel.:"):
                continue
            # linia poate avea lipit la inceput finalul coloanei din stanga (un email) —
            # il eliminam, nu aruncam toata linia, ca sa nu pierdem numele de dupa el
            b2 = re.sub(r"[\w.+-]+@[\w.-]+", "", b).strip()
            if b2:
                cleaned.append(b2)
        block = cleaned

        de_idx, plz, oras = None, "", ""
        for k, b in enumerate(block):
            m = re.match(r"^DE-(\d{5})(.+)$", b)
            if m:
                de_idx = k
                plz = m.group(1)
                oras = m.group(2).replace(",", ", ").strip()
                break

        nume = block[0] if block else ""
        strada = ""
        if de_idx is not None and de_idx > 0:
            strada = block[de_idx - 1]
        elif len(block) > 1:
            strada = block[1]
        # reintroducem spatiul dintre numele strazii si numar (ex: "Kolumbusstr.16" -> "Kolumbusstr. 16")
        strada = re.sub(r"([^\d\s])(\d+)$", r"\1 \2", strada)

        result["numeClient"] = _fix_squished_spacing(nume)
        result["strada"] = _fix_squished_spacing(strada)
        result["plz"] = plz
        result["oras"] = _fix_squished_spacing(oras)

    # ── Tour-ul si data/ora livrarii efective, din PAKETLEBENSLAUF ──
    # Linia arata asa (pe 2 randuri, din cauza impachetarii coloanelor):
    #   "15.09.2026 0010180Neufahrn(DE) Zustellungan:MoritzGammel DE-80636-0010280Tour:"
    #   "18:05Uhr 501-Service:327"
    # Daca exista mai multe tentative de livrare, pastram ULTIMA aparitie (livrarea finala).
    for i, l in enumerate(lines):
        if "Zustellungan:" not in l.replace(" ", ""):
            continue
        date_m = re.match(r"^(\d{2}\.\d{2}\.\d{4})", l)
        date_str = date_m.group(1) if date_m else ""
        tour, time_str = "", ""
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            time_m = re.match(r"^(\d{2}:\d{2})Uhr", nxt)
            if time_m:
                time_str = time_m.group(1)
            tour_m = re.search(r"(\d{2,4})-Service", nxt)
            if tour_m:
                tour = tour_m.group(1)
        if date_str:
            result["dataOraLivrare"] = f"{date_str} {time_str}".strip()
        if tour:
            result["tour"] = tour

    return result


def verify_mailgun_signature(token, timestamp, signature):
    """Verifica semnatura Mailgun ca sa fim siguri ca request-ul chiar vine de la Mailgun."""
    signing_key = os.environ.get("MAILGUN_SIGNING_KEY")
    if not signing_key:
        return False  # daca nu e configurat, refuzam din siguranta
    hmac_digest = hmac.new(
        key=signing_key.encode("utf-8"),
        msg=(timestamp + token).encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(hmac_digest, signature)


def parse_de_date(date_str):
    """Converteste 'DD.MM.YY' sau 'DD.MM.YYYY' in datetime."""
    if not date_str:
        return None
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})", date_str.strip())
    if not m:
        return None
    d, mo, y = m.groups()
    y = int(y)
    if y < 100:
        y += 2000
    try:
        return datetime(y, int(mo), int(d))
    except ValueError:
        return None



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
    _u, _deny = require_parse_user()
    if _deny:
        return _deny
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
    _u, _deny = require_parse_user()
    if _deny:
        return _deny
    if "file" not in request.files:
        return jsonify({"error": "Lipsește cheia 'file'"}), 400
    try:
        return jsonify(parse_ods_reclamatii(request.files["file"].read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/parse-sachverhalt-dpd", methods=["POST"])
def parse_sachverhalt_dpd_endpoint():
    """Parseaza un PDF Sachverhalt DPD ('Paketdaten ...') — folosit de spațiul
    Corinei pentru a completa automat nume/adresă/tour/dată livrare la adăugarea
    unei reclamații noi. Suma rămâne mereu introdusă manual (nu e în acest PDF)."""
    _u, _deny = require_parse_user()
    if _deny:
        return _deny
    if "file" not in request.files:
        return jsonify({"error": "Lipsește cheia 'file'"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Fișierul trebuie să fie PDF"}), 400
    try:
        return jsonify(parse_sachverhalt_dpd_pdf(f.read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/webhook-sachverhalt", methods=["POST"])
def webhook_sachverhalt():
    """
    Endpoint apelat de Mailgun (Inbound Route) cand soseste un email nou
    pe adresa dedicata (ex: sachverhalt@mtctransportgmbh.de).
    Extrage automat PDF-urile Sachverhalt atasate, le parseaza si creeaza
    reclamatii noi in Firestore, marcate 'sursaAutomata: true' pentru
    verificare umana ulterioara (nu se salveaza orb, fara control).
    """
    try:
        # Verificam semnatura Mailgun (securitate — evitam request-uri false)
        token = request.form.get("token", "")
        timestamp = request.form.get("timestamp", "")
        signature = request.form.get("signature", "")
        if not verify_mailgun_signature(token, timestamp, signature):
            return jsonify({"error": "Semnatura invalida"}), 403

        sender = request.form.get("sender", "necunoscut")
        subject = request.form.get("subject", "")
        attachment_count = int(request.form.get("attachment-count", 0))

        db = get_firestore()
        results = []

        for i in range(1, attachment_count + 1):
            file_key = f"attachment-{i}"
            if file_key not in request.files:
                continue
            file = request.files[file_key]
            filename = file.filename or ""
            if not filename.lower().endswith(".pdf"):
                continue
            # IMPORTANT: verificam STRICT numele fisierului atasat (nu subiectul
            # email-ului), pentru ca un singur email poate contine MAI MULTE PDF-uri
            # diferite (ex: Sachverhalt + Ablieferungsnachweis). Daca am verifica dupa
            # subiect, am importa gresit si celelalte atasamente irelevante.
            filename_l = filename.lower()
            is_sachverhalt = "sachverhalt" in filename_l or "sendungsauskunft" in filename_l
            if not is_sachverhalt:
                continue  # sarim atasamente care nu sunt Sachverhalt (ex: Ablieferungsnachweis)

            file_bytes = file.read()
            try:
                parsed = parse_sachverhalt_pdf(file_bytes)
            except Exception as e:
                results.append({"filename": filename, "error": str(e)})
                continue

            sendungs_id = parsed.get("sendungsId", "")

            # Deduplicare: daca exista deja o reclamatie cu acelasi numarPachet, sarim
            if sendungs_id:
                existing = db.collection("reclamatii").where("numarPachet", "==", sendungs_id).limit(1).get()
                if len(existing) > 0:
                    results.append({"filename": filename, "status": "duplicat", "sendungsId": sendungs_id})
                    continue

            # Cautam locatia dupa tura (daca soferul exista deja in baza)
            tour = parsed.get("tour", "")
            locatie = ""
            if tour:
                sofer_snap = db.collection("soferi").where("tura", "==", tour).limit(1).get()
                if len(sofer_snap) > 0:
                    locatie = sofer_snap[0].to_dict().get("locatie", "")

            nume_client = f"{parsed.get('nachname','')} {parsed.get('vorname','')}".strip() or parsed.get("name", "")
            data_livrare = parse_de_date(parsed.get("deliveryDate"))
            # Termin NU se mai ia din PDF (Rückinfo bis) — se calculeaza automat:
            # data la care reclamatia intra in sistemul nostru + 4 zile (pana la 23:59 acea zi)
            termin_dt = datetime.now() + timedelta(days=4)
            termin_str = termin_dt.strftime("%Y-%m-%d")

            # Descriere auto-generata cu context util pentru trasabilitate
            descriere_parts = [f"⚡ Import automat din email ({sender})"]
            if subject:
                descriere_parts.append(f"Subiect: {subject}")
            if parsed.get("ruckinfo"):
                descriere_parts.append(f"Rückinfo bis: {parsed.get('ruckinfo')}")
            if parsed.get("deliveryStatus") and parsed.get("deliveryDate"):
                deliv_time_part = f" {parsed.get('deliveryTime')}" if parsed.get("deliveryTime") else ""
                descriere_parts.append(f"{parsed.get('deliveryStatus')} · Livrat: {parsed.get('deliveryDate')}{deliv_time_part}")

            adresa_diferita = parsed.get("adresaLivrareDiferita", False)
            if adresa_diferita:
                orig_full = f"{parsed.get('origStrasse','')}, {parsed.get('origPlz','')} {parsed.get('origOrt','')}".strip(", ")
                deliv_full = f"{parsed.get('strasse','')}, {parsed.get('plz','')} {parsed.get('ort','')}".strip(", ")
                descriere_parts.append(f"⚠️ PACHET LIVRAT LA ALTĂ ADRESĂ! Original: {orig_full} · Livrat efectiv: {deliv_full}")

            este_ablageort = parsed.get("esteAblageort", False)
            if este_ablageort:
                descriere_parts.append("⚠️ ACORD DE OPRIRE — pachetul a fost lăsat la locul de depozitare stabilit (Ablageort), nu predat direct")

            descriere = " · ".join(descriere_parts)

            doc = {
                "numeClient": nume_client,
                "vorname": parsed.get("vorname", ""),
                "nachname": parsed.get("nachname", ""),
                "strada": parsed.get("strasse", ""),
                "plz": parsed.get("plz", ""),
                "ort": parsed.get("ort", ""),
                "origStrada": parsed.get("origStrasse", ""),
                "origPlz": parsed.get("origPlz", ""),
                "origOrt": parsed.get("origOrt", ""),
                "adresaLivrareDiferita": adresa_diferita,
                "esteAblageort": este_ablageort,
                "turaSofer": tour,
                "locatie": locatie,
                "numarPachet": sendungs_id,
                "numePachet": parsed.get("auftraggeber", ""),
                "dataReclamatie": data_livrare if data_livrare else datetime.now(timezone.utc),
                "termin": termin_str,
                "descriere": descriere,
                "status": "neprelucrat",
                "tipReclamatie": "livrare" if not adresa_diferita else "adresa gresita",
                "adaugatDe": "auto-email",
                "adaugatDeNume": "⚡ Import automat (email)",
                "sursaAutomata": True,
                "verificatDeOm": False,
                "emailSender": sender,
                "emailSubject": subject,
                "createdAt": firestore_server_timestamp(),
                "updatedAt": firestore_server_timestamp(),
            }
            db.collection("reclamatii").add(doc)
            results.append({"filename": filename, "status": "adaugat", "sendungsId": sendungs_id, "nume": nume_client})

        return jsonify({"ok": True, "processed": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def firestore_server_timestamp():
    from firebase_admin import firestore
    return firestore.SERVER_TIMESTAMP


# ══════════════════════════════════════════════════════
# AUTENTIFICARE PIN — verificare 100% server-side.
#
# De ce: inainte, PIN-ul era comparat direct in JS-ul din browser impotriva
# unui hash stocat in Firestore, iar regulile Firestore erau complet publice
# ("allow read, write: if true"). Asta insemna ca oricine gasea adresa
# aplicatiei putea citi/scrie toata baza de date DIRECT, ocolind complet
# ecranul de PIN. Acum:
#   1. PIN-ul (6 cifre) e trimis aici, la server, NICIODATA comparat in JS
#   2. Contorizam incercarile gresite in Firestore (rezista si la restart
#      de server) si blocam userul 60s dupa 5 esecuri consecutive
#   3. Daca PIN-ul e corect, emitem un "custom token" Firebase pentru
#      exact acel user — clientul se autentifica REAL cu el
#   4. Regulile Firestore (separat, in consola) vor cere request.auth != null,
#      deci cineva fara sa treaca pe aici nu mai poate atinge baza de date
# ══════════════════════════════════════════════════════

PIN_SALT_SECRET = os.environ.get("PIN_SALT_SECRET", "")
PIN_MAX_ATTEMPTS = 3
PIN_LOCKOUT_SECONDS = 300
ADMIN_UID = "itgIGVH3Y2ZaGCSwJa7yt7Nd14R2"

# Singurele conturi care pot avea PIN. Orice alt "userId" e refuzat — altfel oricine ar putea
# inventa un utilizator nou, își seta un PIN și primea un token valid pentru baza de date.
PIN_USERS = {"tania", "raluca", "mihaela", "madalina", "corina"}
ADMIN_PIN_TARGETS = PIN_USERS | {"admin"}

# ── Limitare simplă a cererilor (în memorie, per proces) ──
import time as _time
import threading as _threading
_rl_lock = _threading.Lock()
_rl_hits = {}

def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else (request.remote_addr or "?"))

def rate_limited(bucket, key, limit, window_s):
    """True dacă `key` a depășit `limit` cereri în ultimele `window_s` secunde."""
    now = _time.time()
    k = (bucket, key)
    with _rl_lock:
        hits = [t for t in _rl_hits.get(k, []) if now - t < window_s]
        if len(hits) >= limit:
            _rl_hits[k] = hits
            return True
        hits.append(now)
        _rl_hits[k] = hits
        if len(_rl_hits) > 5000:   # curățare ocazională
            for kk in [kk for kk, v in _rl_hits.items() if not v or now - v[-1] > 3600]:
                _rl_hits.pop(kk, None)
        return False

def require_parse_user():
    """Pentru /parse*: autentificare obligatorie, cu o excepție TEMPORARĂ controlată de variabila
    PARSE_REQUIRE_AUTH=0 (folosită doar cât timp pagina veche nu trimite încă tokenul). Implicit: obligatoriu."""
    if os.environ.get("PARSE_REQUIRE_AUTH", "1").strip() == "0":
        if rate_limited("parse_open", client_ip(), 20, 60):
            return None, (jsonify({"error": "too_many_requests"}), 429)
        return None, None
    return require_app_user()

def require_app_user():
    """Verifică tokenul Firebase din Authorization: Bearer și că uid-ul e un cont real al aplicației.
    Întoarce (decoded, None) sau (None, răspuns_de_eroare)."""
    decoded = verify_admin_bearer_token()
    if not decoded or (decoded.get("uid") not in PIN_USERS and decoded.get("uid") != ADMIN_UID):
        return None, forbidden_admin_response(decoded)
    if rate_limited("api", decoded.get("uid"), 60, 60):
        return None, (jsonify({"error": "too_many_requests"}), 429)
    return decoded, None


def hash_pin_server(pin, user_id):
    """Hash SHA-256 cu salt secret (server-only) — nu mai poate fi reprodus din JS client."""
    if not PIN_SALT_SECRET:
        raise RuntimeError("PIN_SALT_SECRET nu e setat pe server")
    payload = f"{PIN_SALT_SECRET}:{user_id}:{pin}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_user_doc_ref_and_data(db, user_id):
    ref = db.collection("users").document(user_id)
    snap = ref.get()
    return ref, (snap.to_dict() if snap.exists else {})


def is_valid_pin_format(pin):
    return bool(re.match(r"^\d{6}$", pin or ""))


_WEAK_PINS = {"123321", "112233", "102030", "010203", "159753", "147258", "258369", "789456",
              "741852", "369258", "000111", "111222", "121314", "123123", "112211", "198765"}

def is_weak_pin(pin):
    """PIN prea ușor de ghicit: cifre identice (111111), secvențe (123456, 654321, 012345),
    repetiții (121212, 123123) sau din lista scurtă de PIN-uri uzuale."""
    if not is_valid_pin_format(pin):
        return True
    if len(set(pin)) == 1:
        return True
    d = [int(c) for c in pin]
    step = d[1] - d[0]
    if step in (1, -1) and all(d[i] - d[i - 1] == step for i in range(1, 6)):
        return True
    for k in (2, 3):
        if pin == pin[:k] * (6 // k):
            return True
    return pin in _WEAK_PINS


# Blocare progresivă: primele 2 greșeli doar avertizează; a 3-a → 5 min, a 4-a → 15 min, a 5-a și următoarele → 1 oră.
# Contorul se resetează la un PIN corect sau după 24 h fără greșeli. Nu există blocare definitivă
# (altfel cineva din exterior ar putea bloca pe oricine); Admin poate debloca oricând prin „Schimbă”/„Resetează PIN”.
PIN_FREE_ATTEMPTS = 2
PIN_LOCK_STEPS = [300, 900, 3600]          # secunde, pentru greșeala nr. 3, 4, 5+
PIN_FAIL_DECAY_SECONDS = 24 * 3600

def lockout_seconds_for(fail_count):
    if fail_count <= PIN_FREE_ATTEMPTS:
        return 0
    return PIN_LOCK_STEPS[min(fail_count - PIN_FREE_ATTEMPTS, len(PIN_LOCK_STEPS)) - 1]

def register_failed_pin(ref, udata):
    now = datetime.now(timezone.utc)
    prev = int(udata.get("pinFailCount", 0))
    last = udata.get("pinLastFailAt")
    try:
        if last is not None:
            last_dt = last if getattr(last, "tzinfo", None) else last.replace(tzinfo=timezone.utc)
            if (now - last_dt).total_seconds() > PIN_FAIL_DECAY_SECONDS:
                prev = 0
    except Exception:
        pass
    fail_count = prev + 1
    secs = lockout_seconds_for(fail_count)
    update = {"pinFailCount": fail_count, "pinLastFailAt": now}
    if secs:
        update["pinLockedUntil"] = now + timedelta(seconds=secs)
    ref.set(update, merge=True)
    if secs:
        return jsonify({"error": "locked", "retryAfterSeconds": secs}), 423
    return jsonify({"error": "wrong_pin", "attemptsLeft": PIN_FREE_ATTEMPTS + 1 - fail_count}), 401


def check_and_get_lockout_seconds(udata):
    """Intoarce cate secunde mai sunt de asteptat daca userul e blocat, altfel 0."""
    locked_until = udata.get("pinLockedUntil")
    if not locked_until:
        return 0
    # Firestore intoarce un obiect datetime (cu tz) pentru campurile de tip Timestamp
    try:
        now = datetime.now(timezone.utc)
        locked_dt = locked_until if locked_until.tzinfo else locked_until.replace(tzinfo=timezone.utc)
        remaining = (locked_dt - now).total_seconds()
        return max(0, int(remaining))
    except Exception:
        return 0


@app.route("/auth/login-pin", methods=["POST"])
def auth_login_pin():
    """Verifica PIN-ul unui user si, daca e corect, emite un custom token Firebase."""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("userId") or "").strip()
    pin = (data.get("pin") or "").strip()
    if not user_id or not is_valid_pin_format(pin) or user_id not in PIN_USERS:
        return jsonify({"error": "invalid_request"}), 400
    if rate_limited("login", client_ip(), 30, 600):
        return jsonify({"error": "too_many_requests"}), 429

    try:
        db = get_firestore()
    except Exception as e:
        return jsonify({"error": "server_config", "detail": str(e)}), 500

    ref, udata = get_user_doc_ref_and_data(db, user_id)

    lock_remaining = check_and_get_lockout_seconds(udata)
    if lock_remaining > 0:
        return jsonify({"error": "locked", "retryAfterSeconds": lock_remaining}), 423

    stored_hash = udata.get("pinHashV2")
    if not stored_hash:
        return jsonify({"error": "no_pin_set"}), 404

    try:
        hashed = hash_pin_server(pin, user_id)
    except RuntimeError as e:
        return jsonify({"error": "server_config", "detail": str(e)}), 500

    if not hmac.compare_digest(hashed, stored_hash):
        return register_failed_pin(ref, udata)

    # PIN corect — resetam contorul de esecuri si emitem custom token
    from firebase_admin import auth as fb_auth, firestore as fb_firestore
    ref.set({
        "pinFailCount": 0,
        "pinLockedUntil": fb_firestore.DELETE_FIELD,
        "pin": fb_firestore.DELETE_FIELD,
        "lastLogin": fb_firestore.SERVER_TIMESTAMP,
    }, merge=True)
    token = fb_auth.create_custom_token(user_id)
    return jsonify({"token": token.decode("utf-8") if isinstance(token, bytes) else token})


@app.route("/auth/setup-pin", methods=["POST"])
def auth_setup_pin():
    """Prima setare a PIN-ului pentru un user (nu exista deja unul salvat)."""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("userId") or "").strip()
    pin = (data.get("pin") or "").strip()
    if not user_id or not is_valid_pin_format(pin) or user_id not in PIN_USERS:
        return jsonify({"error": "invalid_request"}), 400
    if rate_limited("setup", client_ip(), 10, 600):
        return jsonify({"error": "too_many_requests"}), 429

    try:
        db = get_firestore()
    except Exception as e:
        return jsonify({"error": "server_config", "detail": str(e)}), 500

    ref, udata = get_user_doc_ref_and_data(db, user_id)
    if udata.get("pinHashV2"):
        return jsonify({"error": "pin_already_set"}), 409
    if is_weak_pin(pin):
        return jsonify({"error": "pin_too_weak"}), 422
    # Prima setare e permisă DOAR după ce Admin a pornit-o (Resetează PIN sau PIN setat de Admin).
    # Fără asta, oricine din internet ar putea prelua un cont care nu are încă PIN.
    if udata.get("pinResetRequired") is not True:
        return jsonify({"error": "setup_not_allowed"}), 403

    try:
        hashed = hash_pin_server(pin, user_id)
    except RuntimeError as e:
        return jsonify({"error": "server_config", "detail": str(e)}), 500

    from firebase_admin import auth as fb_auth, firestore as fb_firestore
    ref.set({
        "pinHashV2": hashed,
        "pin": fb_firestore.DELETE_FIELD,   # PIN-ul NU se mai păstrează în clar
        "pinSetAt": datetime.now(timezone.utc).isoformat(),
        "pinFailCount": 0,
        "pinLockedUntil": fb_firestore.DELETE_FIELD,
        "pinResetRequired": fb_firestore.DELETE_FIELD,
        "lastLogin": fb_firestore.SERVER_TIMESTAMP,
    }, merge=True)
    token = fb_auth.create_custom_token(user_id)
    return jsonify({"token": token.decode("utf-8") if isinstance(token, bytes) else token})


def verify_admin_bearer_token():
    """Verifica headerul Authorization: Bearer <idToken Firebase> si intoarce token-ul decodat.

    Tolereaza mici diferente de ceas (60s) intre serverul Render si Google, care altfel
    dau "Token used too early" si resping un token perfect valid. Daca esueaza, motivul
    ramane in `verify_admin_bearer_token.last_error` (apare in raspunsul 403, ca sa
    se vada in consola browserului DE CE a fost refuzat)."""
    verify_admin_bearer_token.last_error = ""
    authz = request.headers.get("Authorization", "")
    if not authz.startswith("Bearer "):
        verify_admin_bearer_token.last_error = "missing_bearer_token"
        return None
    id_token = authz[7:].strip()
    from firebase_admin import auth as fb_auth
    try:
        # IMPORTANT: aplicatia Firebase Admin se initializeaza "lazy" in get_firestore().
        # Daca prima cerere dupa pornirea serverului e una de Admin, verify_id_token
        # esua cu "default Firebase app does not exist" -> 403 fals. O initializam aici.
        get_firestore()
        return fb_auth.verify_id_token(id_token, clock_skew_seconds=60)
    except Exception as e:
        verify_admin_bearer_token.last_error = f"{type(e).__name__}: {str(e)[:160]}"
        return None


def forbidden_admin_response(decoded):
    reason = verify_admin_bearer_token.last_error or ("not_admin_uid" if decoded else "unknown")
    return jsonify({"error": "forbidden", "reason": reason}), 403


@app.route("/auth/admin-unlock", methods=["POST"])
def auth_admin_unlock():
    """Deblocare rapida a ecranului pentru Admin cu PIN.

    Admin-ul se logheaza INTAI cu email+parola (Firebase). Dupa blocarea ecranului
    la inactivitate, poate deschide aplicatia cu PIN-ul de 6 cifre. Cererea trebuie
    sa poarte ID token-ul sesiunii Admin deja autentificate (Bearer) — deci PIN-ul
    NU poate fi folosit ca sa se logheze cineva de la zero, doar sa deblocheze o
    sesiune existenta. PIN-ul e verificat pe server, cu aceeasi blocare la
    incercari gresite ca la ceilalti useri.
    """
    decoded = verify_admin_bearer_token()
    if not decoded or decoded.get("uid") != ADMIN_UID:
        return forbidden_admin_response(decoded)

    data = request.get_json(silent=True) or {}
    pin = (data.get("pin") or "").strip()
    if not is_valid_pin_format(pin):
        return jsonify({"error": "invalid_request"}), 400

    try:
        db = get_firestore()
    except Exception as e:
        return jsonify({"error": "server_config", "detail": str(e)}), 500

    user_id = "admin"
    ref, udata = get_user_doc_ref_and_data(db, user_id)

    lock_remaining = check_and_get_lockout_seconds(udata)
    if lock_remaining > 0:
        return jsonify({"error": "locked", "retryAfterSeconds": lock_remaining}), 423

    stored_hash = udata.get("pinHashV2")
    if not stored_hash:
        return jsonify({"error": "no_pin_set"}), 404

    try:
        hashed = hash_pin_server(pin, user_id)
    except RuntimeError as e:
        return jsonify({"error": "server_config", "detail": str(e)}), 500

    if not hmac.compare_digest(hashed, stored_hash):
        return register_failed_pin(ref, udata)

    from firebase_admin import firestore as fb_firestore
    ref.set({
        "pinFailCount": 0,
        "pinLockedUntil": fb_firestore.DELETE_FIELD,
        "lastLogin": fb_firestore.SERVER_TIMESTAMP,
    }, merge=True)
    return jsonify({"ok": True})


@app.route("/auth/admin-set-pin", methods=["POST"])
def auth_admin_set_pin():
    """Admin seteaza/schimba direct PIN-ul altui user (sau al sau propriu)."""
    decoded = verify_admin_bearer_token()
    if not decoded or decoded.get("uid") != ADMIN_UID:
        return forbidden_admin_response(decoded)

    data = request.get_json(silent=True) or {}
    target_user_id = (data.get("targetUserId") or "").strip()
    new_pin = (data.get("newPin") or "").strip()
    if target_user_id not in ADMIN_PIN_TARGETS or not is_valid_pin_format(new_pin):
        return jsonify({"error": "invalid_request"}), 400
    if is_weak_pin(new_pin):
        return jsonify({"error": "pin_too_weak"}), 422

    try:
        db = get_firestore()
        hashed = hash_pin_server(new_pin, target_user_id)
    except Exception as e:
        return jsonify({"error": "server_config", "detail": str(e)}), 500

    from firebase_admin import firestore as fb_firestore
    db.collection("users").document(target_user_id).set({
        "pinHashV2": hashed,
        "pin": fb_firestore.DELETE_FIELD,   # PIN-ul NU se mai păstrează în clar
        "pinSetAt": datetime.now(timezone.utc).isoformat(),
        "pinFailCount": 0,
        "pinLockedUntil": fb_firestore.DELETE_FIELD,
    }, merge=True)
    return jsonify({"ok": True})


@app.route("/auth/admin-purge-plain-pins", methods=["POST"])
def auth_admin_purge_plain_pins():
    """Șterge câmpul `pin` (PIN în clar) din toate conturile. Idempotent; doar Admin."""
    decoded = verify_admin_bearer_token()
    if not decoded or decoded.get("uid") != ADMIN_UID:
        return forbidden_admin_response(decoded)
    from firebase_admin import firestore as fb_firestore
    db = get_firestore()
    n = 0
    for uid_ in ADMIN_PIN_TARGETS:
        ref = db.collection("users").document(uid_)
        snap = ref.get()
        if snap.exists and "pin" in (snap.to_dict() or {}):
            ref.update({"pin": fb_firestore.DELETE_FIELD})
            n += 1
    return jsonify({"ok": True, "purged": n})


@app.route("/auth/admin-reset-pin", methods=["POST"])
def auth_admin_reset_pin():
    """Admin sterge PIN-ul unui user — la urmatoarea logare i se va cere sa seteze unul nou."""
    decoded = verify_admin_bearer_token()
    if not decoded or decoded.get("uid") != ADMIN_UID:
        return forbidden_admin_response(decoded)

    data = request.get_json(silent=True) or {}
    target_user_id = (data.get("targetUserId") or "").strip()
    if target_user_id not in ADMIN_PIN_TARGETS:
        return jsonify({"error": "invalid_request"}), 400

    db = get_firestore()
    from firebase_admin import firestore as fb_firestore
    db.collection("users").document(target_user_id).set({
        "pinHashV2": fb_firestore.DELETE_FIELD,
        "pin": fb_firestore.DELETE_FIELD,
        "pinFailCount": 0,
        "pinLockedUntil": fb_firestore.DELETE_FIELD,
        "pinResetRequired": True,
    }, merge=True)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════
# BACKUP IN GITHUB — export complet Firestore -> repository PRIVAT (un commit / backup)
# Variabile de mediu pe Render:
#   GITHUB_BACKUP_TOKEN  = token GitHub (fine-grained, DOAR repo-ul de backup, Contents: Read & write)
#   GITHUB_BACKUP_REPO   = "utilizator/nume-repo"  (privat, initializat cu un README)
#   GITHUB_BACKUP_BRANCH = (optional) implicit "main"
#   BACKUP_CRON_SECRET   = secret pentru declansarea automata din GitHub Actions
# Fiecare modul = un fisier backup/<modul>.json (o inregistrare pe linie -> diferente curate in git).
# Istoricul commit-urilor = istoricul backup-urilor; se poate restaura orice commit din aplicatie.
# Formatul valorilor e identic cu cel din aplicatie: {"__ts":[sec,nanos]}, {"__gp":[lat,lng]}, {"__ref":"cale"}.
# ══════════════════════════════════════════════════════
import threading
import urllib.request
import urllib.error
import base64

BACKUP_MODULES = [
    {"key": "reclamatii",       "cols": ["reclamatii"]},
    {"key": "rechnunguri",      "cols": ["rechnunguri"]},
    {"key": "rechn_lipsa",      "cols": ["rechnung_lipsa"]},
    {"key": "soferi",           "cols": ["soferi"]},
    {"key": "pachete_livrate",  "cols": ["pachete_livrate"]},
    {"key": "pachete_pierdute", "cols": ["pachete_pierdute"]},
    {"key": "utilizatori",      "cols": ["users"], "no_pin": True},
    {"key": "setari",           "cols": ["setari", "config"],
     "skip_docs": ["config/versiune", "config/ultimulBackup", "config/ultimulBackupV2"]},
    {"key": "notificari",       "cols": ["notificari"]},
    {"key": "geocode",          "cols": ["geocode_cache"]},
]
# Spatiul Corinei (soferi_corina / reclamatii_corina) NU se salveaza in GitHub — e privat.
BACKUP_MODULE_KEYS = [m["key"] for m in BACKUP_MODULES]
_backup_lock = threading.Lock()
_backup_running = False


def bk_encode(v):
    from google.cloud.firestore_v1._helpers import GeoPoint
    from google.cloud.firestore_v1.document import DocumentReference
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, datetime):  # include DatetimeWithNanoseconds
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        nanos = getattr(v, "nanosecond", None)
        if nanos is None:
            nanos = v.microsecond * 1000
        return {"__ts": [int(v.timestamp()), int(nanos)]}
    if isinstance(v, GeoPoint):
        return {"__gp": [v.latitude, v.longitude]}
    if isinstance(v, DocumentReference):
        return {"__ref": v.path}
    if isinstance(v, (bytes, bytearray)):
        return {"__b64": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (list, tuple)):
        return [bk_encode(x) for x in v]
    if isinstance(v, dict):
        return {k: bk_encode(x) for k, x in v.items()}
    return str(v)


def bk_collect_module(db, mod):
    recs = []
    for col in mod["cols"]:
        for d in db.collection(col).stream():
            if f"{col}/{d.id}" in mod.get("skip_docs", []):
                continue
            data = bk_encode(d.to_dict() or {})
            if mod.get("no_pin"):
                data = {k: x for k, x in data.items() if not re.match(r"^(pin|failed|locked|lockout)", k, re.I)}
            recs.append({"c": col, "id": d.id, "d": data})
    recs.sort(key=lambda r: (r["c"], r["id"]))
    return recs


def gh_cfg():
    tok = os.environ.get("GITHUB_BACKUP_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_BACKUP_REPO", "").strip()
    br = os.environ.get("GITHUB_BACKUP_BRANCH", "main").strip() or "main"
    missing = [n for n, v in (("GITHUB_BACKUP_TOKEN", tok), ("GITHUB_BACKUP_REPO", repo)) if not v]
    return tok, repo, br, missing


# ── Criptare backup (AES-256-GCM). Datele NU ajung niciodata necriptate in GitHub. ──
def bk_key():
    k = os.environ.get("BACKUP_ENCRYPTION_KEY", "").strip().strip("\"'").strip()
    if len(k) < 24:
        return None
    return hashlib.sha256(("mtc-backup-v1|" + k).encode("utf-8")).digest()


def bk_key_state():
    """Diagnostic fara a dezvalui cheia: 'ok' / 'missing' / 'short:<lungime>'."""
    raw = os.environ.get("BACKUP_ENCRYPTION_KEY")
    if raw is None:
        return "missing"
    n = len(raw.strip().strip("\"'").strip())
    return "ok" if n >= 24 else f"short:{n}"


def bk_encrypt(plain, key):
    """Compresie + AES-256-GCM. Nonce deterministic (HMAC din continut): aceleasi date -> acelasi
    text criptat, deci 'fara modificari' se detecteaza corect si git nu creeaza commit-uri inutile."""
    import zlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    data = zlib.compress(plain.encode("utf-8"), 9)
    nonce = hmac.new(hashlib.sha256(key + b"nonce").digest(), data, hashlib.sha256).digest()[:12]
    ct = AESGCM(key).encrypt(nonce, data, b"mtc-backup-v1")
    return json.dumps({"v": 1, "alg": "AES-256-GCM", "z": 1,
                       "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(ct).decode()}) + "\n"


def bk_decrypt(text, key):
    import zlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    o = json.loads(text)
    data = AESGCM(key).decrypt(base64.b64decode(o["n"]), base64.b64decode(o["c"]), b"mtc-backup-v1")
    return (zlib.decompress(data) if o.get("z") else data).decode("utf-8")


def gh_repo_private(tok, repo):
    return bool(gh_api("GET", f"/repos/{repo}", tok).get("private"))


def gh_api(method, path, tok, body=None, accept="application/vnd.github+json", raw=False):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=(json.dumps(body).encode("utf-8") if body is not None else None),
        method=method,
        headers={"Authorization": "Bearer " + tok, "Accept": accept,
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mtc-reclamatii-backup",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
            return data if raw else (json.loads(data.decode("utf-8")) if data else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"GitHub {e.code}: {detail}")


def backup_set_status(db, **fields):
    from firebase_admin import firestore as fb_firestore
    fields["updatedAt"] = fb_firestore.SERVER_TIMESTAMP
    db.collection("setari").document("github_backup_status").set(fields, merge=True)


def run_github_backup(by):
    global _backup_running
    try:
        db = get_firestore()
        tok, repo, br, missing = gh_cfg()
        if missing:
            raise RuntimeError("Lipsesc variabilele: " + ", ".join(missing))
        key = bk_key()
        if not key:
            raise RuntimeError("BACKUP_ENCRYPTION_KEY pe Render: " + {"missing": "variabila nu există (verifică numele exact)"}.get(bk_key_state(), "prea scurtă (" + bk_key_state() + " caractere, minim 24)") + ". Backup-ul nu se face niciodată necriptat.")
        if not gh_repo_private(tok, repo):
            raise RuntimeError("Repo-ul de backup este PUBLIC. Fă-l privat în GitHub (Settings → Danger Zone → Change repository visibility). Backup-ul a fost oprit.")
        backup_set_status(db, state="running", startedAt=datetime.now(timezone.utc).isoformat(), by=by, error="")
        files, counts = {}, {}
        for mod in BACKUP_MODULES:
            recs = bk_collect_module(db, mod)
            counts[mod["key"]] = len(recs)
            body = "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in recs) + "\n]\n"
            files[f"backup/{mod['key']}.enc"] = bk_encrypt(body, key)
        now = datetime.now(timezone.utc)
        manifest = {"format": "mtc-backup-v3-encrypted", "createdAt": now.isoformat(), "by": by, "counts": counts}
        files["backup/manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1) + "\n"

        ref = gh_api("GET", f"/repos/{repo}/git/ref/heads/{br}", tok)
        head_sha = ref["object"]["sha"]
        head_commit = gh_api("GET", f"/repos/{repo}/git/commits/{head_sha}", tok)
        base_tree = head_commit["tree"]["sha"]

        tree_items = []
        for path, content in files.items():
            if path.endswith("manifest.json"):
                continue  # manifestul (cu ora) il adaugam doar daca s-a schimbat altceva
            blob = gh_api("POST", f"/repos/{repo}/git/blobs", tok, {"content": content, "encoding": "utf-8"})
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        # sterge fisierele vechi necriptate (backup/<modul>.json), daca mai exista in ultimul commit
        old_tree = gh_api("GET", f"/repos/{repo}/git/trees/{base_tree}?recursive=1", tok)
        existing = {t["path"] for t in old_tree.get("tree", [])}
        for m in BACKUP_MODULES:
            p = f"backup/{m['key']}.json"
            if p in existing:
                tree_items.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
        tree = gh_api("POST", f"/repos/{repo}/git/trees", tok, {"base_tree": base_tree, "tree": tree_items})
        if tree["sha"] == base_tree:
            backup_set_status(db, state="ok", changed=False, counts=counts, lastCheckAt=now.isoformat(),
                              message="Fără modificări față de ultimul backup")
            return
        mblob = gh_api("POST", f"/repos/{repo}/git/blobs", tok, {"content": files["backup/manifest.json"], "encoding": "utf-8"})
        tree = gh_api("POST", f"/repos/{repo}/git/trees", tok, {
            "base_tree": base_tree,
            "tree": tree_items + [{"path": "backup/manifest.json", "mode": "100644", "type": "blob", "sha": mblob["sha"]}]})
        summary = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
        msg = f"Backup {now.strftime('%Y-%m-%d %H:%M')} UTC ({by}) — {summary}"
        commit = gh_api("POST", f"/repos/{repo}/git/commits", tok,
                        {"message": msg, "tree": tree["sha"], "parents": [head_sha]})
        gh_api("PATCH", f"/repos/{repo}/git/refs/heads/{br}", tok, {"sha": commit["sha"]})
        backup_set_status(db, state="ok", changed=True, counts=counts, lastBackupAt=now.isoformat(),
                          lastCheckAt=now.isoformat(), commit=commit["sha"],
                          url=f"https://github.com/{repo}/commit/{commit['sha']}", message=msg)
    except Exception as e:
        try:
            backup_set_status(get_firestore(), state="error", error=str(e)[:400])
        except Exception:
            pass
        print("Backup GitHub esuat:", e, flush=True)
    finally:
        with _backup_lock:
            _backup_running = False


def backup_authorized():
    """Admin (token Firebase) SAU secretul de cron. Intoarce (ok, by, response_dict_if_not_ok)."""
    secret = os.environ.get("BACKUP_CRON_SECRET", "")
    sent = request.headers.get("X-Backup-Secret", "")
    if secret and sent and hmac.compare_digest(secret, sent):
        return True, "automat (GitHub Actions)", None
    decoded = verify_admin_bearer_token()
    if decoded and decoded.get("uid") == ADMIN_UID:
        return True, "Admin", None
    return False, "", forbidden_admin_response(decoded)


@app.route("/backup/github/run", methods=["POST"])
def backup_github_run():
    global _backup_running
    ok, by, deny = backup_authorized()
    if not ok:
        return deny
    tok, repo, br, missing = gh_cfg()
    if missing:
        return jsonify({"error": "not_configured", "missing": missing}), 503
    with _backup_lock:
        if _backup_running:
            return jsonify({"ok": True, "state": "already_running"}), 202
        _backup_running = True
    threading.Thread(target=run_github_backup, args=(by,), daemon=True).start()
    return jsonify({"ok": True, "state": "started"}), 202


@app.route("/backup/github/list", methods=["POST"])
def backup_github_list():
    ok, by, deny = backup_authorized()
    if not ok or by != "Admin":
        return deny or (jsonify({"error": "forbidden"}), 403)
    tok, repo, br, missing = gh_cfg()
    if missing:
        return jsonify({"error": "not_configured", "missing": missing}), 503
    try:
        is_private = gh_repo_private(tok, repo)
        commits = gh_api("GET", f"/repos/{repo}/commits?sha={br}&path=backup/manifest.json&per_page=40", tok)
        return jsonify({"repo": repo, "private": is_private, "encrypted": bool(bk_key()), "keyState": bk_key_state(), "commits": [
            {"sha": c["sha"], "date": c["commit"]["committer"]["date"], "message": c["commit"]["message"].split("\n")[0]}
            for c in commits]})
    except Exception as e:
        return jsonify({"error": "github_error", "detail": str(e)}), 502


@app.route("/backup/github/get", methods=["POST"])
def backup_github_get():
    """Intoarce manifestul sau inregistrarile unui modul la un commit anume (pentru restaurare)."""
    ok, by, deny = backup_authorized()
    if not ok or by != "Admin":
        return deny or (jsonify({"error": "forbidden"}), 403)
    tok, repo, br, missing = gh_cfg()
    if missing:
        return jsonify({"error": "not_configured", "missing": missing}), 503
    body = request.get_json(silent=True) or {}
    sha, module = str(body.get("sha", "")), str(body.get("module", ""))
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha) or (module != "manifest" and module not in BACKUP_MODULE_KEYS):
        return jsonify({"error": "bad_request"}), 400
    try:
        if module == "manifest":
            raw = gh_api("GET", f"/repos/{repo}/contents/backup/manifest.json?ref={sha}", tok,
                         accept="application/vnd.github.raw+json", raw=True)
            return app.response_class(raw, mimetype="application/json")
        try:
            raw = gh_api("GET", f"/repos/{repo}/contents/backup/{module}.enc?ref={sha}", tok,
                         accept="application/vnd.github.raw+json", raw=True)
            key = bk_key()
            if not key:
                return jsonify({"error": "no_key", "detail": "BACKUP_ENCRYPTION_KEY lipsește de pe server"}), 503
            return app.response_class(bk_decrypt(raw.decode("utf-8"), key), mimetype="application/json")
        except RuntimeError as e:
            if "404" not in str(e):
                raise
        # commit vechi, dinainte de criptare
        raw = gh_api("GET", f"/repos/{repo}/contents/backup/{module}.json?ref={sha}", tok,
                     accept="application/vnd.github.raw+json", raw=True)
        return app.response_class(raw, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": "github_error", "detail": str(e)}), 502



# ══════════════════════════════════════════════════════
# ANGAJATI (citire prin server, fara a atinge regulile Firestore din proiectul Angajati)
# Necesita pe Render variabila ANGAJATI_SERVICE_ACCOUNT_JSON = continutul cheii de service
# account a proiectului angajati-f1b01 (Firebase Console -> Project settings -> Service accounts).
# Service account-ul ocoleste regulile, deci nu trebuie modificat nimic acolo. Doar citire.
# ══════════════════════════════════════════════════════
_ang_app = None
_ang_cache = {"t": 0, "docs": None}

def get_angajati_db():
    global _ang_app
    import firebase_admin
    from firebase_admin import credentials, firestore
    if _ang_app is None:
        raw = os.environ.get("ANGAJATI_SERVICE_ACCOUNT_JSON", "").strip()
        if not raw:
            raise RuntimeError("not_configured")
        cred = credentials.Certificate(json.loads(raw))
        _ang_app = firebase_admin.initialize_app(cred, name="angajati")
    return firestore.client(app=_ang_app)


@app.route("/angajati/list", methods=["POST", "OPTIONS"])
def angajati_list():
    if request.method == "OPTIONS":
        return ("", 204)
    decoded, deny = require_app_user()          # doar conturi reale ale aplicației
    if deny:
        return deny
    if decoded.get("uid") == "corina":          # spațiul Corinei nu are legătură cu Angajați
        return jsonify({"error": "forbidden"}), 403
    import time
    now = time.time()
    if _ang_cache["docs"] is not None and now - _ang_cache["t"] < 8:
        return jsonify({"ok": True, "docs": _ang_cache["docs"], "cached": True})
    try:
        db_ang = get_angajati_db()
    except RuntimeError as e:
        if str(e) == "not_configured":
            return jsonify({"error": "not_configured", "missing": ["ANGAJATI_SERVICE_ACCOUNT_JSON"]}), 503
        return jsonify({"error": "config_error", "detail": str(e)[:200]}), 500
    except Exception as e:
        return jsonify({"error": "config_error", "detail": f"{type(e).__name__}: {str(e)[:200]}"}), 500
    try:
        docs = []
        for d in db_ang.collection("angajati").stream():
            data = d.to_dict() or {}
            docs.append({"id": d.id, "data": {k: v for k, v in data.items() if isinstance(v, str)}})
        _ang_cache["docs"] = docs
        _ang_cache["t"] = now
        return jsonify({"ok": True, "docs": docs})
    except Exception as e:
        return jsonify({"error": "read_failed", "detail": f"{type(e).__name__}: {str(e)[:200]}"}), 500


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
