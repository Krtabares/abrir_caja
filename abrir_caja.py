"""
Servidor FastAPI para abrir cajón de efectivo
Interfaz gráfica para configuración y servidor HTTP con Cloudflare Tunnel
Versión: 1.0.0
"""

# Versión de la aplicación
VERSION = "1.0.0"

import os
import json
import sys
import subprocess
import threading
try:
    import win32print
    HAVE_WIN32PRINT = True
except ImportError:
    win32print = None
    HAVE_WIN32PRINT = False
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import logging
import socket
import urllib.request
import urllib.error
import time
import asyncio
import re
import requests

# Interfaz gráfica
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QLabel, QLineEdit, QComboBox, 
                               QPushButton, QMessageBox, QSystemTrayIcon, QMenu)
from PySide6.QtCore import Qt, QThread, Signal, QMetaObject, Q_ARG
from PySide6.QtGui import QIcon, QAction

# Determinar el directorio base
if getattr(sys, 'frozen', False):
    DIRECTORIO_APLICACION = os.path.dirname(sys.executable)
else:
    DIRECTORIO_APLICACION = os.path.dirname(os.path.abspath(__file__))

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

RUTA_JSON = os.path.join(DIRECTORIO_APLICACION, "config.json")
print(f"INFO: El archivo de configuración (config.json) se manejará en: {RUTA_JSON}")

# Configuración del servidor (hardcodeada)
CONFIG_SERVIDOR = {
    "host": "0.0.0.0",
    "port": 8444  # Puerto diferente al proyecto comandar_server (8443)
}

# Servidores disponibles
SERVERS = {
    "Normal (ADM)":      "https://adm.premium-soft.com",
    "Colombia (COLADM)": "https://coladm.premium-soft.com/ADMCOL",
    "Colombia VIP":      "https://colvip.premium-soft.com/ADMCOLGRAlVIP",
}
DEFAULT_SERVER = "Normal (ADM)"

# Instancia global del servidor
servidor_thread = None
cloudflared_process = None
cloudflared_url = None


# ==================== FUNCIONES DE IMPRESORA ====================

def listar_impresoras():
    """Lista todas las impresoras disponibles"""
    if not HAVE_WIN32PRINT:
        logger.info("[Linux/Mock] win32print no disponible. Listando impresoras simuladas.")
        return ["Impresora Simulada (Linux Mock)", "XPRINTER 80C USB"]
    try:
        impresoras = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        return [impresora[2] for impresora in impresoras]
    except Exception as e:
        logger.error(f"Error al listar impresoras: {e}")
        return []


def obtener_impresora_predeterminada():
    """Obtiene la impresora predeterminada"""
    if not HAVE_WIN32PRINT:
        return "Impresora Simulada (Linux Mock)"
    try:
        return win32print.GetDefaultPrinter()
    except Exception as e:
        print(f"No se pudo obtener la impresora predeterminada: {e}")
        return None


def abrir_cajon(printer_name):
    """Abre el cajón de la impresora"""
    if not printer_name:
        print("Advertencia: No se seleccionó ninguna impresora.")
        return False

    if not HAVE_WIN32PRINT:
        print(f"ℹ️ [SIMULACIÓN LINUX] Comando ESC/POS (abrir cajón) enviado exitosamente a '{printer_name}'.")
        logger.info(f"[SIMULACIÓN] Comando abrir cajón enviado a '{printer_name}'")
        return True

    try:
        hPrinter = win32print.OpenPrinter(printer_name)
        hJob = win32print.StartDocPrinter(
            hPrinter, 1, ("Abrir Cajón", None, "RAW"))
        win32print.StartPagePrinter(hPrinter)
        comando = bytes([27, 112, 0, 64, 65])  # Comando ESC/POS
        win32print.WritePrinter(hPrinter, comando)
        win32print.EndPagePrinter(hPrinter)
        win32print.EndDocPrinter(hPrinter)
        win32print.ClosePrinter(hPrinter)
        print(f"Comando enviado para abrir el cajón en '{printer_name}'.")
        return True
    except Exception as e:
        print(f"Error al abrir el cajón: {e}")
        return False


# ==================== FUNCIONES DE CONFIGURACIÓN ====================

def guardar_configuracion(usuario, pin, impresora, servidor=None):
    """Guarda la configuración en el archivo JSON"""
    configuracion = {
        "usuario": usuario,
        "pin": pin,
        "impresora": impresora,
        "servidor": servidor if servidor else DEFAULT_SERVER,
    }
    try:
        with open(RUTA_JSON, "w", encoding='utf-8') as archivo:
            json.dump(configuracion, archivo, indent=4, ensure_ascii=False)
        print(f"INFO: Configuración guardada en {RUTA_JSON}")
        return True
    except PermissionError:
        error_msg = (f"Error de permisos: No se pudo guardar la configuración en {RUTA_JSON}. "
                     "Asegúrate de que el programa tenga permisos de escritura en esa carpeta.")
        print(error_msg)
        return False
    except Exception as e:
        error_msg = f"Error al guardar la configuración en {RUTA_JSON}: {e}"
        print(error_msg)
        return False


def cargar_configuracion():
    """Carga la configuración desde el archivo JSON"""
    if not os.path.exists(RUTA_JSON):
        return None
    try:
        with open(RUTA_JSON, "r", encoding='utf-8') as archivo:
            configuracion = json.load(archivo)
            return configuracion
    except Exception as e:
        print(f"Error al cargar la configuración desde {RUTA_JSON}: {e}")
        return None


# ==================== FUNCIONES DE CLOUDFLARE TUNNEL ====================


# ==================== SERVIDOR FASTAPI ====================

app = FastAPI(title="Servidor Abrir Cajón", description="API para abrir cajón de efectivo")

# Middleware para logging de todas las peticiones
@app.middleware("http")
async def log_requests(request, call_next):
    """Registra todas las peticiones HTTP para diagnóstico"""
    start_time = time.time()
    
    # Información del cliente
    client_host = request.client.host if request.client else 'desconocido'
    client_port = request.client.port if request.client else 'desconocido'
    
    # Información de la petición
    metodo = request.method
    path = request.url.path
    query_params = str(request.query_params) if request.query_params else "ninguno"
    headers_info = {k: v for k, v in request.headers.items() if k.lower() in ['content-type', 'user-agent', 'host', 'accept']}
    
    # Log detallado de la petición entrante
    logger.info(f"=== PETICIÓN ENTRANTE ===")
    logger.info(f"Método: {metodo}")
    logger.info(f"Path: {path}")
    logger.info(f"Cliente: {client_host}:{client_port}")
    logger.info(f"Query params: {query_params}")
    logger.info(f"Headers relevantes: {headers_info}")
    print(f"INFO: Petición {metodo} {path} desde {client_host}:{client_port}")
    
    # Leer el cuerpo de la petición si existe (para POST)
    body_info = ""
    if metodo in ["POST", "PUT", "PATCH"]:
        try:
            body_bytes = await request.body()
            if body_bytes:
                body_str = body_bytes.decode('utf-8', errors='replace')
                body_info = f"Body (primeros 200 chars): {body_str[:200]}"
                logger.info(body_info)
        except Exception as e:
            logger.warning(f"No se pudo leer el body de la petición: {e}")
    
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        
        # Log detallado de la respuesta
        logger.info(f"=== RESPUESTA ===")
        logger.info(f"Método: {metodo}")
        logger.info(f"Path: {path}")
        logger.info(f"Código HTTP: {response.status_code}")
        logger.info(f"Tiempo de procesamiento: {process_time:.3f}s")
        logger.info(f"Headers de respuesta: {dict(response.headers)}")
        print(f"INFO: Respuesta {metodo} {path} - {response.status_code} ({process_time:.3f}s)")
        
        return response
    except Exception as e:
        process_time = time.time() - start_time
        logger.error(f"=== ERROR PROCESANDO PETICIÓN ===")
        logger.error(f"Método: {metodo}")
        logger.error(f"Path: {path}")
        logger.error(f"Cliente: {client_host}:{client_port}")
        logger.error(f"Error: {e}")
        logger.error(f"Tiempo antes del error: {process_time:.3f}s")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        print(f"ERROR: Error procesando {metodo} {path}: {e} ({process_time:.3f}s)")
        raise

# Configurar CORS para permitir peticiones desde la web (localhost y producción)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:8080",
        "http://localhost:5000",
        "http://127.0.0.1",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:5000",
        "https://adm.premium-soft.com",
        "https://www.premium-soft.com",
        "*"  # Permitir todos los orígenes para máxima compatibilidad
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# Modelos Pydantic
class AbrirCajonRequest(BaseModel):
    usuario: str
    pin: Optional[str] = None


@app.get("/")
async def root():
    """Endpoint raíz - información del servidor"""
    config = cargar_configuracion()
    global cloudflared_url, cloudflared_process
    
    tunel_activo = False
    if cloudflared_process:
        tunel_activo = cloudflared_process.poll() is None
    
    return {
        "servidor": "Abrir Cajón API",
        "version": VERSION,
        "status": "activo",
        "impresora_configurada": config.get("impresora") is not None if config else False,
        "usuario_configurado": config.get("usuario") is not None if config else False,
        "cloudflare_url": cloudflared_url if cloudflared_url else None,
        "tunel_activo": tunel_activo,
        "url_local": f"http://localhost:{CONFIG_SERVIDOR['port']}",
        "endpoints_disponibles": {
            "abrir_caja": "/abrir-caja (POST)",
            "status": "/status (GET)",
            "health": "/health (GET)",
            "url_tunel": "/url-tunel (GET)",
            "impresoras": "/impresoras (GET)"
        },
        "nota": "Este servidor está diseñado para ser accedido desde la web HTTPS a través de Cloudflare Tunnel o desde localhost"
    }


@app.get("/status")
async def status():
    """Obtiene el estado del servidor"""
    logger.info("Petición /status recibida")
    config = cargar_configuracion()
    global cloudflared_url
    
    return {
        "status": "activo",
        "version": VERSION,
        "mensaje": "Servidor funcionando correctamente",
        "impresora": config.get("impresora") if config else None,
        "usuario_configurado": config.get("usuario") is not None if config else False,
        "cloudflare_url": cloudflared_url if cloudflared_url else None,
        "nota": "Este servidor está diseñado para ser accedido desde la web HTTPS a través de Cloudflare Tunnel"
    }


@app.get("/test")
async def test_endpoint():
    """Endpoint de prueba simple para verificar conectividad"""
    logger.info("Petición /test recibida")
    global cloudflared_url
    
    return {
        "status": "ok",
        "mensaje": "Servidor respondiendo correctamente",
        "timestamp": time.time(),
        "version": VERSION,
        "cloudflare_url": cloudflared_url if cloudflared_url else None
    }


@app.post("/abrir-caja")
async def abrir_caja_endpoint(request: AbrirCajonRequest):
    """
    Abre la gaveta de efectivo desde la página web
    
    Requiere:
    - usuario: Usuario autorizado
    - pin: PIN opcional (si está configurado)
    
    Este endpoint puede ser llamado desde:
    - localhost (desarrollo): http://localhost:8444/abrir-caja
    - Producción HTTPS: https://[cloudflare-url]/abrir-caja
    """
    logger.info(f"=== ENDPOINT /abrir-caja INICIADO ===")
    logger.info(f"Usuario recibido: {request.usuario}")
    logger.info(f"PIN recibido: {'Sí (oculto)' if request.pin else 'No'}")
    print(f"INFO: Endpoint /abrir-caja - Usuario: {request.usuario}")
    
    # Cargar configuración
    logger.info("Cargando configuración...")
    config = cargar_configuracion()
    
    if not config:
        logger.error("No hay configuración disponible")
        print("ERROR: No hay configuración disponible")
        raise HTTPException(status_code=500, detail="No hay configuración disponible")
    
    logger.info(f"Configuración cargada: Usuario={config.get('usuario')}, Impresora={config.get('impresora')}, PIN={'Configurado' if config.get('pin') else 'No configurado'}")
    
    # Verificar usuario
    usuario_configurado = config.get("usuario")
    if not usuario_configurado:
        logger.error("Usuario no configurado en la aplicación")
        print("ERROR: Usuario no configurado")
        raise HTTPException(status_code=500, detail="Usuario no configurado")
    
    logger.info(f"Verificando usuario - Configurado: '{usuario_configurado}', Recibido: '{request.usuario}'")
    
    if request.usuario != usuario_configurado:
        logger.warning(f"Intento de acceso no autorizado - Usuario recibido: '{request.usuario}', Configurado: '{usuario_configurado}'")
        print(f"WARNING: Usuario no autorizado - Recibido: '{request.usuario}', Esperado: '{usuario_configurado}'")
        raise HTTPException(status_code=403, detail="Usuario no autorizado")
    
    logger.info("Usuario verificado correctamente")
    
    # Verificar PIN si está configurado
    pin_configurado = config.get("pin", "").strip() if config.get("pin") else ""
    if pin_configurado:
        logger.info("PIN configurado - Verificando...")
        pin_recibido = (request.pin or "").strip()
        # Comparar los PINs como strings, sin espacios
        if not pin_recibido or pin_recibido != pin_configurado:
            logger.warning(f"PIN incorrecto para usuario: {request.usuario}")
            logger.warning(f"PIN esperado: '{pin_configurado}' (longitud: {len(pin_configurado)})")
            logger.warning(f"PIN recibido: '{pin_recibido}' (longitud: {len(pin_recibido)})")
            print(f"WARNING: PIN incorrecto para usuario: {request.usuario}")
            raise HTTPException(status_code=401, detail="PIN incorrecto")
        logger.info("PIN verificado correctamente")
        print("INFO: PIN verificado correctamente")
    else:
        logger.info("No se requiere PIN")
        print("INFO: No se requiere PIN")
    
    # Verificar que hay una impresora configurada
    impresora = config.get("impresora")
    if not impresora:
        logger.error("No hay impresora configurada")
        print("ERROR: No hay impresora configurada")
        raise HTTPException(status_code=500, detail="No hay impresora configurada")
    
    logger.info(f"Impresora configurada: {impresora}")
    print(f"INFO: Intentando abrir cajón en impresora: {impresora}")
    
    # Abrir el cajón
    logger.info("Enviando comando para abrir cajón...")
    resultado = abrir_cajon(impresora)
    
    if resultado:
        logger.info(f"✓ Cajón abierto exitosamente por usuario: {request.usuario}")
        print(f"INFO: ✓ Cajón abierto exitosamente")
        logger.info("=== ENDPOINT /abrir-caja COMPLETADO EXITOSAMENTE ===")
        return {
            "status": "success",
            "mensaje": "Cajón abierto exitosamente",
            "usuario": request.usuario,
            "impresora": impresora,
            "timestamp": time.time()
        }
    else:
        logger.error(f"✗ Error al abrir el cajón en impresora: {impresora}")
        print(f"ERROR: ✗ Error al abrir el cajón en impresora: {impresora}")
        logger.error("=== ENDPOINT /abrir-caja FALLÓ ===")
        raise HTTPException(status_code=500, detail="Error al abrir el cajón")


@app.get("/impresoras")
async def listar_impresoras_endpoint():
    """Lista todas las impresoras disponibles"""
    impresoras = listar_impresoras()
    return {
        "impresoras": impresoras,
        "predeterminada": obtener_impresora_predeterminada()
    }


@app.get("/url-tunel")
async def obtener_url_tunel():
    """Obtiene la URL actual del túnel Cloudflare"""
    global cloudflared_url, cloudflared_process
    config = cargar_configuracion()
    
    tunel_activo = False
    if cloudflared_process:
        tunel_activo = cloudflared_process.poll() is None
    
    return {
        "url_tunel": cloudflared_url if cloudflared_url else None,
        "url_local": f"http://localhost:{CONFIG_SERVIDOR['port']}",
        "servidor_activo": True,
        "tunel_activo": tunel_activo,
        "usuario_configurado": config.get("usuario") if config else None,
        "impresora_configurada": config.get("impresora") if config else None,
        "version": VERSION
    }


@app.get("/health")
async def health_check():
    """Endpoint de salud para verificar que el servidor está funcionando"""
    config = cargar_configuracion()
    global cloudflared_url, cloudflared_process
    
    tunel_activo = False
    if cloudflared_process:
        tunel_activo = cloudflared_process.poll() is None
    
    return {
        "status": "ok",
        "version": VERSION,
        "servidor_activo": True,
        "configuracion_completa": config is not None and config.get("usuario") and config.get("impresora"),
        "tunel_activo": tunel_activo,
        "url_tunel": cloudflared_url if cloudflared_url else None,
        "timestamp": time.time()
    }


def verificar_puerto_disponible(host, port):
    """Verifica si un puerto está disponible"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)  # Aumentar timeout a 2 segundos
        result = sock.connect_ex((host, port))
        sock.close()
        # result == 0 significa que la conexión fue exitosa (puerto en uso)
        # result != 0 significa que no se pudo conectar (puerto disponible)
        return result != 0
    except Exception as e:
        logger.error(f"Error al verificar puerto {host}:{port}: {e}")
        return False


def verificar_servidor_activo():
    """Verifica si el servidor HTTP está realmente activo haciendo una petición"""
    url = f"http://127.0.0.1:{CONFIG_SERVIDOR['port']}/status"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                data = json.loads(response.read().decode())
                logger.info(f"Servidor HTTP verificado como activo: {data}")
                return True, "Servidor HTTP activo"
            else:
                return False, f"Servidor respondió con código {response.status}"
    except urllib.error.URLError as e:
        logger.warning(f"No se pudo conectar al servidor HTTP: {e}")
        return False, f"Error de conexión: {str(e)}"
    except Exception as e:
        logger.error(f"Error al verificar servidor HTTP: {e}")
        return False, f"Error: {str(e)}"


def _send_cloudflare_url_to_server(url: str):
    """Envía la URL de cloudflared al servidor web mediante API"""
    try:
        config = cargar_configuracion()
        if not config:
            logger.warning("No hay configuración disponible para enviar la URL de cloudflared")
            print("ADVERTENCIA: No hay configuración disponible, no se puede enviar la URL al servidor")
            return
        
        pin = config.get("pin", "")
        if not pin:
            logger.warning("No hay PIN configurado para enviar la URL de cloudflared")
            print("ADVERTENCIA: No hay PIN configurado, no se puede enviar la URL al servidor")
            return
        
        # Construir la URL del endpoint API según el servidor configurado
        servidor_nombre = config.get("servidor", DEFAULT_SERVER)
        servidor_base = SERVERS.get(servidor_nombre, SERVERS[DEFAULT_SERVER])
        # endpoint_url = f"{servidor_base}/Configuracion/GuardarUrlCloudflare"

        endpoint_url = f"http://adminbiz.local/Configuracion/GuardarUrlCloudflare"
        # Datos a enviar al API
        data = {
            "pin": str(pin),
            "url": str(url),
            "id_agencia": ""  # Para abrir_caja no usamos agencia, se guarda como default
        }
        
        # Debug: Mostrar información detallada
        print(f"📤 Enviando URL de cloudflared al servidor:")
        print(f"   Endpoint: {endpoint_url}")
        print(f"   PIN: {pin}")
        print(f"   URL: {url}")
        print(f"   Datos a enviar: {data}")
        logger.info(f"Enviando URL de cloudflared al servidor: {endpoint_url}")
        logger.info(f"Datos completos: {data}")
        
        # Enviar POST al API con reintentos
        print(f"🌐 Realizando petición POST...")
        max_retries = 3
        retry_delay = 2  # segundos
        
        for intento in range(max_retries):
            try:
                response = requests.post(
                    endpoint_url,
                    data=data,
                    timeout=15,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    verify=True  # Verificar certificado SSL
                )
                break  # Si la petición fue exitosa, salir del loop
            except requests.exceptions.SSLError as e:
                logger.warning(f"Error SSL en intento {intento + 1}/{max_retries}: {e}")
                if intento < max_retries - 1:
                    print(f"⚠️ Error SSL, reintentando en {retry_delay} segundos...")
                    time.sleep(retry_delay)
                else:
                    raise
            except requests.exceptions.Timeout as e:
                logger.warning(f"Timeout en intento {intento + 1}/{max_retries}: {e}")
                if intento < max_retries - 1:
                    print(f"⚠️ Timeout, reintentando en {retry_delay} segundos...")
                    time.sleep(retry_delay)
                else:
                    raise
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Error de conexión en intento {intento + 1}/{max_retries}: {e}")
                if intento < max_retries - 1:
                    print(f"⚠️ Error de conexión, reintentando en {retry_delay} segundos...")
                    time.sleep(retry_delay)
                else:
                    raise
        
        print(f"📥 Respuesta recibida:")
        print(f"   Status Code: {response.status_code}")
        print(f"   Headers: {dict(response.headers)}")
        
        response.raise_for_status()
        
        # Verificar respuesta
        response_text = response.text.strip()
        print(f"   Contenido: {response_text}")
        
        if response_text == "true":
            print(f"✅ URL de cloudflared guardada exitosamente en el servidor")
            print(f"   URL: {url}")
            print(f"   PIN: {pin}")
            logger.info(f"URL de cloudflared guardada exitosamente en el servidor")
        else:
            print(f"⚠️ Respuesta del servidor: {response_text}")
            print(f"💡 Si la respuesta no es 'true', revisa el archivo de debug en el servidor")
            logger.warning(f"Respuesta del servidor: {response_text}")
        
    except requests.exceptions.RequestException as e:
        error_msg = f"Error al enviar URL de cloudflared al servidor: {e}"
        print(f"ERROR: {error_msg}")
        logger.error(error_msg)
    except Exception as e:
        error_msg = f"Error inesperado al enviar URL de cloudflared: {e}"
        print(f"ERROR: {error_msg}")
        logger.error(error_msg)
        import traceback
        logger.error(traceback.format_exc())


def _start_cloudflared_tunnel():
    """Ejecuta cloudflared tunnel y extrae la URL del output"""
    global cloudflared_process, cloudflared_url
    try:
        # Ejecutar cloudflared tunnel
        cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{CONFIG_SERVIDOR['port']}"]
        logger.info(f"Ejecutando comando: {' '.join(cmd)}")
        print(f"INFO: Ejecutando cloudflared tunnel...")
        
        # Configurar flags para ocultar la ventana de CMD en Windows
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
            "universal_newlines": True,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        
        cloudflared_process = subprocess.Popen(
            cmd,
            **popen_kwargs
        )
        
        # Leer la salida línea por línea para encontrar la URL
        url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
        url_found = False
        max_lines = 100  # Límite de líneas a leer para buscar la URL
        line_count = 0
        
        while cloudflared_process.poll() is None and not url_found and line_count < max_lines:
            line = cloudflared_process.stdout.readline()
            if not line:
                time.sleep(0.1)  # Pequeña pausa si no hay línea disponible
                continue
            
            line_count += 1
            
            # Buscar la URL en la línea
            match = url_pattern.search(line)
            if match:
                cloudflared_url = match.group(0)
                url_found = True
                print(f"✅ URL del túnel Cloudflare encontrada: {cloudflared_url}")
                logger.info(f"URL del túnel Cloudflare: {cloudflared_url}")
                
                        # Intentar actualizar la URL en el servidor web automáticamente
                # Hacerlo en un hilo separado para no bloquear
                def enviar_url_async():
                    try:
                        time.sleep(1)  # Esperar un momento para asegurar que el túnel esté estable
                        _send_cloudflare_url_to_server(cloudflared_url)
                    except Exception as e:
                        logger.warning(f"No se pudo actualizar la URL en el servidor web: {e}")
                        print(f"ADVERTENCIA: No se pudo actualizar la URL automáticamente en el servidor web")
                
                threading.Thread(target=enviar_url_async, daemon=True).start()
                
                break
            
            logger.debug(f"Cloudflared output: {line.strip()}")
        
        if not url_found:
            logger.warning("No se encontró la URL del túnel en la salida de cloudflared después de leer varias líneas")
            print("ADVERTENCIA: No se pudo extraer la URL del túnel Cloudflare de la salida")
        
        # Monitorear continuamente el proceso para detectar si se cae
        def monitor_cloudflared():
            """Monitorea el proceso de cloudflared y lo reinicia si se cae"""
            while True:
                time.sleep(60)  # Verificar cada minuto
                if cloudflared_process is None or cloudflared_process.poll() is not None:
                    logger.warning("Cloudflared se cerró, reiniciando túnel...")
                    print("⚠️ Cloudflared se cerró, reiniciando túnel...")
                    time.sleep(5)  # Esperar antes de reiniciar
                    # Reiniciar el túnel
                    _start_cloudflared_tunnel()
                    break
        
        # Iniciar monitoreo en un hilo separado
        monitor_thread = threading.Thread(target=monitor_cloudflared, daemon=True)
        monitor_thread.start()
        logger.info("Monitoreo de Cloudflare Tunnel iniciado")
            
    except FileNotFoundError:
        error_msg = "cloudflared no se encuentra en el PATH. Por favor, instálalo."
        print(f"ERROR: {error_msg}")
        logger.error(error_msg)
    except Exception as e:
        error_msg = f"Error al ejecutar cloudflared: {e}"
        print(f"ERROR: {error_msg}")
        logger.error(error_msg)
        import traceback
        logger.error(traceback.format_exc())


def iniciar_servidor_fastapi():
    """Inicia servidor FastAPI HTTP simple con Cloudflare Tunnel"""
    global servidor_thread, cloudflared_process, cloudflared_url
    
    def run_server_http():
        """Servidor HTTP simple (puerto 8444)"""
        try:
            # Verificar si el puerto está disponible
            puerto_disponible = verificar_puerto_disponible("127.0.0.1", CONFIG_SERVIDOR["port"])
            if not puerto_disponible:
                logger.warning(f"El puerto {CONFIG_SERVIDOR['port']} parece estar en uso. Intentando iniciar de todas formas...")
                print(f"ADVERTENCIA: El puerto {CONFIG_SERVIDOR['port']} parece estar en uso.")
            
            logger.info(f"Iniciando servidor HTTP en http://{CONFIG_SERVIDOR['host']}:{CONFIG_SERVIDOR['port']}")
            print(f"INFO: Iniciando servidor HTTP en puerto {CONFIG_SERVIDOR['port']}")
            
            # Configurar uvicorn SIN SSL
            config = uvicorn.Config(
                app,
                host=CONFIG_SERVIDOR["host"],
                port=CONFIG_SERVIDOR["port"],
                log_level="info",
                access_log=True
            )
            
            logger.info("Iniciando servidor HTTP...")
            print("INFO: Iniciando servidor HTTP...")
            
            # Crear el servidor
            server = uvicorn.Server(config)
            
            # Ejecutar el servidor usando run() que es más adecuado para hilos
            try:
                print(f"INFO: Servidor HTTP iniciado correctamente en puerto {CONFIG_SERVIDOR['port']}")
                logger.info(f"Servidor HTTP iniciado correctamente en puerto {CONFIG_SERVIDOR['port']}")
                logger.info(f"Ejecutando servidor HTTP con server.run()...")
                print(f"INFO: Ejecutando servidor HTTP con server.run()...")
                # server.run() crea y ejecuta su propio event loop
                server.run()
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    logger.warning("Event loop cerrado (HTTP)")
                    print("WARNING: Event loop cerrado (HTTP)")
                else:
                    logger.error(f"RuntimeError en servidor HTTP: {e}")
                    print(f"ERROR: RuntimeError en servidor HTTP: {e}")
                    raise
            except KeyboardInterrupt:
                logger.info("Servidor HTTP detenido por interrupción")
                print("INFO: Servidor HTTP detenido por interrupción")
            except Exception as e:
                error_msg = f"Error durante la ejecución del servidor HTTP: {e}"
                logger.error(error_msg)
                print(f"ERROR: {error_msg}")
                import traceback
                traceback_str = traceback.format_exc()
                logger.error(traceback_str)
                print(traceback_str)
                raise
            
        except OSError as e:
            if "Address already in use" in str(e) or "address is already in use" in str(e).lower():
                error_msg = f"El puerto {CONFIG_SERVIDOR['port']} ya está en uso."
                logger.error(error_msg)
                print(f"ERROR: {error_msg}")
            else:
                error_msg = f"Error del sistema al iniciar el servidor HTTP: {e}"
                logger.error(error_msg)
                print(f"ERROR: {error_msg}")
            import traceback
            traceback_str = traceback.format_exc()
            logger.error(traceback_str)
            print(traceback_str)
        except Exception as e:
            error_msg = f"Error inesperado al iniciar el servidor HTTP: {e}"
            logger.error(error_msg)
            print(f"ERROR: {error_msg}")
            import traceback
            traceback_str = traceback.format_exc()
            logger.error(traceback_str)
            print(traceback_str)
        finally:
            logger.info("Servidor HTTP detenido")
            print("INFO: Servidor HTTP detenido")
    
    # Iniciar servidor HTTP en hilo
    if servidor_thread is None or not servidor_thread.is_alive():
        # Hilo para servidor HTTP
        servidor_thread = threading.Thread(target=run_server_http, daemon=True)
        servidor_thread.start()
        logger.info("Hilo del servidor HTTP iniciado")
        
        # Esperar un momento para que el servidor inicie
        time.sleep(2)
        
        # Verificar puerto
        puerto_disponible = verificar_puerto_disponible("127.0.0.1", CONFIG_SERVIDOR["port"])
        
        if puerto_disponible:
            logger.warning(f"El puerto {CONFIG_SERVIDOR['port']} sigue disponible después de iniciar. El servidor puede no haberse iniciado correctamente.")
            print(f"ADVERTENCIA: El puerto {CONFIG_SERVIDOR['port']} sigue disponible. Verifica los logs para errores.")
        else:
            logger.info(f"El puerto {CONFIG_SERVIDOR['port']} está en uso - el servidor parece estar escuchando")
            print(f"INFO: Puerto {CONFIG_SERVIDOR['port']} está en uso - servidor escuchando")
        
        # Verificar que el servidor realmente responda
        time.sleep(1)
        servidor_activo, mensaje = verificar_servidor_activo()
        
        if servidor_activo:
            logger.info("Servidor HTTP verificado y respondiendo correctamente")
            print("INFO: Servidor HTTP verificado y respondiendo correctamente")
            
            # Iniciar Cloudflare Tunnel en un hilo separado solo si no está ya ejecutándose
            if cloudflared_process is None or cloudflared_process.poll() is not None:
                cloudflared_thread = threading.Thread(target=_start_cloudflared_tunnel, daemon=True)
                cloudflared_thread.start()
                logger.info("Hilo de Cloudflare Tunnel iniciado")
            else:
                logger.info("Cloudflare Tunnel ya está ejecutándose")
        else:
            logger.warning(f"Servidor HTTP no responde: {mensaje}")
            print(f"ADVERTENCIA: Servidor HTTP no responde: {mensaje}")
    else:
        logger.info("Servidor FastAPI ya está ejecutándose")
        
        # Verificar si cloudflared está activo, si no, iniciarlo
        if cloudflared_process is None or cloudflared_process.poll() is not None:
            logger.info("Cloudflare Tunnel no está activo, iniciando...")
            cloudflared_thread = threading.Thread(target=_start_cloudflared_tunnel, daemon=True)
            cloudflared_thread.start()


# ==================== INTERFAZ GRÁFICA ====================



class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.configuracion = None
        self.init_ui()
        self.cargar_configuracion_ui()
        self.setup_system_tray()
        
    def init_ui(self):
        """Inicializa la interfaz de usuario"""
        self.setWindowTitle("Abrir Cajón — Configuración")
        self.setFixedSize(480, 580)

        # ── Global stylesheet ─────────────────────────────────────────
        self.setStyleSheet("""
            QMainWindow, QWidget#central {
                background-color: #0d1117;
            }
            QWidget#header {
                background-color: #161b22;
                border-bottom: 1px solid #21262d;
            }
            QLabel#appTitle {
                color: #e6edf3;
                font-size: 17px;
                font-weight: 700;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QLabel#appSubtitle {
                color: #7d8590;
                font-size: 11px;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QLabel#sectionLabel {
                color: #7d8590;
                font-size: 10px;
                font-weight: 600;
                font-family: 'Segoe UI', Arial, sans-serif;
                letter-spacing: 1px;
            }
            QWidget#card {
                background-color: #161b22;
                border: 1px solid #21262d;
                border-radius: 8px;
            }
            QLabel#fieldLabel {
                color: #8b949e;
                font-size: 11px;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-weight: 500;
            }
            QLabel {
                color: #c9d1d9;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 12px;
            }
            QLineEdit, QComboBox {
                background-color: #0d1117;
                color: #e6edf3;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 7px 10px;
                font-size: 12px;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #388bfd;
                background-color: #0d1117;
            }
            QComboBox::drop-down {
                border: none;
                width: 22px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #7d8590;
                margin-right: 6px;
            }
            QComboBox QAbstractItemView {
                background-color: #161b22;
                color: #e6edf3;
                border: 1px solid #30363d;
                selection-background-color: #1f6feb;
                selection-color: white;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 12px;
                padding: 2px;
            }
            QPushButton#btnPrimary {
                background-color: #238636;
                color: #ffffff;
                border: 1px solid #2ea043;
                border-radius: 6px;
                padding: 8px 18px;
                font-size: 12px;
                font-weight: 600;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QPushButton#btnPrimary:hover {
                background-color: #2ea043;
            }
            QPushButton#btnPrimary:pressed {
                background-color: #1a7f37;
            }
            QPushButton#btnSecondary {
                background-color: #21262d;
                color: #c9d1d9;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 8px 18px;
                font-size: 12px;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QPushButton#btnSecondary:hover {
                background-color: #30363d;
                color: #e6edf3;
                border-color: #8b949e;
            }
            QPushButton#btnSecondary:pressed {
                background-color: #161b22;
            }
            QMessageBox {
                background-color: #161b22;
                color: #e6edf3;
            }
        """)

        # ── Root layout ───────────────────────────────────────────────
        central_widget = QWidget()
        central_widget.setObjectName("central")
        self.setCentralWidget(central_widget)
        root = QVBoxLayout(central_widget)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Header ────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("header")
        header.setFixedHeight(68)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 0, 20, 0)
        header_layout.setSpacing(12)

        dot = QLabel("●")
        dot.setStyleSheet("color: #238636; font-size: 22px; padding-bottom: 2px;")
        header_layout.addWidget(dot)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        app_title = QLabel("Abrir Cajón")
        app_title.setObjectName("appTitle")
        app_sub = QLabel(f"Servidor de impresora  ·  v{VERSION}")
        app_sub.setObjectName("appSubtitle")
        title_col.addWidget(app_title)
        title_col.addWidget(app_sub)
        header_layout.addLayout(title_col)
        header_layout.addStretch()
        root.addWidget(header)

        # ── Content ───────────────────────────────────────────────────
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 18, 20, 18)
        content_layout.setSpacing(10)

        # Section: Configuración
        cfg_lbl = QLabel("CONFIGURACIÓN")
        cfg_lbl.setObjectName("sectionLabel")
        content_layout.addWidget(cfg_lbl)

        # Card
        card = QWidget()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        def field_row(label_text, widget):
            lbl = QLabel(label_text)
            lbl.setObjectName("fieldLabel")
            col = QVBoxLayout()
            col.setSpacing(3)
            col.addWidget(lbl)
            col.addWidget(widget)
            return col

        # Usuario
        self.campo_usuario = QLineEdit()
        self.campo_usuario.setPlaceholderText("Nombre de usuario")
        card_layout.addLayout(field_row("Usuario", self.campo_usuario))

        # PIN
        self.campo_pin = QLineEdit()
        self.campo_pin.setEchoMode(QLineEdit.Normal)
        self.campo_pin.setPlaceholderText("Número de empresa (PIN)")
        card_layout.addLayout(field_row("PIN de empresa", self.campo_pin))

        # Servidor
        self.combo_servidor = QComboBox()
        for nombre in SERVERS.keys():
            self.combo_servidor.addItem(nombre)
        card_layout.addLayout(field_row("Servidor", self.combo_servidor))

        # Impresora
        self.combo_impresora = QComboBox()
        impresoras_disponibles = listar_impresoras()
        self.combo_impresora.addItems(impresoras_disponibles)
        impresora_predeterminada = obtener_impresora_predeterminada()
        if impresora_predeterminada and impresora_predeterminada in impresoras_disponibles:
            self.combo_impresora.setCurrentIndex(impresoras_disponibles.index(impresora_predeterminada))
        card_layout.addLayout(field_row("Impresora", self.combo_impresora))

        content_layout.addWidget(card)

        # Section: Estado
        status_lbl = QLabel("ESTADO DEL SERVIDOR")
        status_lbl.setObjectName("sectionLabel")
        content_layout.addWidget(status_lbl)

        self.label_estado = QLabel("Sin configuración")
        self.label_estado.setWordWrap(True)
        self.label_estado.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.label_estado.setMinimumHeight(56)
        self._set_status("Sin configuración", "default")
        content_layout.addWidget(self.label_estado)

        content_layout.addStretch()

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.boton_verificar = QPushButton("Verificar estado")
        self.boton_verificar.setObjectName("btnSecondary")
        self.boton_verificar.clicked.connect(self.verificar_servidor_manual)
        btn_row.addWidget(self.boton_verificar)

        boton_guardar = QPushButton("Guardar configuración")
        boton_guardar.setObjectName("btnPrimary")
        boton_guardar.clicked.connect(self.guardar_configuracion)
        btn_row.addWidget(boton_guardar)

        content_layout.addLayout(btn_row)
        root.addWidget(content)
        
    def setup_system_tray(self):
        """Configura el ícono de la bandeja del sistema"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("El sistema no soporta íconos en la bandeja")
            return
        
        self.tray_icon = QSystemTrayIcon(self)
        # Intentar cargar el ícono
        icon_path = os.path.join(os.path.dirname(__file__), "app_icon_bg.ico")
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            # Usar ícono por defecto si no existe
            self.tray_icon.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        
        self.tray_icon.setToolTip(f"Abrir Cajón v{VERSION}")
        
        # Menú de la bandeja
        tray_menu = QMenu()
        restaurar_action = QAction("Restaurar", self)
        restaurar_action.triggered.connect(self.show)
        tray_menu.addAction(restaurar_action)
        
        salir_action = QAction("Salir", self)
        salir_action.triggered.connect(self.cerrar_aplicacion)
        tray_menu.addAction(salir_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.tray_icon_activated)
        self.tray_icon.show()

    def closeEvent(self, event):
        """Minimiza a la bandeja en lugar de cerrar si la bandeja está disponible"""
        if hasattr(self, 'tray_icon') and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "Abrir Cajón",
                "La aplicación continúa ejecutándose en la bandeja del sistema.",
                QSystemTrayIcon.Information,
                2500
            )
        else:
            self.cerrar_aplicacion()

    def _set_status(self, text, style="default"):
        """Actualiza el label de estado con el estilo correcto"""
        palettes = {
            "default":  ("background-color:#161b22; border:1px solid #21262d;",  "#8b949e"),
            "active":   ("background-color:#0d2818; border:1px solid #238636;",  "#3fb950"),
            "starting": ("background-color:#0d1b2e; border:1px solid #1f6feb;",  "#79c0ff"),
            "error":    ("background-color:#2d1316; border:1px solid #f85149;",  "#ffa198"),
        }
        bg_border, color = palettes.get(style, palettes["default"])
        self.label_estado.setText(text)
        self.label_estado.setStyleSheet(f"""
            QLabel {{
                {bg_border}
                border-radius: 8px;
                color: {color};
                font-size: 12px;
                font-family: 'Segoe UI', Arial, sans-serif;
                padding: 12px 14px;
            }}
        """)


    def tray_icon_activated(self, reason):
        """Maneja los clics en el ícono de la bandeja"""
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()
            self.raise_()
            self.activateWindow()
    
    def cargar_configuracion_ui(self):
        """Carga la configuración y actualiza la UI"""
        self.configuracion = cargar_configuracion()
        if self.configuracion:
            self.campo_usuario.setText(self.configuracion.get("usuario", ""))
            self.campo_pin.setText(self.configuracion.get("pin", ""))
            impresora = self.configuracion.get("impresora", "")
            index = self.combo_impresora.findText(impresora)
            if index >= 0:
                self.combo_impresora.setCurrentIndex(index)
            servidor = self.configuracion.get("servidor", DEFAULT_SERVER)
            index_srv = self.combo_servidor.findText(servidor)
            if index_srv >= 0:
                self.combo_servidor.setCurrentIndex(index_srv)
            # Iniciar servidor automáticamente si hay configuración
            self.iniciar_servidor()
            self.actualizar_estado_servidor()
    
    def guardar_configuracion(self):
        """Guarda la configuración y reinicia el servidor"""
        usuario = self.campo_usuario.text().strip()
        pin = self.campo_pin.text().strip()
        impresora = self.combo_impresora.currentText()
        servidor = self.combo_servidor.currentText()

        if not usuario:
            QMessageBox.warning(self, "Advertencia", "Por favor, complete el usuario.")
            return

        if not impresora:
            QMessageBox.warning(self, "Advertencia", "Por favor, seleccione una impresora.")
            return

        if guardar_configuracion(usuario, pin, impresora, servidor):
            pin_display = f"{'*' * len(pin)}" if pin else "No configurado"
            mensaje = (
                "Configuración guardada exitosamente.\n\n"
                f"Usuario:   {usuario}\n"
                f"PIN:       {pin_display}\n"
                f"Impresora: {impresora}\n"
                f"Servidor:  {servidor}"
            )
            QMessageBox.information(self, "Configuración guardada", mensaje)
            self.configuracion = {
                "usuario": usuario,
                "pin": pin,
                "impresora": impresora,
                "servidor": servidor,
            }
            self.iniciar_servidor()
            self.actualizar_estado_servidor()
        else:
            QMessageBox.critical(self, "Error", "No se pudo guardar la configuración.")
    
    def iniciar_servidor(self):
        """Inicia el servidor FastAPI"""
        # Detener servidor anterior si existe (aunque uvicorn no se puede detener fácilmente)
        global servidor_thread
        if servidor_thread is not None and servidor_thread.is_alive():
            logger.info("Servidor ya está ejecutándose, se reiniciará al guardar configuración")
        
        # Iniciar servidor
        self._set_status("Iniciando servidor...", "starting")
        
        iniciar_servidor_fastapi()
        
        # Esperar y verificar progresivamente
        import time
        for i in range(6):  # Intentar durante 6 segundos
            time.sleep(1)
            puerto_disponible = verificar_puerto_disponible("127.0.0.1", CONFIG_SERVIDOR["port"])
            if not puerto_disponible:
                # El puerto está en uso, el servidor puede estar iniciando
                logger.info(f"Puerto {CONFIG_SERVIDOR['port']} detectado en uso después de {i+1} segundos")
                print(f"INFO: Puerto {CONFIG_SERVIDOR['port']} detectado en uso después de {i+1} segundos")
                break
        
        # Esperar un poco más y verificar que el servidor responda
        time.sleep(2)
        
        # Verificar que el servidor esté realmente activo
        self.actualizar_estado_servidor()
    
    def actualizar_estado_servidor(self):
        """Actualiza el estado del servidor en la UI"""
        config = cargar_configuracion()
        
        if not config:
            self._set_status("Sin configuración — complete los campos y guarde.", "default")
            return

        # Verificar si el servidor está realmente activo
        servidor_activo, mensaje = verificar_servidor_activo()

        if servidor_activo:
            global cloudflared_url
            servidor_nombre = config.get("servidor", DEFAULT_SERVER)
            if cloudflared_url:
                url_display = cloudflared_url if len(cloudflared_url) <= 55 else cloudflared_url[:52] + "..."
                self._set_status(
                    f"Servidor activo  ·  Puerto {CONFIG_SERVIDOR['port']}\n"
                    f"Cloudflare: {url_display}",
                    "active"
                )
            else:
                self._set_status(
                    f"Servidor activo  ·  Puerto {CONFIG_SERVIDOR['port']}\n"
                    f"Servidor: {servidor_nombre}  ·  Túnel iniciando...",
                    "active"
                )
        else:
            self._set_status(f"Iniciando servidor...\n{mensaje}", "starting")
            logger.warning(f"El servidor no está respondiendo: {mensaje}")
    
    def verificar_servidor_manual(self):
        """Verifica manualmente el estado del servidor y muestra información detallada"""
        config = cargar_configuracion()
        global cloudflared_url, cloudflared_process
        
        mensaje_detalle = []
        mensaje_detalle.append("=== DIAGNÓSTICO DEL SERVIDOR ===\n")
        
        # Verificar configuración
        if not config:
            mensaje_detalle.append("❌ Configuración: No encontrada")
        else:
            mensaje_detalle.append("✅ Configuración: OK")
            mensaje_detalle.append(f"   - Usuario:   {config.get('usuario', 'No configurado')}")
            mensaje_detalle.append(f"   - Impresora: {config.get('impresora', 'No configurada')}")
            mensaje_detalle.append(f"   - Servidor:  {config.get('servidor', DEFAULT_SERVER)}")
        
        mensaje_detalle.append("")
        
        # Verificar puerto
        puerto_disponible = verificar_puerto_disponible("127.0.0.1", CONFIG_SERVIDOR["port"])
        if puerto_disponible:
            mensaje_detalle.append(f"⚠️  Puerto {CONFIG_SERVIDOR['port']}: Disponible (servidor no está escuchando)")
        else:
            mensaje_detalle.append(f"✅ Puerto {CONFIG_SERVIDOR['port']}: En uso (algo está escuchando)")
        
        mensaje_detalle.append("")
        
        # Verificar servidor HTTP
        servidor_activo, mensaje_servidor = verificar_servidor_activo()
        if servidor_activo:
            mensaje_detalle.append(f"✅ Servidor HTTP: Activo y respondiendo")
            mensaje_detalle.append(f"   - Servidor local: http://localhost:{CONFIG_SERVIDOR['port']} (solo para esta PC)")
        else:
            mensaje_detalle.append(f"❌ Servidor HTTP: No responde")
            mensaje_detalle.append(f"   - Error: {mensaje_servidor}")
            mensaje_detalle.append(f"   - URL intentada: http://localhost:{CONFIG_SERVIDOR['port']}/status")
        
        mensaje_detalle.append("")
        
        # Verificar Cloudflare Tunnel (IMPORTANTE: La web HTTPS usa esta URL)
        if cloudflared_process and cloudflared_process.poll() is None:
            mensaje_detalle.append("✅ Cloudflare Tunnel: Ejecutándose")
            if cloudflared_url:
                mensaje_detalle.append(f"   - URL para la web HTTPS: {cloudflared_url}")
                mensaje_detalle.append(f"   - Esta es la URL que debe usar tu aplicación web")
                mensaje_detalle.append(f"   - Ejemplo: {cloudflared_url}/abrir-caja")
                mensaje_detalle.append(f"   - ✅ URL enviada automáticamente al servidor web")
            else:
                mensaje_detalle.append("   - URL pública: Generándose... (espera unos segundos)")
        else:
            mensaje_detalle.append("⚠️  Cloudflare Tunnel: No está ejecutándose")
            mensaje_detalle.append("   - CRÍTICO: Sin túnel, la web HTTPS no puede conectarse")
            mensaje_detalle.append("   - El túnel se inicia automáticamente cuando el servidor está activo")
        
        mensaje_detalle.append("")
        mensaje_detalle.append("=== RECOMENDACIONES ===")
        
        if not config:
            mensaje_detalle.append("1. Completa y guarda la configuración (usuario e impresora)")
        if not servidor_activo and config:
            mensaje_detalle.append("2. El servidor debería iniciarse automáticamente al guardar la configuración")
            mensaje_detalle.append("3. Si el problema persiste, verifica el firewall de Windows")
            mensaje_detalle.append("4. Revisa los logs en la consola para más detalles")
        if not cloudflared_url and servidor_activo:
            mensaje_detalle.append("5. El túnel Cloudflare puede tardar unos segundos en generar la URL")
            mensaje_detalle.append("6. Asegúrate de tener cloudflared instalado en el PATH")
        
        mensaje_completo = "\n".join(mensaje_detalle)
        
        QMessageBox.information(
            self,
            "Diagnóstico del Servidor",
            mensaje_completo
        )
        
        # Actualizar estado después de la verificación
        self.actualizar_estado_servidor()
    
    def cerrar_aplicacion(self):
        """Cierra completamente la aplicación"""
        global cloudflared_process
        
        # Detener cloudflared si está ejecutándose
        if cloudflared_process and cloudflared_process.poll() is None:
            try:
                cloudflared_process.terminate()
                try:
                    cloudflared_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cloudflared_process.kill()
                logger.info("Proceso cloudflared terminado")
            except Exception as e:
                logger.error(f"Error al terminar cloudflared: {e}")
        
        if hasattr(self, 'tray_icon'):
            self.tray_icon.hide()
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    if not tray_available:
        logger.warning("El sistema no soporta íconos en la bandeja. Se ejecutará en modo ventana.")
        app.setQuitOnLastWindowClosed(True)
    else:
        app.setQuitOnLastWindowClosed(False)  # No cerrar cuando se cierra la ventana
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
