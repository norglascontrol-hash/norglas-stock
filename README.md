# 📦 Stock Norglas

App web de consulta de stock vía chat, estilo WhatsApp. Sube un PDF de Softland y busca productos desde cualquier celular o computador.

---

## 🚀 Deploy en Railway (5 minutos)

### Prerequisitos
- Cuenta en [railway.app](https://railway.app) (gratis)
- [Node.js](https://nodejs.org) instalado (para el CLI de Railway)
- Git instalado

### Pasos

```bash
# 1. Instala el CLI de Railway
npm install -g @railway/cli

# 2. Entra a la carpeta del proyecto
cd norglas-stock

# 3. Inicia git (si no está iniciado)
git init
git add .
git commit -m "initial commit"

# 4. Login a Railway (abre el navegador)
railway login

# 5. Crea el proyecto y despliega
railway init
railway up
```

### 6. Obtener URL pública

```bash
railway domain
```

Railway te entrega una URL del tipo `https://norglas-stock-production.up.railway.app`

> **Esa URL se puede compartir directo por WhatsApp o abrir en cualquier celular 📱**

---

## 🔄 Actualizar el stock

Cuando tengas un PDF nuevo de Softland:

1. Abre la app en el navegador
2. Toca el botón 📎
3. Selecciona el PDF → se parsea automáticamente en memoria

---

## 🛠 Correr localmente

```bash
pip install -r requirements.txt
python app.py
# Abre http://localhost:5000
```

---

## 🌐 Deploy en Render (alternativa gratuita)

1. Sube el código a GitHub
2. Ve a [render.com](https://render.com) → New Web Service
3. Conecta el repo
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app`
6. ✅ Deploy → obtienes URL pública gratuita

---

## 📋 Estructura

```
norglas-stock/
├── app.py           # Flask app + parser PDF + rutas
├── requirements.txt # Dependencias Python
├── Procfile         # Para Railway/Render
├── .gitignore
└── README.md
```

---

## 💡 Cómo buscar

- Escribe texto libre: `tina logic 170`, `cup 54x54`, `shower 100`
- Busca por categoría: `plancha blanca 3mm`, `varilla cuadrada`
- Si no hay resultados exactos, la app intenta con la mitad de los tokens automáticamente

---

## ⚠️ Notas

- El stock se guarda **en memoria** (se pierde si el servidor se reinicia)
- Para persistencia permanente, conectar una base de datos (PostgreSQL en Railway es gratuito)
- El parser funciona con PDFs de Softland con el formato estándar de columnas de Norglas
