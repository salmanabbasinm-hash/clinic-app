# -*- coding: utf-8 -*-
"""
Clinic Sync Server  -  PC side
Connects the Access database (File1.mdb) with the mobile web app.

Run:   python clinic_sync.py "C:\\path\\File1.mdb"
Needs: Windows + Python 3 + `pip install pyodbc` + Microsoft Access ODBC driver
       (installed together with Microsoft Access / Access Database Engine).
Test mode (any OS):  python clinic_sync.py --sqlite test.db
The Access file is only changed by explicit phone changes (new patient / new visit /
edited fields) and SMS history rows. Nothing is ever deleted.
"""
import sys, os, re, json, time, hmac, uuid, sqlite3, hashlib, socket, threading, secrets, datetime, argparse, shutil, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

FROZEN = getattr(sys, "frozen", False)
_EXE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
if FROZEN and os.name == "nt" and os.environ.get("LOCALAPPDATA"):
    HERE = os.path.join(os.environ["LOCALAPPDATA"], "SalmanHerbalClinic")      # data lives here (survives updates / uninstall)
    try:
        os.makedirs(HERE, exist_ok=True)
    except Exception:
        HERE = _EXE_DIR
else:
    HERE = _EXE_DIR
RES = getattr(sys, "_MEIPASS", HERE) if FROZEN else HERE                                           # bundled app.html, fonts ...
FAILS = {}   # ip -> [count, first_time, blocked_until]
LOCK = threading.RLock()

# api key -> access column
PAT_MAP = [("name", "PatientName"), ("father", "Father/HusbandName"), ("gender", "Gender"), ("age", "Age"),
           ("address", "Adress"), ("phone", "Phone"), ("prevDoctor", "PreviousDoctor"),
           ("prevMethod", "PreviousMethod"), ("prevDuration", "PreviousDuration"),
           ("arrival", "CurrentArrivalCondition"), ("oldReports", "OldMedicalReports"), ("patDiet", "PatientDiet")]
VIS_MAP = [("date", "VisitDate"), ("pulse", "CurrentPulse"), ("symptoms", "Symptoms"), ("note", "Special Note"),
           ("days", "GivenDays"), ("status", "TreatmentStatus"), ("effects", "MedicineEfffects"),
           ("fees", "Fees"), ("paid", "PaidAmount"), ("weight", "Weight"), ("bp", "BloodPressure"), ("rx", "FinalPriscription")]
PAT_KEYS = [k for k, _ in PAT_MAP]
VIS_KEYS = [k for k, _ in VIS_MAP] + ["tests"]


def q(col):
    return "[%s]" % col


def norm(v):
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str):
        v = v.replace("\x00", "")
        return v
    return v


def rhash(d, keys):
    s = json.dumps([d.get(k) for k in keys], ensure_ascii=False, sort_keys=False)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def to_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except Exception:
        return None


def note_with_tests(note, tests, native=None):
    """Tests live in a multi-value Access field that ODBC cannot write, so the PC print also gets them as a
    'Tests:' line in the note (dropped again when the app edits the note)."""
    base = "\n".join(l for l in (note or "").split("\n") if not l.strip().startswith("Tests:")).strip()
    if tests and tests != (native or ""):
        base = (base + "\n" if base else "") + "Tests: " + tests
    return base or None


def to_num(v):
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except Exception:
        return None


def to_date(v):
    if not v:
        return None
    try:
        return datetime.datetime.strptime(str(v)[:10], "%Y-%m-%d")
    except Exception:
        return None


# ------------------------------------------------------------------ backends
class Backend:
    ph = "?"

    def fix(self, sql):
        return sql

    def rows(self, sql, args=()):
        cur = self.conn.cursor()
        cur.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def exec(self, sql, args=()):
        cur = self.conn.cursor()
        cur.execute(sql, args)
        self.conn.commit()
        return cur

    def list_patients(self):
        out = []
        for r in self.rows("SELECT PatientID, %s FROM [patient history]" % ", ".join(q(c) for _, c in PAT_MAP)):
            d = {"pid": r["PatientID"]}
            for k, c in PAT_MAP:
                d[k] = norm(r[c])
            d["age"] = to_int(d["age"])
            out.append(d)
        return out

    def list_visits(self):
        tests = self.tests_map()
        out = []
        for r in self.rows("SELECT PatientID, VisitID, %s FROM [Daily Treatment]" % ", ".join(q(c) for _, c in VIS_MAP)):
            d = {"pid": r["PatientID"], "vid": r["VisitID"]}
            for k, c in VIS_MAP:
                d[k] = norm(r[c])
            d["days"] = to_int(d["days"])
            d["date"] = d["date"][:10] if d["date"] else None
            d["tests"] = tests.get((d["pid"], d["vid"]), "")
            ov = self.overlay.get_tests(d["pid"], d["vid"]) if getattr(self, "overlay", None) else None
            if ov is not None:
                d["tests"] = ov
            d["native_tests"] = tests.get((d["pid"], d["vid"]), "")
            out.append(d)
        return out

    def tests_map(self):
        return {}

    def ensure_columns(self):
        """New columns added to [Daily Treatment] once if missing: PaidAmount, Weight, BloodPressure."""
        for col, ddl in (("PaidAmount", "DOUBLE"), ("Weight", "DOUBLE"), ("BloodPressure", "VARCHAR(60)")):
            cur = self.conn.cursor()
            cur.execute(self.fix("SELECT * FROM [Daily Treatment] WHERE 1=0"))
            have = [c[0].lower() for c in cur.description]
            if col.lower() in have:
                continue
            try:
                self.conn.rollback()
            except Exception:
                pass
            try:
                self.exec("ALTER TABLE [Daily Treatment] ADD COLUMN [%s] %s" % (col, ddl))
                self.conn.commit()
                print("  + column %s added to [Daily Treatment]" % col)
            except Exception as e:
                print("  !! could not add %s: %s" % (col, e))

    # ---- medicines (tblMedicine) and diets (MasterDiets): add / edit / delete from the phone
    def new_id(self, table, idcol, cols, vals):
        """INSERT and return the new id (AutoNumber in Access: read back with MAX)."""
        self.exec("INSERT INTO [%s] (%s) VALUES (%s)" % (table, ", ".join(q(c) for c in cols), ", ".join("?" * len(cols))), vals)
        return int(self.rows("SELECT MAX([%s]) AS m FROM [%s]" % (idcol, table))[0]["m"])

    def insert_med(self, name, usage):
        return self.new_id("tblMedicine", "ID", ["Medicines", "DefaultUsage"], [name, usage])

    def update_med(self, mid, name, usage):
        self.exec("UPDATE [tblMedicine] SET [Medicines]=?, [DefaultUsage]=? WHERE [ID]=?", [name, usage, mid])

    def delete_patient(self, pid):
        self.exec("DELETE FROM [Daily Treatment] WHERE [PatientID]=?", [pid])
        self.exec("DELETE FROM [patient history] WHERE [PatientID]=?", [pid])

    def delete_med(self, mid):
        self.exec("DELETE FROM [tblMedicine] WHERE [ID]=?", [mid])

    def insert_diet(self, cat, mizaj, text):
        return self.new_id("MasterDiets", "MizajID", ["Category", "MizajName", "DietDetail"], [cat, mizaj, text])

    def update_diet(self, did, cat, mizaj, text):
        self.exec("UPDATE [MasterDiets] SET [Category]=?, [MizajName]=?, [DietDetail]=? WHERE [MizajID]=?", [cat, mizaj, text, did])

    def list_meds(self):
        return [{"id": r["ID"], "name": norm(r["Medicines"]), "usage": norm(r["DefaultUsage"])}
                for r in self.rows("SELECT ID, Medicines, DefaultUsage FROM tblMedicine")]

    def list_diets(self):
        return [{"id": r["MizajID"], "cat": norm(r["Category"]), "mizaj": norm(r["MizajName"]), "text": norm(r["DietDetail"])}
                for r in self.rows("SELECT MizajID, Category, MizajName, DietDetail FROM MasterDiets")]

    def get_patient(self, pid):
        for p in self.list_patients():
            if p["pid"] == pid:
                return p

    def get_visit(self, pid, vid):
        for v in self.list_visits():
            if v["pid"] == pid and v["vid"] == vid:
                return v

    def patient_vals(self, d):
        vals = []
        for k, _ in PAT_MAP:
            v = d.get(k)
            if k == "age":
                v = to_int(v)
            if v == "":
                v = None
            vals.append(v)
        return vals

    def visit_vals(self, d):
        vals = []
        for k, _ in VIS_MAP:
            v = d.get(k)
            if k == "date":
                v = to_date(v)
            elif k == "days":
                v = to_int(v)
            elif k in ("paid", "weight"):
                v = to_num(v)
            elif v == "":
                v = None
            vals.append(v)
        return vals

    def update_patient(self, pid, d):
        sets = ", ".join("%s=?" % q(c) for _, c in PAT_MAP)
        self.exec("UPDATE [patient history] SET %s WHERE PatientID=?" % sets, self.patient_vals(d) + [pid])

    def update_visit(self, pid, vid, d):
        d = dict(d)
        sets = ", ".join("%s=?" % q(c) for _, c in VIS_MAP)
        self.exec("UPDATE [Daily Treatment] SET %s WHERE PatientID=? AND VisitID=?" % sets,
                  self.visit_vals(d) + [pid, vid])

    def next_visit_id(self, pid):
        r = self.rows("SELECT MAX(VisitID) AS m FROM [Daily Treatment] WHERE PatientID=%d" % int(pid))
        return (r[0]["m"] or 0) + 1

    def insert_visit(self, pid, d):
        vid = self.next_visit_id(pid)
        cols = ["PatientID", "VisitID"] + [c for _, c in VIS_MAP]
        self.exec("INSERT INTO [Daily Treatment] (%s) VALUES (%s)" % (", ".join(q(c) for c in cols), ", ".join("?" * len(cols))),
                  [pid, vid] + self.visit_vals(d))
        return vid

    def sms_history(self, kind, pid, vid, name, phone, text, when, status):
        if kind == "auto":
            self.exec("INSERT INTO tblAutoSMSHistory (PatientID, VisitID, PatientName, Phone, MessageText, SentDate) VALUES (?,?,?,?,?,?)",
                      [pid or 0, vid or 0, name, phone, text, when])
        else:
            t = "tblBulkSMSHistory" if kind == "bulk" else "tblManualSMSHistory"
            self.exec("INSERT INTO %s (PatientID, VisitID, PatientName, Phone, MessageText, SentDate, Status) VALUES (?,?,?,?,?,?,?)" % t,
                      [pid or 0, vid or 0, name, phone, text, when, status])


ACCESS_DRIVER_URL = "https://download.microsoft.com/download/3/5/C/35C84C36-661A-44E6-9324-8786B8DBE231/accessdatabaseengine_X64.exe"


def _has_access_driver():
    import pyodbc
    return any("Access Driver" in d for d in pyodbc.drivers())


def _msgbox(text, title="Clinic Sync", flags=0x24):
    """Windows Yes/No box (flags 0x24 = Yes/No + question). Returns True for Yes."""
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, text, title, flags) == 6
    except Exception:
        return True


VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def _download(url, name, min_mb=1):
    import urllib.request, tempfile
    for d in (os.path.join(RES, "drivers"), os.path.join(HERE, "drivers")):   # bundled inside the program: no internet needed
        b = os.path.join(d, name)
        if os.path.exists(b) and os.path.getsize(b) > min_mb * 1024 * 1024:
            return b
    dst = os.path.join(tempfile.gettempdir(), name)
    print("Downloading %s ..." % name)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last = -1
        while True:
            b = r.read(262144)
            if not b:
                break
            f.write(b)
            got += len(b)
            if total:
                pc = got * 100 // total
                if pc // 10 != last:
                    last = pc // 10
                    print("  %d%%" % pc)
    if os.path.getsize(dst) < min_mb * 1024 * 1024:
        raise RuntimeError("The download of %s looks incomplete. Check the internet and run again." % name)
    return dst


def _run_admin(path, args):
    import subprocess
    arglist = ",".join("'%s'" % a for a in args)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Start-Process -FilePath '%s' -ArgumentList %s -Verb RunAs -WindowStyle Hidden -Wait" % (path, arglist)], check=False,
                   creationflags=0x08000000 if os.name == "nt" else 0)


def _has_vcruntime():
    return os.path.exists(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "vcruntime140.dll"))


def ensure_access_driver():
    """Install what this PC lacks (Visual C++ runtime, then the Microsoft Access ODBC driver) - automatically."""
    if _has_access_driver():
        return
    if not _msgbox("This PC needs the Microsoft Access database driver (it is included in this program).\n\n"
                   "Install it now?\n(Windows will ask for permission - press Yes.)"):
        raise RuntimeError("pyodbc: Microsoft Access ODBC driver not found (driver). Installation was cancelled.")
    if not _has_vcruntime():
        print("Installing the Microsoft Visual C++ runtime first (press Yes if Windows asks) ...")
        _run_admin(_download(VCREDIST_URL, "vc_redist.x64.exe", 5), ["/install", "/quiet", "/norestart"])
        if not _has_vcruntime():
            raise RuntimeError("pyodbc: Microsoft Access ODBC driver not found (driver). The Visual C++ runtime did not install. "
                               "Install it manually: https://aka.ms/vs/17/release/vc_redist.x64.exe")
    print("Installing the Access driver (press Yes if Windows asks) ...")
    _run_admin(_download(ACCESS_DRIVER_URL, "accessdatabaseengine_X64.exe", 5), ["/quiet", "/norestart"])
    if not _has_access_driver():
        raise RuntimeError("pyodbc: Microsoft Access ODBC driver not found (driver). It did not install. "
                           "Install it manually: https://www.microsoft.com/download/details.aspx?id=54920 (choose the 64-bit file).")
    print("Access driver installed.")


class AccessBackend(Backend):
    def __init__(self, path):
        import pyodbc
        self.conn = pyodbc.connect(r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=%s;" % path, autocommit=False)

    def tests_map(self):
        # RequiredTests is a multi-value field in Access (read only through ODBC)
        m = {}
        try:
            for r in self.rows("SELECT PatientID, VisitID, [RequiredTests].Value AS T FROM [Daily Treatment]"):
                if r["T"]:
                    m.setdefault((r["PatientID"], r["VisitID"]), []).append(str(r["T"]))
        except Exception as e:
            print("  (tests column not readable: %s)" % e)
        return {k: ", ".join(v) for k, v in m.items()}

    def insert_patient(self, d):
        cols = [c for _, c in PAT_MAP]
        self.exec("INSERT INTO [patient history] (%s) VALUES (%s)" % (", ".join(q(c) for c in cols), ", ".join("?" * len(cols))),
                  self.patient_vals(d))
        return int(self.rows("SELECT @@IDENTITY AS i FROM [patient history]")[0]["i"])


class SqliteBackend(Backend):
    """Test backend with the same table names (used only for testing without Access)."""
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.text_factory = str
        self._extend()

    def _extend(self):
        """Extra fields of the program's own database (blood group, visit time)."""
        global PAT_MAP, VIS_MAP, PAT_KEYS, VIS_KEYS
        for tbl, k, col, mp in (("patient history", "blood", "BloodGroup", PAT_MAP), ("Daily Treatment", "vtime", "VisitTime", VIS_MAP)):
            have = [r[1] for r in self.conn.execute('PRAGMA table_info("%s")' % tbl)]
            if col not in have:
                self.conn.execute('ALTER TABLE "%s" ADD COLUMN "%s"' % (tbl, col))
            if (k, col) not in mp:
                mp.append((k, col))
        self.conn.commit()
        PAT_KEYS = [k for k, _ in PAT_MAP]
        VIS_KEYS = [k for k, _ in VIS_MAP] + ["tests"]

    def fix(self, sql):
        return sql.replace("[", '"').replace("]", '"')

    def rows(self, sql, args=()):
        return super().rows(self.fix(sql), args)

    def tests_map(self):
        m = {}
        try:
            for r in self.rows("SELECT PatientID, VisitID, T FROM _tests"):
                m.setdefault((r["PatientID"], r["VisitID"]), []).append(str(r["T"]))
        except Exception:
            return {}
        return {k: ", ".join(v) for k, v in m.items()}

    def exec(self, sql, args=()):
        sql = sql.replace("[", '"').replace("]", '"')
        args = [a.strftime("%Y-%m-%d") if isinstance(a, datetime.datetime) else a for a in args]
        return super().exec(sql, args)

    def new_id(self, table, idcol, cols, vals):
        n = (self.rows("SELECT MAX([%s]) AS m FROM [%s]" % (idcol, table))[0]["m"] or 0) + 1
        self.exec("INSERT INTO [%s] (%s) VALUES (%s)" % (table, ", ".join(q(c) for c in [idcol] + cols), ", ".join("?" * (len(cols) + 1))), [n] + vals)
        return n

    def insert_patient(self, d):
        cols = [c for _, c in PAT_MAP]
        cur = self.exec("INSERT INTO [patient history] (%s) VALUES (%s)" % (", ".join(q(c) for c in cols), ", ".join("?" * len(cols))),
                        self.patient_vals(d))
        return cur.lastrowid


# ------------------------------------------------------------------ sidecar state
class State:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS map(uid TEXT PRIMARY KEY, kind TEXT, pid INTEGER, vid INTEGER);
        CREATE TABLE IF NOT EXISTS sms_queue(id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, text TEXT, pid INTEGER, vid INTEGER,
                                              name TEXT, kind TEXT, created TEXT, status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS sms_done(uid TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS vmts(pid INTEGER, vid INTEGER, ts INTEGER, PRIMARY KEY(pid, vid));
        CREATE TABLE IF NOT EXISTS vtests(pid INTEGER, vid INTEGER, tests TEXT, PRIMARY KEY(pid, vid));
        CREATE TABLE IF NOT EXISTS pdel(pid INTEGER PRIMARY KEY, name TEXT, nv INTEGER, by TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS kvx(k TEXT PRIMARY KEY, v TEXT, ts INTEGER, del INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reps(id TEXT PRIMARY KEY, pu TEXT, pid INTEGER, name TEXT, ts INTEGER, w INTEGER, h INTEGER, del INTEGER DEFAULT 0);
        """)

    # ---- patient test reports (compressed page images kept in a folder next to the program data) ----
    def rep_dir(self):
        d = os.path.join(HERE, "reports")
        os.makedirs(d, exist_ok=True)
        return d

    def rep_put(self, d):
        import base64
        rid = re.sub(r"[^A-Za-z0-9_\-]", "", str(d.get("id") or ""))
        if not rid:
            raise ValueError("bad id")
        raw = base64.b64decode(d.get("data") or "")
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise ValueError("bad size")
        with open(os.path.join(self.rep_dir(), rid + ".jpg"), "wb") as f:
            f.write(raw)
        self.c.execute("INSERT OR REPLACE INTO reps VALUES (?,?,?,?,?,?,?,0)",
                       (rid, d.get("pu"), d.get("pid"), d.get("name") or "", int(d.get("ts") or 0), int(d.get("w") or 0), int(d.get("h") or 0)))
        self.c.commit()
        return {"ok": True, "id": rid}

    def rep_list(self):
        rows = self.c.execute("SELECT id,pu,pid,name,ts,w,h,del FROM reps ORDER BY ts").fetchall()
        return {"items": [{"id": r[0], "pu": r[1], "pid": r[2], "name": r[3], "ts": r[4], "w": r[5], "h": r[6], "del": bool(r[7])} for r in rows]}

    def rep_del(self, rid):
        rid = re.sub(r"[^A-Za-z0-9_\-]", "", str(rid or ""))
        self.c.execute("UPDATE reps SET del=1 WHERE id=?", (rid,))
        self.c.commit()
        try:
            os.remove(os.path.join(self.rep_dir(), rid + ".jpg"))
        except Exception:
            pass
        return {"ok": True}

    def rep_file(self, rid):
        rid = re.sub(r"[^A-Za-z0-9_\-]", "", str(rid or ""))
        fp = os.path.join(self.rep_dir(), rid + ".jpg")
        return open(fp, "rb").read() if os.path.exists(fp) else None

    def get_tests(self, pid, vid):
        r = self.c.execute("SELECT tests FROM vtests WHERE pid=? AND vid=?", (pid, vid)).fetchone()
        return r[0] if r else None

    def get_mts(self, pid, vid):
        r = self.c.execute("SELECT ts FROM vmts WHERE pid=? AND vid=?", (pid, vid)).fetchone()
        return int(r[0]) if r else 0

    def set_mts(self, pid, vid, ts):
        self.c.execute("INSERT OR REPLACE INTO vmts VALUES (?,?,?)", (pid, vid, int(ts or 0)))
        self.c.commit()

    def set_tests(self, pid, vid, tests):
        self.c.execute("INSERT OR REPLACE INTO vtests VALUES (?,?,?)", (pid, vid, tests or ""))
        self.c.commit()

    def x_apply(self, it):
        k, ts = it.get("k"), int(it.get("ts") or 0)
        if not k:
            return
        r = self.c.execute("SELECT ts FROM kvx WHERE k=?", (k,)).fetchone()
        if r and r[0] >= ts:
            return
        v = None if it.get("del") else json.dumps(it.get("v"), ensure_ascii=False)
        self.c.execute("INSERT OR REPLACE INTO kvx VALUES (?,?,?,?)", (k, v, ts, 1 if it.get("del") else 0))
        self.c.commit()

    def x_newer(self, known):
        out = []
        for k, v, ts, dl in self.c.execute("SELECT k,v,ts,del FROM kvx"):
            kt = known.get(k)
            if kt is None or ts > int(kt):
                out.append({"k": k, "ts": ts, "del": bool(dl), "v": (json.loads(v) if v and not dl else None)})
        return out

    def get_map(self, uid):
        return self.c.execute("SELECT pid, vid FROM map WHERE uid=?", (uid,)).fetchone()

    def set_map(self, uid, kind, pid, vid=None):
        self.c.execute("INSERT OR REPLACE INTO map VALUES (?,?,?,?)", (uid, kind, pid, vid))
        self.c.commit()


# ------------------------------------------------------------------ app logic
class App:
    def __init__(self, backend, state, token):
        self.b, self.s, self.token = backend, state, token
        backend.overlay = state
        try:
            backend.ensure_columns()
        except Exception as e:
            print("  (ensure_columns: %s)" % e)

    def snapshot(self):
        pats = self.b.list_patients()
        for p in pats:
            p["hash"] = rhash(p, PAT_KEYS)
        vis = self.b.list_visits()
        for v in vis:
            v["hash"] = rhash(v, VIS_KEYS)
        queue = [dict(zip(["id", "phone", "text", "pid", "vid", "name", "kind", "created"], r)) for r in
                 self.s.c.execute("SELECT id,phone,text,pid,vid,name,kind,created FROM sms_queue WHERE status='pending' ORDER BY id")]
        return {"patients": pats, "visits": vis, "meds": self.b.list_meds(), "diets": self.b.list_diets(),
                "queue": queue, "time": int(time.time() * 1000),
                "pdel": [dict(zip(["pid", "name", "nv", "by"], r)) for r in
                         self.s.c.execute("SELECT pid,name,nv,by FROM pdel ORDER BY ts")]}

    def sync(self, body):
        results = []
        for ch in body.get("patients", []):
            results.append(self.apply_patient(ch))
        for ch in body.get("visits", []):
            results.append(self.apply_visit(ch))
        for sm in body.get("sms", []):
            results.append(self.apply_sms(sm))
        for m in body.get("meds", []):
            results.append(self.apply_med(m))
        for rq in body.get("patDelReq") or []:
            try:
                self.s.c.execute("INSERT OR REPLACE INTO pdel VALUES (?,?,?,?,?)",
                                 (int(rq.get("pid")), rq.get("name") or "", int(rq.get("nv") or 0), body.get("dev") or "", int(time.time() * 1000)))
            except Exception as e:
                print("  !! pdel:", e)
        for k in body.get("patKeep") or []:
            try:
                self.s.c.execute("DELETE FROM pdel WHERE pid=?", (int(k),))
            except Exception as e:
                print("  !! pkeep:", e)
        self.s.c.commit()
        pdl = [x for x in (body.get("patDelOk") or [])]  # صرف صارف کی تصدیق کے بعد
        if pdl:
            try:
                make_backup()
            except Exception as e:
                print("  !! backup before delete:", e)
        for pid in pdl:
            results.append(self.apply_patient_del(pid))
        for mid in body.get("medDel", []):
            results.append(self.apply_med_del(mid))
        for d in body.get("diets", []):
            results.append(self.apply_diet(d))
        ex = body.get("extras") or {}
        for it in ex.get("items", []):
            try:
                self.s.x_apply(it)
                if not it.get("del"):
                    print("  ~ synced list %s" % it.get("k"))
            except Exception as e:
                print("  !! extras:", e)
        snap = self.snapshot()
        snap["results"] = results
        snap["extras"] = {"items": self.s.x_newer(ex.get("known") or {})}
        return snap

    def apply_med(self, m):
        uid = m.get("uid")
        try:
            name = (m.get("name") or "").strip()
            if not name:
                return {"uid": uid, "kind": "med", "status": "error", "msg": "name missing"}
            usage = m.get("usage") or None
            mid = m.get("id")
            if isinstance(mid, int):
                self.b.update_med(mid, name, usage)
            else:
                mid = self.b.insert_med(name, usage)
                print("  + new medicine #%s" % mid)
            return {"uid": uid, "kind": "med", "id": mid, "status": "ok"}
        except Exception as e:
            return {"uid": uid, "kind": "med", "status": "error", "msg": str(e)}

    def apply_patient_del(self, pid):
        try:
            p = int(pid) if str(pid).lstrip("-").isdigit() else pid
            self.b.delete_patient(p)
            try:
                self.s.c.execute("DELETE FROM pdel WHERE pid=?", (int(p),))
                self.s.c.commit()
            except Exception:
                pass
            print("  - patient %s deleted (with visits)" % pid)
            return {"uid": str(pid), "kind": "patdel", "status": "ok"}
        except Exception as e:
            return {"uid": str(pid), "kind": "patdel", "status": "error", "msg": str(e)}

    def apply_med_del(self, mid):
        try:
            self.b.delete_med(int(mid))
            print("  - medicine #%s deleted" % mid)
            return {"uid": str(mid), "kind": "meddel", "status": "ok"}
        except Exception as e:
            return {"uid": str(mid), "kind": "meddel", "status": "error", "msg": str(e)}

    def apply_diet(self, d):
        uid = d.get("uid")
        try:
            cat, miz = (d.get("cat") or "").strip(), (d.get("mizaj") or "").strip()
            if not cat or not miz:
                return {"uid": uid, "kind": "diet", "status": "error", "msg": "category/mizaj missing"}
            did = d.get("id")
            if isinstance(did, int):
                self.b.update_diet(did, cat, miz, d.get("text") or "")
            else:
                did = self.b.insert_diet(cat, miz, d.get("text") or "")
                print("  + new diet #%s (%s / %s)" % (did, cat, miz))
            return {"uid": uid, "kind": "diet", "id": did, "status": "ok"}
        except Exception as e:
            return {"uid": uid, "kind": "diet", "status": "error", "msg": str(e)}

    def apply_patient(self, ch):
        uid = ch.get("uid")
        try:
            pid = ch.get("pid") or (self.s.get_map(uid) or [None])[0]
            if not pid:
                # never create a second copy of the same person (re-push after a reset / lost reply)
                try:
                    nm = lambda x: re.sub(r"\s+", "", str(x or "")).lower()
                    for ex in self.b.list_patients():
                        if nm(ex.get("name")) and nm(ex.get("name")) == nm(ch.get("name")) and nm(ex.get("father")) == nm(ch.get("father")) \
                           and nm(ex.get("phone")) == nm(ch.get("phone")) and nm(ex.get("age")) == nm(ch.get("age")):
                            self.s.set_map(uid, "patient", ex["pid"])
                            print("  = patient already on PC #%s (no duplicate made)" % ex["pid"])
                            return {"uid": uid, "kind": "patient", "pid": ex["pid"], "status": "ok"}
                except Exception as e:
                    print("  (dup check: %s)" % e)
                pid = self.b.insert_patient(ch)
                self.s.set_map(uid, "patient", pid)
                print("  + new patient #%s" % pid)
                return {"uid": uid, "kind": "patient", "pid": pid, "status": "ok"}
            cur = self.b.get_patient(pid)
            if cur is None:
                return {"uid": uid, "kind": "patient", "pid": pid, "status": "ok", "skipped": True}  # PC سے حذف شدہ: اب غلطی نہیں گنی جاتی
            if rhash(cur, PAT_KEYS) != ch.get("base"):
                if rhash(cur, PAT_KEYS) == rhash(ch, PAT_KEYS):
                    return {"uid": uid, "kind": "patient", "pid": pid, "status": "ok"}
                return {"uid": uid, "kind": "patient", "pid": pid, "status": "conflict"}
            self.b.update_patient(pid, ch)
            return {"uid": uid, "kind": "patient", "pid": pid, "status": "ok"}
        except Exception as e:
            return {"uid": uid, "kind": "patient", "status": "error", "msg": str(e)}

    def apply_visit(self, ch):
        uid = ch.get("uid")
        try:
            pid = ch.get("pid")
            if not pid and ch.get("puid"):
                m = self.s.get_map(ch["puid"])
                pid = m[0] if m else None
            if not pid:
                return {"uid": uid, "kind": "visit", "status": "error", "msg": "patient unknown"}
            vid = ch.get("vid") or (self.s.get_map(uid) or [None, None])[1]
            if not vid:
                try:
                    for ex in self.b.list_visits():
                        if ex["pid"] == pid and (ex.get("date") or "") == str(ch.get("date") or "")[:10] and (ch.get("symptoms") or ch.get("rx")) \
                           and (ex.get("symptoms") or "") == (ch.get("symptoms") or "") and (ex.get("rx") or "") == (ch.get("rx") or ""):
                            self.s.set_map(uid, "visit", pid, ex["vid"])
                            print("  = visit already on PC %s/%s (no duplicate made)" % (pid, ex["vid"]))
                            return {"uid": uid, "kind": "visit", "pid": pid, "vid": ex["vid"], "status": "ok"}
                except Exception as e:
                    print("  (visit dup check: %s)" % e)
                d = dict(ch)
                d["note"] = note_with_tests(ch.get("note"), ch.get("tests"))
                vid = self.b.insert_visit(pid, d)
                self.s.set_tests(pid, vid, ch.get("tests") or "")
                self.s.set_map(uid, "visit", pid, vid)
                print("  + new visit %s/%s" % (pid, vid))
                return {"uid": uid, "kind": "visit", "pid": pid, "vid": vid, "status": "ok"}
            cur = self.b.get_visit(pid, vid)
            if cur is None:
                return {"uid": uid, "kind": "visit", "pid": pid, "vid": vid, "status": "ok", "skipped": True}
            mts = int(ch.get("mts") or 0)
            if rhash(cur, VIS_KEYS) != ch.get("base"):
                same = all(cur.get(k) == ch.get(k) for k, _ in VIS_MAP)
                if not same and mts > self.s.get_mts(pid, vid) and (str(cur.get("fees") or "") != str(ch.get("fees") or "") or str(cur.get("paid") or "") != str(ch.get("paid") or "")):
                    try:  # رقم کے معاملے میں سب سے آخر میں درج ہونے والی رقم جیتتی ہے
                        d2 = dict(cur)
                        d2["fees"] = ch.get("fees")
                        d2["paid"] = ch.get("paid")
                        self.b.update_visit(pid, vid, d2)
                        self.s.set_mts(pid, vid, mts)
                        print("  $ money updated from latest entry %s/%s" % (pid, vid))
                    except Exception as e:
                        print("  (money merge: %s)" % e)
                return {"uid": uid, "kind": "visit", "pid": pid, "vid": vid, "status": "ok" if same else "conflict"}
            if mts:
                self.s.set_mts(pid, vid, mts)
            d = dict(ch)
            d["note"] = note_with_tests(ch.get("note"), ch.get("tests"), cur.get("native_tests"))
            self.b.update_visit(pid, vid, d)
            self.s.set_tests(pid, vid, ch.get("tests") or "")
            return {"uid": uid, "kind": "visit", "pid": pid, "vid": vid, "status": "ok"}
        except Exception as e:
            return {"uid": uid, "kind": "visit", "status": "error", "msg": str(e)}

    def apply_sms(self, sm):
        uid = sm.get("uid")
        try:
            if self.s.c.execute("SELECT 1 FROM sms_done WHERE uid=?", (uid,)).fetchone():
                return {"uid": uid, "kind": "sms", "status": "ok"}
            when = datetime.datetime.fromtimestamp((sm.get("ts") or time.time() * 1000) / 1000.0)
            self.b.sms_history(sm.get("smsKind") or "manual", sm.get("pid"), sm.get("vid"), sm.get("name"),
                               sm.get("phone"), sm.get("text"), when, sm.get("status") or "Sent")
            self.s.c.execute("INSERT INTO sms_done VALUES (?)", (uid,))
            if sm.get("qid"):
                self.s.c.execute("UPDATE sms_queue SET status='done' WHERE id=?", (sm["qid"],))
            self.s.c.commit()
            return {"uid": uid, "kind": "sms", "status": "ok"}
        except Exception as e:
            return {"uid": uid, "kind": "sms", "status": "error", "msg": str(e)}

    def enqueue(self, body):
        dup = self.s.c.execute("SELECT 1 FROM sms_queue WHERE status='pending' AND pid=? AND vid=? AND kind=? AND text=?",
                               (body.get("pid"), body.get("vid"), body.get("kind") or "manual", body.get("text"))).fetchone()
        if dup:
            return {"ok": True, "duplicate": True}
        self.s.c.execute("INSERT INTO sms_queue (phone,text,pid,vid,name,kind,created) VALUES (?,?,?,?,?,?,?)",
                         (body.get("phone"), body.get("text"), body.get("pid"), body.get("vid"), body.get("name"),
                          body.get("kind") or "manual", datetime.datetime.now().isoformat(timespec="seconds")))
        self.s.c.commit()
        return {"ok": True}


def scan_page():
    """Scan one page from a flatbed/ADF scanner through Windows WIA (opens the Windows scan dialog)."""
    import base64, tempfile
    if os.name != "nt":
        return {"ok": False, "msg": "سکینر صرف ونڈوز PC پر چلتا ہے"}
    out = os.path.join(tempfile.gettempdir(), "clinic_scan_%d.jpg" % int(time.time() * 1000))
    ps = ("$ErrorActionPreference='Stop';try{$d=New-Object -ComObject WIA.CommonDialog;"
          "$i=$d.ShowAcquireImage(1,1,0,'{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}',$false,$true,$false);"
          "if($i -eq $null){exit 3};$i.SaveFile('%s')}catch{Write-Output $_.Exception.Message;exit 4}") % out.replace("'", "''")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=300, creationflags=0x08000000)
    except Exception as e:
        return {"ok": False, "msg": "سکین شروع نہ ہو سکا: %s" % e}
    if r.returncode == 3:
        return {"ok": False, "cancel": True, "msg": "سکین منسوخ ہوا"}
    if r.returncode != 0 or not os.path.exists(out):
        return {"ok": False, "msg": "سکینر نہیں ملا یا سکین نہ ہو سکا۔ سکینر آن کر کے دوبارہ کوشش کریں، یا «فائل منتخب کریں» استعمال کریں"}
    try:
        with open(out, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
    finally:
        try:
            os.remove(out)
        except Exception:
            pass
    return {"ok": True, "b64": data, "ext": "jpg"}


PORT = 8765


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            print("  %s %s" % (self.command, self.path.split("?")[0]))

        def cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.cors()
            self.end_headers()
            self.wfile.write(body)

        def client_ip(self):
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]

        def blocked(self):
            f = FAILS.get(self.client_ip())
            return bool(f and f[2] > time.time())

        def authed(self):
            ip = self.client_ip()
            if self.blocked():
                return False
            t = self.headers.get("X-Token", "") or parse_qs(urlparse(self.path).query).get("t", [""])[0]
            ok = hmac.compare_digest(t, app.token)
            if not ok and t:
                f = FAILS.setdefault(ip, [0, time.time(), 0])
                if time.time() - f[1] > 600:
                    f[0], f[1] = 0, time.time()
                f[0] += 1
                if f[0] >= 10:
                    f[2] = time.time() + 900
                    print("  too many wrong tokens from %s: blocked 15 min" % ip)
            return ok

        def do_OPTIONS(self):
            self.send(204, b"", "text/plain")

        def do_GET(self):
            p = urlparse(self.path).path
            try:
                if p == "/api/ping":
                    return self.send(200, {"ok": True, "name": "Clinic Sync", "auth": self.authed()})
                if p == "/api/pairinfo":
                    if self.client_address[0] not in ("127.0.0.1", "::1"):
                        return self.send(403, {"error": "local only"})
                    return self.send(200, {"url": "http://%s:%d/?t=%s" % (lan_ip(), PORT, app.token),
                                           "alts": ["http://%s:%d/?t=%s" % (i, PORT, app.token) for i in lan_ips()]})
                if p == "/api/win":
                    if self.client_address[0] not in ("127.0.0.1", "::1"):
                        return self.send(403, {"error": "local only"})
                    q = parse_qs(urlparse(self.path).query)
                    act = (q.get("a") or [""])[0]
                    if act == "upd":
                        u = (q.get("url") or [""])[0]
                        if not u.startswith("https://github.com/salmanabbasinm-hash/") or not u.endswith(".exe"):
                            return self.send(400, {"error": "bad url"})
                        if UPDST["state"] != "run":
                            threading.Thread(target=self_update, args=(u,), daemon=True).start()
                        return self.send(200, {"ok": True})
                    if act == "bk":
                        try:
                            return self.send(200, {"ok": True, "path": make_backup()})
                        except Exception as e:
                            return self.send(500, {"error": str(e)})
                    if act in ("opbk", "oppg"):
                        try:
                            tgt = backup_dir() if act == "opbk" else HERE
                            if os.name == "nt":
                                os.startfile(tgt)
                            return self.send(200, {"ok": True, "path": tgt})
                        except Exception as e:
                            return self.send(500, {"error": str(e)})
                    if act == "updst":
                        return self.send(200, dict(UPDST))
                    if act == "info":
                        return self.send(200, {"bl": bool(WIN.get("bl"))})
                    ok = win_action(act)
                    return self.send(200, {"ok": ok})
                if p == "/api/snapshot":
                    if not self.authed():
                        return self.send(401, {"error": "bad token"})
                    with LOCK:
                        return self.send(200, app.snapshot())
                if p == "/api/reps" or p.startswith("/api/rep/"):
                    if not self.authed():
                        return self.send(401, {"error": "bad token"})
                    with LOCK:
                        if p == "/api/reps":
                            return self.send(200, app.s.rep_list())
                        data = app.s.rep_file(p[len("/api/rep/"):])
                    if data is None:
                        return self.send(404, {"error": "no file"})
                    return self.send(200, data, "image/jpeg")
                files = {"/": ("app.html", "text/html; charset=utf-8"), "/index.html": ("app.html", "text/html; charset=utf-8"),
                         "/sw.js": ("sw.js", "application/javascript"), "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                         "/icon.svg": ("icon.svg", "image/svg+xml"),
                         "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
                         "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"), "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
                         "/jameel.woff": ("jameel.woff", "font/woff"),
                         "/kasheeda.woff": ("kasheeda.woff", "font/woff"),
                         "/pdf.min.js": ("pdf.min.js", "application/javascript"),
                         "/pdf.worker.min.js": ("pdf.worker.min.js", "application/javascript")}
                if p in files:
                    fn, ct = files[p]
                    fp = os.path.join(RES, fn)
                    if not os.path.exists(fp):
                        fp = os.path.join(HERE, fn)
                    if os.path.exists(fp):
                        return self.send(200, open(fp, "rb").read(), ct)
                self.send(404, {"error": "not found"})
            except Exception as e:
                self.send(500, {"error": str(e)})

        def do_POST(self):
            p = urlparse(self.path).path
            try:
                if not self.authed():
                    return self.send(401, {"error": "bad token"})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                with LOCK:
                    if p == "/api/sync":
                        r = app.sync(body)
                        try:
                            c = json.load(open(RELAY_CFGP)) if RELAY_CFGP and os.path.exists(RELAY_CFGP) else {}
                            if c.get("relay") or RELAY_DEF:
                                r["relay"] = c.get("relay") or RELAY_DEF
                        except Exception:
                            pass
                        return self.send(200, r)
                    if p == "/api/relay":
                        if self.client_address[0] not in ("127.0.0.1", "::1"):
                            return self.send(403, {"error": "local only"})
                        c = json.load(open(RELAY_CFGP)) if os.path.exists(RELAY_CFGP) else {}
                        if "url" in body:
                            c["relay"] = str(body.get("url") or "").strip().rstrip("/")
                        json.dump(c, open(RELAY_CFGP, "w"))
                        return self.send(200, {"ok": True})
                    if p == "/api/sms/enqueue":
                        return self.send(200, app.enqueue(body))
                    if p == "/api/clip":
                        if self.client_address[0] not in ("127.0.0.1", "::1"):
                            return self.send(403, {"error": "local only"})
                        return self.send(200, clip_file(body))
                    if p == "/api/scan":
                        return self.send(200, scan_page())
                    if p == "/api/rep":
                        return self.send(200, app.s.rep_put(body))
                    if p == "/api/repdel":
                        return self.send(200, app.s.rep_del(body.get("id")))
                self.send(404, {"error": "not found"})
            except Exception as e:
                print("  ERROR", e)
                self.send(500, {"error": str(e)})
    return H


def clip_file(body):
    """Save a PDF to a temp folder and put it on the Windows clipboard as a file (paste into WhatsApp with Ctrl+V)."""
    import base64, re, tempfile
    name = re.sub(r"[^\w\-. ]", "_", str(body.get("name") or "nuskha.pdf"))[:80] or "nuskha.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    d = os.path.join(tempfile.gettempdir(), "clinic_wa")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(body.get("b64") or ""))
    if os.name != "nt":
        return {"ok": False, "path": path}
    ps = "Set-Clipboard -LiteralPath '%s'" % path.replace("'", "''")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=20,
                       creationflags=0x08000000)
    wa = False
    num = re.sub(r"\D", "", str(body.get("phone") or ""))
    if r.returncode == 0 and num:
        try:
            import urllib.parse
            os.startfile("whatsapp://send?phone=%s&text=%s" % (num, urllib.parse.quote(str(body.get("text") or ""))))
            wa = True
        except Exception:
            wa = False
    return {"ok": r.returncode == 0, "path": path, "wa": wa}


def lan_ips():
    """All usable LAN addresses of this PC, real Wi-Fi/Ethernet first (VPN / virtual adapters last)."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        found.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for r in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(r[4][0])
    except Exception:
        pass
    def rank(ip):
        if ip.startswith("192.168."): return 0
        if ip.startswith("10."): return 1
        if ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31: return 2
        return 3
    out = []
    for ip in found:
        if ip.startswith("127.") or ip.startswith("169.254.") or ip in out:
            continue
        out.append(ip)
    out.sort(key=rank)
    return out


def lan_ip():
    l = lan_ips()
    return l[0] if l else "127.0.0.1"


WIN = {"hwnd": None, "bl": False}
UPDST = {"state": "idle", "pct": 0, "msg": ""}


APPWIN = {"pid": 0}


def kill_old_windows():
    """Close every leftover HealthClinic app window (Edge/Chrome started with our own profile folder) so only one window exists."""
    if os.name != "nt":
        return
    try:
        prof = os.path.join(HERE, "window").replace("'", "''")
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like '*--user-data-dir=" + prof + "*' } "
              "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                       creationflags=0x08000000, capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass


def self_update(url):
    """Download the new installer ourselves (no browser warning), run it and quit; it replaces the installed copy."""
    import urllib.request
    try:
        UPDST.update(state="run", pct=0, msg="")
        tmp = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "HealthClinic_update.exe")
        req = urllib.request.Request(url, headers={"User-Agent": "HealthClinic"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            tot = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                b = r.read(262144)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if tot:
                    UPDST["pct"] = int(got * 100 / tot)
        if os.path.getsize(tmp) < 1000000:
            raise RuntimeError("file too small")
        UPDST.update(state="done", pct=100)
        time.sleep(1.2)
        subprocess.Popen([tmp, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/SUPPRESSMSGBOXES"], creationflags=0x00000008 | 0x00000200, close_fds=True)
        time.sleep(0.5)
        try:
            if APPWIN["pid"] and os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(APPWIN["pid"])], capture_output=True, timeout=8)
        except Exception:
            pass
        kill_old_windows()
        os._exit(0)
    except Exception as e:
        UPDST.update(state="err", msg=str(e)[:120])



def make_borderless(pid):
    """Remove the Edge title bar/buttons of our app window (our own bar is drawn by the page instead)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes as wt
        u = ctypes.windll.user32
        found = []
        EP = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def cb(h, l):
            try:
                if not u.IsWindowVisible(h):
                    return True
                cls = ctypes.create_unicode_buffer(80)
                u.GetClassNameW(h, cls, 80)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                n = u.GetWindowTextLengthW(h)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, buf, n + 1)
                pv = wt.DWORD()
                u.GetWindowThreadProcessId(h, ctypes.byref(pv))
                if "Microsoft\u200b Edge" in buf.value or "- Microsoft" in buf.value or "Google Chrome" in buf.value:
                    return True
                if pv.value == pid or "Clinic" in buf.value or "کلینک" in buf.value:
                    found.append(h)
            except Exception:
                pass
            return True
        for _ in range(60):
            del found[:]
            u.EnumWindows(EP(cb), 0)
            if found:
                break
            time.sleep(0.5)
        if not found:
            return
        h = found[0]
        time.sleep(1.0)
        ok = False
        for _try in range(12):
            st = u.GetWindowLongW(h, -16)
            if st & 0x00C00000 == 0:
                ok = True
                break
            st &= ~(0x00C00000 | 0x00080000)
            u.SetWindowLongW(h, -16, st)
            ex = u.GetWindowLongW(h, -20)
            u.SetWindowLongW(h, -20, ex & ~(0x00000001 | 0x00000200 | 0x00020000))
            r = wt.RECT()
            u.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
            u.SetWindowPos(h, 0, r.left, r.top, r.right - r.left, r.bottom - r.top, 0x0020 | 0x0004 | 0x0040)
            time.sleep(1.0)
        if not ok:    # caption cannot be removed: slide the window up so the native bar is off-screen
            try:
                u.ShowWindow(h, 9)
                capH = u.GetSystemMetrics(4) + u.GetSystemMetrics(33) + u.GetSystemMetrics(92)
                r = wt.RECT()
                u.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
                u.SetWindowPos(h, 0, r.left - 1, r.top - capH, r.right - r.left + 2, r.bottom - r.top + capH, 0x0004 | 0x0040)
            except Exception:
                return
        WIN["hwnd"] = h
        WIN["bl"] = True
    except Exception:
        pass


def win_action(a):
    """Window buttons for our own title bar (Windows only, called from this PC only)."""
    if os.name != "nt":
        return False
    import ctypes
    u = ctypes.windll.user32
    h = u.GetForegroundWindow() or WIN.get("hwnd")
    if a == "min":
        u.ShowWindow(h, 6)
    elif a == "close":
        u.PostMessageW(h, 0x0010, 0, 0)
    else:
        return False
    return True



# ---- online relay (store-and-forward through the user's own free Cloudflare Worker; messages are AES-GCM encrypted) ----
RELAY_CFGP = None
RELAY_DEF = "https://clinic-relay.healthface6.workers.dev"   # shared clinic relay address (filled in by the developer; users need no setup)
def _relay_keys(token):
    return hashlib.sha256(("relay-box|" + token).encode()).hexdigest(), hashlib.sha256(("relay-key|" + token).encode()).digest()


def ws_open(url):
    import ssl, base64
    u = url.split("://", 1)
    scheme, rest = u[0], u[1]
    host, _, path = rest.partition("/")
    path = "/" + path
    port = 443 if scheme == "wss" else 80
    if ":" in host:
        host, port = host.rsplit(":", 1)
        port = int(port)
    s = socket.create_connection((host, port), 15)
    if scheme == "wss":
        s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: ClinicSync\r\n\r\n" % (path, host, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(1024)
        if not c:
            raise RuntimeError("ws closed")
        buf += c
    if b" 101 " not in buf.split(b"\r\n")[0] + b" ":
        raise RuntimeError("ws refused: " + buf.split(b"\r\n")[0].decode("latin1"))
    return s

def ws_send(s, op, data):
    n = len(data)
    h = bytes([0x80 | op])
    if n < 126:
        h += bytes([0x80 | n])
    elif n < 65536:
        h += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        h += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    mk = os.urandom(4)
    s.sendall(h + mk + bytes(b ^ mk[i % 4] for i, b in enumerate(data)))

def _rd(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise RuntimeError("ws closed")
        b += c
    return b

def ws_recv(s):
    while True:
        h = _rd(s, 2)
        op, n = h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = int.from_bytes(_rd(s, 2), "big")
        elif n == 127:
            n = int.from_bytes(_rd(s, 8), "big")
        data = _rd(s, n) if n else b""
        if op == 9:
            ws_send(s, 10, data)
            continue
        if op == 8:
            raise RuntimeError("ws closed by server")
        return op, data

def relay_loop(token):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception:
        print(" relay disabled: 'cryptography' package missing")
        return
    import base64, urllib.request
    box, key = _relay_keys(token)
    aes = AESGCM(key)
    def enc(o):
        n = os.urandom(12)
        return base64.b64encode(n + aes.encrypt(n, json.dumps(o, ensure_ascii=False).encode("utf-8"), None)).decode()
    def dec(s):
        b = base64.b64decode(s)
        return json.loads(aes.decrypt(b[:12], b[12:], None).decode("utf-8"))
    def call(url, data=None):
        rq = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "ClinicSync"})
        return urllib.request.urlopen(rq, timeout=25).read().decode("utf-8")
    seen = {}
    def cfg_base():
        try:
            cfg = json.load(open(RELAY_CFGP)) if RELAY_CFGP and os.path.exists(RELAY_CFGP) else {}
        except Exception:
            cfg = {}
        return (cfg.get("relay") or RELAY_DEF or "").rstrip("/")
    def drain(base):
        items = json.loads(call(base + "/poll/" + box)).get("items", [])
        for it in items:
            try:
                m = json.loads(it["msg"])
                if seen.get(it["dev"]) == m.get("id"):
                    continue
                req = dec(m["data"])
                rq = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, req["p"]), data=(req.get("b") or "").encode("utf-8") if req.get("m") == "POST" else None,
                                            headers={"Content-Type": "application/json", "X-Token": token}, method=req.get("m") or "GET")
                try:
                    r = urllib.request.urlopen(rq, timeout=60)
                    out = {"s": r.status, "b": r.read().decode("utf-8")}
                except Exception as e:
                    out = {"s": 500, "b": json.dumps({"error": str(e)})}
                call(base + "/r/" + box + "/" + it["dev"], json.dumps({"id": m.get("id"), "data": enc(out)}).encode())
                seen[it["dev"]] = m.get("id")
                print("  relay: handled request from", it["dev"])
            except Exception as e:
                print("  relay item error:", e)
    while True:
        base = cfg_base()
        if not base:
            time.sleep(10)
            continue
        # preferred: one idle WebSocket (the relay wakes us the moment a phone has something); fallback: slow polling
        try:
            drain(base)
            ws = ws_open(base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/" + box)
            print("  relay: live connection open")
            last_ping = last_drain = time.time()
            ws.settimeout(10)
            while True:
                if cfg_base() != base:
                    break
                try:
                    op, data = ws_recv(ws)
                    if op == 1 and data == b"q":
                        drain(base)
                        last_drain = time.time()
                except socket.timeout:
                    pass
                now = time.time()
                if now - last_ping > 45:
                    ws_send(ws, 1, b"ping")
                    last_ping = now
                if now - last_drain > 600:
                    drain(base)
                    last_drain = now
        except Exception as e:
            print("  relay connection:", str(e)[:80])
            try:
                time.sleep(45)
                drain(base)
            except Exception as e2:
                print("  relay poll:", str(e2)[:80])
                time.sleep(30)

def start_tunnel(port, token):
    exe = shutil.which("cloudflared") or shutil.which("cloudflared.exe")
    for cand in (os.path.join(HERE, "cloudflared.exe"), os.path.join(HERE, "cloudflared")):
        if not exe and os.path.exists(cand):
            exe = cand
    if not exe:
        print(" --public needs cloudflared. Put cloudflared.exe next to this script (see README).")
        return
    def run():
        p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", "http://localhost:%d" % port],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        shown = False
        for line in p.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not shown:
                shown = True
                link = "%s/?t=%s" % (m.group(0), token)
                open(os.path.join(HERE, "public_link.txt"), "w").write(link)
                print("=" * 60)
                print(" PUBLIC LINK (works from anywhere, keep it private):")
                print("   " + link)
                print("=" * 60)
    threading.Thread(target=run, daemon=True).start()


def open_app_window(url):
    """Open the clinic program in a chrome-less window (looks like a normal desktop program)."""
    time.sleep(1.5)
    kill_old_windows()
    time.sleep(0.5)
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for exe in cands:
        if os.path.exists(exe):
            try:
                prof = os.path.join(HERE, "window")
                t0 = time.time()
                p = subprocess.Popen([exe, "--app=" + url, "--start-fullscreen", "--user-data-dir=" + prof, "--no-first-run", "--kiosk-printing",
                                      "--no-default-browser-check", "--disable-features=Translate,TranslateUI",
                                      "--disable-translate", "--lang=en-US", "--force-ui-direction=ltr"])

                APPWIN["pid"] = p.pid

                def _watch(p=p, t0=t0):
                    try:
                        p.wait()
                        if time.time() - t0 > 8:
                            os._exit(0)
                    except Exception:
                        pass
                threading.Thread(target=_watch, daemon=True).start()
                return
            except Exception:
                pass
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def build_sqlite_from_mdb(mdb, dbpath):
    try:
        return _build_sqlite_from_mdb(mdb, dbpath)
    except Exception as e:
        try:
            h = open(mdb, "rb").read(0x2000)
            info = "format=%r ver=%d page2=%d size=%d" % (h[4:19], h[0x14], h[2 * 4096] if len(h) > 8192 else -1, os.path.getsize(mdb))
        except Exception:
            info = "unreadable"
        raise RuntimeError("Built-in reader could not read this Access file (%s): %s\nSend a photo of this message." % (info, e))


def _build_sqlite_from_mdb(mdb, dbpath):
    """Copy the Access file's data into a local SQLite file using the built-in reader (no Access driver needed)."""
    import struct
    from jet import DB
    db = DB(mdb)
    cat = {r["Name"]: r for r in db.catalog() if r["Type"] == 1}
    tmp = dbpath + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)

    def load(n, cols):
        _, rows = db.read_table(cat[n]["Id"] & 0xffffff)
        c.execute('CREATE TABLE "%s" (%s)' % (n, ", ".join('"%s"' % x for x in cols)))
        for r in rows:
            v = [r.get(x) for x in cols]
            v = [x if not isinstance(x, (bytes, tuple)) else None for x in v]
            v = [x.strftime("%Y-%m-%d %H:%M:%S") if isinstance(x, (datetime.datetime, datetime.date)) else x for x in v]
            c.execute('INSERT INTO "%s" VALUES (%s)' % (n, ",".join("?" * len(cols))), v)
        return rows

    load("patient history", ["PatientID", "PatientName", "Father/HusbandName", "Gender", "Age", "Adress", "Phone", "PreviousDoctor",
                             "PreviousMethod", "PreviousDuration", "CurrentArrivalCondition", "OldMedicalReports", "PatientDiet"])
    vrows = load("Daily Treatment", ["VisitID", "PatientID", "VisitDate", "CurrentPulse", "Symptoms", "Special Note", "GivenDays", "DaysCount",
                                     "TreatmentStatus", "MedicineEfffects", "EndDate", "Fees", "FinalPriscription"])
    load("tblMedicine", ["ID", "Medicines", "DefaultUsage"])
    load("MasterDiets", ["MizajID", "Category", "MizajName", "DietDetail"])
    c.execute("CREATE TABLE tblManualSMSHistory (HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,PatientID,VisitID,PatientName,Phone,MessageText,SentDate,Status)")
    c.execute("CREATE TABLE tblAutoSMSHistory (HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,PatientID,VisitID,PatientName,Phone,MessageText,SentDate)")
    c.execute("CREATE TABLE tblBulkSMSHistory (HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,PatientID,VisitID,PatientName,Phone,MessageText,SentDate,Status)")
    c.execute("CREATE TABLE _tests (PatientID, VisitID, T)")
    try:   # multi-value field "RequiredTests": values live in a hidden flat table
        tm = {}
        for t in db.catalog():
            if t["Type"] == 1 and t["Name"].startswith("f_") and "TempField" in t["Name"]:
                cols, rows = db.read_table(t["Id"] & 0xffffff)
                names = [x["name"] for x in cols]
                if "Value" in names and any("Daily Treatment" in x for x in names):
                    for r in rows:
                        tm.setdefault(r["_TempField*0"], []).append(r["Value"])
        for r in vrows:
            k = r.get("RequiredTests")
            if isinstance(k, bytes) and len(k) == 4:
                for val in tm.get(struct.unpack("<I", k)[0], []):
                    c.execute("INSERT INTO _tests VALUES (?,?,?)", (r["PatientID"], r["VisitID"], str(val)))
    except Exception as e:
        print("  (tests column not copied: %s)" % e)
    c.commit()
    c.close()
    if os.path.exists(dbpath):
        os.remove(dbpath)
    os.replace(tmp, dbpath)


MDB_PATH = None


def backup_dir():
    d = os.path.join(os.path.expanduser("~"), "Documents", "HealthClinic-Backup")
    os.makedirs(d, exist_ok=True)
    return d


APPREF = [None]


def make_backup():
    """Zip the program's data (consistent sqlite copy + settings) into Documents\\HealthClinic-Backup."""
    import zipfile, sqlite3, tempfile, shutil
    d = backup_dir()
    name = "HealthClinic-backup-%s.zip" % time.strftime("%Y-%m-%d_%H%M")
    out = os.path.join(d, name)
    tmp = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for fn in ("clinic_data.db", "sync_state.db"):
                src = os.path.join(HERE, fn)
                if os.path.exists(src):
                    dst = os.path.join(tmp, fn)
                    try:
                        a = sqlite3.connect(src)
                        b = sqlite3.connect(dst)
                        a.backup(b)
                        b.close()
                        a.close()
                    except Exception:
                        shutil.copyfile(src, dst)
                    z.write(dst, fn)
            cj = os.path.join(HERE, "sync_config.json")
            if os.path.exists(cj):
                z.write(cj, "sync_config.json")
            if MDB_PATH and os.path.exists(MDB_PATH):
                z.write(MDB_PATH, os.path.basename(MDB_PATH))
            rd = os.path.join(HERE, "reports")
            if os.path.isdir(rd):
                for fn in os.listdir(rd):
                    z.write(os.path.join(rd, fn), "reports/" + fn)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:   # فون کی "فائل ملائیں" سے ملنے والی JSON کاپی (مریض + وزٹس)
        import json as _j
        ap = APPREF[0]
        pats = ap.b.list_patients()
        for x in pats:
            x["uid"] = "p%s" % x["pid"]
        vis = ap.b.list_visits()
        for x in vis:
            x["uid"] = "v%s_%s" % (x["pid"], x["vid"])
            x["puid"] = "p%s" % x["pid"]
        with open(out[:-4] + ".json", "w", encoding="utf-8") as jf:
            _j.dump({"kind": "pc-backup", "time": int(time.time() * 1000), "patients": pats, "visits": vis}, jf, ensure_ascii=False)
    except Exception as e:
        print("  !! json backup:", e)
    for ext in (".json",):
        oj = sorted([f for f in os.listdir(d) if f.startswith("HealthClinic-backup-") and f.endswith(ext)])
        for f in oj[:-10]:
            try:
                os.remove(os.path.join(d, f))
            except Exception:
                pass
    olds = sorted([f for f in os.listdir(d) if f.startswith("HealthClinic-backup-") and f.endswith(".zip")])
    for f in olds[:-10]:
        try:
            os.remove(os.path.join(d, f))
        except Exception:
            pass
    return out


def backup_loop():
    """One automatic backup per day, at (or after) 12:00 noon."""
    time.sleep(90)
    while True:
        try:
            lt = time.localtime()
            if lt.tm_hour >= 12:
                d = backup_dir()
                pre = "HealthClinic-backup-" + time.strftime("%Y-%m-%d_", lt)
                done = [f for f in os.listdir(d) if f.startswith(pre) and f.endswith(".zip") and f[len(pre):len(pre) + 4] >= "1200"]
                if not done:
                    make_backup()
        except Exception:
            pass
        time.sleep(300)


def open_backend(mdb):
    """Access (if the driver works) else the built-in mode - the user never has to install anything."""
    reason = ""
    try:
        if not _has_access_driver():
            try:
                ensure_access_driver()
            except Exception as e:
                print("Driver install skipped: %s" % str(e)[:150])
        if _has_access_driver():
            return AccessBackend(mdb)
        reason = "Microsoft Access driver is not installed on this PC."
    except Exception as e:
        reason = str(e)[:200]
    dbp = os.path.join(HERE, "clinic_data.db")
    first = not os.path.exists(dbp)
    if first:
        print("Copying the patients data from the Access file (one time) ...")
        build_sqlite_from_mdb(mdb, dbp)
    print("Built-in mode (no Access driver needed). Data file: " + dbp)
    if first:
        _msgbox("Your Access file was copied into the program. It works without any Microsoft driver.\n\n"
                "Data file: %s\n(New records are saved there.)" % dbp, "Clinic Sync", 0x40)
    return SqliteBackend(dbp)


def init_empty_db(dbpath):
    c = sqlite3.connect(dbpath)
    c.execute('CREATE TABLE "patient history" ("PatientID" INTEGER PRIMARY KEY, "PatientName","Father/HusbandName","Gender","Age","Adress","Phone","PreviousDoctor","PreviousMethod","PreviousDuration","CurrentArrivalCondition","OldMedicalReports","PatientDiet")')
    c.execute('CREATE TABLE "Daily Treatment" ("VisitID" INTEGER,"PatientID","VisitDate","CurrentPulse","Symptoms","Special Note","GivenDays","DaysCount","TreatmentStatus","MedicineEfffects","EndDate","Fees","FinalPriscription","PaidAmount","Weight","BloodPressure")')
    c.execute('CREATE TABLE tblMedicine ("ID" INTEGER PRIMARY KEY,"Medicines","DefaultUsage")')
    c.execute('CREATE TABLE MasterDiets ("MizajID" INTEGER PRIMARY KEY,"Category","MizajName","DietDetail")')
    c.execute("CREATE TABLE tblManualSMSHistory (HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,PatientID,VisitID,PatientName,Phone,MessageText,SentDate,Status)")
    c.execute("CREATE TABLE tblAutoSMSHistory (HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,PatientID,VisitID,PatientName,Phone,MessageText,SentDate)")
    c.execute("CREATE TABLE tblBulkSMSHistory (HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,PatientID,VisitID,PatientName,Phone,MessageText,SentDate,Status)")
    c.execute("CREATE TABLE _tests (PatientID, VisitID, T)")
    c.commit()
    c.close()


def _fw_has_rule():
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=Clinic Sync"],
                           capture_output=True, text=True, creationflags=0x08000000)
        return r.returncode == 0 and "Clinic Sync" in (r.stdout or "")
    except Exception:
        return True


def open_firewall(port, cfgp, cfg):
    """Let the phone reach this PC on the Wi-Fi. Checked on every start (Windows asks permission only if the rule is missing)."""
    if os.name != "nt":
        return
    mk = os.path.join(os.path.dirname(os.path.abspath(cfgp)), "fw_asked")
    if os.path.exists(mk) or _fw_has_rule():
        return
    try:
        with open(mk, "w") as f:
            f.write("1")
    except Exception:
        pass
    try:
        _run_admin("netsh", ["advfirewall", "firewall", "add", "rule", "name=Clinic Sync", "dir=in", "action=allow",
                             "protocol=TCP", "localport=%d" % port, "profile=any"])
    except Exception:
        pass


def self_install():
    """Frozen Windows exe run from anywhere (Downloads etc.): install like normal software
    (copy to LocalAppData, Desktop + Start-menu shortcuts, Add/Remove Programs entry) then start installed copy."""
    if not (FROZEN and sys.platform == "win32") or "--noinstall" in sys.argv:
        return
    try:
        import ctypes, winreg
        exe = os.path.abspath(sys.executable)
        low = exe.lower()
        if "healthclinic-setup" in low or "salman herbal clinic" in low or "salmanherbalclinic" in low or "programs\\salman" in low:
            return
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "Programs", "SalmanHerbalClinic")
        target = os.path.join(d, "HealthClinic.exe")
        cnw = 0x08000000
        os.makedirs(d, exist_ok=True)
        for im in ("HealthClinic.exe", "ClinicSync.exe"):
            subprocess.run(["taskkill", "/F", "/FI", "PID ne %d" % os.getpid(), "/IM", im],
                           creationflags=cnw, capture_output=True, stdin=subprocess.DEVNULL)
        kill_old_windows()
        try:
            os.remove(os.path.join(d, "ClinicSync.exe"))
        except Exception:
            pass
        time.sleep(1.0)
        copied = True
        try:
            shutil.copy2(exe, target)
        except Exception:
            copied = os.path.exists(target)
        if not copied:
            return
        ps = (
            "$w=New-Object -ComObject WScript.Shell;"
            "foreach($q in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $q 'Salman Herbal Clinic.lnk')};"
            "$t='%s';$d='%s';$o=@();"
            "foreach($p in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){"
            "$f=Join-Path $p 'HealthClinic.lnk';"
            "$s=$w.CreateShortcut($f);"
            "$s.TargetPath=$t;$s.WorkingDirectory=$d;$s.IconLocation=$t+',0';$s.Description='HealthClinic';$s.Save();$o+=$f};"
            "$o -join '|'"
        ) % (target.replace("'", "''"), d.replace("'", "''"))
        r = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                           creationflags=cnw, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        lnks = [x.strip() for x in (r.stdout or "").strip().split("|") if x.strip()]
        try:
            key = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\SalmanHerbalClinic"
            lines = ["@echo off", "taskkill /F /IM HealthClinic.exe >nul 2>&1", "ping -n 3 127.0.0.1 >nul"]
            lines += ['del /f /q "%s"' % x for x in lnks]
            lines += ['reg delete %s /f >nul 2>&1' % key, "cd /d %TEMP%", 'rmdir /s /q "%s"' % d]
            with open(os.path.join(d, "uninstall.cmd"), "w", encoding="mbcs", errors="replace") as fh:
                fh.write("\r\n".join(lines) + "\r\n")
            un = 'cmd /c copy /y "%s" "%%TEMP%%\\shc_un.cmd" >nul & "%%TEMP%%\\shc_un.cmd"' % os.path.join(d, "uninstall.cmd")
            k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key.split("\\", 1)[1])
            for n, v in (("DisplayName", "HealthClinic"), ("Publisher", "HealthClinic"),
                         ("DisplayIcon", target), ("InstallLocation", d), ("UninstallString", un)):
                winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)
            winreg.CloseKey(k)
        except Exception:
            pass
        subprocess.Popen([target, "--noinstall"], cwd=d, creationflags=0x00000008 | 0x00000200, close_fds=True)
        try:
            time.sleep(2.5)
            ctypes.windll.user32.MessageBoxW(0, "HealthClinic install ho gaya.\nDesktop par icon bana diya gaya hai.\n\nانسٹالیشن مکمل ہو گئی ہے۔\nڈیسک ٹاپ پر آئکن بن گیا ہے۔", "HealthClinic", 0x40 | 0x40000 | 0x10000)
        except Exception:
            pass
        os._exit(0)
    except Exception:
        return


def main():
    self_install()
    ap = argparse.ArgumentParser()
    ap.add_argument("mdb", nargs="?")
    ap.add_argument("--noinstall", action="store_true")
    ap.add_argument("--sqlite")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--public", action="store_true", help="also open a public https link through cloudflared (use from anywhere)")
    ap.add_argument("--app", action="store_true", help="open the program in its own window (Edge/Chrome app mode) on this PC")
    a, _unk = ap.parse_known_args()
    if a.mdb and not os.path.exists(a.mdb):
        a.mdb = None
    cfgp = os.path.join(HERE, "sync_config.json")
    cfg = json.load(open(cfgp)) if os.path.exists(cfgp) else {}
    if "token" not in cfg:
        cfg["token"] = secrets.token_urlsafe(9)
        json.dump(cfg, open(cfgp, "w"))
    global PORT
    PORT = a.port
    if FROZEN:
        a.app = True
    try:    # already running? just open its window instead of failing on the busy port
        import urllib.request
        j = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % a.port, timeout=2).read().decode())
        if j.get("name") == "Clinic Sync":
            open_app_window("http://127.0.0.1:%d/?t=%s" % (a.port, cfg["token"]))
            return
    except Exception:
        pass
    if not a.sqlite and not a.mdb:
        a.sqlite = os.path.join(HERE, "clinic_data.db")   # the program's own database - nothing to install or choose
    global MDB_PATH
    MDB_PATH = a.mdb
    threading.Thread(target=backup_loop, daemon=True).start()
    if a.sqlite:
        if not os.path.exists(a.sqlite):
            init_empty_db(a.sqlite)
        be = SqliteBackend(a.sqlite)
    else:
        be = open_backend(a.mdb)
    app = App(be, State(os.path.join(HERE, "sync_state.db")), cfg["token"])
    APPREF[0] = app
    ip = lan_ip()
    print("=" * 60)
    print(" Clinic Sync running.  Open on the phone (same Wi-Fi):")
    print("   http://%s:%d/?t=%s" % (ip, a.port, cfg["token"]))
    print(" Access database is read/written only on explicit phone sync.")
    print("=" * 60)
    global RELAY_CFGP
    RELAY_CFGP = cfgp
    threading.Thread(target=relay_loop, args=(cfg["token"],), daemon=True).start()
    if a.public:
        start_tunnel(a.port, cfg["token"])
    if a.app:
        threading.Thread(target=open_app_window, args=("http://127.0.0.1:%d/?t=%s" % (a.port, cfg["token"]),), daemon=True).start()
    open_firewall(a.port, cfgp, cfg)
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(app)).serve_forever()


def _fatal(title, text):
    """Show the error where the user can read it (the console window closes instantly otherwise)."""
    try:
        with open(os.path.join(HERE, "HealthClinic-error.txt"), "w", encoding="utf-8") as f:
            f.write(title + "\n\n" + text)
    except Exception:
        pass
    print("\n" + "!" * 60 + "\n " + title + "\n\n" + text + "\n" + "!" * 60)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, title + "\n\n" + text[-1500:], "Clinic Sync", 0x10)
    except Exception:
        pass
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            _fatal("Clinic Sync could not start", "Exit code: %s\nUsage: pick the clinic Access file (File1.mdb) when asked." % e.code)
        raise
    except KeyboardInterrupt:
        pass
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        hint = ""
        low = tb.lower()
        if "pyodbc" in low and ("im002" in low or "data source name not found" in low or "driver" in low):
            hint = ("The Microsoft Access database driver is not installed on this PC.\n"
                    "Install 'Microsoft Access Database Engine 2016 Redistributable' (64-bit) from Microsoft, then run again.\n\n")
        elif "address already in use" in low or "10048" in low:
            hint = "Another Clinic Sync window is already running. Close it and run again.\n\n"
        _fatal("Clinic Sync stopped", hint + tb)
