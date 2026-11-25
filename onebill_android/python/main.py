"""OneBill Windows (local-first). SQLite on this PC. No internet, no account.
Runs a tiny server on 127.0.0.1 only and opens the UI in an Edge/Chrome app window."""
import sys, os, json, sqlite3, threading, time, subprocess, re, webbrowser, hashlib, hmac, secrets, datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

APP = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
def data_dir():
    p = os.environ.get("ONEBILL_DATA")
    if p: d = Path(p)
    elif os.name == "nt" and Path("D:/").exists(): d = Path("D:/OneBill_Data")
    else: d = Path.home() / "OneBill_Data"
    (d / "backups").mkdir(parents=True, exist_ok=True)
    return d
DATA = data_dir(); DB = DATA / "onebill.db"; BK = DATA / "backups"
REG = DATA / "companies.json"; CUR = ["main"]
APP_VERSION = "0.10.1"
def load_registry():
    """Company registry. 'main' is the original database (existing data is never moved)."""
    try: r = json.loads(REG.read_text(encoding="utf-8"))
    except Exception: r = {}
    cs = [c for c in r.get("companies", []) if re.fullmatch(r"[a-z0-9_]{1,40}", str(c.get("id", "")))]
    if not any(c["id"] == "main" for c in cs): cs.insert(0, {"id": "main", "name": "Main company"})
    cur = r.get("current") if any(c["id"] == r.get("current") for c in cs) else "main"
    return {"current": cur, "companies": cs}
def save_registry(r):
    tmp = REG.with_suffix(".tmp"); tmp.write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8"); os.replace(tmp, REG)
def use_company(cid):
    global DB, BK
    if cid == "main": base = DATA
    else: base = DATA / "companies" / cid
    base.mkdir(parents=True, exist_ok=True); (base / "backups").mkdir(exist_ok=True)
    DB, BK = base / "onebill.db", base / "backups"; CUR[0] = cid
use_company(load_registry()["current"])
PORT = int(os.environ.get("ONEBILL_PORT", "8765"))
LOCK = threading.Lock(); LAST_PING = [0.0]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(
 id INTEGER PRIMARY KEY, sku TEXT UNIQUE, name TEXT NOT NULL,
 price INTEGER NOT NULL CHECK(typeof(price)='integer' AND price>=0),
 cost INTEGER NOT NULL DEFAULT 0 CHECK(typeof(cost)='integer' AND cost>=0),
 stock_milli INTEGER NOT NULL DEFAULT 0 CHECK(typeof(stock_milli)='integer'),
 track_stock INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS invoices(
 id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
 customer TEXT NOT NULL DEFAULT '', method TEXT NOT NULL DEFAULT 'cash',
 subtotal INTEGER NOT NULL, discount INTEGER NOT NULL DEFAULT 0, tax INTEGER NOT NULL, total INTEGER NOT NULL,
 tendered INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN('posted','void')), voided_at TEXT);
CREATE TABLE IF NOT EXISTS invoice_lines(
 id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
 product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL,
 qty_milli INTEGER NOT NULL CHECK(qty_milli>0), unit_price INTEGER NOT NULL, line_total INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS stock_moves(
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
 delta_milli INTEGER NOT NULL, reason TEXT NOT NULL, ref TEXT, created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS sm_noupd BEFORE UPDATE ON stock_moves BEGIN SELECT RAISE(ABORT,'stock_moves is append-only'); END;
CREATE TRIGGER IF NOT EXISTS sm_nodel BEFORE DELETE ON stock_moves BEGIN SELECT RAISE(ABORT,'stock_moves is append-only'); END;
CREATE TRIGGER IF NOT EXISTS il_noupd BEFORE UPDATE ON invoice_lines BEGIN SELECT RAISE(ABORT,'posted lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS il_nodel BEFORE DELETE ON invoice_lines BEGIN SELECT RAISE(ABORT,'posted lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS inv_nodel BEFORE DELETE ON invoices BEGIN SELECT RAISE(ABORT,'invoices cannot be deleted; void instead'); END;
CREATE TRIGGER IF NOT EXISTS inv_noupd BEFORE UPDATE ON invoices
 WHEN OLD.status='void' OR NEW.number<>OLD.number OR NEW.total<>OLD.total OR NEW.subtotal<>OLD.subtotal
   OR NEW.tax<>OLD.tax OR NEW.discount<>OLD.discount OR NEW.created_at<>OLD.created_at
 BEGIN SELECT RAISE(ABORT,'posted invoice is immutable (only posted->void)'); END;
CREATE TABLE IF NOT EXISTS parties(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL DEFAULT 'customer' CHECK(kind IN('customer','supplier')), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, party_id INTEGER NOT NULL REFERENCES parties(id),
 amount INTEGER NOT NULL CHECK(typeof(amount)='integer'), kind TEXT NOT NULL, ref TEXT, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS lg_noupd BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS lg_nodel BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, party_id INTEGER REFERENCES parties(id),
 total INTEGER NOT NULL, paid INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN('posted','void')), voided_at TEXT);
CREATE TABLE IF NOT EXISTS purchase_lines(id INTEGER PRIMARY KEY, purchase_id INTEGER NOT NULL REFERENCES purchases(id), product_id INTEGER NOT NULL REFERENCES products(id),
 name TEXT NOT NULL, qty_milli INTEGER NOT NULL CHECK(qty_milli>0), unit_cost INTEGER NOT NULL, line_total INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS credit_notes(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
 invoice_id INTEGER NOT NULL REFERENCES invoices(id), party_id INTEGER REFERENCES parties(id),
 subtotal INTEGER NOT NULL, discount INTEGER NOT NULL, tax INTEGER NOT NULL, total INTEGER NOT NULL, refund_cash INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS credit_note_lines(id INTEGER PRIMARY KEY, cn_id INTEGER NOT NULL REFERENCES credit_notes(id), invoice_line_id INTEGER NOT NULL REFERENCES invoice_lines(id),
 product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL, qty_milli INTEGER NOT NULL CHECK(qty_milli>0), unit_price INTEGER NOT NULL, line_total INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS cn_noupd BEFORE UPDATE ON credit_notes BEGIN SELECT RAISE(ABORT,'credit notes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cn_nodel BEFORE DELETE ON credit_notes BEGIN SELECT RAISE(ABORT,'credit notes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cnl_noupd BEFORE UPDATE ON credit_note_lines BEGIN SELECT RAISE(ABORT,'credit note lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cnl_nodel BEFORE DELETE ON credit_note_lines BEGIN SELECT RAISE(ABORT,'credit note lines are immutable'); END;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, display TEXT NOT NULL DEFAULT '',
 role TEXT NOT NULL CHECK(role IN('owner','admin','manager','employee')), perms TEXT NOT NULL DEFAULT '[]', readonly INTEGER NOT NULL DEFAULT 0,
 pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_login TEXT);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, username TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE TRIGGER IF NOT EXISTS ac_noupd BEFORE UPDATE ON activity BEGIN SELECT RAISE(ABORT,'activity log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ac_nodel BEFORE DELETE ON activity BEGIN SELECT RAISE(ABORT,'activity log is append-only'); END;
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE, kind TEXT NOT NULL CHECK(kind IN('cash','bank')),
 opening INTEGER NOT NULL DEFAULT 0 CHECK(typeof(opening)='integer'), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS acct_txns(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, account_id INTEGER NOT NULL REFERENCES accounts(id), amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount<>0),
 kind TEXT NOT NULL CHECK(kind IN('income','capital_in','drawings','transfer','loan_in','loan_repay')), grp TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN('posted','void')));
CREATE TRIGGER IF NOT EXISTS at_nodel BEFORE DELETE ON acct_txns BEGIN SELECT RAISE(ABORT,'account entries cannot be deleted; void instead'); END;
CREATE TRIGGER IF NOT EXISTS at_noupd BEFORE UPDATE ON acct_txns WHEN OLD.status='void' OR NEW.amount<>OLD.amount OR NEW.account_id<>OLD.account_id OR NEW.created_at<>OLD.created_at OR NEW.kind<>OLD.kind
 BEGIN SELECT RAISE(ABORT,'account entries are immutable (only posted->void)'); END;
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN('quotation','sales_order','purchase_order','challan')), number TEXT UNIQUE NOT NULL,
 created_at TEXT NOT NULL, party_id INTEGER REFERENCES parties(id), customer TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','delivered','converted','cancelled')),
 subtotal INTEGER NOT NULL, discount INTEGER NOT NULL DEFAULT 0, tax INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL, advance INTEGER NOT NULL DEFAULT 0, due_date TEXT NOT NULL DEFAULT '',
 note TEXT NOT NULL DEFAULT '', converted_to TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS doc_lines(id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES docs(id), product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL,
 qty_milli INTEGER NOT NULL CHECK(qty_milli>0), unit_price INTEGER NOT NULL, line_total INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS dl_noupd BEFORE UPDATE ON doc_lines BEGIN SELECT RAISE(ABORT,'document lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS dl_nodel BEFORE DELETE ON doc_lines BEGIN SELECT RAISE(ABORT,'document lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS d_nodel BEFORE DELETE ON docs BEGIN SELECT RAISE(ABORT,'documents cannot be deleted; cancel instead'); END;
CREATE TABLE IF NOT EXISTS purchase_returns(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, purchase_id INTEGER NOT NULL REFERENCES purchases(id),
 party_id INTEGER REFERENCES parties(id), total INTEGER NOT NULL, refund_cash INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS purchase_return_lines(id INTEGER PRIMARY KEY, pr_id INTEGER NOT NULL REFERENCES purchase_returns(id), purchase_line_id INTEGER NOT NULL REFERENCES purchase_lines(id),
 product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL, qty_milli INTEGER NOT NULL CHECK(qty_milli>0), unit_cost INTEGER NOT NULL, line_total INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS pr_noupd BEFORE UPDATE ON purchase_returns BEGIN SELECT RAISE(ABORT,'purchase returns are immutable'); END;
CREATE TRIGGER IF NOT EXISTS pr_nodel BEFORE DELETE ON purchase_returns BEGIN SELECT RAISE(ABORT,'purchase returns are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prl_noupd BEFORE UPDATE ON purchase_return_lines BEGIN SELECT RAISE(ABORT,'purchase return lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prl_nodel BEFORE DELETE ON purchase_return_lines BEGIN SELECT RAISE(ABORT,'purchase return lines are immutable'); END;
CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, category TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0), note TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN('posted','void')));
CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
 service TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'New' CHECK(status IN('New','Contacted','Won','Lost')));
CREATE TRIGGER IF NOT EXISTS pu_nodel BEFORE DELETE ON purchases BEGIN SELECT RAISE(ABORT,'purchases cannot be deleted; void instead'); END;
CREATE TRIGGER IF NOT EXISTS pl_nodel BEFORE DELETE ON purchase_lines BEGIN SELECT RAISE(ABORT,'purchase lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ex_nodel BEFORE DELETE ON expenses BEGIN SELECT RAISE(ABORT,'expenses cannot be deleted; void instead'); END;
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, party_id INTEGER NOT NULL REFERENCES parties(id),
 amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount>0), method TEXT NOT NULL DEFAULT 'cash', note TEXT NOT NULL DEFAULT '',
 ledger_id INTEGER REFERENCES ledger(id), status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN('posted','void')), voided_at TEXT);
CREATE TRIGGER IF NOT EXISTS pay_nodel BEFORE DELETE ON payments BEGIN SELECT RAISE(ABORT,'payments cannot be deleted; void instead'); END;
CREATE TRIGGER IF NOT EXISTS pay_noupd BEFORE UPDATE ON payments
 WHEN OLD.status='void' OR NEW.status<>'void' OR NEW.amount<>OLD.amount OR NEW.party_id<>OLD.party_id OR NEW.created_at<>OLD.created_at
   OR NEW.ledger_id IS NOT OLD.ledger_id OR NEW.method<>OLD.method OR NEW.note<>OLD.note
 BEGIN SELECT RAISE(ABORT,'payments are immutable (only posted->void)'); END;
CREATE TABLE IF NOT EXISTS payment_allocs(id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES payments(id), invoice_id INTEGER NOT NULL REFERENCES invoices(id),
 amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount>0), created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS pa_nodel BEFORE DELETE ON payment_allocs BEGIN SELECT RAISE(ABORT,'payment allocations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS pa_noupd BEFORE UPDATE ON payment_allocs BEGIN SELECT RAISE(ABORT,'payment allocations are immutable'); END;
CREATE TABLE IF NOT EXISTS stock_takes(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 username TEXT NOT NULL DEFAULT '', n_items INTEGER NOT NULL DEFAULT 0, n_changed INTEGER NOT NULL DEFAULT 0, value_diff INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock_take_lines(id INTEGER PRIMARY KEY, take_id INTEGER NOT NULL REFERENCES stock_takes(id), product_id INTEGER NOT NULL REFERENCES products(id),
 name TEXT NOT NULL, expected_milli INTEGER NOT NULL, counted_milli INTEGER NOT NULL CHECK(counted_milli>=0), diff_milli INTEGER NOT NULL, cost INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER IF NOT EXISTS stk_noupd BEFORE UPDATE ON stock_takes BEGIN SELECT RAISE(ABORT,'stock takes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS stk_nodel BEFORE DELETE ON stock_takes BEGIN SELECT RAISE(ABORT,'stock takes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS stl_noupd BEFORE UPDATE ON stock_take_lines BEGIN SELECT RAISE(ABORT,'stock take lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS stl_nodel BEFORE DELETE ON stock_take_lines BEGIN SELECT RAISE(ABORT,'stock take lines are immutable'); END;

CREATE TABLE IF NOT EXISTS shifts(id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', account_id INTEGER NOT NULL REFERENCES accounts(id),
 opened_at TEXT NOT NULL, opening_cash INTEGER NOT NULL DEFAULT 0 CHECK(typeof(opening_cash)='integer' AND opening_cash>=0),
 closed_at TEXT, counted_cash INTEGER CHECK(counted_cash IS NULL OR (typeof(counted_cash)='integer' AND counted_cash>=0)),
 expected_cash INTEGER, difference INTEGER, adj_grp TEXT NOT NULL DEFAULT '',
 open_note TEXT NOT NULL DEFAULT '', close_note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','closed')));
CREATE TRIGGER IF NOT EXISTS sh_nodel BEFORE DELETE ON shifts BEGIN SELECT RAISE(ABORT,'shifts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS sh_noupd BEFORE UPDATE ON shifts
 WHEN OLD.status='closed' OR NEW.status NOT IN('open','closed') OR (OLD.status='open' AND NEW.status='open' AND (NEW.opening_cash<>OLD.opening_cash OR NEW.opened_at<>OLD.opened_at OR NEW.account_id<>OLD.account_id))
 BEGIN SELECT RAISE(ABORT,'closed shifts are immutable'); END;
CREATE TABLE IF NOT EXISTS bank_recons(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
 statement_date TEXT NOT NULL, statement_balance INTEGER NOT NULL CHECK(typeof(statement_balance)='integer'),
 book_balance INTEGER NOT NULL CHECK(typeof(book_balance)='integer'), cleared_total INTEGER NOT NULL DEFAULT 0,
 difference INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS br_noupd BEFORE UPDATE ON bank_recons BEGIN SELECT RAISE(ABORT,'reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS br_nodel BEFORE DELETE ON bank_recons BEGIN SELECT RAISE(ABORT,'reconciliations cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS bank_recon_items(id INTEGER PRIMARY KEY, recon_id INTEGER NOT NULL REFERENCES bank_recons(id),
 account_id INTEGER NOT NULL REFERENCES accounts(id), txn_key TEXT NOT NULL, amount INTEGER NOT NULL, cleared_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS bri_noupd BEFORE UPDATE ON bank_recon_items BEGIN SELECT RAISE(ABORT,'reconciliation items are immutable'); END;
CREATE TRIGGER IF NOT EXISTS bri_nodel BEFORE DELETE ON bank_recon_items BEGIN SELECT RAISE(ABORT,'reconciliation items cannot be deleted'); END;
"""
DEFAULTS = {"shop_name": "My Shop", "currency": "Rs", "tax_bp": "0", "prefix": "INV",
            "inv_seq": "0", "pur_seq": "0", "cn_seq": "0", "prn_seq": "0", "low_stock": "5", "receipt_footer": "Thank you!",
            "country": "Pakistan", "timezone": "Asia/Karachi", "address": "", "phone": "", "email": "", "website": "", "tax_id": "", "tax_label": "Tax",
            "bank_info": "", "description": "", "card_account": "", "paper": "80mm", "orient": "portrait", "margin_mm": "5", "copies": "1",
            "theme_a": "classic", "theme_t": "compact", "print_opts": "", "doc_names": "", "terms": "", "logo": "", "signature": "", "invoice_title_tax": "0", "costing": "manual", "shifts_enabled": "0", "drive_enabled": "0", "drive_allow_plain": "0", "reminder_text": "Dear {name}, Rs {amount} is due on invoice {number} (due {due}). Thank you - {shop}."}

class ApiError(Exception):
    def __init__(s, msg, code=400): super().__init__(msg); s.code = code

def conn(path=None):
    c = sqlite3.connect(path or DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=10000")
    return c
def migrate(c):
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(invoices)")}
    if "party_id" not in cols: c.execute("ALTER TABLE invoices ADD COLUMN party_id INTEGER REFERENCES parties(id)")
    if "paid" not in cols: c.execute("ALTER TABLE invoices ADD COLUMN paid INTEGER NOT NULL DEFAULT 0")
    if "account_id" not in cols: c.execute("ALTER TABLE invoices ADD COLUMN account_id INTEGER REFERENCES accounts(id)")
    for col, ddl in (("line_disc", "INTEGER NOT NULL DEFAULT 0"), ("due_date", "TEXT NOT NULL DEFAULT ''"), ("ref_no", "TEXT NOT NULL DEFAULT ''"), ("notes", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols: c.execute(f"ALTER TABLE invoices ADD COLUMN {col} {ddl}")   # v0.5 invoice details
    lcols = {r["name"] for r in c.execute("PRAGMA table_info(invoice_lines)")}
    for col, ddl in (("disc", "INTEGER NOT NULL DEFAULT 0"), ("disc_bp", "INTEGER NOT NULL DEFAULT 0"), ("tax_bp", "INTEGER"), ("alloc_disc", "INTEGER NOT NULL DEFAULT 0"), ("tax_amt", "INTEGER")):
        if col not in lcols: c.execute(f"ALTER TABLE invoice_lines ADD COLUMN {col} {ddl}")   # v0.5; tax_amt NULL = old-style invoice
    def addcols(table, spec):   # v0.7: additive columns only, never rename or drop
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, ddl in spec:
            if col not in have: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    addcols("products", (("unit", "TEXT NOT NULL DEFAULT 'pcs'"), ("category", "TEXT NOT NULL DEFAULT ''")))   # v0.8: unit label + category
    addcols("parties", (("tax_no", "TEXT NOT NULL DEFAULT ''"),))
    addcols("purchases", (("tax", "INTEGER NOT NULL DEFAULT 0"), ("line_disc", "INTEGER NOT NULL DEFAULT 0")))
    addcols("purchase_lines", (("disc", "INTEGER NOT NULL DEFAULT 0"), ("disc_bp", "INTEGER NOT NULL DEFAULT 0"), ("tax_bp", "INTEGER"), ("tax_amt", "INTEGER")))   # tax_amt NULL = old-style purchase (no tax)
    addcols("purchase_returns", (("tax", "INTEGER NOT NULL DEFAULT 0"),))
    addcols("purchase_return_lines", (("tax_bp", "INTEGER"), ("tax_amt", "INTEGER")))
    addcols("credit_note_lines", (("tax_bp", "INTEGER"), ("tax_amt", "INTEGER"), ("alloc_disc", "INTEGER NOT NULL DEFAULT 0")))   # NULL tax_amt = credit note made before v0.7
    addcols("shifts", (("adj_grp", "TEXT NOT NULL DEFAULT ''"), ("open_note", "TEXT NOT NULL DEFAULT ''"), ("close_note", "TEXT NOT NULL DEFAULT ''")))
    addcols("bank_recons", (("cleared_total", "INTEGER NOT NULL DEFAULT 0"), ("difference", "INTEGER NOT NULL DEFAULT 0"), ("note", "TEXT NOT NULL DEFAULT ''"), ("username", "TEXT NOT NULL DEFAULT ''")))
    for k, v in DEFAULTS.items(): c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    if not c.execute("SELECT 1 FROM accounts").fetchone(): c.execute("INSERT INTO accounts(name,kind,opening) VALUES('Cash in hand','cash',0)")
def init():
    c = conn(); c.execute("PRAGMA journal_mode=WAL"); migrate(c); c.close()

def now(): return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def money(s):
    try: d = Decimal(str(s if s not in (None, "") else 0)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except InvalidOperation: raise ApiError("Invalid amount")
    if d < 0: raise ApiError("Amount cannot be negative")
    return int(d * 100)
def qty(s, allow_neg=False):
    try: d = Decimal(str(s)).quantize(Decimal("0.001"), ROUND_HALF_UP)
    except InvalidOperation: raise ApiError("Invalid quantity")
    if d < 0 and not allow_neg: raise ApiError("Quantity cannot be negative")
    return int(d * 1000)
def settings(c): return {r["k"]: r["v"] for r in c.execute("SELECT k,v FROM settings")}
def next_seq(c, key, prefix, table):
    last = c.execute(f"SELECT number FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    m = re.search(r"(\d+)$", last["number"]) if last else None
    r = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    seq = max(int(r["v"]) if r else 0, int(m.group(1)) if m else 0) + 1
    c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(seq)))
    return f"{prefix}-{seq:06d}"
def next_number(c, st): return next_seq(c, "inv_seq", st["prefix"], "invoices")
def seqs(c):
    out = {}
    for key, table in (("inv_seq", "invoices"), ("pur_seq", "purchases"), ("cn_seq", "credit_notes"), ("prn_seq", "purchase_returns")):
        last = c.execute(f"SELECT number FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
        m = re.search(r"(\d+)$", last["number"]) if last else None
        r = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        out[key] = max(int(r["v"]) if r else 0, int(m.group(1)) if m else 0)
    return out
def party_of(c, b):
    pid = b.get("party_id")
    if pid in (None, "", 0, "0"): return None
    p = c.execute("SELECT id FROM parties WHERE id=? AND active=1", (int(pid),)).fetchone()
    if not p: raise ApiError("Party not found")
    return p["id"]
def ledger(c, pid, amount, kind, ref=None, note=""):
    if pid and amount: return c.execute("INSERT INTO ledger VALUES(NULL,?,?,?,?,?,?)", (pid, amount, kind, ref, note, now())).lastrowid
def rng(q):
    t = dt.date.today().isoformat(); a = (q.get("from") or q.get("date") or [t])[0] or t
    return a, ((q.get("to") or q.get("date") or [a])[0] or a)

def api_products(c, q, b):
    cat = (q.get("category") or [None])[0]
    if q.get("uncategorized"): return [dict(r) for r in c.execute("SELECT * FROM products WHERE category='' ORDER BY active DESC, name")]   # ?uncategorized=1 = items with no category
    if cat is None: return [dict(r) for r in c.execute("SELECT * FROM products ORDER BY active DESC, name")]
    return [dict(r) for r in c.execute("SELECT * FROM products WHERE category=? ORDER BY active DESC, name", (cat,))]
def api_categories(c, q, b):
    """v0.8: categories are the distinct text values on products (no separate table). Returns name + number of items."""
    return [dict(r) for r in c.execute("SELECT category name, COUNT(*) n FROM products WHERE category<>'' GROUP BY category ORDER BY category COLLATE NOCASE")]
def tidy(v, n): return " ".join(str(v if v is not None else "").split())[:n]
def api_product_save(c, q, b):
    name = (b.get("name") or "").strip()
    if not name: raise ApiError("Name is required")
    sku = (b.get("sku") or "").strip() or None
    f = (name, sku, money(b.get("price")), money(b.get("cost")), 1 if b.get("track_stock", True) else 0, 1 if b.get("active", True) else 0)
    unit = (tidy(b.get("unit"), 12) or "pcs") if "unit" in b else None   # not sent on an edit = keep the stored value
    cat = tidy(b.get("category"), 40) if "category" in b else None       # sent empty = clear the category
    try:
        c.execute("BEGIN IMMEDIATE")
        if b.get("id"):
            c.execute("UPDATE products SET name=?,sku=?,price=?,cost=?,track_stock=?,active=? WHERE id=?", f + (int(b["id"]),))
            pid = int(b["id"])
            if unit is not None: c.execute("UPDATE products SET unit=? WHERE id=?", (unit, pid))
            if cat is not None: c.execute("UPDATE products SET category=? WHERE id=?", (cat, pid))
        else:
            pid = c.execute("INSERT INTO products(name,sku,price,cost,track_stock,active,unit,category) VALUES(?,?,?,?,?,?,?,?)", f + (unit or "pcs", cat or "")).lastrowid
            o = qty(b.get("opening", 0), True)
            if o: c.execute("UPDATE products SET stock_milli=? WHERE id=?", (o, pid)); \
                  c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (pid, o, "opening", None, now()))
        c.execute("COMMIT")
    except sqlite3.IntegrityError as e:
        c.execute("ROLLBACK"); raise ApiError("SKU already used" if "sku" in str(e) else str(e))
    return {"id": pid}
def api_stock(c, q, b):
    d = qty(b.get("delta"), True); pid = int(b["product_id"])
    if d == 0: raise ApiError("Enter a non-zero quantity")
    c.execute("BEGIN IMMEDIATE")
    r = c.execute("SELECT stock_milli FROM products WHERE id=?", (pid,)).fetchone()
    if not r: c.execute("ROLLBACK"); raise ApiError("No such product", 404)
    if r["stock_milli"] + d < 0: c.execute("ROLLBACK"); raise ApiError("Stock cannot go below zero")
    c.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?", (d, pid))
    c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (pid, d, (b.get("reason") or "adjustment")[:60], None, now()))
    c.execute("COMMIT"); return {"ok": True}

def take_plan(c, b):
    """v0.8 stock take: validate the count sheet and work out the differences against the stock the system holds right now.
    Items with a blank counted quantity are not part of the take. Counted can never be negative, so stock never goes below zero."""
    lines, seen = [], set()
    for it in (b.get("items") or []):
        if it.get("counted") in (None, ""): continue
        pid = int(it["product_id"])
        if pid in seen: raise ApiError("The same item is on the count sheet twice")
        seen.add(pid)
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p: raise ApiError("Item not found")
        if not p["track_stock"]: raise ApiError(f"Stock is not tracked for: {p['name']}")
        cm = qty(it["counted"])
        d = cm - p["stock_milli"]
        v = (abs(d) * p["cost"] + 500) // 1000 * (1 if d >= 0 else -1)
        lines.append({"product_id": pid, "name": p["name"], "unit": p["unit"], "expected_milli": p["stock_milli"], "counted_milli": cm, "diff_milli": d, "cost": p["cost"], "value_diff": v})
    if not lines: raise ApiError("Enter at least one counted quantity")
    return lines
def api_stock_take_preview(c, q, b):
    lines = take_plan(c, b)
    return {"lines": lines, "n_items": len(lines), "n_changed": sum(1 for l in lines if l["diff_milli"]), "value_diff": sum(l["value_diff"] for l in lines)}
def api_stock_take(c, q, b):
    c.execute("BEGIN IMMEDIATE")
    lines = take_plan(c, b)          # recomputed inside the transaction: the server never trusts the preview
    number = next_seq(c, "st_seq", "ST", "stock_takes")
    by = b.get("_by"); user = (by or {}).get("username", "") if isinstance(by, dict) else ""
    tid = c.execute("INSERT INTO stock_takes(number,created_at,note,username,n_items,n_changed,value_diff) VALUES(?,?,?,?,?,?,?)",
                    (number, now(), tidy(b.get("note"), 120), user, len(lines), sum(1 for l in lines if l["diff_milli"]), sum(l["value_diff"] for l in lines))).lastrowid
    for l in lines:
        c.execute("INSERT INTO stock_take_lines(take_id,product_id,name,expected_milli,counted_milli,diff_milli,cost) VALUES(?,?,?,?,?,?,?)",
                  (tid, l["product_id"], l["name"], l["expected_milli"], l["counted_milli"], l["diff_milli"], l["cost"]))
        if l["diff_milli"]:
            c.execute("UPDATE products SET stock_milli=? WHERE id=?", (l["counted_milli"], l["product_id"]))
            c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (l["product_id"], l["diff_milli"], "stock_take", number, now()))
    c.execute("COMMIT"); return api_stock_take_get(c, {"id": [str(tid)]}, {})
def api_stock_takes(c, q, b):
    return [dict(r) for r in c.execute("SELECT * FROM stock_takes ORDER BY id DESC LIMIT 200")]
def api_stock_take_get(c, q, b):
    t = c.execute("SELECT * FROM stock_takes WHERE id=?", (int((q.get("id") or ["0"])[0]),)).fetchone()
    if not t: raise ApiError("Stock take not found", 404)
    ls = [dict(r) for r in c.execute("SELECT l.*, p.unit, (ABS(l.diff_milli)*l.cost+500)/1000*(CASE WHEN l.diff_milli<0 THEN -1 ELSE 1 END) value_diff FROM stock_take_lines l JOIN products p ON p.id=l.product_id WHERE l.take_id=? ORDER BY l.id", (t["id"],))]
    return {**dict(t), "lines": ls}

def pct_bp(v, what="Percent"):
    """'17' or '12.5' -> basis points (1700 / 1250). Blank -> None. Range 0-100 %."""
    if v in (None, ""): return None
    try: d = Decimal(str(v).replace("%", "").strip()).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except InvalidOperation: raise ApiError(f"{what} is not a number")
    if d < 0 or d > 100: raise ApiError(f"{what} must be between 0 and 100")
    return int(d * 100)
def split_by_weight(total, weights):
    """Share `total` (integer paisa) over `weights` with no paisa lost (largest remainder). Used for the invoice-level discount."""
    s = sum(weights)
    if total <= 0 or s <= 0: return [0] * len(weights)
    base = [total * w // s for w in weights]; rem = total - sum(base)
    order = sorted(range(len(weights)), key=lambda k: (-(total * weights[k] % s), k))
    for k in order[:rem]: base[k] += 1
    return base
def clean_date(v):
    v = (v or "").strip()
    if not v: return ""
    try: dt.date.fromisoformat(v)
    except ValueError: raise ApiError("Due date must be a valid date (YYYY-MM-DD)")
    return v

def api_sale(c, q, b, _prices=None):
    items = b.get("items") or []
    if not items: raise ApiError("Cart is empty")
    c.execute("BEGIN IMMEDIATE")
    st = settings(c); lines = []; sub = 0; line_disc = 0; detailed = False; dflt = int(st["tax_bp"])
    for it in items:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (int(it["product_id"]),)).fetchone()
        if not p: raise ApiError("Product not found or inactive")
        qm = qty(it["qty"])
        if qm <= 0: raise ApiError("Quantity must be above zero")
        if p["track_stock"] and p["stock_milli"] < qm: raise ApiError(f"Not enough stock: {p['name']}")
        up = _prices[int(it["product_id"])] if _prices else p["price"]   # _prices only comes from internal conversions (quoted price)
        gross = (up * qm + 500) // 1000          # integer half-up, no floats
        d_amt, d_bp = 0, 0                       # v0.5: optional per-line discount, either an amount or a percent (not both)
        has_amt, has_pct = it.get("disc") not in (None, "", 0, "0"), it.get("disc_pct") not in (None, "", 0, "0")
        if has_amt and has_pct: raise ApiError(f"Use either a discount amount or a percent, not both: {p['name']}")
        if has_pct: d_bp = pct_bp(it.get("disc_pct"), "Discount percent"); d_amt = (gross * d_bp + 5000) // 10000
        elif has_amt: d_amt = money(it.get("disc"))
        if d_amt > gross: raise ApiError(f"Line discount is larger than the line amount: {p['name']}")
        tbp = pct_bp(it.get("tax_pct"), "Tax percent")   # None = use the company default rate
        if d_amt or tbp is not None: detailed = True
        lt = gross - d_amt                        # line_total = amount after line discount (what reports add up)
        lines.append({"p": p, "qm": qm, "lt": lt, "up": up, "d": d_amt, "dbp": d_bp, "tbp": tbp}); sub += lt; line_disc += d_amt
    disc = money(b.get("discount"))
    if disc > sub: raise ApiError("Discount is larger than subtotal")
    if detailed:   # per-line tax: invoice discount is shared over the lines, then each line is taxed at its own rate
        for l, a in zip(lines, split_by_weight(disc, [x["lt"] for x in lines])):
            l["alloc"] = a; r = dflt if l["tbp"] is None else l["tbp"]; l["tax"] = ((l["lt"] - a) * r + 5000) // 10000
        tax = sum(l["tax"] for l in lines)
    else:
        tax = ((sub - disc) * dflt + 5000) // 10000   # old behaviour, unchanged when no line discount / tax is used
        for l in lines: l["alloc"] = 0; l["tax"] = None
    total = sub - disc + tax
    method = b.get("method") if b.get("method") in ("cash", "card", "other") else "cash"
    tendered = money(b.get("tendered")) if b.get("tendered") not in (None, "") else total
    pid = party_of(c, b)
    if pid: paid = min(tendered, total)                # credit sale: the rest goes to the party's account
    else:
        if method == "cash" and tendered < total: raise ApiError("Cash received is less than total")
        paid = total
    cust = (b.get("customer") or "")[:80]
    if pid: cust = c.execute("SELECT name FROM parties WHERE id=?", (pid,)).fetchone()["name"]
    due, ref_no, notes = clean_date(b.get("due_date")), (b.get("ref_no") or "").strip()[:40], (b.get("notes") or "").strip()[:200]
    number = next_number(c, st)
    iid = c.execute("INSERT INTO invoices(number,created_at,customer,method,subtotal,discount,tax,total,tendered,party_id,paid,account_id,line_disc,due_date,ref_no,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (number, now(), cust, method, sub, disc, tax, total, tendered, pid, paid, sale_account(c, method), line_disc, due, ref_no, notes)).lastrowid
    for l in lines:
        p, qm = l["p"], l["qm"]
        c.execute("INSERT INTO invoice_lines(invoice_id,product_id,name,qty_milli,unit_price,line_total,disc,disc_bp,tax_bp,alloc_disc,tax_amt) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (iid, p["id"], p["name"], qm, l["up"], l["lt"], l["d"], l["dbp"], (dflt if l["tbp"] is None else l["tbp"]) if detailed else None, l["alloc"], l["tax"]))
        if p["track_stock"]:
            c.execute("UPDATE products SET stock_milli=stock_milli-? WHERE id=?", (qm, p["id"]))
            c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (p["id"], -qm, "sale", number, now()))
    ledger(c, pid, total - paid, "sale", number)
    c.execute("COMMIT")
    return api_invoice(c, {"id": [str(iid)]}, {})

def api_invoices(c, q, b):
    d = (q.get("date") or [""])[0]; pid = (q.get("party_id") or [""])[0]; w, a = [], []
    if d: w.append("substr(i.created_at,1,10)=?"); a.append(d)
    if pid: w.append("i.party_id=?"); a.append(int(pid))
    sql = "SELECT i.*, p.name party FROM invoices i LEFT JOIN parties p ON p.id=i.party_id" + (" WHERE " + " AND ".join(w) if w else "") + " ORDER BY i.id DESC LIMIT 300"
    return [dict(r) for r in c.execute(sql, a)]

def api_invoice(c, q, b):
    i = c.execute("SELECT i.*, p.name party, p.phone party_phone, p.address party_address, p.tax_no party_tax_no FROM invoices i LEFT JOIN parties p ON p.id=i.party_id WHERE i.id=?", (int(q["id"][0]),)).fetchone()
    if not i: raise ApiError("Invoice not found", 404)
    return {**dict(i), "lines": [dict(r) for r in c.execute("SELECT l.*, COALESCE(pr.sku,'') sku FROM invoice_lines l LEFT JOIN products pr ON pr.id=l.product_id WHERE l.invoice_id=? ORDER BY l.id", (i["id"],))]}

def api_void(c, q, b):
    c.execute("BEGIN IMMEDIATE")
    i = c.execute("SELECT * FROM invoices WHERE id=?", (int(b["id"]),)).fetchone()
    if not i or i["status"] != "posted": c.execute("ROLLBACK"); raise ApiError("Invoice cannot be voided")
    if c.execute("SELECT 1 FROM credit_notes WHERE invoice_id=?", (i["id"],)).fetchone(): c.execute("ROLLBACK"); raise ApiError("This invoice has sale returns, so it cannot be voided")
    for l in c.execute("SELECT il.*, p.track_stock FROM invoice_lines il JOIN products p ON p.id=il.product_id WHERE invoice_id=?", (i["id"],)).fetchall():
        if l["track_stock"]:
            c.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?", (l["qty_milli"], l["product_id"]))
            c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (l["product_id"], l["qty_milli"], "void", i["number"], now()))
    c.execute("UPDATE invoices SET status='void', voided_at=? WHERE id=?", (now(), i["id"]))
    if i["party_id"]: ledger(c, i["party_id"], -(i["total"] - i["paid"]), "void", i["number"])
    c.execute("COMMIT"); return {"ok": True}
def _returned(c, line_id):
    return c.execute("SELECT COALESCE(SUM(qty_milli),0) v FROM credit_note_lines WHERE invoice_line_id=?", (line_id,)).fetchone()["v"]
def api_invoice_returns(c, q, b):
    """What can still be returned on an invoice, plus earlier credit notes."""
    iid = int(q["id"][0]); inv = api_invoice(c, q, b)
    for l in inv["lines"]: l["returned_milli"] = _returned(c, l["id"])
    inv["credit_notes"] = [dict(r) for r in c.execute("SELECT * FROM credit_notes WHERE invoice_id=? ORDER BY id", (iid,))]
    return inv
def api_sale_return(c, q, b):
    items = [x for x in (b.get("items") or []) if qty(x.get("qty") or 0) > 0]
    if not items: raise ApiError("Choose at least one item to return")
    c.execute("BEGIN IMMEDIATE")
    inv = c.execute("SELECT * FROM invoices WHERE id=?", (int(b["invoice_id"]),)).fetchone()
    if not inv or inv["status"] != "posted": raise ApiError("Invoice not found or void")
    lines = {l["id"]: l for l in c.execute("SELECT il.*, p.track_stock FROM invoice_lines il JOIN products p ON p.id=il.product_id WHERE invoice_id=?", (inv["id"],))}
    picked, seen = [], set()
    for x in items:
        lid = int(x["line_id"]); l = lines.get(lid); qm = qty(x["qty"])
        if not l or lid in seen: raise ApiError("Invalid line")
        seen.add(lid)
        if qm > l["qty_milli"] - _returned(c, lid): raise ApiError(f"Cannot return more than was sold: {l['name']}")
        lt = (l["unit_price"] * qm + 500) // 1000 if l["tax_amt"] is None else (l["line_total"] * qm + l["qty_milli"] // 2) // l["qty_milli"]   # v0.5: new-style lines return their share of the net line amount
        picked.append((l, qm, lt))
    prior = c.execute("SELECT COALESCE(SUM(subtotal),0) s, COALESCE(SUM(discount),0) d, COALESCE(SUM(tax),0) t FROM credit_notes WHERE invoice_id=?", (inv["id"],)).fetchone()
    sub = sum(p[2] for p in picked)
    full = all(_returned(c, lid) + dict((p[0]["id"], p[1]) for p in picked).get(lid, 0) == l["qty_milli"] for lid, l in lines.items())
    if full: sub = inv["subtotal"] - prior["s"]; disc = inv["discount"] - prior["d"]; tax = inv["tax"] - prior["t"]   # last return settles the invoice exactly
    elif all(l["tax_amt"] is not None for l in lines.values()):   # v0.5 invoice: exact share of each line's own discount and tax
        sh = lambda v, l, qm: (v * qm + l["qty_milli"] // 2) // l["qty_milli"]
        disc = sum(sh(l["alloc_disc"], l, qm) for l, qm, _ in picked); tax = sum(sh(l["tax_amt"], l, qm) for l, qm, _ in picked)
    else:
        disc = (inv["discount"] * sub + inv["subtotal"] // 2) // inv["subtotal"] if inv["subtotal"] else 0
        tax = (inv["tax"] * sub + inv["subtotal"] // 2) // inv["subtotal"] if inv["subtotal"] else 0
    total = sub - disc + tax
    ltax, ldisc = {}, {}   # v0.7: each returned line's own tax / discount share, so the tax report can group returns by rate
    if all(l["tax_amt"] is not None for l in lines.values()):
        shr = lambda v, l, qm: (v * qm + l["qty_milli"] // 2) // l["qty_milli"]
        for l, qm, _ in picked: ltax[l["id"]] = shr(l["tax_amt"], l, qm); ldisc[l["id"]] = shr(l["alloc_disc"], l, qm)
        for d, target in ((ltax, tax), (ldisc, disc)):   # line shares must add up to the credit note totals (rounding goes to the biggest line)
            diff = target - sum(d.values())
            if diff: k = max(d, key=lambda x: d[x]); d[k] += diff
    pid = inv["party_id"]
    cash = money(b.get("refund_cash")) if b.get("refund_cash") not in (None, "") else (0 if pid else total)
    if not pid: cash = total
    if cash > total: raise ApiError("Cash refund is more than the credit note total")
    number = next_seq(c, "cn_seq", "CN", "credit_notes")
    cid = c.execute("INSERT INTO credit_notes(number,created_at,invoice_id,party_id,subtotal,discount,tax,total,refund_cash,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (number, now(), inv["id"], pid, sub, disc, tax, total, cash, (b.get("note") or "").strip()[:120])).lastrowid
    for l, qm, lt in picked:
        c.execute("INSERT INTO credit_note_lines(cn_id,invoice_line_id,product_id,name,qty_milli,unit_price,line_total,tax_bp,tax_amt,alloc_disc) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (cid, l["id"], l["product_id"], l["name"], qm, l["unit_price"], lt, l["tax_bp"] if l["id"] in ltax else None, ltax.get(l["id"]), ldisc.get(l["id"], 0)))
        if l["track_stock"]:
            c.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?", (qm, l["product_id"]))
            c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (l["product_id"], qm, "sale_return", number, now()))
    if pid:
        ledger(c, pid, -total, "return", number, inv["number"])
        ledger(c, pid, cash, "refund_paid", number)
    c.execute("COMMIT")
    return {**dict(c.execute("SELECT * FROM credit_notes WHERE id=?", (cid,)).fetchone()), "invoice_number": inv["number"],
            "lines": [dict(r) for r in c.execute("SELECT * FROM credit_note_lines WHERE cn_id=? ORDER BY id", (cid,))]}
def api_credit_notes(c, q, b):
    a, z = rng(q)
    return [dict(r) for r in c.execute("SELECT n.*, i.number invoice_number FROM credit_notes n JOIN invoices i ON i.id=n.invoice_id WHERE substr(n.created_at,1,10) BETWEEN ? AND ? ORDER BY n.id DESC LIMIT 300", (a, z))]
def api_credit_note(c, q, b):
    n = c.execute("SELECT n.*, i.number invoice_number, p.name party, p.phone party_phone, p.address party_address, p.tax_no party_tax_no FROM credit_notes n JOIN invoices i ON i.id=n.invoice_id LEFT JOIN parties p ON p.id=n.party_id WHERE n.id=?", (int(q["id"][0]),)).fetchone()
    if not n: raise ApiError("Credit note not found", 404)
    return {**dict(n), "lines": [dict(r) for r in c.execute("SELECT l.*, COALESCE(pr.sku,'') sku FROM credit_note_lines l LEFT JOIN products pr ON pr.id=l.product_id WHERE l.cn_id=? ORDER BY l.id", (n["id"],))]}

def api_report(c, q, b):
    d = (q.get("date") or [dt.date.today().isoformat()])[0]
    w = "substr(created_at,1,10)=? AND status='posted'"
    t = dict(c.execute(f"SELECT COUNT(*) n, COALESCE(SUM(subtotal),0) subtotal, COALESCE(SUM(discount),0) discount, COALESCE(SUM(tax),0) tax, COALESCE(SUM(total),0) total FROM invoices WHERE {w}", (d,)).fetchone())
    by = [dict(r) for r in c.execute(f"SELECT method, COUNT(*) n, SUM(total) total FROM invoices WHERE {w} GROUP BY method", (d,))]
    top = [dict(r) for r in c.execute(f"SELECT il.name, SUM(il.qty_milli) qty_milli, SUM(il.line_total) total FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE {w.replace('created_at','i.created_at').replace('status','i.status')} GROUP BY il.product_id ORDER BY total DESC LIMIT 10", (d,))]
    cogs = c.execute(f"SELECT COALESCE(SUM((il.qty_milli*p.cost+500)/1000),0) v FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id JOIN products p ON p.id=il.product_id WHERE {w.replace('created_at','i.created_at').replace('status','i.status')}", (d,)).fetchone()["v"]
    low = [dict(r) for r in c.execute("SELECT name, stock_milli FROM products WHERE active=1 AND track_stock=1 AND stock_milli<=? ORDER BY stock_milli", (int(settings(c)["low_stock"]) * 1000,))]
    return {"date": d, "totals": t, "by_method": by, "top": top, "low_stock": low, "est_cost": cogs, "voided": c.execute("SELECT COUNT(*) n FROM invoices WHERE status='void' AND substr(created_at,1,10)=?", (d,)).fetchone()["n"]}
def api_parties(c, q, b):
    return [dict(r) for r in c.execute("SELECT p.*, COALESCE((SELECT SUM(amount) FROM ledger WHERE party_id=p.id),0) balance FROM parties p ORDER BY active DESC, name")]
def api_party_save(c, q, b):
    name = (b.get("name") or "").strip()
    if not name: raise ApiError("Name is required")
    kind = b.get("kind") if b.get("kind") in ("customer", "supplier") else "customer"
    f = (name[:80], (b.get("phone") or "").strip()[:40], (b.get("address") or "").strip()[:200], kind, 1 if b.get("active", True) else 0)
    tax_no = re.sub(r"\s+", " ", str(b.get("tax_no") or "")).strip()[:40]   # free text (NTN / STRN / CNIC), printed as typed
    c.execute("BEGIN IMMEDIATE")
    if b.get("id"):
        pid = int(b["id"]); c.execute("UPDATE parties SET name=?,phone=?,address=?,kind=?,active=? WHERE id=?", f + (pid,))
        if "tax_no" in b: c.execute("UPDATE parties SET tax_no=? WHERE id=?", (tax_no, pid))   # an edit that does not mention tax_no keeps the old value
    else:
        pid = c.execute("INSERT INTO parties(name,phone,address,kind,active,tax_no) VALUES(?,?,?,?,?,?)", f + (tax_no,)).lastrowid
        o = money(b.get("opening"))
        ledger(c, pid, o if b.get("opening_type") != "pay" else -o, "opening")
    c.execute("COMMIT"); return {"id": pid}
def api_party(c, q, b):
    p = c.execute("SELECT * FROM parties WHERE id=?", (int(q["id"][0]),)).fetchone()
    if not p: raise ApiError("Party not found", 404)
    run, rows = 0, []
    for r in c.execute("SELECT * FROM ledger WHERE party_id=? ORDER BY id", (p["id"],)):
        run += r["amount"]; rows.append({**dict(r), "balance": run})
    return {"party": dict(p), "rows": rows[::-1], "balance": run}
# ---- v0.6 (Step 3): payments against invoices -----------------------------------------------
# Outstanding of a posted invoice = total - paid at sale - posted allocations - (credit note total - cash refunded).
# The party ledger stays the single source of the party balance; allocations only say which invoice a receipt settles.
OPEN_SQL = """SELECT i.id, i.number, i.created_at, i.due_date, i.total, i.paid, i.party_id, p.name party, p.phone party_phone,
 i.total - i.paid
   - COALESCE((SELECT SUM(a.amount) FROM payment_allocs a JOIN payments y ON y.id=a.payment_id WHERE a.invoice_id=i.id AND y.status='posted'),0)
   - COALESCE((SELECT SUM(n.total - n.refund_cash) FROM credit_notes n WHERE n.invoice_id=i.id),0) AS outstanding
 FROM invoices i JOIN parties p ON p.id=i.party_id WHERE i.status='posted' AND i.party_id IS NOT NULL"""
def open_invoices(c, pid=None):
    """Posted party invoices, oldest first (FIFO order). Includes fully paid ones (outstanding 0)."""
    if pid: return [dict(r) for r in c.execute(OPEN_SQL + " AND i.party_id=? ORDER BY i.created_at, i.id", (pid,))]
    return [dict(r) for r in c.execute(OPEN_SQL + " ORDER BY i.created_at, i.id")]
def plan_payment(c, pid, amt, manual=None):
    """Which invoices a receipt settles. FIFO (oldest first) unless the caller sends an explicit split.
    Returns [(invoice row, amount)]. Never allocates more than is outstanding; any remainder becomes advance."""
    rows = open_invoices(c, pid)
    if manual in (None, "", []):
        out, rem = [], amt
        for r in rows:
            if rem <= 0: break
            if r["outstanding"] > 0:
                a = min(rem, r["outstanding"]); out.append((r, a)); rem -= a
        return out
    if not isinstance(manual, list): raise ApiError("Allocations must be a list")
    byid = {r["id"]: r for r in rows}; out, seen, tot = [], set(), 0
    for x in manual:
        try: iid = int(x.get("invoice_id"))
        except (TypeError, ValueError, AttributeError): raise ApiError("Invalid invoice in allocation")
        a = money(x.get("amount"))
        if a == 0: continue
        r = byid.get(iid)
        if not r: raise ApiError("Invoice not found for this party")
        if iid in seen: raise ApiError("The same invoice is listed twice")
        seen.add(iid)
        if a > r["outstanding"]: raise ApiError(f"More than is outstanding on {r['number']}")
        out.append((r, a)); tot += a
    if tot > amt: raise ApiError("Allocations are more than the amount received")
    return out
def _split_view(plan, amt):
    return {"applied": [{"invoice_id": r["id"], "number": r["number"], "due_date": r["due_date"] or r["created_at"][:10], "amount": a} for r, a in plan],
            "advance": amt - sum(a for _, a in plan)}
def api_payment(c, q, b):
    pid = party_of(c, b)
    if not pid: raise ApiError("Choose a party")
    amt = money(b.get("amount"))
    if amt <= 0: raise ApiError("Amount must be above zero")
    d = b.get("direction")
    if d not in ("in", "out"): raise ApiError("Invalid direction")
    note = f"{b.get('method') or 'cash'} {(b.get('note') or '').strip()[:80]}".strip()
    c.execute("BEGIN IMMEDIATE")
    if d == "out":
        ledger(c, pid, amt, "payment_out", None, note)
        c.execute("COMMIT"); return {"ok": True}
    try: plan = plan_payment(c, pid, amt, b.get("allocations"))
    except ApiError: c.execute("ROLLBACK"); raise
    lid = ledger(c, pid, -amt, "payment_in", None, note)
    prid = c.execute("INSERT INTO payments(created_at,party_id,amount,method,note,ledger_id) VALUES(?,?,?,?,?,?)",
                     (now(), pid, amt, (b.get("method") or "cash")[:20], note[:120], lid)).lastrowid
    for r, a in plan:
        c.execute("INSERT INTO payment_allocs(payment_id,invoice_id,amount,created_at) VALUES(?,?,?,?)", (prid, r["id"], a, now()))
    c.execute("COMMIT")
    return {"ok": True, "payment_id": prid, "created_at": now(), "party": c.execute("SELECT name FROM parties WHERE id=?", (pid,)).fetchone()["name"], **_split_view(plan, amt)}
def api_payment_preview(c, q, b):
    """Same split the payment would get, without writing anything (the confirmation dialog uses it)."""
    pid = party_of(c, b)
    if not pid: raise ApiError("Choose a party")
    amt = money(b.get("amount"))
    if amt <= 0: raise ApiError("Amount must be above zero")
    return {"amount": amt, **_split_view(plan_payment(c, pid, amt, b.get("allocations")), amt)}
def api_payment_void(c, q, b):
    """Reverses the receipt: its ledger entry is reversed (party owes again) and its allocations stop counting."""
    c.execute("BEGIN IMMEDIATE")
    p = c.execute("SELECT * FROM payments WHERE id=?", (int(b["id"]),)).fetchone()
    if not p or p["status"] != "posted": c.execute("ROLLBACK"); raise ApiError("Payment cannot be voided")
    ledger(c, p["party_id"], p["amount"], "payment_void", None, f"void receipt {p['id']}")
    c.execute("UPDATE payments SET status='void', voided_at=? WHERE id=?", (now(), p["id"]))
    c.execute("COMMIT"); return {"ok": True}
def api_open_invoices(c, q, b):
    pid = _qint(q, "party_id")
    if not pid: raise ApiError("Choose a party")
    return open_invoices(c, pid)   # every posted invoice of the party, oldest first; outstanding may be 0 or negative (credit)
def api_overdue(c, q, b):
    """Unpaid party invoices, most overdue first. No due date = due on the sale date."""
    d = (q.get("date") or [dt.date.today().isoformat()])[0]
    try: t = dt.date.fromisoformat(d)
    except ValueError: raise ApiError("Invalid date")
    out = []
    for r in open_invoices(c):
        if r["outstanding"] <= 0: continue
        r["due"] = r["due_date"] or r["created_at"][:10]
        r["days_overdue"] = (t - dt.date.fromisoformat(r["due"])).days
        out.append(r)
    return sorted(out, key=lambda r: (-r["days_overdue"], r["id"]))
def _qint(q, k):
    try: return int((q.get(k) or ["0"])[0] or 0)
    except ValueError: raise ApiError(f"{k} must be a number")

def api_purchase(c, q, b):
    items = b.get("items") or []
    if not items: raise ApiError("Purchase is empty")
    c.execute("BEGIN IMMEDIATE")
    costing = settings(c).get("costing", "manual")
    lines, total, detailed, line_disc, tax = [], 0, False, 0, 0
    for it in items:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (int(it["product_id"]),)).fetchone()
        if not p: raise ApiError("Product not found or inactive")
        qm = qty(it["qty"]); uc = money(it.get("cost"))
        if qm <= 0: raise ApiError("Quantity must be above zero")
        gross = (uc * qm + 500) // 1000
        d_amt, d_bp = 0, 0                       # v0.7: optional per-line discount (amount or percent, not both), like sales
        has_amt, has_pct = it.get("disc") not in (None, "", 0, "0"), it.get("disc_pct") not in (None, "", 0, "0")
        if has_amt and has_pct: raise ApiError(f"Use either a discount amount or a percent, not both: {p['name']}")
        if has_pct: d_bp = pct_bp(it.get("disc_pct"), "Discount percent"); d_amt = (gross * d_bp + 5000) // 10000
        elif has_amt: d_amt = money(it.get("disc"))
        if d_amt > gross: raise ApiError(f"Line discount is larger than the line amount: {p['name']}")
        tbp = pct_bp(it.get("tax_pct"), "Tax percent")   # input tax rate; blank = 0%
        if d_amt or tbp is not None: detailed = True
        lt = gross - d_amt
        lines.append({"p": p, "qm": qm, "uc": uc, "lt": lt, "d": d_amt, "dbp": d_bp, "tbp": tbp or 0}); total += lt; line_disc += d_amt
    if detailed:
        for l in lines: l["tax"] = (l["lt"] * l["tbp"] + 5000) // 10000
        tax = sum(l["tax"] for l in lines); total += tax
    else:
        for l in lines: l["tax"] = None   # old-style purchase: no tax columns filled
    pid = party_of(c, b)
    paid = money(b["paid"]) if b.get("paid") not in (None, "") else total
    if paid > total: raise ApiError("Paid is more than the total")
    if not pid and paid < total: raise ApiError("Choose a supplier for a credit purchase")
    number = next_seq(c, "pur_seq", "PUR", "purchases")
    iid = c.execute("INSERT INTO purchases(number,created_at,party_id,total,paid,note,tax,line_disc) VALUES(?,?,?,?,?,?,?,?)", (number, now(), pid, total, paid, (b.get("note") or "")[:120], tax, line_disc)).lastrowid
    for l in lines:
        p, qm, uc, lt = l["p"], l["qm"], l["uc"], l["lt"]
        c.execute("INSERT INTO purchase_lines(purchase_id,product_id,name,qty_milli,unit_cost,line_total,disc,disc_bp,tax_bp,tax_amt) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (iid, p["id"], p["name"], qm, uc, lt, l["d"], l["dbp"], l["tbp"] if detailed else None, l["tax"]))
        if costing == "average" and p["track_stock"]:
            # v0.8 moving weighted average on the NET line amount (after line discount, tax excluded), integer half-up.
            # Re-read the product: the same item can be on two lines of one purchase. Voids and returns never change cost.
            cur = c.execute("SELECT stock_milli, cost FROM products WHERE id=?", (p["id"],)).fetchone()
            old = max(cur["stock_milli"], 0)
            if old > 0 or lt > 0:
                nq = old + qm; nv = old * cur["cost"] + lt * 1000
                c.execute("UPDATE products SET cost=? WHERE id=?", ((2 * nv + nq) // (2 * nq), p["id"]))
        elif uc > 0: c.execute("UPDATE products SET cost=? WHERE id=?", (uc, p["id"]))   # manual (default): cost = latest entered purchase cost
        if p["track_stock"]:
            c.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?", (qm, p["id"]))
            c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (p["id"], qm, "purchase", number, now()))
    ledger(c, pid, -(total - paid), "purchase", number)
    c.execute("COMMIT"); return api_purchase_get(c, {"id": [str(iid)]}, {})
def api_purchases(c, q, b):
    a, z = rng(q)
    return [dict(r) for r in c.execute("SELECT u.*, p.name party FROM purchases u LEFT JOIN parties p ON p.id=u.party_id WHERE substr(u.created_at,1,10) BETWEEN ? AND ? ORDER BY u.id DESC LIMIT 300", (a, z))]
def api_purchase_get(c, q, b):
    u = c.execute("SELECT u.*, p.name party, p.phone party_phone, p.address party_address, p.tax_no party_tax_no FROM purchases u LEFT JOIN parties p ON p.id=u.party_id WHERE u.id=?", (int(q["id"][0]),)).fetchone()
    if not u: raise ApiError("Purchase not found", 404)
    return {**dict(u), "lines": [dict(r) for r in c.execute("SELECT * FROM purchase_lines WHERE purchase_id=? ORDER BY id", (u["id"],))]}
def api_purchase_void(c, q, b):
    c.execute("BEGIN IMMEDIATE")
    u = c.execute("SELECT * FROM purchases WHERE id=?", (int(b["id"]),)).fetchone()
    if not u or u["status"] != "posted": raise ApiError("Purchase cannot be voided")
    if c.execute("SELECT 1 FROM purchase_returns WHERE purchase_id=?", (u["id"],)).fetchone(): raise ApiError("This purchase has returns, so it cannot be voided")
    ls = c.execute("SELECT l.*, p.track_stock, p.stock_milli FROM purchase_lines l JOIN products p ON p.id=l.product_id WHERE purchase_id=?", (u["id"],)).fetchall()
    for l in ls:
        if l["track_stock"] and l["stock_milli"] < l["qty_milli"]: raise ApiError(f"Cannot void: {l['name']} was already sold")
    for l in ls:
        if l["track_stock"]:
            c.execute("UPDATE products SET stock_milli=stock_milli-? WHERE id=?", (l["qty_milli"], l["product_id"]))
            c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (l["product_id"], -l["qty_milli"], "purchase_void", u["number"], now()))
    c.execute("UPDATE purchases SET status='void', voided_at=? WHERE id=?", (now(), u["id"]))
    ledger(c, u["party_id"], u["total"] - u["paid"], "void", u["number"])
    c.execute("COMMIT"); return {"ok": True}

def api_expense(c, q, b):
    amt = money(b.get("amount"))
    if amt <= 0: raise ApiError("Amount must be above zero")
    c.execute("INSERT INTO expenses(created_at,category,amount,note) VALUES(?,?,?,?)", (now(), (b.get("category") or "General").strip()[:40] or "General", amt, (b.get("note") or "").strip()[:120]))
    return {"ok": True}
def api_expenses(c, q, b):
    a, z = rng(q)
    return [dict(r) for r in c.execute("SELECT * FROM expenses WHERE substr(created_at,1,10) BETWEEN ? AND ? ORDER BY id DESC LIMIT 500", (a, z))]
def api_expense_void(c, q, b):
    n = c.execute("UPDATE expenses SET status='void' WHERE id=? AND status='posted'", (int(b["id"]),)).rowcount
    if not n: raise ApiError("Expense cannot be voided")
    return {"ok": True}

LEAD_ST = ("New", "Contacted", "Won", "Lost")
def api_leads(c, q, b): return [dict(r) for r in c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 500")]
def api_lead_save(c, q, b):
    name = (b.get("name") or "").strip()
    if not name: raise ApiError("Name is required")
    st_ = b.get("status") if b.get("status") in LEAD_ST else "New"
    f = (name[:100], (b.get("phone") or "").strip()[:40], (b.get("email") or "").strip()[:120], (b.get("service") or "").strip()[:80], (b.get("message") or "").strip()[:2000], st_)
    if b.get("id"): c.execute("UPDATE leads SET name=?,phone=?,email=?,service=?,message=?,status=? WHERE id=?", f + (int(b["id"]),))
    else: c.execute("INSERT INTO leads(name,phone,email,service,message,status,created_at) VALUES(?,?,?,?,?,?,?)", f + (now(),))
    return {"ok": True}
def api_lead_delete(c, q, b): c.execute("DELETE FROM leads WHERE id=?", (int(b["id"]),)); return {"ok": True}

def api_report(c, q, b):
    a, z = rng(q); P = (a, z)
    w = "substr(created_at,1,10) BETWEEN ? AND ? AND status='posted'"
    wi = w.replace("created_at", "i.created_at").replace("status", "i.status")
    t = dict(c.execute(f"SELECT COUNT(*) n, COALESCE(SUM(subtotal),0) subtotal, COALESCE(SUM(discount),0) discount, COALESCE(SUM(tax),0) tax, COALESCE(SUM(total),0) total FROM invoices WHERE {w}", P).fetchone())
    by = [dict(r) for r in c.execute(f"SELECT method, COUNT(*) n, SUM(total) total FROM invoices WHERE {w} GROUP BY method", P)]
    top = [dict(r) for r in c.execute(f"SELECT il.name, SUM(il.qty_milli) qty_milli, SUM(il.line_total) total FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE {wi} GROUP BY il.product_id ORDER BY total DESC LIMIT 10", P)]
    cogs = c.execute(f"SELECT COALESCE(SUM((il.qty_milli*p.cost+500)/1000),0) v FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id JOIN products p ON p.id=il.product_id WHERE {wi}", P).fetchone()["v"]
    pur = c.execute(f"SELECT COUNT(*) n, COALESCE(SUM(total),0) total FROM purchases WHERE {w}", P).fetchone()
    exp = c.execute(f"SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE {w}", P).fetchone()["v"]
    exp_by = [dict(r) for r in c.execute(f"SELECT category, SUM(amount) total FROM expenses WHERE {w} GROUP BY category ORDER BY total DESC", P)]
    low = [dict(r) for r in c.execute("SELECT name, stock_milli FROM products WHERE active=1 AND track_stock=1 AND stock_milli<=? ORDER BY stock_milli", (int(settings(c)["low_stock"]) * 1000,))]
    bal = [r["b"] for r in c.execute("SELECT COALESCE(SUM(amount),0) b FROM ledger GROUP BY party_id")]
    rw = "substr(created_at,1,10) BETWEEN ? AND ?"
    ret = dict(c.execute(f"SELECT COUNT(*) n, COALESCE(SUM(subtotal),0) subtotal, COALESCE(SUM(discount),0) discount, COALESCE(SUM(tax),0) tax, COALESCE(SUM(total),0) total FROM credit_notes WHERE {rw}", P).fetchone())
    ret_cogs = c.execute("SELECT COALESCE(SUM((l.qty_milli*p.cost+500)/1000),0) v FROM credit_note_lines l JOIN credit_notes n ON n.id=l.cn_id JOIN products p ON p.id=l.product_id WHERE substr(n.created_at,1,10) BETWEEN ? AND ?", P).fetchone()["v"]
    gross = t["subtotal"] - t["discount"] - cogs - (ret["subtotal"] - ret["discount"] - ret_cogs)
    return {"from": a, "to": z, "returns": ret, "date": a, "totals": t, "by_method": by, "top": top, "low_stock": low, "est_cost": cogs, "gross": gross, "expenses": exp, "expenses_by": exp_by,
            "net": gross - exp, "purchases": dict(pur), "receivable": sum(x for x in bal if x > 0), "payable": -sum(x for x in bal if x < 0),
            "stock_value": c.execute("SELECT COALESCE(SUM((stock_milli*cost+500)/1000),0) v FROM products WHERE track_stock=1 AND stock_milli>0").fetchone()["v"],
            "voided": c.execute("SELECT COUNT(*) n FROM invoices WHERE status='void' AND substr(created_at,1,10) BETWEEN ? AND ?", P).fetchone()["n"]}

class Raw:
    def __init__(s, body, ctype, headers=None): s.body, s.ctype, s.headers = body, ctype, headers or {}
EXPORTS = {"invoices": "SELECT i.number, i.created_at, COALESCE(p.name,i.customer) party, i.method, i.subtotal, i.discount, i.tax, i.total, i.paid, i.status FROM invoices i LEFT JOIN parties p ON p.id=i.party_id WHERE substr(i.created_at,1,10) BETWEEN ? AND ? ORDER BY i.id",
 "purchases": "SELECT u.number, u.created_at, p.name party, u.total, u.paid, u.status FROM purchases u LEFT JOIN parties p ON p.id=u.party_id WHERE substr(u.created_at,1,10) BETWEEN ? AND ? ORDER BY u.id",
 "expenses": "SELECT created_at, category, amount, note, status FROM expenses WHERE substr(created_at,1,10) BETWEEN ? AND ? ORDER BY id",
 "products": "SELECT sku, name, price, cost, stock_milli stock, track_stock, active, unit, category FROM products ORDER BY name",
 "parties": "SELECT p.name, p.phone, p.kind, COALESCE((SELECT SUM(amount) FROM ledger WHERE party_id=p.id),0) balance FROM parties p ORDER BY p.name"}
def api_export(c, q, b):
    import csv, io
    what = (q.get("what") or [""])[0]
    if what not in EXPORTS: raise ApiError("Unknown export", 404)
    a, z = rng(q); cur = c.execute(EXPORTS[what], (a, z) if "BETWEEN" in EXPORTS[what] else ())
    out = io.StringIO(); w = csv.writer(out); w.writerow([d[0] for d in cur.description])
    for r in cur:
        w.writerow([f"{v/100:.2f}" if k in ("subtotal","discount","tax","total","paid","price","cost","balance","amount") and isinstance(v, int)
                    else f"{v/1000:g}" if k == "stock" else v for k, v in zip(r.keys(), r)])
    return Raw(("\ufeff" + out.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="onebill_{what}_{a}_{z}.csv"'})
THEMES_A = ("classic", "modern", "minimal", "bold", "ledger")       # A4 / A5 invoice layouts
THEMES_T = ("compact", "standard", "detailed", "bold", "minimal")   # 80 mm / 58 mm receipt layouts
PRINT_FLAGS = ("h_logo", "h_address", "h_phone", "h_web", "h_tax", "c_serial", "c_sku", "c_qty", "c_rate", "c_disc", "c_tax", "t_words", "t_bank", "t_terms", "t_sign", "t_footer", "t_party", "t_paid", "t_buyer_tax")
PRINT_KINDS = ("invoice", "credit_note", "purchase_return", "quotation", "sales_order", "challan", "purchase_order")
IMG_MAX = 400_000
def check_image(v):
    """Logo/signature: '' clears it; otherwise a PNG/JPEG/WEBP data URL up to 400 KB whose bytes really are that image type."""
    import base64, binascii
    v = str(v or "").strip()
    if not v: return ""
    m = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", v)
    if not m: raise ApiError("Picture must be a PNG, JPEG or WEBP file")
    try: raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError): raise ApiError("Picture is damaged")
    if len(raw) > IMG_MAX: raise ApiError("Picture is too big (maximum 400 KB)")
    magic = {"png": raw[:8] == b"\x89PNG\r\n\x1a\n", "jpeg": raw[:3] == b"\xff\xd8\xff", "webp": raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"}
    if not magic[m.group(1)]: raise ApiError("Picture does not match its file type")
    return v
def api_open_url(c, q, b):
    """Opens WhatsApp in the default browser/app. Only wa.me links are allowed."""
    u = str(b.get("url") or "")
    if not re.fullmatch(r"https://wa\.me/[0-9]{6,15}(\?text=[A-Za-z0-9%._~!*'()-]{0,3000})?", u): raise ApiError("Link not allowed")
    if not os.environ.get("ONEBILL_NOWINDOW"): webbrowser.open(u)
    return {"ok": True}
def api_settings_get(c, q, b):
    st = settings(c); has = bool(st.pop("pw_hash", None)); st.pop("pw_salt", None)
    return {**st, "data_dir": str(DB.parent), "password_set": has, "company_id": CUR[0], "version": APP_VERSION,
            "owner": {"name": "One Solution Agency", "website": "https://onesolutionagency.online"}}
def api_settings_set(c, q, b):
    if "reminder_text" in b: c.execute("INSERT OR REPLACE INTO settings VALUES('reminder_text',?)", (str(b["reminder_text"]).strip()[:300] or DEFAULTS["reminder_text"],))
    for k in ("shop_name", "currency", "prefix", "low_stock", "receipt_footer"):
        if k in b: c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(b[k]).strip()[:60] or DEFAULTS[k]))
    if "costing" in b:
        if b["costing"] not in ("manual", "average"): raise ApiError("Costing must be manual or average")
        c.execute("INSERT OR REPLACE INTO settings VALUES('costing',?)", (b["costing"],))
    if "shifts_enabled" in b:
        if b["shifts_enabled"] not in (True, False, "0", "1", 0, 1): raise ApiError("Invalid shifts_enabled setting")
        c.execute("INSERT OR REPLACE INTO settings VALUES('shifts_enabled',?)", ("1" if b["shifts_enabled"] in (True, "1", 1) else "0",))
    for dk in ("drive_enabled", "drive_allow_plain"):
        if dk in b:
            if b[dk] not in (True, False, "0", "1", 0, 1): raise ApiError(f"Invalid {dk} setting")
            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (dk, "1" if b[dk] in (True, "1", 1) else "0"))
    if "invoice_title_tax" in b:
        if b["invoice_title_tax"] not in (True, False, "0", "1", 0, 1): raise ApiError("Invalid tax invoice title setting")
        c.execute("INSERT OR REPLACE INTO settings VALUES('invoice_title_tax',?)", ("1" if b["invoice_title_tax"] in (True, "1", 1) else "0",))
    if "card_account" in b:
        v = str(b["card_account"] or "")
        if v and not c.execute("SELECT 1 FROM accounts WHERE id=? AND active=1", (int(v),)).fetchone(): raise ApiError("Account not found")
        c.execute("INSERT OR REPLACE INTO settings VALUES('card_account',?)", (v,))
    for k in ("country", "timezone", "address", "phone", "email", "website", "tax_id", "tax_label", "bank_info", "description"):
        if k in b: c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(b[k]).strip()[:300]))
    if "paper" in b:
        if b["paper"] not in ("A4", "A5", "80mm", "58mm"): raise ApiError("Paper must be A4, A5, 80mm or 58mm")
        c.execute("INSERT OR REPLACE INTO settings VALUES('paper',?)", (b["paper"],))
    if "orient" in b:
        if b["orient"] not in ("portrait", "landscape"): raise ApiError("Invalid orientation")
        c.execute("INSERT OR REPLACE INTO settings VALUES('orient',?)", (b["orient"],))
    for k, lo, hi in (("margin_mm", 0, 30), ("copies", 1, 5)):
        if k in b:
            try: v = int(b[k])
            except (TypeError, ValueError): raise ApiError(f"Invalid {k}")
            if not lo <= v <= hi: raise ApiError(f"{k} must be {lo}-{hi}")
            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))
    if "theme_a" in b:
        if b["theme_a"] not in THEMES_A: raise ApiError("Unknown invoice theme")
        c.execute("INSERT OR REPLACE INTO settings VALUES('theme_a',?)", (b["theme_a"],))
    if "theme_t" in b:
        if b["theme_t"] not in THEMES_T: raise ApiError("Unknown receipt theme")
        c.execute("INSERT OR REPLACE INTO settings VALUES('theme_t',?)", (b["theme_t"],))
    if "print_opts" in b:
        o = b["print_opts"]
        if isinstance(o, str): o = json.loads(o or "{}") if o.strip() else {}
        if not isinstance(o, dict): raise ApiError("Invalid print options")
        clean = {}
        for k, v in o.items():
            if k in PRINT_FLAGS and isinstance(v, bool): clean[k] = v
            elif k == "copy_label" and v in ("none", "dup", "cust"): clean[k] = v
            else: raise ApiError(f"Unknown print option: {str(k)[:30]}")
        c.execute("INSERT OR REPLACE INTO settings VALUES('print_opts',?)", (json.dumps(clean, sort_keys=True),))
    if "doc_names" in b:
        o = b["doc_names"]
        if isinstance(o, str): o = json.loads(o or "{}") if o.strip() else {}
        if not isinstance(o, dict): raise ApiError("Invalid document names")
        clean = {}
        for k, v in o.items():
            if k not in PRINT_KINDS: raise ApiError(f"Unknown document type: {str(k)[:30]}")
            v = re.sub(r"\s+", " ", str(v or "")).strip()[:40]
            if v: clean[k] = v
        c.execute("INSERT OR REPLACE INTO settings VALUES('doc_names',?)", (json.dumps(clean, ensure_ascii=False, sort_keys=True),))
    if "terms" in b:
        c.execute("INSERT OR REPLACE INTO settings VALUES('terms',?)", (str(b["terms"] or "").strip()[:1000],))
    for k in ("logo", "signature"):
        if k in b:
            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, check_image(b[k])))
    if "tax_percent" in b:
        try: bp = int((Decimal(str(b["tax_percent"] or 0)) * 100).to_integral_value(ROUND_HALF_UP))
        except InvalidOperation: raise ApiError("Invalid tax")
        if not 0 <= bp <= 10000: raise ApiError("Tax must be 0-100%")
        c.execute("INSERT OR REPLACE INTO settings VALUES('tax_bp',?)", (str(bp),))
    return {"ok": True}

# ---------- v0.10 Step 7: encrypted backups (.obk) & Google Drive backup ----------
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC as _PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes as _hashes
    HAS_CRYPTO = True
except ImportError:
    _AESGCM = None
    _PBKDF2HMAC = None
    _hashes = None
    HAS_CRYPTO = False

OBK_MAGIC = b"OBK1"
OBK_VERSION = 1
OBK_KDF_PBKDF2_SHA256 = 1
OBK_DEFAULT_ITERATIONS = 600_000
OBK_MAX_ITERATIONS = 2_000_000   # upper limit accepted from a file header: a hostile .obk must not be able to keep the PC busy for minutes
OBK_SALT_LEN = 16
OBK_NONCE_LEN = 12
OBK_HEADER_LEN = 4 + 1 + 1 + 4 + OBK_SALT_LEN + OBK_NONCE_LEN

def _derive_obk_key(passphrase, salt, iterations):
    if not HAS_CRYPTO:
        raise ApiError("Encrypted backups require the optional 'cryptography' package")
    kdf = _PBKDF2HMAC(algorithm=_hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode("utf-8"))

def encrypt_backup_bytes(plain_bytes, passphrase, iterations=OBK_DEFAULT_ITERATIONS):
    if not HAS_CRYPTO:
        raise ApiError("Encrypted backups require the optional 'cryptography' package")
    pw = str(passphrase or "")
    if not pw:
        raise ApiError("Passphrase is required for encrypted backups")
    iters = int(iterations)
    if iters < 1 or iters > OBK_MAX_ITERATIONS:
        raise ApiError("Invalid PBKDF2 iteration count")
    salt = os.urandom(OBK_SALT_LEN)
    nonce = os.urandom(OBK_NONCE_LEN)
    header = OBK_MAGIC + bytes([OBK_VERSION, OBK_KDF_PBKDF2_SHA256]) + iters.to_bytes(4, "big") + salt + nonce
    key = _derive_obk_key(pw, salt, iters)
    ct = _AESGCM(key).encrypt(nonce, plain_bytes, header)
    return header + ct

def decrypt_backup_bytes(blob, passphrase):
    if not HAS_CRYPTO:
        raise ApiError("Encrypted backups require the optional 'cryptography' package")
    pw = str(passphrase or "")
    if not pw:
        raise ApiError("Passphrase is required to decrypt this backup")
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < OBK_HEADER_LEN + 16:
        raise ApiError("Backup file is truncated or damaged")
    raw = bytes(blob)
    if raw[:4] != OBK_MAGIC:
        raise ApiError("This file is not a valid encrypted OneBill backup")
    ver = raw[4]
    kdf_id = raw[5]
    if ver != OBK_VERSION or kdf_id != OBK_KDF_PBKDF2_SHA256:
        raise ApiError("Unsupported encrypted backup format")
    iters = int.from_bytes(raw[6:10], "big")
    if iters <= 0 or iters > OBK_MAX_ITERATIONS:
        raise ApiError("Invalid encrypted backup parameters")
    salt = raw[10:26]
    nonce = raw[26:38]
    header = raw[:OBK_HEADER_LEN]
    ct = raw[OBK_HEADER_LEN:]
    try:
        key = _derive_obk_key(pw, salt, iters)
        return _AESGCM(key).decrypt(nonce, ct, header)
    except Exception:
        raise ApiError("Wrong passphrase or backup file is damaged")

def make_backup(tag="manual", passphrase=None, iterations=OBK_DEFAULT_ITERATIONS):
    pw = "" if passphrase is None else str(passphrase)
    if pw:
        if not HAS_CRYPTO:
            raise ApiError("Encrypted backups require the optional 'cryptography' package")
        tmp_db = BK / f".tmp_plain_{secrets.token_hex(6)}.db"
        s = conn()
        d = sqlite3.connect(tmp_db)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        try:
            plain = tmp_db.read_bytes()
        finally:
            if tmp_db.exists():
                tmp_db.unlink()
        enc_bytes = encrypt_backup_bytes(plain, pw, iterations=iterations)
        name = f"onebill_{dt.datetime.now():%Y%m%d_%H%M%S}_{tag}.obk"
        (BK / name).write_bytes(enc_bytes)
        return name
    name = f"onebill_{dt.datetime.now():%Y%m%d_%H%M%S}_{tag}.db"; dst = BK / name
    s, d = conn(), sqlite3.connect(dst)
    try: s.backup(d)
    finally: d.close(); s.close()
    return name

def api_backup_list(c, q, b):
    files = list(BK.glob("onebill_*.db")) + list(BK.glob("onebill_*.obk"))
    items = []
    for f in sorted(files, key=lambda p: p.name, reverse=True):
        items.append({"name": f.name, "size": f.stat().st_size, "encrypted": f.suffix == ".obk"})
    return items

def api_backup(c, q, b):
    pw = b.get("passphrase")
    if pw is not None and str(pw) != "":
        name = make_backup("manual", passphrase=str(pw))
        return {"name": name, "encrypted": True, "folder": str(BK),
                "warning": "If you lose the passphrase the backup cannot be opened."}
    return {"name": make_backup("manual"), "encrypted": False, "folder": str(BK)}

def _apply_validated_sqlite_file(c, sqlite_path):
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        ok = src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ok = ok and bool(src.execute("SELECT 1 FROM sqlite_master WHERE name='invoices'").fetchone())
        if not ok:
            raise ApiError("This file is not a valid OneBill backup")
        live = seqs(c)
        safety = make_backup("prerestore")
        src.backup(c)
    except sqlite3.DatabaseError:
        raise ApiError("This file is not a valid OneBill backup")
    finally:
        src.close(); drop_sidecars(sqlite_path)
    migrate(c)
    for k, v in seqs(c).items():
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(max(v, live[k]))))
    return {"ok": True, "safety_backup": safety,
            "note": "Invoice and purchase counters kept at or above the highest number used before restore."}

def api_restore(c, q, b):
    name = b.get("name", "")
    if re.fullmatch(r"onebill_[0-9_a-z]+\.db", name):
        p = BK / name
        if not p.exists(): raise ApiError("Backup not found", 404)
        if not valid_backup(p): raise ApiError("This file is not a valid OneBill backup")
        return _apply_validated_sqlite_file(c, p)
    if re.fullmatch(r"onebill_[0-9_a-z]+\.obk", name):
        p = BK / name
        if not p.exists(): raise ApiError("Backup not found", 404)
        plain_bytes = decrypt_backup_bytes(p.read_bytes(), b.get("passphrase"))
        tmp_db = BK / f".tmp_restore_{secrets.token_hex(6)}.db"
        try:
            tmp_db.write_bytes(plain_bytes)
            if not valid_backup(tmp_db):
                raise ApiError("Decrypted file is not a valid OneBill backup")
            return _apply_validated_sqlite_file(c, tmp_db)
        finally:
            if tmp_db.exists():
                tmp_db.unlink()
            drop_sidecars(tmp_db)
    raise ApiError("Backup not found", 404)

def drop_sidecars(path):
    """Opening a WAL-mode backup read-only can leave '-wal' / '-shm' files next to it; remove them once the file is closed."""
    for suf in ("-wal", "-shm"):
        try: Path(str(path) + suf).unlink()
        except OSError: pass
def valid_backup(path):
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try: return src.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and bool(src.execute("SELECT 1 FROM sqlite_master WHERE name='invoices'").fetchone())
        finally: src.close(); drop_sidecars(path)
    except sqlite3.DatabaseError: return False

def api_backup_download(c, q, b):
    name = (q.get("name") or [""])[0]
    if not (re.fullmatch(r"onebill_[0-9_a-z]+\.db", name) or re.fullmatch(r"onebill_[0-9_a-z]+\.obk", name)) or not (BK / name).exists():
        raise ApiError("Backup not found", 404)
    return Raw((BK / name).read_bytes(), "application/octet-stream", {"Content-Disposition": f'attachment; filename="{name}"'})

def api_backup_upload(c, q, b):
    import base64, binascii
    try: raw = base64.b64decode(b.get("data") or "", validate=True)
    except (binascii.Error, ValueError): raise ApiError("Invalid file data")
    if len(raw) > 150 * 1024 * 1024: raise ApiError("Backup file is too large")
    if raw.startswith(OBK_MAGIC):
        if len(raw) < OBK_HEADER_LEN + 16: raise ApiError("This file is not a valid encrypted OneBill backup")
        name = f"onebill_{dt.datetime.now():%Y%m%d_%H%M%S}_uploaded.obk"
        (BK / name).write_bytes(raw)
        return {"name": name, "encrypted": True}
    if not raw.startswith(b"SQLite format 3\x00"): raise ApiError("This file is not a valid OneBill backup")
    name = f"onebill_{dt.datetime.now():%Y%m%d_%H%M%S}_uploaded.db"; tmp = BK / (name + ".part")
    tmp.write_bytes(raw)
    if not valid_backup(tmp): tmp.unlink(); raise ApiError("This file is not a valid OneBill backup")
    os.replace(tmp, BK / name)
    return {"name": name, "encrypted": False}

# ---------- Google Drive backup (v0.10.1: sign-in + upload). NOT TESTED with a real Google account. ----------
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
DRIVE_PENDING_SECONDS = 600
DRIVE_MAX_UPLOAD = 150 * 1024 * 1024
DRIVE_LOCK = threading.Lock()
DRIVE_PENDING = {}   # the one sign-in in progress: state, verifier, redirect_uri, cfg (path of the config file), expires

class DriveHttpError(Exception):
    """Google answered with an HTTP error. status = HTTP code, payload = parsed JSON (or {})."""
    def __init__(s, status, payload=None): super().__init__(f"HTTP {status}"); s.status = status; s.payload = payload if isinstance(payload, dict) else {}

class DriveAuthExpired(ApiError):
    """The saved Google sign-in no longer works (revoked or expired): the owner must sign in again."""

def drive_http_real(spec):
    """Send one request {method,url,headers,body} to Google. Returns {status, headers(lowercase), json}.
    Raises DriveHttpError for HTTP errors and ApiError when Google cannot be reached. Tokens are never put in messages."""
    import urllib.request, urllib.error
    req = urllib.request.Request(spec["url"], data=spec.get("body"), headers=dict(spec.get("headers") or {}), method=spec["method"])
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, raw, hdrs = r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        try: payload = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except ValueError: payload = {}
        raise DriveHttpError(e.code, payload)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ApiError("Cannot reach Google. Check the internet connection and try again.")
    try: parsed = json.loads(raw.decode("utf-8") or "{}")
    except ValueError: parsed = {}
    return {"status": status, "headers": hdrs, "json": parsed if isinstance(parsed, dict) else {}}
drive_http = drive_http_real   # tests replace this with a fake Google; no test ever touches the network

def _drive_local_path(): return DB.parent / "drive_config.json"
def _load_drive_local(path=None):
    p = path or _drive_local_path()
    out = {"client_id": "", "client_secret": "", "refresh_token": "", "access_token": "", "expires_at": 0.0}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            for k in ("client_id", "client_secret", "refresh_token", "access_token"): out[k] = str(d.get(k) or "").strip()
            try: out["expires_at"] = float(d.get("expires_at") or 0)
            except (TypeError, ValueError): pass
    except (OSError, ValueError): pass
    return out
def _save_drive_local(cfg, path=None):
    """Secrets live only in this file inside the data folder: never in the database, the settings table, a backup or an API answer."""
    p = path or _drive_local_path()
    data = {k: str(cfg.get(k) or "").strip() for k in ("client_id", "client_secret", "refresh_token", "access_token")}
    data["expires_at"] = float(cfg.get("expires_at") or 0)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try: os.chmod(tmp, 0o600)
    except OSError: pass
    os.replace(tmp, p)

def build_drive_auth_url(client_id, redirect_uri, code_challenge, state):
    from urllib.parse import urlencode
    if not client_id: raise ApiError("Google Drive client ID is not set. Enter it first.")
    return DRIVE_AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": DRIVE_SCOPE,
        "code_challenge": code_challenge, "code_challenge_method": "S256", "state": state, "access_type": "offline", "prompt": "consent"})

def _token_spec(form):
    from urllib.parse import urlencode
    body = urlencode({k: v for k, v in form.items() if v not in (None, "")}).encode("ascii")
    return {"method": "POST", "url": DRIVE_TOKEN_URL, "headers": {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}, "body": body}
def _token_call(form, http_fn=None):
    try: r = (http_fn or drive_http)(_token_spec(form))
    except DriveHttpError as e:
        if e.payload.get("error") == "invalid_grant": raise DriveAuthExpired("Google sign-in expired or was cancelled. Sign in again.")
        raise ApiError("Google refused the sign-in (" + re.sub(r"[^A-Za-z0-9_ .-]", "", str(e.payload.get("error") or e.status))[:60] + "). Check the client ID and secret.")
    j = r.get("json") or {}
    if not j.get("access_token"): raise ApiError("Google did not return an access token")
    return j

def drive_start_auth(client_id, client_secret=None, port=None):
    """Begin a sign-in: PKCE (S256) + random state. Returns the Google URL to open in the browser. Only one sign-in is pending at a time."""
    import hashlib, base64
    loc = _load_drive_local()
    cid = str(client_id or loc["client_id"]).strip()
    if not cid: raise ApiError("Google Drive client ID is not set. Enter it first.")
    sec = loc["client_secret"] if client_secret is None else str(client_secret).strip()
    if cid != loc["client_id"]: loc = {**loc, "refresh_token": "", "access_token": "", "expires_at": 0.0}   # a different Google app: old tokens do not belong to it
    _save_drive_local({**loc, "client_id": cid, "client_secret": sec})
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://127.0.0.1:{port or PORT}/oauth/drive"
    with DRIVE_LOCK:
        DRIVE_PENDING.clear()
        DRIVE_PENDING.update({"state": state, "verifier": verifier, "redirect_uri": redirect_uri, "cfg": _drive_local_path(), "expires": time.time() + DRIVE_PENDING_SECONDS})
    return {"auth_url": build_drive_auth_url(cid, redirect_uri, challenge, state), "redirect_uri": redirect_uri, "expires_in": DRIVE_PENDING_SECONDS}

def drive_finish_auth(params, http_fn=None):
    """Called when Google sends the browser back to /oauth/drive. Returns (ok, plain message). The random state must match the pending sign-in."""
    with DRIVE_LOCK:
        pend = dict(DRIVE_PENDING)
        if not pend or time.time() > pend["expires"]:
            DRIVE_PENDING.clear(); return False, "No Google sign-in is waiting (it may have timed out). Start it again in OneBill."
        if not hmac.compare_digest(str(params.get("state") or ""), pend["state"]):
            return False, "This sign-in link does not match the one OneBill started. Nothing was saved."
        DRIVE_PENDING.clear()   # a sign-in can be completed only once
    if params.get("error"): return False, "Google sign-in was cancelled or refused. Nothing was saved."
    code = str(params.get("code") or "")
    if not code: return False, "Google did not send a sign-in code. Nothing was saved."
    loc = _load_drive_local(pend["cfg"])
    try:
        j = _token_call({"grant_type": "authorization_code", "code": code, "client_id": loc["client_id"], "client_secret": loc["client_secret"],
                         "redirect_uri": pend["redirect_uri"], "code_verifier": pend["verifier"]}, http_fn)
    except ApiError as e: return False, str(e)
    loc.update(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in") or 3600) - 60)
    if j.get("refresh_token"): loc["refresh_token"] = j["refresh_token"]
    _save_drive_local(loc, pend["cfg"])
    return True, "Google Drive is connected. You can close this tab and go back to OneBill."

def drive_valid_token(http_fn=None, force=False):
    """Return a usable access token, refreshing it when it is missing, near expiry or force=True."""
    loc = _load_drive_local()
    if not force and loc["access_token"] and loc["expires_at"] > time.time() + 30: return loc["access_token"]
    if not loc["refresh_token"]: raise ApiError("Google Drive is not connected. Sign in first.")
    try: j = _token_call({"grant_type": "refresh_token", "refresh_token": loc["refresh_token"], "client_id": loc["client_id"], "client_secret": loc["client_secret"]}, http_fn)
    except DriveAuthExpired:
        _save_drive_local({**loc, "refresh_token": "", "access_token": "", "expires_at": 0.0}); raise
    loc.update(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in") or 3600) - 60)
    if j.get("refresh_token"): loc["refresh_token"] = j["refresh_token"]
    _save_drive_local(loc)
    return loc["access_token"]

def build_drive_upload_init(backup_name, size, token):
    """Step 1 of a resumable upload: tell Drive the file name and size; Drive answers with an upload address (Location header)."""
    meta = json.dumps({"name": backup_name, "mimeType": "application/octet-stream"}).encode("utf-8")
    return {"method": "POST", "url": DRIVE_UPLOAD_URL, "body": meta,
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                        "X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(size), "Content-Length": str(len(meta))}}
def build_drive_upload_put(location, content):
    """Step 2: send the file bytes to the upload address from step 1."""
    return {"method": "PUT", "url": location, "body": content, "headers": {"Content-Type": "application/octet-stream", "Content-Length": str(len(content))}}

def _drive_error_message(e):
    err = e.payload.get("error") if isinstance(e.payload.get("error"), dict) else {}
    reasons = " ".join(str(x.get("reason") or "") for x in (err.get("errors") or []) if isinstance(x, dict))
    if "storageQuotaExceeded" in reasons: return "Google Drive is full. Free some space and try again."
    if e.status in (401, 403) and "rateLimit" not in reasons and "userRateLimit" not in reasons: return "Google refused the upload (permission). Sign in to Google Drive again."
    if e.status == 429 or "rateLimit" in reasons or e.status >= 500: return f"Google Drive is busy or unavailable (error {e.status}). Try again later."
    return f"Google Drive rejected the upload (error {e.status})."

def drive_upload_backup(c, name, http_fn=None):
    st = settings(c)
    if st.get("drive_enabled") != "1": raise ApiError("Google Drive backup is turned off in Settings")
    if re.fullmatch(r"onebill_[0-9_a-z]+\.db", name):
        if st.get("drive_allow_plain") != "1": raise ApiError("Only encrypted (.obk) backups can be uploaded to Google Drive. Plain backups hold all your data unprotected.")
    elif not re.fullmatch(r"onebill_[0-9_a-z]+\.obk", name): raise ApiError("Backup not found", 404)
    p = BK / name
    if not p.exists(): raise ApiError("Backup not found", 404)
    if p.stat().st_size > DRIVE_MAX_UPLOAD: raise ApiError("Backup file is too large to upload")
    content = p.read_bytes()
    http = http_fn or drive_http
    token = drive_valid_token(http_fn)
    for attempt in (1, 2):
        try:
            r = http(build_drive_upload_init(name, len(content), token))
            loc = (r.get("headers") or {}).get("location") or ""
            if not loc.startswith("https://www.googleapis.com/"): raise ApiError("Google Drive did not give a valid upload address")
            r = http(build_drive_upload_put(loc, content))
            break
        except DriveHttpError as e:
            if e.status == 401 and attempt == 1:   # access token expired early: refresh once and retry
                token = drive_valid_token(http_fn, force=True); continue
            raise ApiError(_drive_error_message(e))
    j = r.get("json") or {}
    if not j.get("id"): raise ApiError("Google Drive did not confirm the upload")
    return {"ok": True, "name": name, "drive_id": str(j["id"]), "note": "NOT TESTED with a real Google account"}

def api_drive_status(c, q, b):
    st = settings(c); loc = _load_drive_local()
    with DRIVE_LOCK: signing_in = bool(DRIVE_PENDING) and time.time() <= DRIVE_PENDING.get("expires", 0)
    return {"enabled": st.get("drive_enabled") == "1", "allow_plain": st.get("drive_allow_plain") == "1", "has_crypto": HAS_CRYPTO,
            "client_id_configured": bool(loc["client_id"]), "client_secret_configured": bool(loc["client_secret"]),
            "connected": bool(loc["refresh_token"] or loc["access_token"]), "signing_in": signing_in,
            "redirect_uri": f"http://127.0.0.1:{PORT}/oauth/drive", "note": "NOT TESTED with a real Google account"}   # never returns the client ID, secret or tokens

def api_drive_config(c, q, b):
    loc = _load_drive_local()
    if "client_id" in b:
        cid = str(b["client_id"] or "").strip()[:300]
        if cid != loc["client_id"]: loc.update(refresh_token="", access_token="", expires_at=0.0)
        loc["client_id"] = cid
    if "client_secret" in b: loc["client_secret"] = str(b["client_secret"] or "").strip()[:300]
    _save_drive_local(loc)
    return {"ok": True}

def api_drive_auth_start(c, q, b):
    if settings(c).get("drive_enabled") != "1": raise ApiError("Google Drive backup is turned off in Settings")
    r = drive_start_auth(b.get("client_id") or None, b.get("client_secret") if "client_secret" in b else None)
    return {**r, "note": "NOT TESTED with a real Google account"}

def api_drive_disconnect(c, q, b):
    loc = _load_drive_local()
    _save_drive_local({**loc, "refresh_token": "", "access_token": "", "expires_at": 0.0})
    with DRIVE_LOCK: DRIVE_PENDING.clear()
    return {"ok": True, "note": "Removed from this PC. To also remove OneBill's access, visit your Google account > Security > Third-party access."}

def api_drive_upload(c, q, b):
    return drive_upload_backup(c, str(b.get("name") or "").strip())

def drive_result_page(ok, msg):
    from html import escape
    return ("<!doctype html><meta charset=utf-8><title>OneBill</title><body style=\"font-family:system-ui,sans-serif;max-width:520px;margin:60px auto;padding:0 16px\">"
            f"<h2>{'Connected' if ok else 'Not connected'}</h2><p>{escape(msg)}</p></body>").encode("utf-8")

# ---- companies (each company = its own database + backups folder) ----
def api_companies(c, q, b):
    r = load_registry()
    return {"current": CUR[0], "companies": r["companies"]}
def api_company_create(c, q, b):
    name = (b.get("name") or "").strip()[:60]
    if not name: raise ApiError("Company name is required")
    r = load_registry()
    if any(x["name"].lower() == name.lower() for x in r["companies"]): raise ApiError("A company with this name already exists")
    cid = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "co"
    while any(x["id"] == cid for x in r["companies"]) or cid == "main": cid = cid[:24] + "_" + secrets.token_hex(2)
    base = DATA / "companies" / cid; base.mkdir(parents=True, exist_ok=True); (base / "backups").mkdir(exist_ok=True)
    n = conn(base / "onebill.db"); n.execute("PRAGMA journal_mode=WAL"); migrate(n)
    n.execute("INSERT OR REPLACE INTO settings VALUES('shop_name',?)", (name,)); n.close()
    r["companies"].append({"id": cid, "name": name}); save_registry(r)
    return {"id": cid}
def api_company_switch(c, q, b):
    r = load_registry(); cid = b.get("id")
    if not any(x["id"] == cid for x in r["companies"]): raise ApiError("Company not found", 404)
    use_company(cid); r["current"] = cid; save_registry(r); SESSIONS.clear()
    n = conn(); n.execute("PRAGMA journal_mode=WAL"); migrate(n); n.close()
    return {"ok": True, "id": cid}
def api_company_delete(c, q, b):
    r = load_registry(); cid = b.get("id"); co = next((x for x in r["companies"] if x["id"] == cid), None)
    if not co: raise ApiError("Company not found", 404)
    if cid == "main": raise ApiError("The main company holds your original data and cannot be deleted")
    if cid == CUR[0]: raise ApiError("Switch to another company before deleting this one")
    if (b.get("confirm_name") or "").strip() != co["name"]: raise ApiError("Type the company name exactly to confirm")
    trash = DATA / "deleted_companies"; trash.mkdir(exist_ok=True)
    dst = trash / f"{cid}_{dt.datetime.now():%Y%m%d_%H%M%S}"
    os.replace(DATA / "companies" / cid, dst)      # kept on disk, not erased: can be recovered by moving it back
    r["companies"] = [x for x in r["companies"] if x["id"] != cid]; save_registry(r)
    return {"ok": True, "kept_at": str(dst)}

def api_openfolder(c, q, b):
    if os.name == "nt": os.startfile(str(BK))
    return {"folder": str(BK)}
def api_ping(c, q, b): LAST_PING[0] = time.time(); return {"app": "onebill"}
def api_quit(c, q, b): threading.Timer(0.3, lambda: os._exit(0)).start(); return {"ok": True}

SESSIONS = {}   # token -> user dict (None = legacy single-password mode)
PERMS = ("sales", "purchases", "inventory", "parties", "expenses", "reports", "accounting", "settings", "backup", "users")
ROLE_PERMS = {"owner": PERMS + ("owner",), "admin": PERMS, "manager": ("sales", "purchases", "inventory", "parties", "expenses", "reports"), "employee": ("sales", "parties")}
ANY = ("sales", "purchases", "inventory", "parties", "expenses", "reports")
NEEDS = {  # route -> any one of these permissions
 "/api/sale": ("sales",), "/api/void": ("sales",), "/api/sale_return": ("sales",), "/api/invoices": ("sales", "reports"), "/api/invoice": ("sales", "reports"),
 "/api/invoice_returns": ("sales",), "/api/credit_notes": ("sales", "reports"), "/api/credit_note": ("sales", "reports"),
 "/api/products": ("sales", "purchases", "inventory"), "/api/product": ("inventory",), "/api/stock": ("inventory",), "/api/categories": ("sales", "purchases", "inventory"), "/api/stock_take": ("inventory",), "/api/stock_take_preview": ("inventory",), "/api/stock_takes": ("inventory",),
 "/api/parties": ("sales", "purchases", "parties"), "/api/party": ("parties", "sales", "purchases"), "/api/payment": ("parties",), "/api/payment_preview": ("parties",), "/api/payment_void": ("parties",), "/api/open_invoices": ("sales", "parties"), "/api/overdue": ("sales", "reports"),
 "/api/tax_report": ("reports",), "/api/purchase": ("purchases",), "/api/purchases": ("purchases", "reports"), "/api/purchase_get": ("purchases",), "/api/purchase_void": ("purchases",),
 "/api/expense": ("expenses",), "/api/expenses": ("expenses", "reports"), "/api/expense_void": ("expenses",),
 "/api/leads": ("parties",), "/api/lead": ("parties",), "/api/lead_delete": ("parties",),
 "/api/report": ("reports",), "/api/export": ANY, "/api/open_url": ANY, "/api/settings:POST": ("settings",),
 "/api/backups": ("backup",), "/api/backup": ("backup",), "/api/restore": ("backup",), "/api/openfolder": ("backup",), "/api/backup_download": ("backup",), "/api/backup_upload": ("backup",), "/api/drive_status": ("backup",), "/api/drive_auth_start": ("backup",), "/api/drive_upload": ("backup",), "/api/drive_config": ("backup",), "/api/drive_disconnect": ("backup",),
 "/api/company": ("owner",), "/api/company_switch": ("owner",), "/api/company_delete": ("owner",), "/api/password": ("owner",), "/api/owner_setup": ("owner",),
 "/api/docs": ("sales", "purchases"), "/api/doc": ("sales", "purchases"), "/api/doc_status": ("sales", "purchases"), "/api/doc_convert": ("sales", "purchases"),
 "/api/purchase_return": ("purchases",), "/api/purchase_returns": ("purchases", "reports"), "/api/purchase_return_get": ("purchases", "reports"), "/api/purchase_returnable": ("purchases",),
 "/api/accounts": ("accounting",), "/api/account": ("accounting",), "/api/acct_txn": ("accounting",), "/api/acct_txns": ("accounting",), "/api/acct_void": ("accounting",), "/api/fin_report": ("accounting",),
 "/api/recon_preview": ("accounting",), "/api/recon_save": ("accounting",), "/api/recons": ("accounting",), "/api/recon": ("accounting",),
 "/api/shift_status": ("sales",), "/api/shift_open": ("sales",), "/api/shift_close": ("sales",), "/api/shifts": ("sales", "reports"), "/api/shift": ("sales", "reports"),
 "/api/users": ("users",), "/api/user": ("users",), "/api/activity": ("users",)}
READONLY_OK = ("/api/my_password", "/api/logout", "/api/login", "/api/ping")
def hash_pw(pw, salt): return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000).hex()
def user_dict(r): return {"id": r["id"], "username": r["username"], "display": r["display"], "role": r["role"], "perms": list(ROLE_PERMS["owner"]) if r["role"] == "owner" else [x for x in json.loads(r["perms"]) if x in PERMS], "readonly": bool(r["readonly"])}
def users_on(c): return c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
def log_act(c, user, action, detail=""):
    c.execute("INSERT INTO activity(created_at,username,action,detail) VALUES(?,?,?,?)", (now(), (user or {}).get("username", "") if isinstance(user, dict) else (user or ""), action, detail[:300]))
def drop_sessions(uid=None):
    for t in [t for t, u in SESSIONS.items() if uid is None or (u and u["id"] == uid)]: SESSIONS.pop(t, None)
def check_new_pw(pw):
    if len(pw) < 6: raise ApiError("Use at least 6 characters for user passwords")
def pw_check(c, pw):
    st = settings(c)
    if not st.get("pw_hash"): return True
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(st["pw_salt"]), 200_000).hex()
    return hmac.compare_digest(h, st["pw_hash"])
def api_password(c, q, b):
    st = settings(c)
    if st.get("pw_hash") and not pw_check(c, str(b.get("old") or "")): raise ApiError("Current password is wrong", 403)
    new = str(b.get("new") or "")
    if new == "":
        c.execute("DELETE FROM settings WHERE k IN('pw_hash','pw_salt')"); SESSIONS.clear(); return {"ok": True, "enabled": False}
    if len(new) < 4: raise ApiError("Use at least 4 characters")
    salt = secrets.token_bytes(16)
    c.execute("INSERT OR REPLACE INTO settings VALUES('pw_salt',?)", (salt.hex(),))
    c.execute("INSERT OR REPLACE INTO settings VALUES('pw_hash',?)", (hashlib.pbkdf2_hmac("sha256", new.encode(), salt, 200_000).hex(),))
    SESSIONS.clear(); return {"ok": True, "enabled": True}

def api_owner_setup(c, q, b):
    """Turns on user accounts: creates the Owner and retires the old single password."""
    if users_on(c): raise ApiError("User accounts are already enabled")
    name = (b.get("username") or "").strip()[:30]; pw = str(b.get("password") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", name): raise ApiError("Username: 3-30 letters, digits, . _ -")
    check_new_pw(pw); salt = secrets.token_bytes(16)
    c.execute("INSERT INTO users(username,display,role,perms,pw_hash,pw_salt,created_at) VALUES(?,?,?,?,?,?,?)", (name, (b.get("display") or name)[:60], "owner", "[]", hash_pw(pw, salt), salt.hex(), now()))
    c.execute("DELETE FROM settings WHERE k IN('pw_hash','pw_salt')"); drop_sessions()
    log_act(c, name, "owner_setup", "user accounts enabled"); return {"ok": True}
def api_users(c, q, b):
    return [{**user_dict(r), "active": bool(r["active"]), "last_login": r["last_login"], "created_at": r["created_at"]} for r in c.execute("SELECT * FROM users ORDER BY id")]
def api_user_save(c, q, b):
    me = b.pop("_by"); name = (b.get("username") or "").strip()[:30]; role = b.get("role")
    if role not in ROLE_PERMS: raise ApiError("Invalid role")
    if me["role"] != "owner" and role in ("owner", "admin"): raise ApiError("Only the owner can create or edit owner/admin accounts", 403)
    perms = [x for x in (b.get("perms") if b.get("perms") is not None else ROLE_PERMS[role]) if x in PERMS]
    if me["role"] != "owner": perms = [x for x in perms if x in me["perms"]]
    pw = str(b.get("password") or ""); ro = 1 if b.get("readonly") else 0; act = 1 if b.get("active", True) else 0
    c.execute("BEGIN IMMEDIATE")
    if b.get("id"):
        uid = int(b["id"]); old = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not old: raise ApiError("User not found", 404)
        if me["role"] != "owner" and old["role"] in ("owner", "admin"): raise ApiError("Only the owner can edit owner/admin accounts", 403)
        others = c.execute("SELECT COUNT(*) n FROM users WHERE role='owner' AND active=1 AND id<>?", (uid,)).fetchone()["n"]
        if old["role"] == "owner" and (role != "owner" or not act) and others == 0: raise ApiError("There must always be one active owner")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", name): raise ApiError("Username: 3-30 letters, digits, . _ -")
        try: c.execute("UPDATE users SET username=?,display=?,role=?,perms=?,readonly=?,active=? WHERE id=?", (name, (b.get("display") or name)[:60], role, json.dumps(perms), ro, act, uid))
        except sqlite3.IntegrityError: raise ApiError("Username already taken")
        if pw: check_new_pw(pw); salt = secrets.token_bytes(16); c.execute("UPDATE users SET pw_hash=?,pw_salt=? WHERE id=?", (hash_pw(pw, salt), salt.hex(), uid))
        drop_sessions(uid)
    else:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", name): raise ApiError("Username: 3-30 letters, digits, . _ -")
        check_new_pw(pw); salt = secrets.token_bytes(16)
        try: uid = c.execute("INSERT INTO users(username,display,role,perms,readonly,pw_hash,pw_salt,active,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (name, (b.get("display") or name)[:60], role, json.dumps(perms), ro, hash_pw(pw, salt), salt.hex(), act, now())).lastrowid
        except sqlite3.IntegrityError: raise ApiError("Username already taken")
    c.execute("COMMIT"); return {"id": uid}
def api_my_password(c, q, b):
    me = b.pop("_by")
    if not me: raise ApiError("Accounts are not enabled")
    r = c.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()
    if not hmac.compare_digest(hash_pw(str(b.get("old") or ""), bytes.fromhex(r["pw_salt"])), r["pw_hash"]): raise ApiError("Current password is wrong", 403)
    new = str(b.get("new") or ""); check_new_pw(new); salt = secrets.token_bytes(16)
    c.execute("UPDATE users SET pw_hash=?,pw_salt=? WHERE id=?", (hash_pw(new, salt), salt.hex(), r["id"])); return {"ok": True}
def api_activity(c, q, b):
    u = (q.get("user") or [""])[0]
    return [dict(r) for r in c.execute("SELECT * FROM activity" + (" WHERE username=?" if u else "") + " ORDER BY id DESC LIMIT 500", (u,) if u else ())]

# ---------- accounts: cash / bank, money movements, financial statements ----------
def cash_account(c): return c.execute("SELECT id FROM accounts WHERE kind='cash' AND active=1 ORDER BY id LIMIT 1").fetchone()["id"]
def sale_account(c, method):
    """Account that receives a sale's money: cash for cash sales; the chosen card/bank account for card/other (else cash)."""
    v = str(settings(c).get("card_account") or "")
    if method in ("card", "other") and v.isdigit() and c.execute("SELECT 1 FROM accounts WHERE id=? AND active=1", (int(v),)).fetchone(): return int(v)
    return cash_account(c)
def flows(c, a="0000-00-00", z="9999-12-31"):
    """Every money movement, as (time, account_id, signed amount, source, ref, note). Sales/purchases/expenses/party payments are read from
    their own records (single source of truth); manual entries come from acct_txns. Card/other sales go to the chosen card account (else cash)."""
    cash = cash_account(c)
    out, D = [], "substr(created_at,1,10) BETWEEN ? AND ?"
    for r in c.execute(f"SELECT created_at, number, method, paid, customer, account_id FROM invoices WHERE status='posted' AND paid>0 AND {D}", (a, z)):
        out.append((r["created_at"], r["account_id"] or sale_account(c, r["method"]), r["paid"], "sale", r["number"], r["customer"]))
    for r in c.execute(f"SELECT created_at, number, paid FROM purchases WHERE status='posted' AND paid>0 AND {D}", (a, z)): out.append((r["created_at"], cash, -r["paid"], "purchase", r["number"], ""))
    for r in c.execute(f"SELECT created_at, category, amount, note FROM expenses WHERE status='posted' AND {D}", (a, z)): out.append((r["created_at"], cash, -r["amount"], "expense", r["category"], r["note"]))
    for r in c.execute(f"SELECT l.created_at, l.kind, l.amount, p.name FROM ledger l JOIN parties p ON p.id=l.party_id WHERE l.kind IN('payment_in','payment_out','payment_void','advance','advance_refund') AND {D.replace('created_at','l.created_at')}", (a, z)):
        out.append((r["created_at"], cash, -r["amount"], "party_" + r["kind"][8:] if r["kind"].startswith("payment") else r["kind"], r["name"], ""))
    for r in c.execute(f"SELECT created_at, number, refund_cash FROM purchase_returns WHERE refund_cash>0 AND {D}", (a, z)): out.append((r["created_at"], cash, r["refund_cash"], "purchase_return_refund", r["number"], ""))
    for r in c.execute(f"SELECT created_at, number, refund_cash FROM credit_notes WHERE refund_cash>0 AND {D}", (a, z)): out.append((r["created_at"], cash, -r["refund_cash"], "sale_return_refund", r["number"], ""))
    for r in c.execute(f"SELECT id, created_at, account_id, amount, kind, grp, note FROM acct_txns WHERE status='posted' AND {D}", (a, z)):
        out.append((r["created_at"], r["account_id"], r["amount"], r["kind"], r["grp"], r["note"]))
    return sorted(out, key=lambda x: x[0])
def acct_balances(c):
    bal = {r["id"]: r["opening"] for r in c.execute("SELECT id, opening FROM accounts")}
    for f in flows(c): bal[f[1]] = bal.get(f[1], 0) + f[2]
    return bal
def api_accounts(c, q, b):
    bal = acct_balances(c)
    return [{**dict(r), "balance": bal.get(r["id"], 0)} for r in c.execute("SELECT * FROM accounts ORDER BY active DESC, id")]
def api_account_save(c, q, b):
    name = (b.get("name") or "").strip()[:40]
    if not name: raise ApiError("Account name is required")
    kind = b.get("kind") if b.get("kind") in ("cash", "bank") else "bank"
    c.execute("BEGIN IMMEDIATE")
    try:
        if b.get("id"):
            old = c.execute("SELECT * FROM accounts WHERE id=?", (int(b["id"]),)).fetchone()
            if not old: raise ApiError("Account not found", 404)
            act = 1 if b.get("active", True) else 0
            if not act and old["id"] == cash_account(c) and c.execute("SELECT COUNT(*) n FROM accounts WHERE kind='cash' AND active=1").fetchone()["n"] == 1: raise ApiError("Keep at least one active cash account")
            if not act and acct_balances(c).get(old["id"], 0) != 0: raise ApiError("Move the remaining balance out before disabling this account")
            c.execute("UPDATE accounts SET name=?, active=? WHERE id=?", (name, act, old["id"])); aid = old["id"]
        else:
            o = money(b.get("opening")); aid = c.execute("INSERT INTO accounts(name,kind,opening) VALUES(?,?,?)", (name, kind, o)).lastrowid
    except sqlite3.IntegrityError: raise ApiError("An account with this name already exists")
    c.execute("COMMIT"); return {"id": aid}
ACCT_TYPES = ("income", "capital_in", "drawings", "transfer", "loan_in", "loan_repay")
def api_acct_txn(c, q, b):
    typ = b.get("type")
    if typ not in ACCT_TYPES: raise ApiError("Invalid entry type")
    amt = money(b.get("amount"))
    if amt <= 0: raise ApiError("Amount must be above zero")
    date = (b.get("date") or "").strip(); when = now() if not date else date[:10] + " " + dt.datetime.now().strftime("%H:%M:%S")
    try: dt.date.fromisoformat(when[:10])
    except ValueError: raise ApiError("Invalid date")
    note = (b.get("note") or "").strip()[:120]
    c.execute("BEGIN IMMEDIATE")
    def acct(k):
        r = c.execute("SELECT * FROM accounts WHERE id=? AND active=1", (int(b.get(k) or 0),)).fetchone()
        if not r: raise ApiError("Choose an active account")
        return r
    a1 = acct("account_id"); bal = acct_balances(c); grp = secrets.token_hex(6)
    out = typ in ("drawings", "transfer", "loan_repay")
    if out and bal.get(a1["id"], 0) < amt: raise ApiError(f"Not enough money in {a1['name']}")
    if typ == "loan_repay":
        owed = c.execute("SELECT COALESCE(SUM(amount),0) v FROM acct_txns WHERE kind IN('loan_in','loan_repay') AND status='posted'").fetchone()["v"]
        if amt > owed: raise ApiError("Repayment is more than the loan outstanding")
    ins = lambda aid, a_: c.execute("INSERT INTO acct_txns(created_at,account_id,amount,kind,grp,note) VALUES(?,?,?,?,?,?)", (when, aid, a_, typ, grp, note))
    if typ == "transfer":
        a2 = acct("to_account_id")
        if a2["id"] == a1["id"]: raise ApiError("Choose two different accounts")
        ins(a1["id"], -amt); ins(a2["id"], amt)
    else: ins(a1["id"], -amt if out else amt)
    c.execute("COMMIT"); return {"group": grp}
def api_acct_void(c, q, b):
    c.execute("BEGIN IMMEDIATE")
    rows = c.execute("SELECT * FROM acct_txns WHERE grp=? AND status='posted'", (str(b.get("group")),)).fetchall()
    if not rows: raise ApiError("Entry not found or already voided", 404)
    c.execute("UPDATE acct_txns SET status='void' WHERE grp=?", (rows[0]["grp"],))
    bad = [r for r in acct_balances(c).items() if r[1] < 0]       # a void must not leave an account below zero
    if bad: raise ApiError("Cannot void: it would make an account balance negative")
    c.execute("COMMIT"); return {"ok": True}
def api_acct_txns(c, q, b):
    aid = int((q.get("account_id") or ["0"])[0]); a, z = rng(q)
    acc = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not acc: raise ApiError("Account not found", 404)
    before = acc["opening"] + sum(f[2] for f in flows(c, "0000-00-00", (dt.date.fromisoformat(a) - dt.timedelta(days=1)).isoformat()) if f[1] == aid)
    run, rows = before, []
    for f in flows(c, a, z):
        if f[1] != aid: continue
        run += f[2]; rows.append({"at": f[0], "amount": f[2], "source": f[3], "ref": f[4], "note": f[5], "balance": run})
    grp = {r["grp"]: r["id"] for r in c.execute("SELECT grp, MAX(id) id FROM acct_txns GROUP BY grp")}
    for r in rows:
        if r["source"] in ACCT_TYPES: r["group"] = r["ref"]
    return {"account": dict(acc), "opening_balance": before, "rows": rows[::-1], "closing": run}
def api_fin_report(c, q, b):
    a, z = rng(q); rp = api_report(c, {"from": [a], "to": [z]}, {})
    oi = c.execute("SELECT COALESCE(SUM(amount),0) v FROM acct_txns WHERE kind='income' AND status='posted' AND substr(created_at,1,10) BETWEEN ? AND ?", (a, z)).fetchone()["v"]
    net_sales = rp["totals"]["subtotal"] - rp["totals"]["discount"] - (rp["returns"]["subtotal"] - rp["returns"]["discount"])
    pnl = {"net_sales": net_sales, "cost_of_goods": rp["est_cost"] - c.execute("SELECT COALESCE(SUM((l.qty_milli*p.cost+500)/1000),0) v FROM credit_note_lines l JOIN credit_notes n ON n.id=l.cn_id JOIN products p ON p.id=l.product_id WHERE substr(n.created_at,1,10) BETWEEN ? AND ?", (a, z)).fetchone()["v"],
           "gross": rp["gross"], "expenses": rp["expenses"], "expenses_by": rp["expenses_by"], "other_income": oi, "net": rp["gross"] - rp["expenses"] + oi, "tax_collected": rp["totals"]["tax"] - rp["returns"]["tax"]}
    bal = acct_balances(c); accts = [{"name": r["name"], "kind": r["kind"], "balance": bal.get(r["id"], 0)} for r in c.execute("SELECT * FROM accounts WHERE active=1 ORDER BY id")]
    pb = [r["b"] for r in c.execute("SELECT COALESCE(SUM(amount),0) b FROM ledger GROUP BY party_id")]
    loans = c.execute("SELECT COALESCE(SUM(amount),0) v FROM acct_txns WHERE kind IN('loan_in','loan_repay') AND status='posted'").fetchone()["v"]
    assets = sum(x["balance"] for x in accts) + sum(x for x in pb if x > 0) + rp["stock_value"]; liab = -sum(x for x in pb if x < 0) + loans
    sheet = {"accounts": accts, "receivables": sum(x for x in pb if x > 0), "inventory": rp["stock_value"], "total_assets": assets, "payables": -sum(x for x in pb if x < 0), "loans": loans, "total_liabilities": liab, "equity": assets - liab,
             "note": "Owner's equity is the balancing figure (assets - liabilities). Tax collected is not shown as a liability."}
    cf = {}
    for f in flows(c, a, z): cf[f[3]] = cf.get(f[3], 0) + f[2]
    transfers_net = cf.pop("transfer", 0)
    cashflow = {"by_source": cf, "net_change": sum(cf.values()) + transfers_net, "transfers_net": transfers_net,
                "opening_total": sum(r["opening"] for r in c.execute("SELECT opening FROM accounts WHERE active=1")) + sum(f[2] for f in flows(c, "0000-00-00", (dt.date.fromisoformat(a) - dt.timedelta(days=1)).isoformat()))}
    cashflow["closing_total"] = cashflow["opening_total"] + cashflow["net_change"]
    return {"from": a, "to": z, "pnl": pnl, "balance_sheet": sheet, "cashflow": cashflow}

# ---------- v0.9 Step 6: cash drawer / shifts & bank reconciliation ----------
def shift_user_key(user):
    if isinstance(user, dict) and user.get("username"): return str(user["username"])
    if isinstance(user, str) and user: return user
    return "_default_"

def shift_expected_cash(c, aid, opened_at, closed_at=None, opening_override=None):
    acc = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not acc: raise ApiError("Account not found", 404)
    if opening_override is not None:
        opening = opening_override
    else:
        sh = c.execute("SELECT opening_cash FROM shifts WHERE account_id=? AND opened_at=? ORDER BY id DESC LIMIT 1", (aid, opened_at)).fetchone()
        opening = sh["opening_cash"] if sh else 0
    end_ts = closed_at or "9999-12-31 23:59:59"
    move_sum = 0
    for f in flows(c, "0000-00-00", "9999-12-31"):
        t_str, f_aid, amt = f[0], f[1], f[2]
        if f_aid == aid and opened_at <= t_str <= end_ts:
            move_sum += amt
    return opening + move_sum

def shift_dict(c, r):
    d = dict(r)
    acc = c.execute("SELECT name, kind FROM accounts WHERE id=?", (r["account_id"],)).fetchone()
    d["account_name"] = acc["name"] if acc else ""
    d["account_kind"] = acc["kind"] if acc else ""
    if r["status"] == "open":
        exp = shift_expected_cash(c, r["account_id"], r["opened_at"], None, r["opening_cash"])
        d["expected_cash"] = exp
        d["movements"] = exp - r["opening_cash"]
    else:
        d["movements"] = (r["expected_cash"] or 0) - r["opening_cash"]
    return d

def api_shift_status(c, q, b):
    st = settings(c); enabled = st.get("shifts_enabled") == "1"
    uk = shift_user_key(b.get("_by"))
    open_r = c.execute("SELECT * FROM shifts WHERE status='open' AND username=? ORDER BY id DESC LIMIT 1", (uk,)).fetchone()
    any_open = c.execute("SELECT * FROM shifts WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    cur_r = open_r or (any_open if uk == "_default_" else None)
    return {"enabled": enabled, "open_shift": shift_dict(c, cur_r) if cur_r else None}

def api_shift_open(c, q, b):
    if settings(c).get("shifts_enabled") != "1":
        raise ApiError("Cash shifts are disabled in Settings")
    opening = money(b.get("opening_cash"))
    note = (b.get("note") or "").strip()[:160]
    uk = shift_user_key(b.get("_by"))
    aid_raw = b.get("account_id")
    c.execute("BEGIN IMMEDIATE")
    if aid_raw not in (None, "", 0, "0"):
        acc = c.execute("SELECT * FROM accounts WHERE id=? AND active=1", (int(aid_raw),)).fetchone()
        if not acc: raise ApiError("Account not found")
        if acc["kind"] != "cash": raise ApiError("Shift drawer must use a cash account")
        aid = acc["id"]
    else:
        aid = cash_account(c)
    existing = c.execute("SELECT id FROM shifts WHERE status='open' AND username=?", (uk,)).fetchone()
    if existing: raise ApiError("You already have an open shift; close it before opening a new one")
    ts = now()
    sid = c.execute("INSERT INTO shifts(username,account_id,opened_at,opening_cash,open_note,status) VALUES(?,?,?,?,?,'open')",
                    (uk, aid, ts, opening, note)).lastrowid
    c.execute("COMMIT")
    return shift_dict(c, c.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone())

def api_shift_close(c, q, b):
    if settings(c).get("shifts_enabled") != "1":
        raise ApiError("Cash shifts are disabled in Settings")
    if "counted_cash" not in b or str(b.get("counted_cash", "")).strip() == "":
        raise ApiError("Counted cash is required to close a shift")
    counted = money(b.get("counted_cash"))
    note = (b.get("note") or "").strip()[:160]
    uk = shift_user_key(b.get("_by"))
    c.execute("BEGIN IMMEDIATE")
    sid_arg = b.get("id")
    if sid_arg not in (None, "", 0, "0"):
        sh = c.execute("SELECT * FROM shifts WHERE id=?", (int(sid_arg),)).fetchone()
        if not sh: raise ApiError("Shift not found", 404)
    else:
        sh = c.execute("SELECT * FROM shifts WHERE status='open' AND username=? ORDER BY id DESC LIMIT 1", (uk,)).fetchone()
        if not sh and uk == "_default_":
            sh = c.execute("SELECT * FROM shifts WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        if not sh: raise ApiError("No open shift found")
    if sh["status"] != "open":
        raise ApiError("Shift is already closed")
    closed_ts = now()
    exp = shift_expected_cash(c, sh["account_id"], sh["opened_at"], closed_ts, sh["opening_cash"])
    diff = counted - exp
    adj_grp = ""
    if diff != 0:
        adj_grp = secrets.token_hex(6)
        kind = "income" if diff > 0 else "drawings"
        label = "Cash over" if diff > 0 else "Cash short"
        desc = f"Shift #{sh['id']} {label}" + (f": {note}" if note else "")
        c.execute("INSERT INTO acct_txns(created_at,account_id,amount,kind,grp,note,status) VALUES(?,?,?,?,?,?,'posted')",
                  (closed_ts, sh["account_id"], diff, kind, adj_grp, desc[:120]))
    c.execute("UPDATE shifts SET closed_at=?, counted_cash=?, expected_cash=?, difference=?, adj_grp=?, close_note=?, status='closed' WHERE id=?",
              (closed_ts, counted, exp, diff, adj_grp, note, sh["id"]))
    c.execute("COMMIT")
    return shift_dict(c, c.execute("SELECT * FROM shifts WHERE id=?", (sh["id"],)).fetchone())

def api_shifts(c, q, b):
    rows = c.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 100").fetchall()
    return [shift_dict(c, r) for r in rows]

def api_shift_get(c, q, b):
    sid = int((q.get("id") or ["0"])[0])
    r = c.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
    if not r: raise ApiError("Shift not found", 404)
    d = shift_dict(c, r)
    end_ts = r["closed_at"] or "9999-12-31 23:59:59"
    mvs = []
    for f in flows(c, "0000-00-00", "9999-12-31"):
        if f[1] == r["account_id"] and r["opened_at"] <= f[0] <= end_ts:
            if r["adj_grp"] and f[4] == r["adj_grp"]:
                continue
            mvs.append({"at": f[0], "amount": f[2], "source": f[3], "ref": f[4], "note": f[5]})
    d["items"] = mvs
    return d

def _recon_flows(c, aid):
    """Posted movements on account `aid` with immutable keys (time|account_id|amount|source|row_id) so renaming parties never alters keys."""
    cash = cash_account(c)
    out = []
    for r in c.execute("SELECT id, created_at, number, method, paid, customer, account_id FROM invoices WHERE status='posted' AND paid>0"):
        a_id = r["account_id"] or sale_account(c, r["method"])
        if a_id == aid:
            out.append({"key": f"{r['created_at']}|{aid}|{r['paid']}|sale|{r['id']}",
                        "at": r["created_at"], "amount": r["paid"], "source": "sale", "ref": r["number"], "note": r["customer"]})
    if aid == cash:
        for r in c.execute("SELECT id, created_at, number, paid FROM purchases WHERE status='posted' AND paid>0"):
            out.append({"key": f"{r['created_at']}|{aid}|{-r['paid']}|purchase|{r['id']}",
                        "at": r["created_at"], "amount": -r["paid"], "source": "purchase", "ref": r["number"], "note": ""})
        for r in c.execute("SELECT id, created_at, category, amount, note FROM expenses WHERE status='posted'"):
            out.append({"key": f"{r['created_at']}|{aid}|{-r['amount']}|expense|{r['id']}",
                        "at": r["created_at"], "amount": -r["amount"], "source": "expense", "ref": r["category"], "note": r["note"]})
        for r in c.execute("SELECT l.id, l.created_at, l.kind, l.amount, p.name FROM ledger l JOIN parties p ON p.id=l.party_id WHERE l.kind IN('payment_in','payment_out','payment_void','advance','advance_refund')"):
            src_name = "party_" + r["kind"][8:] if r["kind"].startswith("payment") else r["kind"]
            out.append({"key": f"{r['created_at']}|{aid}|{-r['amount']}|{src_name}|{r['id']}",
                        "at": r["created_at"], "amount": -r["amount"], "source": src_name, "ref": r["name"], "note": ""})
        for r in c.execute("SELECT id, created_at, number, refund_cash FROM purchase_returns WHERE refund_cash>0"):
            out.append({"key": f"{r['created_at']}|{aid}|{r['refund_cash']}|purchase_return_refund|{r['id']}",
                        "at": r["created_at"], "amount": r["refund_cash"], "source": "purchase_return_refund", "ref": r["number"], "note": ""})
        for r in c.execute("SELECT id, created_at, number, refund_cash FROM credit_notes WHERE refund_cash>0"):
            out.append({"key": f"{r['created_at']}|{aid}|{-r['refund_cash']}|sale_return_refund|{r['id']}",
                        "at": r["created_at"], "amount": -r["refund_cash"], "source": "sale_return_refund", "ref": r["number"], "note": ""})
    for r in c.execute("SELECT id, created_at, account_id, amount, kind, grp, note FROM acct_txns WHERE status='posted' AND account_id=?", (aid,)):
        out.append({"key": f"{r['created_at']}|{aid}|{r['amount']}|{r['kind']}|{r['id']}",
                    "at": r["created_at"], "amount": r["amount"], "source": r["kind"], "ref": r["grp"], "note": r["note"]})
    return sorted(out, key=lambda x: x["at"])

def _voided_txn_keys(c, aid):
    posted_keys = {t["key"] for t in _recon_flows(c, aid)}
    cleared_rows = c.execute("SELECT id, recon_id, txn_key, amount, cleared_at FROM bank_recon_items WHERE account_id=?", (aid,)).fetchall()
    voided = []
    for cr in cleared_rows:
        if cr["txn_key"] not in posted_keys:
            voided.append({"id": cr["id"], "recon_id": cr["recon_id"], "txn_key": cr["txn_key"], "amount": cr["amount"], "cleared_at": cr["cleared_at"]})
    return voided

def _compute_recon(c, aid, stmt_date, stmt_bal, cleared_keys):
    acc = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not acc: raise ApiError("Account not found", 404)
    try: dt.date.fromisoformat(stmt_date)
    except ValueError: raise ApiError("Invalid statement date")
    book_bal = acct_balances(c).get(aid, 0)
    prev_cleared = {r["txn_key"] for r in c.execute("SELECT txn_key FROM bank_recon_items WHERE account_id=?", (aid,)).fetchall()}
    all_keys = set(prev_cleared) | set(cleared_keys)
    txns = []
    for t in _recon_flows(c, aid):
        if t["at"][:10] <= stmt_date:
            txns.append({**t, "previously_cleared": t["key"] in prev_cleared, "cleared": t["key"] in all_keys})
    cleared_sum = sum(t["amount"] for t in txns if t["cleared"])
    reconciled_balance = acc["opening"] + cleared_sum
    diff = stmt_bal - reconciled_balance
    return {"account": dict(acc), "statement_date": stmt_date, "statement_balance": stmt_bal,
            "book_balance": book_bal, "reconciled_balance": reconciled_balance, "cleared_total": cleared_sum,
            "difference": diff, "can_finish": diff == 0, "transactions": txns,
            "voided_cleared": _voided_txn_keys(c, aid)}

def api_recon_preview(c, q, b):
    aid = int(b.get("account_id") or 0)
    stmt_date = str(b.get("statement_date") or dt.date.today().isoformat()).strip()[:10]
    raw_sb = b.get("statement_balance")
    if raw_sb is None or str(raw_sb).strip() == "":
        raise ApiError("Statement ending balance is required")
    stmt_bal = money(raw_sb)
    ck = b.get("cleared_keys")
    if ck is None: ck = []
    if not isinstance(ck, list) or not all(isinstance(x, str) for x in ck):
        raise ApiError("cleared_keys must be a list")
    return _compute_recon(c, aid, stmt_date, stmt_bal, ck)

def api_recon_save(c, q, b):
    aid = int(b.get("account_id") or 0)
    stmt_date = str(b.get("statement_date") or "").strip()[:10]
    if not stmt_date: raise ApiError("Statement date is required")
    raw_sb = b.get("statement_balance")
    if raw_sb is None or str(raw_sb).strip() == "":
        raise ApiError("Statement ending balance is required")
    stmt_bal = money(raw_sb)
    note = (b.get("note") or "").strip()[:160]
    ck = b.get("cleared_keys")
    if ck is None: ck = []
    if not isinstance(ck, list) or not all(isinstance(x, str) for x in ck):
        raise ApiError("cleared_keys must be a list")
    uk = shift_user_key(b.get("_by"))
    c.execute("BEGIN IMMEDIATE")
    info = _compute_recon(c, aid, stmt_date, stmt_bal, ck)
    if info["difference"] != 0:
        raise ApiError(f"Reconciliation difference must be 0.00 before finishing (current difference: {info['difference']/100:.2f})")
    ts = now()
    rid = c.execute("INSERT INTO bank_recons(account_id,statement_date,statement_balance,book_balance,cleared_total,difference,note,username,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (aid, stmt_date, stmt_bal, info["book_balance"], info["cleared_total"], 0, note, uk, ts)).lastrowid
    valid_map = {t["key"]: t["amount"] for t in info["transactions"]}
    for k in sorted(set(ck)):
        if k in valid_map:
            c.execute("INSERT INTO bank_recon_items(recon_id,account_id,txn_key,amount,cleared_at) VALUES(?,?,?,?,?)",
                      (rid, aid, k, valid_map[k], ts))
    c.execute("COMMIT")
    return {"id": rid, "account_id": aid, "statement_date": stmt_date, "statement_balance": stmt_bal,
            "book_balance": info["book_balance"], "difference": 0, "created_at": ts}

def api_recons(c, q, b):
    aid = int((q.get("account_id") or ["0"])[0])
    sql = "SELECT r.*, a.name account_name FROM bank_recons r JOIN accounts a ON a.id=r.account_id"
    params = ()
    if aid:
        sql += " WHERE r.account_id=?"
        params = (aid,)
    sql += " ORDER BY r.id DESC LIMIT 100"
    return [dict(r) for r in c.execute(sql, params).fetchall()]

def api_recon_get(c, q, b):
    rid = int((q.get("id") or ["0"])[0])
    r = c.execute("SELECT r.*, a.name account_name FROM bank_recons r JOIN accounts a ON a.id=r.account_id WHERE r.id=?", (rid,)).fetchone()
    if not r: raise ApiError("Reconciliation not found", 404)
    items = [dict(x) for x in c.execute("SELECT * FROM bank_recon_items WHERE recon_id=? ORDER BY id", (rid,)).fetchall()]
    out = dict(r); out["items"] = items
    return out

# ---------- quotations, sales/purchase orders, delivery challans ----------
DOC_KINDS = {"quotation": "QT", "sales_order": "SO", "purchase_order": "PO", "challan": "DC"}
def fmt_milli(m): return f"{m // 1000}.{m % 1000:03d}"
def fmt_cents(v): return f"{v // 100}.{v % 100:02d}"
def doc_number(c, kind):
    pre = DOC_KINDS[kind]; r = c.execute("SELECT COALESCE(MAX(CAST(substr(number,?) AS INTEGER)),0) m FROM docs WHERE kind=?", (len(pre) + 2, kind)).fetchone()
    k = "seq_" + kind; v = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone(); n = max(int(v["v"]) if v else 0, r["m"]) + 1
    c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(n))); return f"{pre}-{n:06d}"
def doc_get(c, did):
    d = c.execute("SELECT d.*, p.name party, p.phone party_phone, p.address party_address FROM docs d LEFT JOIN parties p ON p.id=d.party_id WHERE d.id=?", (did,)).fetchone()
    if not d: raise ApiError("Document not found", 404)
    return {**dict(d), "lines": [dict(r) for r in c.execute("SELECT l.*, COALESCE(pr.sku,'') sku FROM doc_lines l LEFT JOIN products pr ON pr.id=l.product_id WHERE l.doc_id=? ORDER BY l.id", (did,))]}
def doc_create(c, kind, b, source=None):
    """Writes one document (no stock or money effect except an order's advance payment). Caller holds the transaction."""
    items = b.get("items") or []
    if not items: raise ApiError("Add at least one item")
    purchase = kind == "purchase_order"; lines, sub = [], 0
    for it in items:
        pr = c.execute("SELECT * FROM products WHERE id=? AND active=1", (int(it["product_id"]),)).fetchone()
        if not pr: raise ApiError("Product not found or inactive")
        qm = qty(it["qty"])
        if qm <= 0: raise ApiError("Quantity must be above zero")
        up = money(it["price"]) if it.get("price") not in (None, "") else (pr["cost"] if purchase else pr["price"])
        lt = (up * qm + 500) // 1000; lines.append((pr, qm, up, lt)); sub += lt
    disc = money(b.get("discount"))
    if disc > sub: raise ApiError("Discount is larger than subtotal")
    tax = 0 if purchase else ((sub - disc) * int(settings(c)["tax_bp"]) + 5000) // 10000
    total = sub - disc + tax; pid = party_of(c, b); adv = money(b.get("advance")) if kind in ("sales_order", "purchase_order") else 0
    if adv and not pid: raise ApiError("Choose a party to record an advance payment")
    if adv > total: raise ApiError("Advance is more than the total")
    due = (b.get("due_date") or "").strip()
    if due:
        try: dt.date.fromisoformat(due)
        except ValueError: raise ApiError("Invalid date")
    cust = c.execute("SELECT name FROM parties WHERE id=?", (pid,)).fetchone()["name"] if pid else (b.get("customer") or "")[:80]
    number = doc_number(c, kind)
    did = c.execute("INSERT INTO docs(kind,number,created_at,party_id,customer,subtotal,discount,tax,total,advance,due_date,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (kind, number, now(), pid, cust, sub, disc, tax, total, adv, due, (b.get("note") or "").strip()[:120])).lastrowid
    for pr, qm, up, lt in lines: c.execute("INSERT INTO doc_lines VALUES(NULL,?,?,?,?,?,?)", (did, pr["id"], pr["name"], qm, up, lt))
    if adv: ledger(c, pid, -adv if kind == "sales_order" else adv, "advance", number)      # customer advance: credit; supplier advance: we paid, so debit
    return did
def doc_allowed(user, kind):
    """Purchase orders need the Purchases permission, every other document needs Sales (legacy single-password mode: everything)."""
    return user is None or ("purchases" if kind == "purchase_order" else "sales") in user["perms"]
def api_doc_create(c, q, b):
    kind = b.get("kind"); me = b.pop("_by", None)
    if kind not in DOC_KINDS: raise ApiError("Invalid document type")
    if not doc_allowed(me, kind): raise ApiError("You do not have permission for this", 403)
    c.execute("BEGIN IMMEDIATE"); did = doc_create(c, kind, b); c.execute("COMMIT"); return doc_get(c, did)
def api_docs(c, q, b):
    kind = (q.get("kind") or [""])[0]; st = (q.get("status") or [""])[0]; me = b.pop("_by", None)
    return [dict(r) for r in c.execute("SELECT d.*, p.name party FROM docs d LEFT JOIN parties p ON p.id=d.party_id WHERE (?='' OR d.kind=?) AND (?='' OR d.status=?) ORDER BY d.id DESC LIMIT 300", (kind, kind, st, st)) if doc_allowed(me, r["kind"])]
def api_doc(c, q, b):
    me = b.pop("_by", None); d = doc_get(c, int(q["id"][0]))
    if not doc_allowed(me, d["kind"]): raise ApiError("You do not have permission for this", 403)
    return d
def _cancel_advance(c, d):
    if d["advance"] and d["party_id"]:       # the advance is handed back
        ledger(c, d["party_id"], d["advance"] if d["kind"] == "sales_order" else -d["advance"], "advance_refund", d["number"])
def api_doc_status(c, q, b):
    st = b.get("status"); me = b.pop("_by", None)
    if st not in ("cancelled", "delivered"): raise ApiError("Invalid status")
    c.execute("BEGIN IMMEDIATE"); d = c.execute("SELECT * FROM docs WHERE id=?", (int(b["id"]),)).fetchone()
    if not d: raise ApiError("Document not found", 404)
    if not doc_allowed(me, d["kind"]): raise ApiError("You do not have permission for this", 403)
    if d["status"] != "open" and not (st == "cancelled" and d["status"] == "delivered"): raise ApiError("This document is already " + d["status"])
    if st == "delivered" and d["kind"] not in ("sales_order", "challan", "purchase_order"): raise ApiError("Only orders and challans can be marked delivered")
    c.execute("UPDATE docs SET status=? WHERE id=?", (st, d["id"]))
    if st == "cancelled": _cancel_advance(c, d)
    c.execute("COMMIT"); return doc_get(c, d["id"])
def api_doc_convert(c, q, b):
    to = b.get("to"); did = int(b["id"]); me = b.pop("_by", None)
    c.execute("BEGIN IMMEDIATE"); d = c.execute("SELECT * FROM docs WHERE id=?", (did,)).fetchone()
    if not d: raise ApiError("Document not found", 404)
    if not doc_allowed(me, d["kind"]) or not doc_allowed(me, "purchase_order" if to == "purchase" else "quotation"): raise ApiError("You do not have permission for this", 403)
    ok = {("quotation", "sales_order"), ("quotation", "invoice"), ("sales_order", "invoice"), ("challan", "invoice"), ("purchase_order", "purchase")}
    if (d["kind"], to) not in ok: raise ApiError("This conversion is not possible")
    if d["status"] not in ("open", "delivered"): raise ApiError("This document is already " + d["status"])
    lines = c.execute("SELECT * FROM doc_lines WHERE doc_id=?", (did,)).fetchall()
    items = [{"product_id": l["product_id"], "qty": fmt_milli(l["qty_milli"]), "price": fmt_cents(l["unit_price"])} for l in lines]
    if to == "invoice" and ((d["subtotal"] - d["discount"]) * int(settings(c)["tax_bp"]) + 5000) // 10000 != d["tax"]:
        c.execute("ROLLBACK"); raise ApiError("The tax rate changed since this document was made. Create the invoice manually.")
    c.execute("COMMIT")
    # claim the document first so two clicks cannot create two invoices; give it back if creating the result fails
    c.execute("BEGIN IMMEDIATE")
    if c.execute("UPDATE docs SET status='converted' WHERE id=? AND status IN('open','delivered')", (did,)).rowcount != 1: c.execute("ROLLBACK"); raise ApiError("Already converted")
    c.execute("COMMIT")
    try:
        if to == "sales_order":
            c.execute("BEGIN IMMEDIATE"); nid = doc_create(c, "sales_order", {"items": items, "discount": fmt_cents(d["discount"]), "party_id": d["party_id"], "customer": d["customer"], "note": d["note"], "due_date": b.get("due_date") or d["due_date"], "advance": b.get("advance")}, did)
            c.execute("UPDATE docs SET converted_to=? WHERE id=?", (c.execute("SELECT number FROM docs WHERE id=?", (nid,)).fetchone()["number"], did)); c.execute("COMMIT"); return doc_get(c, nid)
        if to == "invoice":
            pid = d["party_id"]; prices = {l["product_id"]: l["unit_price"] for l in lines}
            tendered = b.get("tendered") if b.get("tendered") not in (None, "") else ("0" if pid else fmt_cents(d["total"]))
            inv = api_sale(c, {}, {"items": items, "discount": fmt_cents(d["discount"]), "party_id": pid, "customer": d["customer"], "method": b.get("method") or "cash", "tendered": tendered}, _prices=prices)
        else:
            inv = api_purchase(c, {}, {"items": [{"product_id": l["product_id"], "qty": fmt_milli(l["qty_milli"]), "cost": fmt_cents(l["unit_price"])} for l in lines], "party_id": d["party_id"], "paid": b.get("paid") if b.get("paid") not in (None, "") else ("0" if d["party_id"] else fmt_cents(d["total"])), "note": d["number"]})
    except Exception:
        if c.in_transaction: c.execute("ROLLBACK")
        c.execute("UPDATE docs SET status='open' WHERE id=?", (did,)); raise
    c.execute("UPDATE docs SET converted_to=? WHERE id=?", (inv["number"], did))
    return {"document": doc_get(c, did), "result": inv}

# ---------- purchase returns ----------
def _pr_returned(c, lid): return c.execute("SELECT COALESCE(SUM(qty_milli),0) v FROM purchase_return_lines WHERE purchase_line_id=?", (lid,)).fetchone()["v"]
def api_purchase_returnable(c, q, b):
    u = api_purchase_get(c, q, b)
    for l in u["lines"]: l["returned_milli"] = _pr_returned(c, l["id"])
    return u
def api_purchase_return(c, q, b):
    items = [x for x in (b.get("items") or []) if qty(x.get("qty") or 0) > 0]
    if not items: raise ApiError("Choose at least one item to return")
    c.execute("BEGIN IMMEDIATE")
    u = c.execute("SELECT * FROM purchases WHERE id=?", (int(b["purchase_id"]),)).fetchone()
    if not u or u["status"] != "posted": raise ApiError("Purchase not found or void")
    lines = {l["id"]: l for l in c.execute("SELECT l.*, p.track_stock, p.stock_milli FROM purchase_lines l JOIN products p ON p.id=l.product_id WHERE purchase_id=?", (u["id"],))}
    picked, seen, total = [], set(), 0
    for x in items:
        lid = int(x["line_id"]); l = lines.get(lid); qm = qty(x["qty"])
        if not l or lid in seen: raise ApiError("Invalid line")
        seen.add(lid)
        if qm > l["qty_milli"] - _pr_returned(c, lid): raise ApiError(f"Cannot return more than was bought: {l['name']}")
        if l["track_stock"] and l["stock_milli"] < qm: raise ApiError(f"Not enough stock to send back: {l['name']}")
        if l["tax_amt"] is None: lt = (l["unit_cost"] * qm + 500) // 1000; tx = None   # old-style purchase: no tax
        else:   # v0.7 line: exact share of the net line amount and of its tax; the last return of a line takes what is left
            done = c.execute("SELECT COALESCE(SUM(line_total),0) a, COALESCE(SUM(tax_amt),0) t FROM purchase_return_lines WHERE purchase_line_id=?", (lid,)).fetchone()
            if qm == l["qty_milli"] - _pr_returned(c, lid): lt, tx = l["line_total"] - done["a"], l["tax_amt"] - done["t"]
            else: lt, tx = (l["line_total"] * qm + l["qty_milli"] // 2) // l["qty_milli"], (l["tax_amt"] * qm + l["qty_milli"] // 2) // l["qty_milli"]
        picked.append((l, qm, lt, tx)); total += lt + (tx or 0)
    if all(_pr_returned(c, lid) + {p[0]["id"]: p[1] for p in picked}.get(lid, 0) == l["qty_milli"] for lid, l in lines.items()):
        total = u["total"] - c.execute("SELECT COALESCE(SUM(total),0) v FROM purchase_returns WHERE purchase_id=?", (u["id"],)).fetchone()["v"]   # last return settles exactly
    pid = u["party_id"]
    cash = money(b.get("refund_cash")) if b.get("refund_cash") not in (None, "") else (0 if pid else total)
    if not pid: cash = total
    if cash > total: raise ApiError("Cash refund is more than the return total")
    number = next_seq(c, "prn_seq", "PRN", "purchase_returns")
    rid = c.execute("INSERT INTO purchase_returns(number,created_at,purchase_id,party_id,total,refund_cash,note,tax) VALUES(?,?,?,?,?,?,?,?)",
                    (number, now(), u["id"], pid, total, cash, (b.get("note") or "").strip()[:120], sum(p[3] or 0 for p in picked))).lastrowid
    for l, qm, lt, tx in picked:
        c.execute("INSERT INTO purchase_return_lines(pr_id,purchase_line_id,product_id,name,qty_milli,unit_cost,line_total,tax_bp,tax_amt) VALUES(?,?,?,?,?,?,?,?,?)",
                  (rid, l["id"], l["product_id"], l["name"], qm, l["unit_cost"], lt, l["tax_bp"] if tx is not None else None, tx))
        if l["track_stock"]:
            c.execute("UPDATE products SET stock_milli=stock_milli-? WHERE id=?", (qm, l["product_id"]))
            c.execute("INSERT INTO stock_moves VALUES(NULL,?,?,?,?,?)", (l["product_id"], -qm, "purchase_return", number, now()))
    if pid: ledger(c, pid, total, "purchase_return", number, u["number"]); ledger(c, pid, -cash, "refund_received", number)
    c.execute("COMMIT"); return api_purchase_return_get(c, {"id": [str(rid)]}, {})
def api_purchase_returns(c, q, b):
    a, z = rng(q)
    return [dict(r) for r in c.execute("SELECT r.*, u.number purchase_number FROM purchase_returns r JOIN purchases u ON u.id=r.purchase_id WHERE substr(r.created_at,1,10) BETWEEN ? AND ? ORDER BY r.id DESC LIMIT 300", (a, z))]
def api_purchase_return_get(c, q, b):
    r = c.execute("SELECT r.*, u.number purchase_number, p.name party, p.phone party_phone, p.address party_address, p.tax_no party_tax_no FROM purchase_returns r JOIN purchases u ON u.id=r.purchase_id LEFT JOIN parties p ON p.id=r.party_id WHERE r.id=?", (int(q["id"][0]),)).fetchone()
    if not r: raise ApiError("Purchase return not found", 404)
    return {**dict(r), "lines": [dict(x) for x in c.execute("SELECT * FROM purchase_return_lines WHERE pr_id=? ORDER BY id", (r["id"],))]}

LEGACY_TAX = "Before per-line tax (v0.4 and older)"
def rate_label(bp):
    if bp is None: return LEGACY_TAX
    v = Decimal(bp) / 100
    return (f"{v:f}".rstrip("0").rstrip(".") if v != v.to_integral() else str(int(v))) + "%"
def tax_rows(c, sql_new, sql_old, a, z, keys):
    """Rows grouped by tax rate; the legacy bucket (rate None) holds documents made before per-line tax existed."""
    out = {}
    for r in c.execute(sql_new, (a, z)): out[r["rate"]] = dict(zip(keys, (r["taxable"], r["tax"])))
    if sql_old:
        r = c.execute(sql_old, (a, z)).fetchone()
        if r["taxable"] or r["tax"]: out[None] = dict(zip(keys, (r["taxable"], r["tax"])))
    return out
def api_tax_report(c, q, b):
    """Management tax figures for a date range. Output tax (sales minus credit notes) minus input tax (purchases minus purchase returns). Not a tax return."""
    a, z = rng(q)
    D = "substr({}.created_at,1,10) BETWEEN ? AND ?"
    sales = tax_rows(c, f"SELECT il.tax_bp rate, SUM(il.line_total - il.alloc_disc) taxable, SUM(il.tax_amt) tax FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE i.status='posted' AND {D.format('i')} AND il.tax_amt IS NOT NULL GROUP BY il.tax_bp",
                     f"SELECT SUM(i.subtotal - i.discount) taxable, SUM(i.tax) tax FROM invoices i WHERE i.status='posted' AND {D.format('i')} AND NOT EXISTS (SELECT 1 FROM invoice_lines il WHERE il.invoice_id=i.id AND il.tax_amt IS NOT NULL)", a, z, ("sales_taxable", "sales_tax"))
    rets = tax_rows(c, f"SELECT l.tax_bp rate, SUM(l.line_total - l.alloc_disc) taxable, SUM(l.tax_amt) tax FROM credit_note_lines l JOIN credit_notes n ON n.id=l.cn_id WHERE {D.format('n')} AND l.tax_amt IS NOT NULL GROUP BY l.tax_bp",
                    f"SELECT SUM(n.subtotal - n.discount) taxable, SUM(n.tax) tax FROM credit_notes n WHERE {D.format('n')} AND NOT EXISTS (SELECT 1 FROM credit_note_lines l WHERE l.cn_id=n.id AND l.tax_amt IS NOT NULL)", a, z, ("ret_taxable", "ret_tax"))
    buys = tax_rows(c, f"SELECT l.tax_bp rate, SUM(l.line_total) taxable, SUM(l.tax_amt) tax FROM purchase_lines l JOIN purchases u ON u.id=l.purchase_id WHERE u.status='posted' AND {D.format('u')} AND l.tax_amt IS NOT NULL GROUP BY l.tax_bp", None, a, z, ("buy_taxable", "buy_tax"))
    pret = tax_rows(c, f"SELECT l.tax_bp rate, SUM(l.line_total) taxable, SUM(l.tax_amt) tax FROM purchase_return_lines l JOIN purchase_returns r ON r.id=l.pr_id WHERE {D.format('r')} AND l.tax_amt IS NOT NULL GROUP BY l.tax_bp", None, a, z, ("pret_taxable", "pret_tax"))
    def build(parts, keys, net):
        rates = sorted({k for p in parts for k in p}, key=lambda k: (k is None, k or 0)); rows = []
        for k in rates:
            row = {"rate_bp": k, "label": rate_label(k), **{x: 0 for x in keys}}
            for p in parts: row.update(p.get(k, {}))
            row["net_taxable"] = row[keys[0]] - row[keys[2]]; row["net_tax"] = row[keys[1]] - row[keys[3]]; rows.append(row)
        return {"rows": rows, "taxable": sum(r["net_taxable"] for r in rows), "tax": sum(r["net_tax"] for r in rows)}
    out = build((sales, rets), ("sales_taxable", "sales_tax", "ret_taxable", "ret_tax"), True)
    inp = build((buys, pret), ("buy_taxable", "buy_tax", "pret_taxable", "pret_tax"), True)
    return {"from": a, "to": z, "output": out, "input": inp, "net_payable": out["tax"] - inp["tax"],
            "note": "Management report - not a tax return.", "legacy_label": LEGACY_TAX}

ROUTES = {("GET", "/api/products"): api_products, ("POST", "/api/product"): api_product_save, ("POST", "/api/stock"): api_stock, ("GET", "/api/categories"): api_categories, ("POST", "/api/stock_take"): api_stock_take, ("POST", "/api/stock_take_preview"): api_stock_take_preview, ("GET", "/api/stock_takes"): api_stock_takes, ("GET", "/api/stock_take"): api_stock_take_get,
 ("POST", "/api/sale"): api_sale, ("GET", "/api/invoices"): api_invoices, ("GET", "/api/invoice"): api_invoice, ("POST", "/api/void"): api_void,
 ("GET", "/api/report"): api_report, ("GET", "/api/tax_report"): api_tax_report, ("GET", "/api/settings"): api_settings_get, ("POST", "/api/settings"): api_settings_set,
 ("GET", "/api/backups"): api_backup_list, ("POST", "/api/backup"): api_backup, ("POST", "/api/restore"): api_restore, ("GET", "/api/drive_status"): api_drive_status, ("POST", "/api/drive_auth_start"): api_drive_auth_start, ("POST", "/api/drive_upload"): api_drive_upload, ("POST", "/api/drive_config"): api_drive_config, ("POST", "/api/drive_disconnect"): api_drive_disconnect,
 ("POST", "/api/openfolder"): api_openfolder, ("POST", "/api/open_url"): api_open_url,
 ("GET", "/api/parties"): api_parties, ("POST", "/api/party"): api_party_save, ("GET", "/api/party"): api_party, ("POST", "/api/payment"): api_payment, ("POST", "/api/payment_preview"): api_payment_preview, ("POST", "/api/payment_void"): api_payment_void,
 ("GET", "/api/open_invoices"): api_open_invoices, ("GET", "/api/overdue"): api_overdue,
 ("POST", "/api/purchase"): api_purchase, ("GET", "/api/purchases"): api_purchases, ("GET", "/api/purchase"): api_purchase_get, ("POST", "/api/purchase_void"): api_purchase_void,
 ("POST", "/api/expense"): api_expense, ("GET", "/api/expenses"): api_expenses, ("POST", "/api/expense_void"): api_expense_void,
 ("GET", "/api/leads"): api_leads, ("POST", "/api/lead"): api_lead_save, ("POST", "/api/lead_delete"): api_lead_delete,
 ("GET", "/api/export"): api_export,
 ("GET", "/api/invoice_returns"): api_invoice_returns, ("POST", "/api/sale_return"): api_sale_return, ("GET", "/api/credit_notes"): api_credit_notes, ("GET", "/api/credit_note"): api_credit_note,
 ("GET", "/api/backup_download"): api_backup_download, ("POST", "/api/backup_upload"): api_backup_upload,
 ("POST", "/api/doc"): api_doc_create, ("GET", "/api/docs"): api_docs, ("GET", "/api/doc"): api_doc, ("POST", "/api/doc_status"): api_doc_status, ("POST", "/api/doc_convert"): api_doc_convert,
 ("POST", "/api/purchase_return"): api_purchase_return, ("GET", "/api/purchase_returns"): api_purchase_returns, ("GET", "/api/purchase_return_get"): api_purchase_return_get, ("GET", "/api/purchase_returnable"): api_purchase_returnable,
 ("GET", "/api/accounts"): api_accounts, ("POST", "/api/account"): api_account_save, ("POST", "/api/acct_txn"): api_acct_txn, ("GET", "/api/acct_txns"): api_acct_txns, ("POST", "/api/acct_void"): api_acct_void, ("GET", "/api/fin_report"): api_fin_report,
 ("POST", "/api/recon_preview"): api_recon_preview, ("POST", "/api/recon_save"): api_recon_save, ("GET", "/api/recons"): api_recons, ("GET", "/api/recon"): api_recon_get,
 ("GET", "/api/shift_status"): api_shift_status, ("POST", "/api/shift_open"): api_shift_open, ("POST", "/api/shift_close"): api_shift_close, ("GET", "/api/shifts"): api_shifts, ("GET", "/api/shift"): api_shift_get,
 ("POST", "/api/owner_setup"): api_owner_setup, ("GET", "/api/users"): api_users, ("POST", "/api/user"): api_user_save, ("POST", "/api/my_password"): api_my_password, ("GET", "/api/activity"): api_activity,
 ("GET", "/api/companies"): api_companies, ("POST", "/api/company"): api_company_create, ("POST", "/api/company_switch"): api_company_switch, ("POST", "/api/company_delete"): api_company_delete, ("POST", "/api/password"): api_password, ("GET", "/api/ping"): api_ping, ("POST", "/api/quit"): api_quit}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype="application/json", extra=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(raw)
    def token(self):
        m = re.search(r"(?:^|;\s*)ob_sess=([0-9a-f]+)", self.headers.get("Cookie") or "")
        return m.group(1) if m else ""
    def handle_any(self, method):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path); host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"): return self.send(403, {"error": "Forbidden host"})
        if method == "GET" and u.path == "/oauth/drive":   # Google sends the browser back here after sign-in; the random state is the check, no database is touched
            ok, msg = drive_finish_auth({k: v[0] for k, v in parse_qs(u.query).items()})
            return self.send(200 if ok else 400, drive_result_page(ok, msg), "text/html; charset=utf-8")
        if method == "GET" and not u.path.startswith("/api/"):
            f = APP / "ui" / "index.html"
            return self.send(200, f.read_bytes(), "text/html; charset=utf-8") if u.path in ("/", "/index.html") else self.send(404, {"error": "Not found"})
        public = u.path in ("/api/ping", "/api/me", "/api/login", "/api/logout")
        fn = ROUTES.get((method, u.path))
        if not fn and not public: return self.send(404, {"error": "Not found"})
        if method == "POST" and self.headers.get("X-OneBill") != "1": return self.send(403, {"error": "Missing header"})  # blocks cross-site posts
        c = None
        try:
            n = int(self.headers.get("Content-Length") or 0); b = json.loads(self.rfile.read(n) or b"{}") if n else {}
            with LOCK:
                c = conn()
                has_pw = bool(settings(c).get("pw_hash")); uon = users_on(c); user = SESSIONS.get(self.token())
                authed = (not has_pw and not uon) or self.token() in SESSIONS
                if u.path == "/api/me": return self.send(200, {"need_login": has_pw or uon, "need_user": uon, "ok": authed, "user": user,
                                                              "legacy_password": has_pw and not uon})
                if u.path == "/api/login":
                    if uon:
                        r = c.execute("SELECT * FROM users WHERE username=? AND active=1", (str(b.get("username") or ""),)).fetchone()
                        good = bool(r) and hmac.compare_digest(hash_pw(str(b.get("password") or ""), bytes.fromhex(r["pw_salt"])), r["pw_hash"])
                        if not good:
                            log_act(c, str(b.get("username") or "")[:30], "login_failed"); time.sleep(0.7); return self.send(401, {"error": "Wrong username or password"})
                        t = secrets.token_hex(24); SESSIONS[t] = user_dict(r)
                        c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), r["id"])); log_act(c, r["username"], "login")
                    else:
                        if not pw_check(c, str(b.get("password") or "")):
                            time.sleep(0.7); return self.send(401, {"error": "Wrong password"})
                        t = secrets.token_hex(24); SESSIONS[t] = None
                    return self.send(200, {"ok": True}, extra={"Set-Cookie": f"ob_sess={t}; Path=/; HttpOnly; SameSite=Strict"})
                if u.path == "/api/logout":
                    if user: log_act(c, user, "logout")
                    SESSIONS.pop(self.token(), None); return self.send(200, {"ok": True}, extra={"Set-Cookie": "ob_sess=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                if u.path != "/api/ping" and not authed: return self.send(401, {"error": "Login required", "login": True})
                if user:   # account mode: server-side permission check (the UI only hides things, this is the real gate)
                    need = NEEDS.get(u.path + ":POST" if (u.path == "/api/settings" and method == "POST") else u.path)
                    if need and not any(x in user["perms"] for x in need): return self.send(403, {"error": "You do not have permission for this"})
                    if method == "POST" and user["readonly"] and u.path not in READONLY_OK: return self.send(403, {"error": "Your account is read-only"})
                if u.path in ("/api/user", "/api/my_password", "/api/stock_take", "/api/shift_status", "/api/shift_open", "/api/shift_close", "/api/recon_save") or u.path in ("/api/doc", "/api/docs", "/api/doc_status", "/api/doc_convert"): b["_by"] = user
                if u.path == "/api/owner_setup" and uon: return self.send(400, {"error": "User accounts are already enabled"})
                res = fn(c, parse_qs(u.query), b)
                if method == "POST" and u.path not in ("/api/ping", "/api/owner_setup"):
                    safe = {k: v for k, v in b.items() if k not in ("password", "old", "new", "data", "_by", "confirm_name", "logo", "signature", "passphrase", "code_verifier", "client_secret", "refresh_token", "access_token")}
                    log_act(c, user, u.path[5:], json.dumps(safe, ensure_ascii=False)[:300])
            if isinstance(res, Raw): self.send(200, res.body, res.ctype, res.headers)
            else: self.send(200, res)
        except ApiError as e: self.send(e.code, {"error": str(e)})
        except (KeyError, ValueError, TypeError, json.JSONDecodeError): self.send(400, {"error": "Invalid request"})
        except sqlite3.Error as e: self.send(500, {"error": f"Database error: {e}"})
        finally:
            if c is not None:
                if c.in_transaction: c.execute("ROLLBACK")
                c.close()
    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")

def running(port):
    try:
        import urllib.request
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1)).get("app") == "onebill"
    except Exception: return False
def open_window(url):
    for p in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", "")):
        for sub in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            e = Path(p) / sub
            if p and e.exists(): subprocess.Popen([str(e), f"--app={url}", "--window-size=1280,820"]); return
    webbrowser.open(url)
def clean_temp_files():
    """Remove half-written temporary files that a crash during a backup, restore or upload could leave behind."""
    for pat in (".tmp_plain_*", ".tmp_restore_*", "*.part"):
        for f in BK.glob(pat):
            try: f.unlink()
            except OSError: pass
def autobackup():
    clean_temp_files()
    today = dt.datetime.now().strftime("%Y%m%d")
    if not list(BK.glob(f"onebill_{today}_*_auto.db")): make_backup("auto")
    for f in sorted(BK.glob("onebill_*_auto.db"))[:-30]: f.unlink()   # keep 30 newest automatic backups

def main():
    if "--reset-password" in sys.argv:
        init(); c = conn(); c.execute("DELETE FROM settings WHERE k IN('pw_hash','pw_salt')"); c.close(); print("Password removed."); return
    if running(PORT): open_window(f"http://127.0.0.1:{PORT}/"); return
    init(); autobackup()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)       # 127.0.0.1 only: not reachable from the network
    def watchdog():
        while True:
            time.sleep(10)
            if LAST_PING[0] and time.time() - LAST_PING[0] > 180: os._exit(0)
    threading.Thread(target=watchdog, daemon=True).start()
    if not os.environ.get("ONEBILL_NOWINDOW"): open_window(f"http://127.0.0.1:{PORT}/")
    srv.serve_forever()
if __name__ == "__main__": main()
