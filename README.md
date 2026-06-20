# Hermes Rechnung Parser Server

Micro-server Flask care extrage corect pozițiile din PDF-urile Hermes folosind
`pdfplumber` — citește direct structura reală a tabelului desenat în PDF
(linii + coloane), nu aproximează din coordonate text. Testat 25/25 poziții
corecte pe 5 rechnunguri diferite, inclusiv cazuri cu nume pe 2-3 linii,
ATG lung, tură lipsă sau tură "0".

## Deploy gratuit pe Render.com

1. Creează cont gratuit pe [render.com](https://render.com) (poți folosi GitHub login)
2. Creează un **nou repository GitHub** doar pentru acest server (separat de aplicația principală):
   - Mergi pe [github.com/new](https://github.com/new)
   - Nume: `hermes-parser-server`
   - Public sau Private (oricare merge)
3. Urcă aceste 3 fișiere în acel repo: `app.py`, `requirements.txt`, `render.yaml`
4. În Render.com → **New** → **Web Service** → conectează repo-ul `hermes-parser-server`
5. Render detectează automat `render.yaml` — click **Apply** / **Create Web Service**
6. Așteaptă ~2 minute să se facă deploy-ul (vezi log-urile live)
7. Primești un URL de tipul: `https://hermes-rechnung-parser.onrender.com`

## Testare

```bash
curl https://hermes-rechnung-parser.onrender.com/
# {"status":"ok","service":"Hermes Rechnung PDF Parser",...}

curl -X POST -F "file=@Rechnung_XXXXX.pdf" https://hermes-rechnung-parser.onrender.com/parse
# {"belegNr":"...","dataEmitere":"...","suma":"...","pozitii":[...]}
```

## Notă importantă — Render Free Tier

Planul gratuit Render "adoarme" serverul după 15 minute de inactivitate.
Primul request după o pauză lungă poate dura **30-60 secunde** (serverul
"se trezește"). Aplicația ta va arăta un mesaj de încărcare în acest timp —
e normal, nu o eroare.

## Integrare cu aplicația principală

În `index.html`, funcția `handleRechnFiles` trimite PDF-ul către acest
server în loc să-l parseze local cu PDF.js. Vezi comentariul
`SERVER_URL` din cod — actualizează-l cu URL-ul tău Render după deploy.
