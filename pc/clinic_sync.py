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
HERE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))   # config / state / cloudflared live here
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
                    "Start-Process -FilePath '%s' -ArgumentList %s -Verb RunAs -Wait" % (path, arglist)], check=False)


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
        CREATE TABLE IF NOT EXISTS vtests(pid INTEGER, vid INTEGER, tests TEXT, PRIMARY KEY(pid, vid));
        CREATE TABLE IF NOT EXISTS kvx(k TEXT PRIMARY KEY, v TEXT, ts INTEGER, del INTEGER DEFAULT 0);
        """)

    def get_tests(self, pid, vid):
        r = self.c.execute("SELECT tests FROM vtests WHERE pid=? AND vid=?", (pid, vid)).fetchone()
        return r[0] if r else None

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
                "queue": queue, "time": int(time.time() * 1000)}

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
                pid = self.b.insert_patient(ch)
                self.s.set_map(uid, "patient", pid)
                print("  + new patient #%s" % pid)
                return {"uid": uid, "kind": "patient", "pid": pid, "status": "ok"}
            cur = self.b.get_patient(pid)
            if cur is None:
                return {"uid": uid, "kind": "patient", "pid": pid, "status": "error", "msg": "not found on PC"}
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
                d = dict(ch)
                d["note"] = note_with_tests(ch.get("note"), ch.get("tests"))
                vid = self.b.insert_visit(pid, d)
                self.s.set_tests(pid, vid, ch.get("tests") or "")
                self.s.set_map(uid, "visit", pid, vid)
                print("  + new visit %s/%s" % (pid, vid))
                return {"uid": uid, "kind": "visit", "pid": pid, "vid": vid, "status": "ok"}
            cur = self.b.get_visit(pid, vid)
            if cur is None:
                return {"uid": uid, "kind": "visit", "pid": pid, "vid": vid, "status": "error", "msg": "not found on PC"}
            if rhash(cur, VIS_KEYS) != ch.get("base"):
                same = all(cur.get(k) == ch.get(k) for k, _ in VIS_MAP)
                return {"uid": uid, "kind": "visit", "pid": pid, "vid": vid, "status": "ok" if same else "conflict"}
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
                    ok = win_action(act)
                    if ok and act == "exit":
                        threading.Timer(0.7, os._exit, [0]).start()
                    return self.send(200, {"ok": ok})
                if p == "/api/snapshot":
                    if not self.authed():
                        return self.send(401, {"error": "bad token"})
                    with LOCK:
                        return self.send(200, app.snapshot())
                files = {"/": ("app.html", "text/html; charset=utf-8"), "/index.html": ("app.html", "text/html; charset=utf-8"),
                         "/sw.js": ("sw.js", "application/javascript"), "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                         "/icon.svg": ("icon.svg", "image/svg+xml"),
                         "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
                         "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"), "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
                         "/jameel.woff": ("jameel.woff", "font/woff"),
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
                        return self.send(200, app.sync(body))
                    if p == "/api/sms/enqueue":
                        return self.send(200, app.enqueue(body))
                self.send(404, {"error": "not found"})
            except Exception as e:
                print("  ERROR", e)
                self.send(500, {"error": str(e)})
    return H


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


def win_action(a):
    """Window buttons for the chrome-less program window (Windows only, called from this PC only)."""
    if os.name != "nt":
        return False
    import ctypes
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    if a == "min":
        u.ShowWindow(h, 6)
    elif a == "full":
        u.keybd_event(0x7A, 0, 0, 0)
        u.keybd_event(0x7A, 0, 2, 0)
    elif a == "exit":
        u.PostMessageW(h, 0x0010, 0, 0)
    else:
        return False
    return True


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
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for exe in cands:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, "--app=" + url, "--start-fullscreen"])
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
    if os.name != "nt" or _fw_has_rule():
        return
    for _ in range(2):
        try:
            _run_admin("netsh", ["advfirewall", "firewall", "add", "rule", "name=Clinic Sync", "dir=in", "action=allow",
                                 "protocol=TCP", "localport=%d" % port, "profile=any"])
        except Exception:
            pass
        if _fw_has_rule():
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mdb", nargs="?")
    ap.add_argument("--sqlite")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--public", action="store_true", help="also open a public https link through cloudflared (use from anywhere)")
    ap.add_argument("--app", action="store_true", help="open the program in its own window (Edge/Chrome app mode) on this PC")
    a = ap.parse_args()
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
    if a.sqlite:
        if not os.path.exists(a.sqlite):
            init_empty_db(a.sqlite)
        be = SqliteBackend(a.sqlite)
    else:
        be = open_backend(a.mdb)
    app = App(be, State(os.path.join(HERE, "sync_state.db")), cfg["token"])
    ip = lan_ip()
    print("=" * 60)
    print(" Clinic Sync running.  Open on the phone (same Wi-Fi):")
    print("   http://%s:%d/?t=%s" % (ip, a.port, cfg["token"]))
    print(" Access database is read/written only on explicit phone sync.")
    print("=" * 60)
    if a.public:
        start_tunnel(a.port, cfg["token"])
    if a.app:
        threading.Thread(target=open_app_window, args=("http://127.0.0.1:%d/?t=%s" % (a.port, cfg["token"]),), daemon=True).start()
    open_firewall(a.port, cfgp, cfg)
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(app)).serve_forever()


def _fatal(title, text):
    """Show the error where the user can read it (the console window closes instantly otherwise)."""
    try:
        with open(os.path.join(HERE, "ClinicSync-error.txt"), "w", encoding="utf-8") as f:
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
