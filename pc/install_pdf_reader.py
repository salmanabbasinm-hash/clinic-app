# -*- coding: utf-8 -*-
# Downloads the free PDF reader library (pdf.js, Mozilla) next to clinic_sync.py so that
# "Word / PDF import" in the Diets screen can read PDF files.  Run once:  install_pdf_reader.bat
import os, sys, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/"
for name in ("pdf.min.js", "pdf.worker.min.js"):
    dest = os.path.join(HERE, name)
    print("downloading", name, "...")
    try:
        with urllib.request.urlopen(BASE + name, timeout=60) as r, open(dest, "wb") as f:
            f.write(r.read())
        print("  ok  ->", dest)
    except Exception as e:
        print("  FAILED:", e)
        print("  Open this link in the browser and save the file into the ClinicSync folder:")
        print("  ", BASE + name)
        sys.exit(1)
print("Done. Restart start_sync.bat")
