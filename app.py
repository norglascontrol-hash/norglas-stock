import os
import re
import io
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ── In-memory stock storage ──
stock_data = []
stock_fecha = None

# ── Column ranges (cx = (x0+x1)/2) - calibrated from actual PDF ──
COL_RANGES = {
    'SubGrupo':     (0,   225),
    'Producto':     (225, 340),
    'StaElena':     (340, 380),
    'Tabancura':    (380, 420),
    'Pana':         (420, 460),
    'Total':        (460, 500),
    'PorEntregar':  (500, 545),
    'Disponible':   (545, float('inf')),
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
                # Page offset ensures rows from different pages don't collide
                row_key = page_num * 10000 + round(w['top'] / 8) * 8
                col = col_for(cx)
                if col is None:
                    continue
                key = (row_key, col)
                if key in rows_by_line:
                    rows_by_line[key] += ' ' + w['text']
                else:
                    rows_by_line[key] = w['text']

    lines = defaultdict(dict)
    for (row_key, col), val in rows_by_line.items():
        lines[row_key][col] = val

    products = []
    for row_key in sorted(lines.keys()):
        row = lines[row_key]
        subgrupo = row.get('SubGrupo', '').strip()
        producto = row.get('Producto', '').strip()

        # Skip header rows: contain "Producto" and "Disponible"
        if 'producto' in producto.lower() and 'disponible' in row.get('Disponible', '').lower():
            continue
        # Skip title rows
        if producto in ('Stock Norglas Softland', 'Stock Norglas', 'Softland', 'Norglas'):
            continue
        if not producto:
            continue

        def to_num(s):
            if not s:
                return 0
            try:
                return float(s.replace(',', '.'))
            except:
                return 0

        products.append({
            'SubGrupo':    subgrupo,
            'Producto':    producto,
            'StaElena':    to_num(row.get('StaElena', '')),
            'Tabancura':   to_num(row.get('Tabancura', '')),
            'Pana':        to_num(row.get('Pana', '')),
            'Total':       to_num(row.get('Total', '')),
            'PorEntregar': to_num(row.get('PorEntregar', '')),
            'Disponible':  to_num(row.get('Disponible', '')),
        })

    return products

def normalize(text):
    text = text.lower()
    text = re.sub(r'[./()\-_]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def search_stock(query):
    if not stock_data:
        return None  # signal: no stock loaded
    tokens = normalize(query).split()
    if not tokens:
        return []

    results = []
    for item in stock_data:
        haystack = normalize(item['SubGrupo'] + ' ' + item['Producto'])
        matched = sum(1 for t in tokens if t in haystack)
        if matched == len(tokens):
            results.append((matched, item))

    # Fallback: at least half tokens
    if not results:
        half = max(1, len(tokens) // 2)
        for item in stock_data:
            haystack = normalize(item['SubGrupo'] + ' ' + item['Producto'])
            matched = sum(1 for t in tokens if t in haystack)
            if matched >= half:
                results.append((matched, item))

    results.sort(key=lambda x: -x[0])
    return [item for _, item in results[:15]]

# ── HTML Template ──
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>📦 Stock Norglas</title>
<style>
  :root {
    --blue: #1565C0;
    --blue-light: #E3F2FD;
    --blue-mid: #1976D2;
    --orange: #E65100;
    --green: #2E7D32;
    --red: #C62828;
    --gray: #9E9E9E;
    --bg: #F0F4F8;
    --white: #FFFFFF;
    --shadow: 0 1px 4px rgba(0,0,0,0.15);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); display: flex; flex-direction: column; height: 100dvh; }

  /* HEADER */
  #header {
    background: linear-gradient(135deg, #0D47A1, #1976D2, #42A5F5);
    color: white; padding: 12px 16px; flex-shrink: 0;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
  }
  #header h1 { font-size: 1.15rem; font-weight: 700; letter-spacing: 0.3px; }
  #header .subtitle { font-size: 0.75rem; opacity: 0.85; margin-top: 2px; }

  /* CHAT AREA */
  #chat {
    flex: 1; overflow-y: auto; padding: 12px 10px;
    display: flex; flex-direction: column; gap: 10px;
  }

  /* MESSAGES */
  .msg-wrap { display: flex; align-items: flex-end; gap: 6px; }
  .msg-wrap.user { flex-direction: row-reverse; }
  .bubble {
    max-width: 85%; padding: 10px 13px; border-radius: 16px;
    font-size: 0.88rem; line-height: 1.45; word-break: break-word;
    box-shadow: var(--shadow);
  }
  .bubble.bot { background: var(--white); border-bottom-left-radius: 4px; }
  .bubble.user { background: var(--blue); color: white; border-bottom-right-radius: 4px; }
  .avatar { width: 28px; height: 28px; border-radius: 50%; display: flex;
             align-items: center; justify-content: center; font-size: 15px;
             background: #E3F2FD; flex-shrink: 0; }

  /* CHIPS */
  #chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 2px 0 4px; }
  .chip {
    background: var(--blue-light); color: var(--blue);
    border: 1px solid #90CAF9; border-radius: 20px;
    padding: 5px 11px; font-size: 0.78rem; cursor: pointer;
    transition: background 0.15s;
  }
  .chip:hover { background: #BBDEFB; }

  /* SPINNER */
  .spinner { display: inline-block; width: 18px; height: 18px;
    border: 2px solid #ccc; border-top-color: var(--blue);
    border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* PRODUCT CARDS */
  .product-card { margin: 4px 0; }
  .product-name { font-weight: 700; font-size: 0.9rem; color: #1A237E; }
  .product-cat { font-size: 0.75rem; color: var(--gray); margin: 1px 0 6px; }
  .table-wrap { overflow-x: auto; }
  .stock-table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 340px; }
  .stock-table th { background: #E8EAF6; color: #3949AB;
                    padding: 5px 8px; text-align: right; font-weight: 600;
                    white-space: nowrap; border-bottom: 2px solid #C5CAE9; }
  .stock-table td { padding: 5px 8px; text-align: right; border-bottom: 1px solid #EEE; }
  .col-fisico { background: var(--blue-light); font-weight: 700; }
  .col-comprometido { color: var(--orange); font-weight: 600; }
  .col-disp-pos { color: var(--green); font-weight: 700; }
  .col-disp-zero { color: var(--gray); }
  .col-disp-neg { color: var(--red); font-weight: 700; }
  .divider { border: none; border-top: 1px solid #E0E0E0; margin: 10px 0; }

  /* INPUT BAR */
  #inputbar {
    display: flex; align-items: center; gap: 8px;
    padding: 10px 12px; background: white;
    border-top: 1px solid #DDD; flex-shrink: 0;
    box-shadow: 0 -2px 6px rgba(0,0,0,0.07);
  }
  #inputbar label {
    font-size: 1.3rem; cursor: pointer; color: var(--blue); flex-shrink: 0;
    padding: 4px; border-radius: 50%; transition: background 0.15s;
  }
  #inputbar label:hover { background: var(--blue-light); }
  #fileInput { display: none; }
  #queryInput {
    flex: 1; border: 1px solid #CCC; border-radius: 22px;
    padding: 9px 15px; font-size: 0.9rem; outline: none;
    transition: border-color 0.2s;
  }
  #queryInput:focus { border-color: var(--blue); }
  #sendBtn {
    background: var(--blue); color: white; border: none; border-radius: 50%;
    width: 40px; height: 40px; font-size: 1.1rem; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.15s; flex-shrink: 0;
  }
  #sendBtn:hover { background: var(--blue-mid); }
  @media (min-width: 640px) {
    #chat { max-width: 720px; margin: 0 auto; width: 100%; }
    #inputbar { max-width: 720px; margin: 0 auto; width: 100%; }
    #header { text-align: center; }
  }
</style>
</head>
<body>

<div id="header">
  <h1>📦 Stock Norglas</h1>
  <div class="subtitle" id="subtitle">Sin stock cargado · Sube un PDF para comenzar</div>
</div>

<div id="chat"></div>

<div id="inputbar">
  <label for="fileInput" title="Subir PDF de stock">📎</label>
  <input type="file" id="fileInput" accept=".pdf">
  <input type="text" id="queryInput" placeholder="Buscar producto..." autocomplete="off">
  <button id="sendBtn">➤</button>
</div>

<script>
const chat = document.getElementById('chat');
const queryInput = document.getElementById('queryInput');
const fileInput = document.getElementById('fileInput');
const subtitle = document.getElementById('subtitle');

let stockInfo = { n: 0, fecha: null };

fetch('/status').then(r=>r.json()).then(d=>{
  stockInfo = d;
  updateSubtitle();
  showWelcome();
});

function updateSubtitle() {
  if (stockInfo.fecha) {
    subtitle.textContent = `Stock al ${stockInfo.fecha} · ${stockInfo.n.toLocaleString()} productos`;
  } else {
    subtitle.textContent = 'Sin stock cargado · Sube un PDF para comenzar';
  }
}

function addMsg(html, type) {
  const wrap = document.createElement('div');
  wrap.className = 'msg-wrap ' + type;
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = type === 'user' ? '👤' : '🤖';
  const bub = document.createElement('div');
  bub.className = 'bubble ' + type;
  bub.innerHTML = html;
  if (type === 'user') {
    wrap.appendChild(bub);
    wrap.appendChild(av);
  } else {
    wrap.appendChild(av);
    wrap.appendChild(bub);
  }
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  return bub;
}

function showWelcome() {
  const chips = ['tina logic 170','cup 54x54','shower 100x185','varilla 3mm'];
  const chipsHtml = chips.map(c =>
    `<span class="chip" onclick="sendQuery('${c}')">${c}</span>`
  ).join('');
  addMsg(`¡Hola! Soy el asistente de stock Norglas 👋<br>
Escribe el nombre de un producto para buscar su disponibilidad.<br>
Para actualizar el stock, toca el botón 📎 y sube el PDF de Softland.<br><br>
<div id="chips">${chipsHtml}</div>`, 'bot');
}

function sendQuery(text) {
  if (!text.trim()) return;
  addMsg(escHtml(text), 'user');
  queryInput.value = '';
  const spinner = addMsg('<span class="spinner"></span> Buscando...', 'bot');

  fetch('/buscar', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: text })
  }).then(r => r.json()).then(data => {
    if (data.no_stock) {
      spinner.innerHTML = '⚠️ No hay stock cargado aún.<br>Toca el botón 📎 para subir el PDF de Softland.';
      return;
    }
    if (!data.results || data.results.length === 0) {
      spinner.innerHTML = `😕 No encontré productos para <b>"${escHtml(text)}"</b>.<br>Prueba con otros términos.`;
      return;
    }
    spinner.innerHTML = buildResultsHTML(data.results, text);
    chat.scrollTop = chat.scrollHeight;
  }).catch(() => {
    spinner.innerHTML = '❌ Error al buscar. Intenta de nuevo.';
  });
}

function buildResultsHTML(results, query) {
  let html = `<b>${results.length} resultado${results.length>1?'s':''}</b> para "<i>${escHtml(query)}</i>":<br><br>`;
  results.forEach((p, i) => {
    if (i > 0) html += '<hr class="divider">';
    const disp = p.Disponible;
    let dispClass = disp > 0 ? 'col-disp-pos' : (disp < 0 ? 'col-disp-neg' : 'col-disp-zero');
    html += `<div class="product-card">
      <div class="product-name">${escHtml(p.Producto)}</div>
      ${p.SubGrupo ? `<div class="product-cat">${escHtml(p.SubGrupo)}</div>` : ''}
      <div class="table-wrap">
      <table class="stock-table">
        <tr>
          <th>Sta Elena</th><th>Tabancura</th><th>Pana</th>
          <th>Físico</th><th>Comprometido</th><th>Disponible</th>
        </tr>
        <tr>
          <td>${fmt(p.StaElena)}</td>
          <td>${fmt(p.Tabancura)}</td>
          <td>${fmt(p.Pana)}</td>
          <td class="col-fisico">${fmt(p.Total)}</td>
          <td class="col-comprometido">${fmt(p.PorEntregar)}</td>
          <td class="${dispClass}">${fmt(disp)}</td>
        </tr>
      </table>
      </div>
    </div>`;
  });
  return html;
}

function fmt(n) {
  const num = parseFloat(n);
  if (isNaN(num)) return '0';
  return num % 1 === 0 ? num.toFixed(0) : num.toFixed(2);
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

document.getElementById('sendBtn').addEventListener('click', () => {
  sendQuery(queryInput.value.trim());
});
queryInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') sendQuery(queryInput.value.trim());
});

fileInput.addEventListener('change', async () => {
  const file = fileInput.files[0];
  if (!file) return;
  fileInput.value = '';
  addMsg(`📎 Subiendo <b>${escHtml(file.name)}</b>...`, 'user');
  const spinner = addMsg('<span class="spinner"></span> Procesando PDF de stock...', 'bot');

  const formData = new FormData();
  formData.append('file', file);

  try {
    const r = await fetch('/upload', { method: 'POST', body: formData });
    const data = await r.json();
    if (data.error) {
      spinner.innerHTML = `❌ Error: ${escHtml(data.error)}`;
    } else {
      stockInfo = { n: data.n, fecha: data.fecha };
      updateSubtitle();
      spinner.innerHTML = `✅ <b>Stock actualizado:</b> ${data.n.toLocaleString()} productos al ${escHtml(data.fecha)}`;
    }
  } catch(e) {
    spinner.innerHTML = '❌ Error al procesar el PDF.';
  }
});
</script>
</body>
</html>"""

# ── Routes ──

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/status')
def status():
    return jsonify({'n': len(stock_data), 'fecha': stock_fecha})

@app.route('/buscar', methods=['POST'])
def buscar():
    body = request.get_json(force=True)
    query = body.get('query', '').strip()
    if not query:
        return jsonify({'results': []})
    results = search_stock(query)
    if results is None:
        return jsonify({'no_stock': True})
    return jsonify({'results': results})

@app.route('/upload', methods=['POST'])
def upload():
    global stock_data, stock_fecha
    if 'file' not in request.files:
        return jsonify({'error': 'No se recibió archivo'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'El archivo debe ser PDF'}), 400
    try:
        file_bytes = f.read()
        products = parse_pdf(file_bytes)
        stock_data = products
        stock_fecha = datetime.now().strftime('%d/%m/%Y')
        return jsonify({'n': len(products), 'fecha': stock_fecha})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
