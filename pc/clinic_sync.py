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


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


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
                subprocess.Popen([exe, "--app=" + url, "--start-maximized"])
                return
            except Exception:
                pass
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


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
    if FROZEN:
        a.app = True
    if not a.sqlite and not a.mdb:
        a.mdb = cfg.get("mdb")
        if not a.mdb or not os.path.exists(a.mdb):
            a.mdb = None
            try:
                import tkinter
                from tkinter import filedialog
                r = tkinter.Tk(); r.withdraw(); r.attributes("-topmost", True)
                a.mdb = filedialog.askopenfilename(title="Select the clinic Access file (File1.mdb)", filetypes=[("Access database", "*.mdb *.accdb"), ("All files", "*.*")]) or None
                r.destroy()
            except Exception:
                pass
        if a.mdb:
            cfg["mdb"] = a.mdb
            json.dump(cfg, open(cfgp, "w"))
    if a.sqlite:
        be = SqliteBackend(a.sqlite)
    elif a.mdb:
        be = AccessBackend(a.mdb)
    else:
        ap.error("give the .mdb path")
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
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
