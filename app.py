import os
import re
import io
import json
import hashlib
import secrets
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, session, redirect

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# ── Supabase client (REST via requests) ───────────────────────────────────────
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')  # service_role key

def sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
    }

def sb_get(table, params=''):
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    req = urllib.request.Request(url, headers=sb_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def sb_post(table, data):
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers=sb_headers(), method='POST')
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def sb_delete(table, params):
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    req = urllib.request.Request(url, headers=sb_headers(), method='DELETE')
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status

def sb_patch(table, params, data):
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers=sb_headers(), method='PATCH')
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# ── Auth helpers ───────────────────────────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_user(username):
    try:
        rows = sb_get('usuarios', f'username=eq.{username}&select=*')
        return rows[0] if rows else None
    except:
        return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        if session.get('role') != 'admin':
            return jsonify({'error': 'Solo el administrador puede hacer esto'}), 403
        return f(*args, **kwargs)
    return decorated

# ── PDF Parser ─────────────────────────────────────────────────────────────────
COL_RANGES = {
    'SubGrupo':    (0,   225),
    'Producto':    (225, 340),
    'StaElena':   (340, 380),
    'Tabancura':  (380, 420),
    'Pana':       (420, 460),
    'Total':      (460, 500),
    'PorEntregar':(500, 545),
    'Disponible': (545, float('inf')),
}

def col_for(cx):
    for name, (lo, hi) in COL_RANGES.items():
        if lo <= cx < hi:
            return name
    return None

def parse_pdf(file_bytes):
    import pdfplumber
    from collections import defaultdict
    rows_by_line = {}
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            for w in words:
                cx = (w['x0'] + w['x1']) / 2
                row_key = page_num * 10000 + round(w['top'] / 8) * 8
                col = col_for(cx)
                if col is None:
                    continue
                key = (row_key, col)
                rows_by_line[key] = rows_by_line.get(key, '') + (' ' if key in rows_by_line else '') + w['text']
    lines = defaultdict(dict)
    for (rk, col), val in rows_by_line.items():
        lines[rk][col] = val
    products = []
    for rk in sorted(lines):
        row = lines[rk]
        sg = row.get('SubGrupo', '').strip()
        pr = row.get('Producto', '').strip()
        if 'producto' in pr.lower() and 'disponible' in row.get('Disponible','').lower():
            continue
        if pr in ('Stock Norglas Softland','Stock Norglas','Softland','Norglas'):
            continue
        if not pr:
            continue
        def n(s):
            try: return float((s or '').replace(',','.'))
            except: return 0.0
        products.append({
            'subgrupo':     sg,
            'producto':     pr,
            'sta_elena':   n(row.get('StaElena','')),
            'tabancura':   n(row.get('Tabancura','')),
            'pana':        n(row.get('Pana','')),
            'total':       n(row.get('Total','')),
            'por_entregar':n(row.get('PorEntregar','')),
            'disponible':  n(row.get('Disponible','')),
        })
    return products

# ── Stock cache (evitar leer Supabase en cada búsqueda) ───────────────────────
_stock_cache = []
_stock_fecha = None
_cache_ts = 0

def get_stock():
    global _stock_cache, _stock_fecha, _cache_ts
    import time
    if _stock_cache and (time.time() - _cache_ts) < 300:  # cache 5 min
        return _stock_cache, _stock_fecha
    try:
        rows = sb_get('productos', 'select=*&limit=2000')
        _stock_cache = rows
        # leer fecha del primer producto
        meta = sb_get('stock_meta', 'select=*&order=id.desc&limit=1')
        _stock_fecha = meta[0]['fecha'] if meta else None
        _cache_ts = time.time()
        return _stock_cache, _stock_fecha
    except Exception as e:
        return _stock_cache, _stock_fecha  # devolver cache anterior si falla

def invalidate_cache():
    global _cache_ts
    _cache_ts = 0

# ── Búsqueda ──────────────────────────────────────────────────────────────────
def normalize(text):
    text = text.lower()
    text = re.sub(r'[./()\-_]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def search_stock(query, stock):
    tokens = normalize(query).split()
    if not tokens:
        return []
    scored = []
    for item in stock:
        h = normalize((item.get('subgrupo') or '') + ' ' + (item.get('producto') or ''))
        m = sum(1 for t in tokens if t in h)
        if m == len(tokens):
            scored.append((m, item))
    if not scored:
        half = max(1, len(tokens)//2)
        for item in stock:
            h = normalize((item.get('subgrupo') or '') + ' ' + (item.get('producto') or ''))
            m = sum(1 for t in tokens if t in h)
            if m >= half:
                scored.append((m, item))
    scored.sort(key=lambda x: -x[0])
    return [i for _, i in scored[:15]]

# ── Motor analítico ────────────────────────────────────────────────────────────
ANALYTIC_PATTERNS = [
    (r'resumen|panorama|estado general|cuántos productos|cuantos productos', 'resumen'),
    (r'stock\s*0|sin stock|stock cero|agotado|sin disponible', 'stock_cero'),
    (r'negativo|bajo cero|menor.*cero|deuda', 'stock_negativo'),
    (r'comprometido.*mayor|mayor.*que.*(físico|fisico)|pedidos.*sin stock|más comprometido que', 'comprometido_mayor'),
    (r'mayor.*(disponible)|más.*(disponible)|top.*disponible|máximo disponible', 'top_disponible'),
    (r'mayor.*comprometido|más.*comprometido|top.*comprometido|máximo comprometido', 'top_comprometido'),
    (r'mayor.*(físico|fisico|total)|más.*(físico|fisico)|top.*(físico|fisico)', 'top_fisico'),
    (r'crít|crit|alerta|urgente|quiebre', 'criticos'),
    (r'bodega|por bodega|sta elena|tabancura|pana', 'por_bodega'),
    (r'categor|grupo|subgrupo|familia', 'top_categoria'),
]

def detect_top_n(query):
    m = re.search(r'\b(top\s*|los?\s*|las?\s*)(\d+)', query.lower())
    return int(m.group(2)) if m else 10

def fmt_num(n):
    if n == int(n): return f"{int(n):,}".replace(',','.')
    return f"{n:,.2f}".replace(',','X').replace('.', ',').replace('X','.')

def analytic_response(query, stock):
    q = query.lower()
    N = detect_top_n(query)
    qtype = None
    for pattern, qt in ANALYTIC_PATTERNS:
        if re.search(pattern, q):
            qtype = qt; break
    if qtype is None:
        return None

    D, T, PE, SE, TAB, PANA = 'disponible','total','por_entregar','sta_elena','tabancura','pana'

    if qtype == 'resumen':
        total = len(stock)
        con = sum(1 for p in stock if p[D] > 0)
        cero = sum(1 for p in stock if p[D] == 0)
        neg = sum(1 for p in stock if p[D] < 0)
        cats = len(set(p.get('subgrupo','') for p in stock if p.get('subgrupo')))
        tf = sum(p[T] for p in stock)
        tc = sum(p[PE] for p in stock)
        td = sum(p[D] for p in stock)
        return (f"📊 <b>Resumen general</b><br><br>"
                f"• Productos: <b>{total:,}</b> · Categorías: <b>{cats}</b><br>"
                f"• Disponible &gt; 0: <b style='color:#2E7D32'>{con:,}</b><br>"
                f"• Disponible = 0: <b style='color:#9E9E9E'>{cero:,}</b><br>"
                f"• Disponible negativo: <b style='color:#C62828'>{neg:,}</b><br><br>"
                f"• Físico total: <b>{fmt_num(tf)}</b><br>"
                f"• Comprometido total: <b style='color:#E65100'>{fmt_num(tc)}</b><br>"
                f"• Disponible neto: <b style='color:#1565C0'>{fmt_num(td)}</b>")

    if qtype == 'stock_cero':
        items = [p for p in stock if p[D] == 0
                 and not any(x in (p.get('producto') or '').upper()
                             for x in ['KILO','SERVICIO','FLETE','EMBALAJE'])]
        return _html_lista(f"🔴 Stock disponible = 0 <small>({len(items)} productos)</small>",
                           items[:N], D, len(items) > N)

    if qtype == 'stock_negativo':
        items = sorted([p for p in stock if p[D] < 0], key=lambda x: x[D])
        return _html_lista(f"🚨 Stock negativo <small>({len(items)} productos)</small>",
                           items[:N], D, len(items) > N)

    if qtype == 'top_disponible':
        items = sorted([p for p in stock if p[D] > 0], key=lambda x: -x[D])
        return _html_lista(f"🟢 Top {N} mayor disponible", items[:N], D)

    if qtype == 'top_comprometido':
        items = sorted([p for p in stock if p[PE] > 0], key=lambda x: -x[PE])
        return _html_lista(f"🟠 Top {N} mayor comprometido", items[:N], PE)

    if qtype == 'top_fisico':
        items = sorted([p for p in stock if p[T] > 0], key=lambda x: -x[T])
        return _html_lista(f"📦 Top {N} mayor stock físico", items[:N], T)

    if qtype == 'criticos':
        items = sorted([p for p in stock if p[D] < 0], key=lambda x: x[D])
        return _html_lista(f"⚠️ Productos críticos <small>({len(items)})</small>",
                           items[:N], D, len(items) > N)

    if qtype == 'por_bodega':
        se = sum(p[SE] for p in stock)
        tab = sum(p[TAB] for p in stock)
        pana = sum(p[PANA] for p in stock)
        total = se + tab + pana or 1
        top_se   = max(stock, key=lambda x: x[SE])
        top_tab  = max(stock, key=lambda x: x[TAB])
        top_pana = max(stock, key=lambda x: x[PANA])
        return (f"🏭 <b>Stock por bodega</b><br><br>"
                f"• <b>Sta Elena:</b> {fmt_num(se)} ({se/total*100:.1f}%)<br>"
                f"  ↳ {top_se['producto'][:45]} ({fmt_num(top_se[SE])})<br><br>"
                f"• <b>Tabancura:</b> {fmt_num(tab)} ({tab/total*100:.1f}%)<br>"
                f"  ↳ {top_tab['producto'][:45]} ({fmt_num(top_tab[TAB])})<br><br>"
                f"• <b>Pana:</b> {fmt_num(pana)} ({pana/total*100:.1f}%)<br>"
                f"  ↳ {top_pana['producto'][:45]} ({fmt_num(top_pana[PANA])})<br><br>"
                f"• <b>Total:</b> {fmt_num(total)}")

    if qtype == 'top_categoria':
        from collections import defaultdict
        cs, cc = defaultdict(float), defaultdict(int)
        for p in stock:
            sg = p.get('subgrupo') or ''
            if sg: cs[sg] += p[D]; cc[sg] += 1
        top = sorted(cs.items(), key=lambda x: -x[1])[:N]
        rows = ''.join(
            f"<tr><td>{i+1}</td><td>{cat}</td>"
            f"<td style='text-align:right'>{cc[cat]}</td>"
            f"<td style='text-align:right;color:#2E7D32;font-weight:700'>{fmt_num(v)}</td></tr>"
            for i,(cat,v) in enumerate(top))
        return (f"📂 <b>Top {N} categorías por disponible</b><br><br>"
                f"<div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:13px'>"
                f"<tr style='background:#E8EAF6'><th>#</th><th style='text-align:left'>Categoría</th>"
                f"<th>Prods</th><th>Disponible</th></tr>{rows}</table></div>")

    if qtype == 'comprometido_mayor':
        items = sorted([p for p in stock if p[PE] > p[T]], key=lambda x: x[PE]-x[T], reverse=True)
        return _html_lista(f"🚨 Comprometido &gt; Físico <small>({len(items)})</small>",
                           items[:N], D, len(items) > N)
    return None

def _html_lista(titulo, items, campo, hay_mas=False):
    LABELS = {'disponible':'Disponible','por_entregar':'Comprometido','total':'Físico'}
    COLORS = {'disponible':('#2E7D32','#C62828'),'por_entregar':('#E65100','#E65100'),'total':('#1565C0','#9E9E9E')}
    label = LABELS.get(campo, campo)
    cp, cn = COLORS.get(campo, ('#333','#C62828'))
    if not items:
        return f"{titulo}<br><br>No hay productos en esta categoría."
    rows = ''.join(
        f"<tr><td style='padding:4px 6px;font-size:12px;color:#555'>{i+1}</td>"
        f"<td style='padding:4px 6px;font-size:12px'><b>{(p.get('producto') or '')[:50]}</b>"
        f"<br><span style='color:#9E9E9E;font-size:11px'>{(p.get('subgrupo') or '')[:40]}</span></td>"
        f"<td style='padding:4px 8px;text-align:right;font-weight:700;"
        f"color:{cp if p[campo]>=0 else cn};font-size:13px'>{fmt_num(p[campo])}</td></tr>"
        for i,p in enumerate(items))
    extra = f"<br><small>Mostrando {len(items)} de más resultados.</small>" if hay_mas else ""
    return (f"{titulo}<br><br>"
            f"<div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse'>"
            f"<tr style='background:#E8EAF6;font-size:12px'>"
            f"<th style='padding:4px 6px;text-align:left'>#</th>"
            f"<th style='padding:4px 6px;text-align:left'>Producto</th>"
            f"<th style='padding:4px 8px;text-align:right'>{label}</th></tr>"
            f"{rows}</table></div>{extra}")

# ── HTML Pages ─────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Norglas – Acceso</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:linear-gradient(135deg,#0D47A1,#1976D2,#42A5F5);
         min-height:100dvh; display:flex; align-items:center; justify-content:center; }
  .card { background:white; border-radius:16px; padding:36px 28px; width:100%;
          max-width:360px; box-shadow:0 8px 32px rgba(0,0,0,0.25); margin:16px; }
  .logo { text-align:center; margin-bottom:24px; }
  .logo h1 { font-size:1.6rem; color:#1565C0; }
  .logo p  { color:#9E9E9E; font-size:0.85rem; margin-top:4px; }
  label { display:block; font-size:0.85rem; font-weight:600; color:#333; margin-bottom:4px; }
  input { width:100%; border:1.5px solid #DDD; border-radius:10px;
          padding:11px 14px; font-size:0.95rem; outline:none; margin-bottom:14px; }
  input:focus { border-color:#1565C0; }
  button { width:100%; background:#1565C0; color:white; border:none;
           border-radius:10px; padding:13px; font-size:1rem; font-weight:700;
           cursor:pointer; transition:background .15s; }
  button:hover { background:#1976D2; }
  .error { background:#FFEBEE; color:#C62828; border-radius:8px;
           padding:10px 14px; font-size:0.85rem; margin-bottom:14px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>📦 Stock Norglas</h1>
    <p>Consulta de inventario en tiempo real</p>
  </div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <label>Usuario</label>
    <input type="text" name="username" placeholder="tu usuario" required autocomplete="username">
    <label>Contraseña</label>
    <input type="password" name="password" placeholder="••••••••" required autocomplete="current-password">
    <button type="submit">Ingresar</button>
  </form>
</div>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin – Stock Norglas</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#F0F4F8; min-height:100dvh; }
  header { background:linear-gradient(135deg,#0D47A1,#1976D2);
           color:white; padding:14px 20px; display:flex;
           align-items:center; justify-content:space-between; }
  header h1 { font-size:1.1rem; }
  .nav { display:flex; gap:10px; }
  .nav a { color:rgba(255,255,255,0.85); text-decoration:none; font-size:0.85rem;
            background:rgba(255,255,255,0.15); padding:6px 12px; border-radius:20px; }
  .nav a:hover { background:rgba(255,255,255,0.25); }
  .container { max-width:760px; margin:24px auto; padding:0 16px; }
  .card { background:white; border-radius:14px; padding:22px;
          box-shadow:0 2px 8px rgba(0,0,0,0.08); margin-bottom:20px; }
  .card h2 { font-size:1rem; color:#1565C0; margin-bottom:16px; }
  .upload-area { border:2px dashed #90CAF9; border-radius:10px;
                 padding:28px; text-align:center; cursor:pointer; transition:background .15s; }
  .upload-area:hover { background:#E3F2FD; }
  .upload-area input { display:none; }
  .upload-area p { color:#666; font-size:0.9rem; margin-top:8px; }
  .btn { display:inline-block; background:#1565C0; color:white; border:none;
         border-radius:8px; padding:10px 20px; font-size:0.9rem; font-weight:600;
         cursor:pointer; transition:background .15s; }
  .btn:hover { background:#1976D2; }
  .btn.danger { background:#C62828; }
  .btn.danger:hover { background:#E53935; }
  .btn.sm { padding:6px 14px; font-size:0.8rem; }
  .msg { padding:10px 14px; border-radius:8px; font-size:0.9rem; margin-top:12px; display:none; }
  .msg.ok  { background:#E8F5E9; color:#2E7D32; }
  .msg.err { background:#FFEBEE; color:#C62828; }
  table { width:100%; border-collapse:collapse; font-size:0.88rem; }
  th { background:#E8EAF6; color:#3949AB; padding:8px 12px; text-align:left; font-weight:600; }
  td { padding:8px 12px; border-bottom:1px solid #EEE; vertical-align:middle; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px;
           font-size:0.75rem; font-weight:700; }
  .badge.admin { background:#E3F2FD; color:#1565C0; }
  .badge.vendedor { background:#F3E5F5; color:#6A1B9A; }
  input[type=text], input[type=password] {
    border:1.5px solid #DDD; border-radius:8px; padding:8px 12px;
    font-size:0.9rem; outline:none; width:100%; margin-bottom:10px; }
  input[type=text]:focus, input[type=password]:focus { border-color:#1565C0; }
  .form-row { display:flex; gap:10px; flex-wrap:wrap; }
  .form-row > * { flex:1; min-width:140px; }
  .spinner { display:inline-block; width:16px; height:16px; border:2px solid #ccc;
             border-top-color:#1565C0; border-radius:50%;
             animation:spin .7s linear infinite; vertical-align:middle; }
  @keyframes spin { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>📦 Stock Norglas — Administración</h1>
  <div class="nav">
    <a href="/">Ver Chat</a>
    <a href="/logout">Salir</a>
  </div>
</header>
<div class="container">

  <!-- SUBIR PDF -->
  <div class="card">
    <h2>📤 Actualizar stock desde PDF</h2>
    <div class="upload-area" onclick="document.getElementById('pdfFile').click()">
      <div style="font-size:2.5rem">📄</div>
      <p>Toca para seleccionar el PDF de Softland</p>
      <p id="fname" style="color:#1565C0;font-weight:600;margin-top:6px"></p>
      <input type="file" id="pdfFile" accept=".pdf">
    </div>
    <button class="btn" style="margin-top:14px;width:100%" onclick="uploadPDF()">
      Subir y actualizar stock
    </button>
    <div id="uploadMsg" class="msg"></div>
  </div>

  <!-- GESTIÓN USUARIOS -->
  <div class="card">
    <h2>👥 Usuarios</h2>
    <div id="usersTable">Cargando...</div>
    <hr style="margin:16px 0;border:none;border-top:1px solid #EEE">
    <h2>➕ Agregar usuario</h2>
    <div class="form-row">
      <input type="text"     id="newUser" placeholder="nombre de usuario">
      <input type="password" id="newPass" placeholder="contraseña">
    </div>
    <div class="form-row">
      <select id="newRole" style="border:1.5px solid #DDD;border-radius:8px;
              padding:8px 12px;font-size:0.9rem;outline:none;flex:1">
        <option value="vendedor">Vendedor</option>
        <option value="admin">Administrador</option>
      </select>
      <button class="btn" onclick="addUser()">Crear usuario</button>
    </div>
    <div id="userMsg" class="msg"></div>
  </div>

</div>
<script>
// ── Upload PDF ──
document.getElementById('pdfFile').addEventListener('change', function(){
  const name = this.files[0]?.name || '';
  document.getElementById('fname').textContent = name;
});

async function uploadPDF() {
  const file = document.getElementById('pdfFile').files[0];
  if (!file) { showMsg('uploadMsg','Selecciona un PDF primero.','err'); return; }
  const msg = document.getElementById('uploadMsg');
  msg.style.display='block'; msg.className='msg ok';
  msg.innerHTML='<span class="spinner"></span> Procesando PDF...';
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/upload', {method:'POST', body:fd});
    const d = await r.json();
    if (d.error) { showMsg('uploadMsg', d.error, 'err'); }
    else { showMsg('uploadMsg', `✅ Stock actualizado: ${d.n.toLocaleString('es-CL')} productos al ${d.fecha}`, 'ok'); }
  } catch(e) { showMsg('uploadMsg', 'Error al subir PDF.', 'err'); }
}

// ── Users ──
async function loadUsers() {
  const r = await fetch('/admin/usuarios');
  const users = await r.json();
  if (!users.length) { document.getElementById('usersTable').innerHTML='<p style="color:#9E9E9E">Sin usuarios aún.</p>'; return; }
  let html = '<table><tr><th>Usuario</th><th>Rol</th><th>Acción</th></tr>';
  users.forEach(u => {
    html += `<tr>
      <td><b>${esc(u.username)}</b></td>
      <td><span class="badge ${u.role}">${u.role}</span></td>
      <td><button class="btn sm danger" onclick="deleteUser('${esc(u.username)}')">Eliminar</button></td>
    </tr>`;
  });
  html += '</table>';
  document.getElementById('usersTable').innerHTML = html;
}

async function addUser() {
  const username = document.getElementById('newUser').value.trim();
  const password = document.getElementById('newPass').value.trim();
  const role     = document.getElementById('newRole').value;
  if (!username || !password) { showMsg('userMsg','Completa usuario y contraseña.','err'); return; }
  const r = await fetch('/admin/usuarios', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username, password, role})
  });
  const d = await r.json();
  if (d.error) { showMsg('userMsg', d.error, 'err'); }
  else {
    showMsg('userMsg', `✅ Usuario "${username}" creado.`, 'ok');
    document.getElementById('newUser').value='';
    document.getElementById('newPass').value='';
    loadUsers();
  }
}

async function deleteUser(username) {
  if (!confirm(`¿Eliminar usuario "${username}"?`)) return;
  const r = await fetch(`/admin/usuarios/${encodeURIComponent(username)}`, {method:'DELETE'});
  const d = await r.json();
  if (d.error) { showMsg('userMsg', d.error, 'err'); }
  else { showMsg('userMsg', `Usuario "${username}" eliminado.`, 'ok'); loadUsers(); }
}

function showMsg(id, text, type) {
  const el = document.getElementById(id);
  el.textContent = text; el.className = 'msg '+type; el.style.display='block';
}
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

loadUsers();
</script>
</body>
</html>"""

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>📦 Stock Norglas</title>
<style>
  :root { --blue:#1565C0;--blue-light:#E3F2FD;--blue-mid:#1976D2;
          --orange:#E65100;--green:#2E7D32;--red:#C62828;
          --gray:#9E9E9E;--bg:#F0F4F8;--white:#fff;
          --shadow:0 1px 4px rgba(0,0,0,0.15); }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:var(--bg); display:flex; flex-direction:column; height:100dvh; }
  #header { background:linear-gradient(135deg,#0D47A1,#1976D2,#42A5F5);
            color:white; padding:12px 16px; flex-shrink:0;
            box-shadow:0 2px 6px rgba(0,0,0,0.3);
            display:flex; align-items:center; justify-content:space-between; }
  #header-left h1 { font-size:1.1rem; font-weight:700; }
  #header-left .sub { font-size:0.72rem; opacity:.85; margin-top:2px; }
  #header-right { display:flex; gap:8px; }
  .hbtn { color:rgba(255,255,255,.85); text-decoration:none; font-size:0.78rem;
           background:rgba(255,255,255,.15); padding:6px 11px; border-radius:20px;
           border:none; cursor:pointer; }
  .hbtn:hover { background:rgba(255,255,255,.25); }
  #chat { flex:1; overflow-y:auto; padding:12px 10px;
          display:flex; flex-direction:column; gap:10px; }
  .msg-wrap { display:flex; align-items:flex-end; gap:6px; }
  .msg-wrap.user { flex-direction:row-reverse; }
  .bubble { max-width:88%; padding:10px 13px; border-radius:16px;
            font-size:.88rem; line-height:1.5; word-break:break-word;
            box-shadow:var(--shadow); }
  .bubble.bot  { background:var(--white); border-bottom-left-radius:4px; }
  .bubble.user { background:var(--blue); color:white; border-bottom-right-radius:4px; }
  .avatar { width:28px; height:28px; border-radius:50%; display:flex;
            align-items:center; justify-content:center; font-size:15px;
            background:#E3F2FD; flex-shrink:0; }
  .chip-wrap { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
  .chip { background:var(--blue-light); color:var(--blue); border:1px solid #90CAF9;
          border-radius:20px; padding:5px 11px; font-size:.78rem; cursor:pointer; }
  .chip:hover { background:#BBDEFB; }
  .chip.ana { background:#FFF8E1; color:#E65100; border-color:#FFCC80; }
  .chip.ana:hover { background:#FFE0B2; }
  .spinner { display:inline-block; width:16px; height:16px; border:2px solid #ccc;
    border-top-color:var(--blue); border-radius:50%;
    animation:spin .7s linear infinite; vertical-align:middle; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .badge-ai { background:#E65100; color:white; font-size:.7rem; font-weight:700;
              padding:2px 7px; border-radius:10px; }
  .product-name { font-weight:700; font-size:.9rem; color:#1A237E; }
  .product-cat  { font-size:.75rem; color:var(--gray); margin:1px 0 6px; }
  .table-wrap { overflow-x:auto; }
  .stk { width:100%; border-collapse:collapse; font-size:13px; min-width:340px; }
  .stk th { background:#E8EAF6; color:#3949AB; padding:5px 8px; text-align:right;
             font-weight:600; white-space:nowrap; border-bottom:2px solid #C5CAE9; }
  .stk td { padding:5px 8px; text-align:right; border-bottom:1px solid #EEE; }
  .cf { background:var(--blue-light); font-weight:700; }
  .cc { color:var(--orange); font-weight:600; }
  .dp { color:var(--green);  font-weight:700; }
  .dz { color:var(--gray); }
  .dn { color:var(--red);    font-weight:700; }
  .divider { border:none; border-top:1px solid #E0E0E0; margin:10px 0; }
  #inputbar { display:flex; align-items:center; gap:8px; padding:10px 12px;
              background:white; border-top:1px solid #DDD; flex-shrink:0;
              box-shadow:0 -2px 6px rgba(0,0,0,0.07); }
  #inputbar label { font-size:1.3rem; cursor:pointer; color:var(--blue);
                    flex-shrink:0; padding:4px; border-radius:50%; }
  #fileInput { display:none; }
  #qi { flex:1; border:1.5px solid #CCC; border-radius:22px; padding:9px 15px;
        font-size:.9rem; outline:none; transition:border-color .2s; }
  #qi:focus { border-color:var(--blue); }
  #sb { background:var(--blue); color:white; border:none; border-radius:50%;
        width:40px; height:40px; font-size:1.1rem; cursor:pointer;
        display:flex; align-items:center; justify-content:center; flex-shrink:0; }
  #sb:hover { background:var(--blue-mid); }
  @media (min-width:640px) {
    #chat, #inputbar { max-width:720px; margin:0 auto; width:100%; }
  }
</style>
</head>
<body>
<div id="header">
  <div id="header-left">
    <h1>📦 Stock Norglas</h1>
    <div class="sub" id="sub">Cargando...</div>
  </div>
  <div id="header-right">
    {% if is_admin %}<a href="/admin" class="hbtn">⚙️ Admin</a>{% endif %}
    <a href="/logout" class="hbtn">Salir</a>
  </div>
</div>
<div id="chat"></div>
<div id="inputbar">
  {% if is_admin %}
  <label for="fileInput" title="Subir PDF (solo admin)">📎</label>
  <input type="file" id="fileInput" accept=".pdf">
  {% endif %}
  <input type="text" id="qi" placeholder="Buscar producto o hacer pregunta..." autocomplete="off">
  <button id="sb">➤</button>
</div>
<script>
const chat=document.getElementById('chat'), qi=document.getElementById('qi'), sub=document.getElementById('sub');
let sInfo={n:0,fecha:null};
const IS_ADMIN = {{ 'true' if is_admin else 'false' }};

fetch('/status').then(r=>r.json()).then(d=>{ sInfo=d; updateSub(); showWelcome(); });

function updateSub() {
  sub.textContent = sInfo.fecha
    ? `Stock al ${sInfo.fecha} · ${sInfo.n.toLocaleString('es-CL')} productos`
    : 'Sin stock · Admin debe subir el PDF';
}

function addMsg(html, type) {
  const wrap=document.createElement('div'); wrap.className='msg-wrap '+type;
  const av=document.createElement('div'); av.className='avatar';
  av.textContent=type==='user'?'👤':'🤖';
  const bub=document.createElement('div'); bub.className='bubble '+type;
  bub.innerHTML=html;
  if(type==='user'){wrap.appendChild(bub);wrap.appendChild(av);}
  else{wrap.appendChild(av);wrap.appendChild(bub);}
  chat.appendChild(wrap); chat.scrollTop=chat.scrollHeight; return bub;
}

function showWelcome() {
  const sc=['tina logic 170','cup 54x54','shower 100x185','varilla 3mm']
    .map(c=>`<span class="chip" onclick="sq('${c}')">${c}</span>`).join('');
  const ac=['Resumen general','Productos con stock 0','Top 10 mayor disponible',
    'Top 10 mayor comprometido','Stock negativo','Stock por bodega',
    'Top 10 categorías','Comprometido mayor que físico']
    .map(c=>`<span class="chip ana" onclick="sq('${esc(c)}')">${c}</span>`).join('');
  addMsg(`¡Hola! Soy el asistente de stock Norglas 👋<br>
Puedo <b>buscar productos</b> o <b>analizar el inventario</b>.<br><br>
<b>🔍 Búsqueda:</b><div class="chip-wrap">${sc}</div><br>
<b>📊 Análisis:</b><div class="chip-wrap">${ac}</div>`,'bot');
}

function sq(text) {
  if(!text.trim()) return;
  addMsg(esc(text),'user'); qi.value='';
  const sp=addMsg('<span class="spinner"></span> Procesando...','bot');
  fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({query:text})})
  .then(r=>r.json()).then(d=>{
    if(d.no_stock){ sp.innerHTML='⚠️ Sin stock cargado. El administrador debe subir el PDF.'; return; }
    if(d.type==='search'){
      sp.innerHTML=d.results&&d.results.length
        ? buildResults(d.results,text)
        : `😕 No encontré "<b>${esc(text)}</b>". Prueba otros términos.`;
    } else if(d.type==='analytic'){
      sp.innerHTML=`<span class="badge-ai">📊 Análisis</span>&nbsp;${d.answer}`;
    } else {
      sp.innerHTML=`❌ ${esc(d.error||'Error')}`;
    }
    chat.scrollTop=chat.scrollHeight;
  }).catch(()=>{ sp.innerHTML='❌ Error. Intenta de nuevo.'; });
}

function buildResults(results,query) {
  let html=`<b>${results.length} resultado${results.length>1?'s':''}</b> para "<i>${esc(query)}</i>":<br><br>`;
  results.forEach((p,i)=>{
    if(i>0) html+='<hr class="divider">';
    const d=p.disponible, dc=d>0?'dp':d<0?'dn':'dz';
    html+=`<div>
      <div class="product-name">${esc(p.producto||'')}</div>
      ${p.subgrupo?`<div class="product-cat">${esc(p.subgrupo)}</div>`:''}
      <div class="table-wrap"><table class="stk">
        <tr><th>Sta Elena</th><th>Tabancura</th><th>Pana</th>
            <th>Físico</th><th>Comprometido</th><th>Disponible</th></tr>
        <tr><td>${f(p.sta_elena)}</td><td>${f(p.tabancura)}</td><td>${f(p.pana)}</td>
            <td class="cf">${f(p.total)}</td>
            <td class="cc">${f(p.por_entregar)}</td>
            <td class="${dc}">${f(d)}</td></tr>
      </table></div></div>`;
  });
  return html;
}

function f(n){const x=parseFloat(n);return isNaN(x)?'0':x%1===0?x.toFixed(0):x.toFixed(2);}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

document.getElementById('sb').addEventListener('click',()=>sq(qi.value.trim()));
qi.addEventListener('keydown',e=>{if(e.key==='Enter')sq(qi.value.trim());});

{% if is_admin %}
const fi=document.getElementById('fileInput');
fi.addEventListener('change',async()=>{
  const file=fi.files[0]; if(!file)return; fi.value='';
  addMsg(`📎 Subiendo <b>${esc(file.name)}</b>...`,'user');
  const sp=addMsg('<span class="spinner"></span> Procesando PDF...','bot');
  const fd=new FormData(); fd.append('file',file);
  try {
    const r=await fetch('/upload',{method:'POST',body:fd});
    const d=await r.json();
    if(d.error){ sp.innerHTML=`❌ ${esc(d.error)}`; }
    else { sInfo={n:d.n,fecha:d.fecha}; updateSub();
           sp.innerHTML=`✅ <b>Stock actualizado:</b> ${d.n.toLocaleString('es-CL')} productos al ${esc(d.fecha)}`; }
  } catch(e){ sp.innerHTML='❌ Error al procesar el PDF.'; }
});
{% endif %}
</script>
</body>
</html>"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'GET':
        return render_template_string(LOGIN_HTML, error=None)
    username = request.form.get('username','').strip()
    password = request.form.get('password','').strip()
    user = get_user(username)
    if not user or user['password_hash'] != hash_pw(password):
        return render_template_string(LOGIN_HTML, error='Usuario o contraseña incorrectos')
    session['user'] = username
    session['role'] = user['role']
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
@login_required
def index():
    return render_template_string(CHAT_HTML, is_admin=(session.get('role')=='admin'))

@app.route('/admin')
@admin_required
def admin():
    return render_template_string(ADMIN_HTML)

@app.route('/admin/usuarios', methods=['GET'])
@admin_required
def admin_get_users():
    try:
        users = sb_get('usuarios', 'select=username,role&order=created_at.asc')
        return jsonify(users)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/usuarios', methods=['POST'])
@admin_required
def admin_create_user():
    data = request.get_json(force=True)
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    role     = data.get('role', 'vendedor')
    if not username or not password:
        return jsonify({'error': 'Faltan datos'}), 400
    if role not in ('admin', 'vendedor'):
        return jsonify({'error': 'Rol inválido'}), 400
    try:
        existing = sb_get('usuarios', f'username=eq.{username}&select=username')
        if existing:
            return jsonify({'error': f'El usuario "{username}" ya existe'}), 400
        sb_post('usuarios', {
            'username': username,
            'password_hash': hash_pw(password),
            'role': role
        })
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/usuarios/<username>', methods=['DELETE'])
@admin_required
def admin_delete_user(username):
    if username == session.get('user'):
        return jsonify({'error': 'No puedes eliminarte a ti mismo'}), 400
    try:
        sb_delete('usuarios', f'username=eq.{username}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/status')
@login_required
def status():
    _, fecha = get_stock()
    stock, _ = get_stock()
    return jsonify({'n': len(stock), 'fecha': fecha})

@app.route('/chat', methods=['POST'])
@login_required
def chat_route():
    body = request.get_json(force=True)
    query = body.get('query','').strip()
    if not query:
        return jsonify({'results': []})
    stock, fecha = get_stock()
    if not stock:
        return jsonify({'no_stock': True})
    answer = analytic_response(query, stock)
    if answer is not None:
        return jsonify({'type': 'analytic', 'answer': answer})
    results = search_stock(query, stock)
    return jsonify({'type': 'search', 'results': results or []})

@app.route('/upload', methods=['POST'])
@admin_required
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No se recibió archivo'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'El archivo debe ser PDF'}), 400
    try:
        products = parse_pdf(f.read())
        fecha = datetime.now().strftime('%d/%m/%Y')
        # Limpiar tabla y reinsertar en lotes de 500
        sb_delete('productos', 'id=gte.0')
        for i in range(0, len(products), 500):
            sb_post('productos', products[i:i+500])
        # Guardar metadata
        sb_delete('stock_meta', 'id=gte.0')
        sb_post('stock_meta', [{'fecha': fecha, 'n_productos': len(products)}])
        invalidate_cache()
        return jsonify({'n': len(products), 'fecha': fecha})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
