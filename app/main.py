import requests
import json
import pandas as pd
import xml.etree.ElementTree as ET
import os
import shutil
import urllib.parse
import mimetypes
import sys
import logging
import colorlog
import jwt
import bcrypt

from contextlib import asynccontextmanager
from enum import Enum
from typing import Optional
from fastapi import FastAPI, Query, UploadFile, File, Form, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timedelta
from database import WallEntry, guardar_en_muro, obtener_entradas_muro, db, User, get_user_from_db, create_user_in_db
from dotenv import load_dotenv

# Carga las variables del archivo .env al entorno de ejecución
load_dotenv()

# API KEY de AEMET OpenData
API_KEY_AEMET = os.environ.get("API_KEY_AEMET")

# API KEY de Google AI Studio para GEMINI
API_KEY_GEMINI = os.environ.get("API_KEY_GEMINI")

SECRET_KEY = os.environ.get("SECRET_KEY")
ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# Días que se conservan los ficheros de contexto de la IA (uploads/ai_context).
# En el arranque, los ficheros más antiguos que este umbral se eliminan.
# Configurable mediante la variable de entorno AI_CONTEXT_RETENTION_DAYS.
try:
    AI_CONTEXT_RETENTION_DAYS = int(os.environ.get("AI_CONTEXT_RETENTION_DAYS", "15"))
    if AI_CONTEXT_RETENTION_DAYS < 0:
        raise ValueError
except ValueError:
    AI_CONTEXT_RETENTION_DAYS = 15

if not API_KEY_AEMET or not API_KEY_GEMINI or not SECRET_KEY:
    raise EnvironmentError("Error: Falta configurar API_KEY_AEMET o API_KEY_GEMINI o SECRET_KEY en las variables de entorno.")

# Configurar logger
log_level = os.environ.get("LOG_LEVEL", "DEBUG").upper() # LOG_LEVEL (por defecto INFO)

logger = logging.getLogger("MadriaWall_App")

logger.setLevel(getattr(logging, log_level))

formatter = colorlog.ColoredFormatter(
    "%(asctime)s [%(log_color)s%(levelname)s%(reset)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    log_colors={
        'DEBUG':    'cyan',
        'INFO':     'green',
        'WARNING':  'yellow',
        'ERROR':    'red',
        'CRITICAL': 'bold_red',
    }
)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)
logger.addHandler(handler)

tags_metadata = [
    {
        "name": "Asistente Urbano",
        "description": "Análisis inteligente sobre clima, calidad del aire y tráfico en Madrid.",
    },
    {
        "name": "Muro",
        "description": "Tu espacio personal: gestiona tus archivos, notas y contenido digital.",
    }
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.usuarios.create_index("username", unique=True)
    base_path_ai_context = os.path.join("uploads", "ai_context")
    # registro de usuario básico al inicio
    await registrar_usuario("ping", "ping", inicio=True)
    # Limpieza en el arranque: elimina ficheros de contexto de la IA caducados
    await limpiar_entorno(db, base_path_ai_context)
    yield


async def limpiar_entorno(db, base_path_ai_context):
    """
    Limpieza en el arranque de los ficheros de contexto de la IA.

    Elimina únicamente los ficheros bajo 'uploads/ai_context' cuya fecha de
    modificación sea más antigua que AI_CONTEXT_RETENTION_DAYS días, y borra
    su registro correspondiente en MongoDB.

    IMPORTANTE: el contenido personal (uploads/personal) NUNCA se elimina,
    ya que esta función solo recorre 'base_path_ai_context'.
    """
    # 0. Si no existe el directorio, no hay nada que limpiar
    if not os.path.isdir(base_path_ai_context):
        logger.debug(f"El directorio {base_path_ai_context} no existe, no hay nada que limpiar.")
        return

    # 1. Calcular el instante de corte: ficheros con mtime anterior se borran
    ahora = datetime.now().timestamp()
    limite_segundos = AI_CONTEXT_RETENTION_DAYS * 24 * 60 * 60
    corte = ahora - limite_segundos

    # Ruta absoluta del directorio raíz de contexto, para comprobar que
    # cada fichero a borrar está realmente dentro (defensa frente a symlinks).
    raiz_ai = os.path.realpath(base_path_ai_context)

    ficheros_borrados = 0
    registros_borrados = 0

    # 2. Recorremos el árbol de uploads/ai_context sin seguir symlinks
    for dirpath, _dirnames, filenames in os.walk(base_path_ai_context, followlinks=False):
        for nombre in filenames:
            ruta = os.path.join(dirpath, nombre)

            try:
                # Ignoramos symlinks: solo tratamos ficheros regulares
                if os.path.islink(ruta):
                    continue

                # Comprobamos que la ruta real sigue dentro de ai_context
                ruta_real = os.path.realpath(ruta)
                if not (ruta_real == raiz_ai or ruta_real.startswith(raiz_ai + os.sep)):
                    logger.warning(f"Ignorado fichero fuera de ai_context: {ruta}")
                    continue

                # Solo borramos si es más antiguo que el umbral
                if os.path.getmtime(ruta) >= corte:
                    continue

                # 2.1. Borrar el fichero del disco
                os.remove(ruta)
                ficheros_borrados += 1
                logger.debug(f"Contexto IA caducado eliminado del disco: {ruta}")

                # 2.2. Borrar el registro asociado en MongoDB (si existe)
                try:
                    resultado = await db.muro.delete_one({"file_path": ruta})
                    registros_borrados += resultado.deleted_count
                except Exception as e:
                    logger.exception(f"Error al borrar el registro en BD para {ruta}: {e}")

            except Exception as e:
                # Un fallo con un fichero no debe detener la limpieza del resto
                logger.exception(f"Error al limpiar el fichero {ruta}: {e}")

    logger.debug(
        f"Limpieza de contexto IA (> {AI_CONTEXT_RETENTION_DAYS} días): "
        f"{ficheros_borrados} ficheros y {registros_borrados} registros eliminados."
    )
    
app = FastAPI(
    title="MadriaWall API", 
    description="Tu asistente urbano inteligente de Madrid y muro de contenido digital.", 
    openapi_tags=tags_metadata,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # En producción limitar esto a un dominio específico
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

AI_SUBDIR = "uploads/ai_context"
os.makedirs(AI_SUBDIR, exist_ok=True)

PERSONAL_SUBDIR = "uploads/personal"
os.makedirs(PERSONAL_SUBDIR, exist_ok=True)

# Registramos tipos MIME que 'mimetypes' no reconoce por defecto (capturas de red)
mimetypes.add_type("application/vnd.tcpdump.pcap", ".pcap")
mimetypes.add_type("application/vnd.tcpdump.pcap", ".pcapng")
# Aseguramos el tipo MIME de CSV (para que la vista lo reconozca como texto/CSV)
mimetypes.add_type("text/csv", ".csv")

# ID de Madrid según AEMET (ejemplo para Madrid capital)
ID_MUNICIPIO = "28079"

# URL para consulta de calidad del aire en Madrid
URL_CALIDAD_AIRE = "https://ciudadesabiertas.madrid.es/dynamicAPI/API/query/calair_tiemporeal.json"

# URL para consulta de tráfico en Madrid
URL_TRAFFIC = "https://informo.madrid.es/informo/tmadrid/pm.xml"

# Configurar API KEY del cliente de Gemini
client = genai.Client(api_key=API_KEY_GEMINI)

# Definimos la configuración para el Agente de IA sobre clima en Madrid
config_agente = types.GenerateContentConfig(
    system_instruction="Eres un agente experto en meteorología y calidad del aire. Analiza los datos de AEMET sobre el tiempo y del ayuntamiento de Madrid sobre calidad del aire que recibas y responde de forma amable, breve y profesional. Si no hay datos, infórmalo.",
    temperature=0.7
)

# Definimos la configuración para el Agente de IA particular
fecha_hoy = datetime.now().strftime("%d de %B de %Y")

with open('config.json', 'r', encoding='utf-8') as f:
    config_data = json.load(f)

instruccion = config_data['ai_agent']['system_instruction']

config_agente_alumnos = types.GenerateContentConfig(
    system_instruction=f"Hoy es {fecha_hoy}. {instruccion}",
    temperature=0.7
)

# Variables global para almacenar respuesta de la AEMET en caché
cache_datos = None

# Función para generar hash de password
def hash_password(password: str):
    # 1. Convertir a bytes
    pwd_bytes = password.encode('utf-8')
    # 2. Truncar a 72 bytes (para evitar el error de bcrypt)
    if len(pwd_bytes) > 72:
        pwd_bytes = pwd_bytes[:72]
    # 3. Generar el hash (salt es automático)
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode('utf-8')

# Función para verificar password
def verify_password(plain_password, hashed_password):
    # 1. Convertir a bytes
    pwd_bytes = plain_password.encode('utf-8')
    # 2. Truncar a 72 bytes (igual que al guardar)
    if len(pwd_bytes) > 72:
        pwd_bytes = pwd_bytes[:72]
    # 3. Verificar
    return bcrypt.checkpw(pwd_bytes, hashed_password.encode('utf-8'))

# Registrar usuario
async def registrar_usuario(username, password, inicio=False):
    # 1. Verificar si el usuario ya existe
    existing_user = await get_user_from_db(username)
    if existing_user and not inicio:
        raise HTTPException(status_code=400, detail="El usuario ya existe")
    if existing_user and inicio:
        return {"message": "Usuario ya registrado"}

    # 2. Hashear la password
    hashed = hash_password(password)

    # 3. Guardar usuario en MongoDB
    new_user = User(username=username, hashed_password=hashed)
    await create_user_in_db(new_user)

    return {"message": "Usuario registrado correctamente"}

# Función para el registro de usuarios en la app
@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    registrar_usuario(username, password)

# Función para el login de usuarios en la app
@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    # 1. Buscar usuario en MongoDB
    user_doc = await get_user_from_db(form_data.username)
    
    if not user_doc:
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos")
    
    # 2. Verificar password con el hash guardado en MongoDB
    if not verify_password(form_data.password, user_doc["hashed_password"]):
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos")
    
    # 3. Crear token
    expire = datetime.utcnow() + timedelta(minutes=10)
    token = jwt.encode({"sub": form_data.username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)
    
    return {"access_token": token, "token_type": "bearer"}

# Función para validar el token que el usuario envía en el header de las peticiones realizadas a la app
def get_current_user(token: str = Depends(oauth2_scheme)):
    logger.debug(f"Token recibido: {token}")
    logger.debug(f"Secret Key actual: {SECRET_KEY}")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        logger.debug("Token expirado")
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError as e:
        logger.debug(f"Token inválido: {e}")
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

def guardar_muestra(data, categoria: str):
    """
    Guarda muestras de datos en subdirectorios categorizados con política FIFO (máximo de 5 muestras).
    categoria: "air_quality", "meteo", "traffic"
    """
    base_path = os.path.join("uploads", "ai_context", categoria, "raw")
    
    # 1. Asegurar que el directorio existe
    os.makedirs(base_path, exist_ok=True)
    
    # 2. Gestionar rotación: identificar y borrar el más antiguo si hay 5
    # Obtenemos lista de archivos con su ruta completa
    archivos = [os.path.join(base_path, f) for f in os.listdir(base_path)]
    # Ordenamos por fecha de creación (los más antiguos primero)
    archivos.sort(key=os.path.getctime)
    
    if len(archivos) >= 5:
        archivo_a_borrar = archivos[0]
        db.muro.delete_one({"file_path": archivo_a_borrar})
        os.remove(archivo_a_borrar)
        logger.debug(f"Muestra de datos antigua eliminada del muro: {archivo_a_borrar}")
        
    # 3. Guardar nueva muestra con timestamp
    timestamp = datetime.now()
    if categoria == "air_quality" or categoria == "meteo":
        nombre_archivo = f"{categoria}_{timestamp.strftime('%Y%m%d_%H%M%S')}.json"
        ruta_completa = os.path.join(base_path, nombre_archivo)
        with open(ruta_completa, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    elif categoria == "traffic":
        nombre_archivo = f"{categoria}_{timestamp.strftime('%Y%m%d_%H%M%S')}.xml"
        ruta_completa = os.path.join(base_path, nombre_archivo)
        with open(ruta_completa, 'wb') as f:
            f.write(data)
        
    # 4. Guardar referencia en MongoDB aquí
    entry = WallEntry(
        content_type="text", 
        description=f"Muestra de datos de categoría '{categoria}'", 
        file_path=ruta_completa,
        timestamp=timestamp
    )
    
    db.muro.insert_one(entry.model_dump())

    logger.info("Nueva muestra de datos registrada en el muro en path " + str(ruta_completa))

    return ruta_completa

def guardar_muestra_procesada(data, categoria: str):
    """
    Guarda muestras de datos procesadas en subdirectorios categorizados con política FIFO (máximo de 5 muestras).
    categoria: "air_quality", "meteo", "traffic"
    """
    base_path = os.path.join("uploads", "ai_context", categoria, "processed")
    
    # 1. Asegurar que el directorio existe
    os.makedirs(base_path, exist_ok=True)
    
    # 2. Gestionar rotación: identificar y borrar el más antiguo si hay 5
    # Obtenemos lista de archivos con su ruta completa
    archivos = [os.path.join(base_path, f) for f in os.listdir(base_path)]
    # Ordenamos por fecha de creación (los más antiguos primero)
    archivos.sort(key=os.path.getctime)
    
    if len(archivos) >= 5:
        archivo_a_borrar = archivos[0]
        db.muro.delete_one({"file_path": archivo_a_borrar})
        os.remove(archivo_a_borrar)
        logger.debug(f"Muestra antigua eliminada del muro: {archivo_a_borrar}")
        
    # 3. Guardar nueva muestra con timestamp
    timestamp = datetime.now()
    nombre_archivo = f"{categoria}_{timestamp.strftime('%Y%m%d_%H%M%S')}.json"
    ruta_completa = os.path.join(base_path, nombre_archivo)
    with open(ruta_completa, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    # 4. Guardar referencia en MongoDB aquí
    entry = WallEntry(
        content_type="text", 
        description=f"Muestra de datos procesada de categoría '{categoria}'", 
        file_path=ruta_completa,
        timestamp=timestamp
    )
    
    db.muro.insert_one(entry.model_dump())

    logger.info("Nueva muestra de datos procesada registrada en el muro en path " + str(ruta_completa))

    return ruta_completa

def obtener_datos_aemet():
    """
    Consulta la API de AEMET, descarga el JSON detallado y 
    extrae los campos clave de forma segura.
    """
    global cache_datos
    
    try:
        # 1. Petición inicial para obtener la URL de descarga
        url_base = f"https://opendata.aemet.es/opendata/api/prediccion/especifica/municipio/diaria/{ID_MUNICIPIO}/"
        headers = {"api_key": API_KEY_AEMET}
        
        response = requests.get(url_base, headers=headers)
        response.raise_for_status() # Lanza error si la petición falla
        
        logger.info("Petición exitosa sobre datos metoerológicos en Madrid. Información básica sobre datos recibidos: " + str(response.json()))
        
        url_datos = response.json().get("datos")
        
        # 2. Descarga del archivo JSON real
        logger.info(f"Guardando datos de URL {url_datos} en fichero local ...")
        respuesta_datos = requests.get(url_datos)
        respuesta_datos.raise_for_status()
        datos_reales = respuesta_datos.json()

        try:
            guardar_muestra(datos_reales, "meteo")
        except Exception as e:
            logger.exception(f"Ocurrió un error al guardar los datos meteorológicos de Madrid: {e}")
       
        # 3. Extracción segura de datos

        # Accedemos a la lista de días para hacer predicción semanal
        lista_dias = datos_reales[0].get('prediccion', {}).get('dia', [])
        
        # Procesamos cada día de la lista
        pronostico_semanal = []
        for dia in lista_dias:
            datos_diarios = {
                "fecha": dia.get('fecha', 'Desconocida'),
                "temp_max": obtener_valor_aemet(dia, 'temperatura', 'maxima', 'Desconocida'),
                "temp_min": obtener_valor_aemet(dia, 'temperatura', 'minima', 'Desconocida'),
                "sensacion_termica_min": obtener_valor_aemet(dia, 'sensTermica', 'minima', 'Desconocida'),
                "sensacion_termica_max": obtener_valor_aemet(dia, 'sensTermica', 'maxima', 'Desconocida'),
                "humedad_min": obtener_valor_aemet(dia, 'humedadRelativa', 'minima', 'Desconocida'),
                "humedad_max": obtener_valor_aemet(dia, 'humedadRelativa', 'maxima', 'Desconocida'),
                "estado_cielo": obtener_valor_aemet(dia, 'estadoCielo', 'descripcion', 'Desconocido'),
                "precipitacion": obtener_valor_aemet(dia, 'probPrecipitacion', 'value', 'Desconocida'),
                "cota_nieve": obtener_valor_aemet(dia, 'cotaNieveProv', 'value', 'Desconocida'),
                "indice_uv_max": obtener_valor_aemet(dia, 'uvMax', 'value', 'Desconocido'),
                "viento": obtener_valor_aemet(dia, 'viento', 'velocidad', 'Desconocida')
            }
            pronostico_semanal.append(datos_diarios)
        
        guardar_muestra_procesada(pronostico_semanal, "meteo")

        logger.info("Datos de meterología en Madrid enviados a Agente de IA: " + str(pronostico_semanal))
        #logger.info("Datos de meterología en Madrid enviados a Agente de IA...")

        cache_datos = pronostico_semanal 

        return pronostico_semanal 

    except Exception as e:
        logger.error(f"Error procesando consulta a la AEMET: {e}")
        if cache_datos:
            logger.info("Devolviendo últimos datos meteorológicos disponibles en caché: " + str(cache_datos))
            return cache_datos
        else:
            logger.error("No hay datos disponibles en caché...")
            return {"error": "Sin datos"}
    
def obtener_valor_aemet(prediccion, campo, subcampo, por_defecto='N/A'):
    """
    Función de utilidad global para extraer datos de AEMET sin importar 
    si es lista o diccionario y comprobando si hay datos vacíos o nulos
    """
    data = prediccion.get(campo)
    
    resultado = None

    if isinstance(data, list):
        # 1. Intentamos encontrar el objeto con periodo 00-24
        item_encontrado = None
        for elemento in data:
            if elemento.get('periodo') == '00-24':
                item_encontrado = elemento
                break
        
        # 2. Si no encontramos 00-24, usamos el primero de la lista
        if item_encontrado is None and len(data) > 0:
            item_encontrado = data[0]
            
        # 3. Si tenemos un item, extraemos el valor
        if item_encontrado is not None:
            resultado = item_encontrado.get(subcampo)

    elif isinstance(data, dict):
        resultado = data.get(subcampo)

    else:
        resultado = data

    if resultado is None or (isinstance(resultado, str) and resultado.strip() == ""):
        return por_defecto
        
    return resultado

def procesar_calidad_aire(url):
    """
    Consulta el dataset sobre calidad del aire de la comunidad de Madrid, descarga el JSON detallado y 
    extrae los campos clave de forma segura.
    """
    # 1. Extracción
    try:
        response = requests.get(url)
        response.raise_for_status()
        logger.info("Petición exitosa sobre datos de calidad del aire en Madrid. Información básica sobre datos recibidos: {'URL': '" + url + "'}")
        data = response.json()
        logger.info(f"Guardando datos de URL {url} en fichero local ...")
        try:
            guardar_muestra(data, "air_quality")
        except Exception as e:
            logger.exception(f"Ocurrió un error al guardar los datos de calidad del aire de Madrid: {e}")
        df = pd.DataFrame(data['records'])
    except Exception as e:
        return f"Error al obtener los datos de calidad del aire en Madrid: {e}"

    # 2. Preparación de columnas
    cols_fijas = ["PROVINCIA", "MUNICIPIO", "ESTACION", "MAGNITUD", "PUNTO_MUESTREO", "ANO", "MES", "DIA"]
    cols_H = [c for c in df.columns if c.startswith('H')]
    cols_V = [c for c in df.columns if c.startswith('V')]

    # 3. Melt de Valores (H) y Validez (V) por separado
    df_h = df.melt(id_vars=cols_fijas, value_vars=cols_H, var_name="H_CODE", value_name="VALOR")
    df_v = df.melt(id_vars=cols_fijas, value_vars=cols_V, var_name="V_CODE", value_name="VALIDEZ")

    # 4. Sincronización: Extraemos el número de hora para poder unir los dataframes
    df_h['HORA_NUM'] = df_h['H_CODE'].str.extract(r'(\d+)').astype(int)
    df_v['HORA_NUM'] = df_v['V_CODE'].str.extract(r'(\d+)').astype(int)

    # 5. Unión (Merge) para alinear Valor y Validez en una sola fila por hora
    df_limpio = pd.merge(df_h, df_v, on=cols_fijas + ['HORA_NUM'])

    # 6. Filtrado: 
    # 6.1. Solo nos interesan los valores validados ('V')
    df_limpio = df_limpio[df_limpio['VALIDEZ'] == 'V'].copy()

    # 6.2. Extraer el código de técnica de medición (el último valor después del último guion bajo de PUNTO_MUESTREO)
    # Ejemplo: '28079011_12_8' -> toma el elemento en la posición -1 (es decir, 8) al separar por '_'
    df_limpio['MEDICION'] = df_limpio['PUNTO_MUESTREO'].str.split('_').str[-1]

    # 6.3. Normalización de estaciones, magnitudes y técnicas de medición.
    catalogos = {
        "provincias": {"28": "Madrid"},
        "municipios": {"079": "Madrid"},
        "estaciones": {"01":"Paseo de Recoletos", "02": "Glorieta del Emperador Carlos V", "04": "Plaza de España", "06": "Plaza de Doctor Marañon", "07": "Plaza de Marques de Salamanca", "07": "Plaza de Marques de Salamanca ", 
                       "08": "Escuelas Aguirre", "09": "Plaza de Luca de Tena", "11": "Avenida Ramon y Cajal", "12": "Plaza de Manuel Becerra", "14": "Plaza de Fernandez Ladrera", "15": "Plaza de Castilla", 
                       "16": "Plaza de Arturo Soria", "17": "Villaverde Alto", "18": "Calle del Farolillo", "19": "Huerta Castañeda", "21": "Plaza de Cristo Rey", "22": "Paseo de Pontones", "23": "Final de Calle Alcalá", 
                       "24": "Casa de Campo", "25": "Santa Eugenia", "26": "Urbanización Emabajada (Barajas)", "27": "Barajas", "35": "Plaza del Carmen", "38": "Cuatro Caminos", "39": "Barrio del Pilar",
                       "40": "Vallecas", "47": "Mendez Alvaro", "48": "Paseo de la Castellana", "49": "Retiro", "50": "Plaza de Castilla", "54": "Ensanche Vallecas", "55": "Urbanización Emabajada (Barajas)",
                       "56": "Plaza Elíptiva", "57": "Sanchinarro", "58": "El Pardo", "59": "Parque Juan Carlos I", "60": "Tres Olivos"},
        "magnitudes": {"01": "Dioxido de Azufre", "6": "Monoxido de Carbono", "7": "Monoxido de Nitrogeno", "8": "Dioxido de Nitrogeno", "9": "Particulas < 2.5 micras", "10": "Particulas < 10 micras", "12": "Oxido de Nitrogeno",
                       "14": "Ozono", "20": "Tolueno", "30": "Benceno", "35": "Etilbenceno", "37": "Metaxileno", "38": "Paraxileno", "39": "Ortoxileno", "42": "Hidrocarburos totales (hexano)", "43": "Metano", 
                       "44": "Hidrocarburos no metanicos (hexano)", "431": "Metaparaxileno"},
        "mediciones": {"6": "Absorcion ultravioleta", "8": "Quimioluminiscencia", "38": "Fluorescencia ultravioleta", "47": "Microbalanza/Espectrometria", "48": "Absorción infrarroja", "59": "Cromatografia de gases"}
    }

    # 6.4. Aplicamos el mapeo (Normalización)
    df_limpio['PROVINCIA'] = df_limpio['PROVINCIA'].map(catalogos['provincias']).fillna("Desconocido")
    df_limpio['MUNICIPIO'] = df_limpio['MUNICIPIO'].map(catalogos['municipios']).fillna("Desconocido")
    df_limpio['ESTACION'] = df_limpio['ESTACION'].map(catalogos['estaciones']).fillna("Desconocido")
    df_limpio['MAGNITUD'] = df_limpio['MAGNITUD'].map(catalogos['magnitudes']).fillna("Desconocido")
    df_limpio['MEDICION'] = df_limpio['MEDICION'].map(catalogos['mediciones']).fillna("Desconocido")

    # 7. Conversión a formato fecha (datetime) para facilitar el análisis
    df_limpio['FECHA_HORA'] = pd.to_datetime(
        df_limpio['ANO'] + '-' + df_limpio['MES'] + '-' + df_limpio['DIA'] + ' '
    )  + pd.to_timedelta(df_limpio['HORA_NUM'] - 1, unit='h')
    
    # 8. Limpieza final de columnas auxiliares
    df_resultado = df_limpio.drop(columns=['H_CODE', 'V_CODE', 'HORA_NUM', 'VALIDEZ', 'DIA', 'MES', 'ANO', 'PUNTO_MUESTREO'])

    # 9. Convertir FECHA_HORA a string para que sea serializable a JSON/Diccionario
    df_resultado['FECHA_HORA'] = df_resultado['FECHA_HORA'].dt.strftime('%Y-%m-%d %H:%M:%S')

    logger.info("Datos de calidad del aire en Madrid enviados a Agente de IA...")

    guardar_muestra_procesada(df_resultado.to_dict(orient='records'), "air_quality")

    return df_resultado.to_dict(orient='records')

def obtener_valor_trafico(pm, tag):
    nodo = pm.find(tag)

    if nodo is None:
        return "N/A"
    
    if nodo.text is None:
        return "Desconocido"

    valor = nodo.text.strip()

    if valor in ("", "-"):
        return "Desconocido"

    try:
        numero = float(valor)

        if numero < 0:
            return "Desconocido"

        if numero.is_integer():
            return int(numero)

        return numero

    except ValueError:
        return valor
    
def procesar_trafico(url):
    """
    Consulta el dataset sobre tráfico de la comunidad de Madrid, descarga el XML detallado y 
    extrae los campos clave de forma segura.
    """
    # 1. Extracción
    try:
        response = requests.get(url)
        response.raise_for_status()
        logger.info("Petición exitosa sobre datos de tráfico en Madrid. Información básica sobre datos recibidos: {'URL': '" + url + "'}")
        data = response.content
        logger.info(f"Guardando datos de URL {url} en fichero traffic_madrid_*.xml ...")
        try:
            guardar_muestra(data, "traffic")
            #with open("traffic_madrid.xml", 'wb') as f:
            #    f.write(data)
        except Exception as e:
            logger.exception(f"Ocurrió un error al guardar los datos de tráfico en Madrid:: {e}")
        # Parseamos el XML
        root = ET.fromstring(response.content)
    except Exception as e:
        return f"Error al obtener los datos de tráfico en Madrid: {e}"

    # 2. Prcesamiento de XML: Extracción de la fecha global y los puntos de medida (pm)
    fecha_reporte = root.find('fecha_hora').text
    lista_pms = []

    for pm in root.findall('pm'):
        # Extraemos los datos de cada elemento
        item = {
            'FECHA_HORA': fecha_reporte,
            'IDELEM': obtener_valor_trafico(pm, 'idelem'),
            'DESCRIPCION': obtener_valor_trafico(pm, 'descripcion'),
            'ACCESO_ASOCIADO': obtener_valor_trafico(pm, 'accesoAsociado'),
            'INTENSIDAD': obtener_valor_trafico(pm, 'intensidad'),
            'OCUPACION': obtener_valor_trafico(pm, 'ocupacion'),
            'CARGA': obtener_valor_trafico(pm, 'carga'),
            'NIVEL_SERVICIO': obtener_valor_trafico(pm, 'nivelServicio'),
            'INTENSIDAD_SATURACION': obtener_valor_trafico(pm, 'intensidadSat'),
            'ERROR': obtener_valor_trafico(pm, 'error'),
            'SUBAREA': obtener_valor_trafico(pm, 'subarea'),
            'COORDENADA_X_UTM': obtener_valor_trafico(pm, 'st_x'),
            'COORDENADA_Y_UTM': obtener_valor_trafico(pm, 'st_y'),
            'VELOCIDAD': obtener_valor_trafico(pm, 'velocidad')
        }
        lista_pms.append(item)

    # 3. Creación y limpieza del DataFrame
    df = pd.DataFrame(lista_pms)

    df = df[df['ERROR'] == 'N'].copy()

    df = df.drop(columns=['ERROR'])

    columnas_excepcionales = ['ACCESO_ASOCIADO', 'COORDENADA_X_UTM', 'COORDENADA_Y_UTM', 'SUBAREA']
    df = df.drop(columns=[c for c in columnas_excepcionales if c in df.columns])

    df['FECHA_HORA'] = pd.to_datetime(df['FECHA_HORA'], format='%d/%m/%Y %H:%M:%S')

    # 4. Definición del Glosario (Metadatos) e integración en el DataFrame
    glosario = {
        "IDELEM": "Identificador único del punto de medida en la red. Se corresponde con el campo idelem presente en el fichero georreferenciado y que permite su posicionamiento sobre plano e identificación del vial y sentido de la circulación.",
        "DESCRIPCION": "Denominación del punto de medida.",
        "ACCESO_ASOCIADO": "Código de control relacionado con el control semafórico para la modificación de los tiempos.",
        "INTENSIDAD": "Intensidad de número de vehículos por hora. Un valor negativo implica la ausencia de datos.",
        "OCUPACION": "Porcentaje de tiempo que está un detector de tráfico ocupado por un vehículo. Por ejemplo, una ocupación del 50% en un periodo de 15 minutos significa que ha habido vehículos situados sobre el detector durante 7 minutos y 30 segundos. Un valor negativo implica la ausencia de datos.",
        "CARGA": "Parámetro de carga del vial. Representa una estimación del grado de congestión, calculado a partir de un algoritmo que usa como variables la intensidad y ocupación, con ciertos factores de corrección. Establece el grado de uso de la vía en un rango de 0 (vacía) a 100 (colapso). Un valor negativo implica la ausencia de datos.",
        "NIVEL_SERVICIO": "Parámetro calculado en función de la velocidad y la ocupación. Con ellos se forma una matriz de 4x4 con la que se determina cada uno de los niveles de servicio posibles: tráfico fluido (0), tráfico lento (1), retenciones (2) y congestión (3). Los umbrales de velocidad y los de ocupación que determinan dichos niveles de servicio varían en función del punto de medida.",
        "INTENSIDAD_SATURACION": "Intensidad de saturación de la vía en veh/hora y que se corresponde con el máximo número de vehículos que pueden pasar en el acceso a la intersección manteniéndose la fase verde del semáforo.",
        "ERROR": "Código de control de la validez de los datos del punto de medida.",
        "SUBAREA": "Identificador de la subárea de explotación de tráfico a la que pertenece el punto de medida.",
        "COORDENADA_X_UTM": "Coordenada X UTM del centroide que representa al punto de medida en el fichero georreferenciado.",
        "COORDENADA_Y_UTM": "Coordenada Y UTM del centroide que representa al punto de medida en el fichero georreferenciado.",
        "VELOCIDAD": "Velocidad media de los vehículos detectados en el periodo de integración."
    }

    df.attrs['glosario'] = glosario

    df['FECHA_HORA'] = df['FECHA_HORA'].dt.strftime('%Y-%m-%d %H:%M:%S')

    logger.info("Datos de tráfico en Madrid enviados a Agente de IA...")

    guardar_muestra_procesada(df.to_dict(orient='records'), "traffic")

    return df.to_dict(orient='records')

@app.post("/assistant-alumno/analisys", 
        summary="Consultar a la IA para obtener conclusiones prácticas sobre el agente definido por el alumno", 
        description=(
            "Consulta a la IA sobre la cuestión plateada por el alumno según la instrucción proporcionada."
        ), 
        tags=["Asistente Particular"])

async def obtener_analisis_particular(pregunta: str = Form(..., description="Pregunta a realizar al asistente.", examples=[""]), usuario: str = Depends(get_current_user)):
    """
    Permite enviar consulta a IA en función de la pregunta formula por el usuario. 
    """
    prompt = f"{pregunta}"
    
    logger.info("Pregunta del usuario: " + pregunta)

    # * Posibilidad de ver los modelos de IA que dispone el API de Gemini:
    '''
    for model in client.models.list():
        logger.debug(f"Modelo: {model.name}")
    '''

    try: 
        # 3. Consultamos a Gemini
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=config_agente_alumnos
        )
        
        logger.info("Respuesta de la IA: " + response.text)
        
        return {"respuesta": response.text, "disclaimer": "El contenido ha sido generado por IA y puede ser incorrecto."}
    
    except ClientError as e:
        
        logger.debug(f"Error de la API de Gemini: {e}")
        
        status_code = e.code if e.code else status.HTTP_502_BAD_GATEWAY

        error_message = str(e.message) if e.message else str(e)
        
        # Verificamos si el código de error es 429 (Resource Exhausted)
        if status_code == 429:
            logger.error(f"Límite de cuota alcanzado (Gemini): {error_message}")
            message = "Hemos superado el límite temporal de consultas a la IA. Por favor, inténtalo de nuevo más tarde."
        else: 
            logger.exception(f"Error de la API de Gemini: {error_message}")
            message = "Ha ocurrido un error al conectar con el servicio de IA."
            
        raise HTTPException(
            status_code=status_code,
            detail = {
                "message": message,
                "error": error_message
            }
        )
    
    except Exception as e:
        logger.exception(f"Error interno inesperado: {e}")
        # Manejo de cualquier otro error inesperado
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = { 
                "message": "Ocurrió un error inesperado al procesar tu solicitud.",
                "error": "Internal Server Error"
            }
        )
    
@app.post("/assistant/analisys", 
        summary="Consultar a la IA para obtener conclusiones prácticas sobre el estado de Madrid", 
        description=(
            "Consulta a la IA sobre la condición actual de Madrid. El sistema genera respuestas personalizadas "
            "en función del conocimiento adquirido tras analizar en tiempo real los datos climáticos "
            "y la calidad del aire en la ciudad. (El análisis de tráfico está actualmente desactivado.)"
        ), 
        tags=["Asistente Urbano"])
async def obtener_analisis_urbano(pregunta: str = Form(..., description="Pregunta a realizar al asistente urbano.", examples=[""]), usuario: str = Depends(get_current_user)):
    """
    Consulta los datos actualizados sobre meteorología y calidad del aire en Madrid y permite enviar consulta a IA en función
    de la pregunta formula por el usuario y del conocimiento adquirido sobre el estado de Madrid.

    NOTA: la recogida de datos de tráfico está desactivada (ver más abajo).
    """
    
    # 1. Obtenemos datos
    datos_meteo = obtener_datos_aemet()

    url_grafico_meteo = None

    if isinstance(datos_meteo, list) and len(datos_meteo) > 0:
        try:
            url_grafico_meteo = obtener_url_grafico_temperaturas(datos_meteo)

            graficos_base_path = os.path.join("uploads", "ai_context", "meteo", "charts")
            os.makedirs( graficos_base_path, exist_ok=True)

            archivos = [os.path.join(graficos_base_path, f) for f in os.listdir(graficos_base_path)]
            archivos.sort(key=os.path.getctime)
            
            if len(archivos) >= 5:
                archivo_a_borrar = archivos[0]
                db.muro.delete_one({"file_path": archivo_a_borrar})
                os.remove(archivo_a_borrar)
                logger.debug(f"Gráfica antigua eliminada del muro: {archivo_a_borrar}")

            timestamp = datetime.now()
            nombre_archivo = f"meteo_chart_{timestamp.strftime('%Y%m%d_%H%M%S')}.png"
            ruta_completa = os.path.join(graficos_base_path, nombre_archivo)
            
            download_image = requests.get(url_grafico_meteo)

            if download_image.status_code == 200:
                with open(ruta_completa, 'wb') as f:
                    f.write(download_image.content)
                    
                entry = WallEntry(
                    content_type="image", 
                    description="Gráfica de categoría 'meteo' sobre temperatura", 
                    file_path=ruta_completa,
                    timestamp=timestamp
                )
                
                db.muro.insert_one(entry.model_dump())

                logger.info("Nueva gráfica registrada en el muro en path " + str(ruta_completa))
            else: 
                logger.error(f"Error al descargar la imagen: Código {download_image.status_code}")
        
        except Exception as e:
            logger.error(f"Error generando gráfico: {e}")
            url_grafico_meteo = None

    datos_calidad_aire = procesar_calidad_aire(URL_CALIDAD_AIRE)

    url_grafico_aire = None
    if isinstance(datos_calidad_aire, list) and len(datos_calidad_aire) > 0:
        try:
            url_grafico_aire = obtener_url_grafico_aire(datos_calidad_aire)

            graficos_base_path = os.path.join("uploads", "ai_context", "air_quality", "charts")
            os.makedirs( graficos_base_path, exist_ok=True)

            archivos = [os.path.join(graficos_base_path, f) for f in os.listdir(graficos_base_path)]
            archivos.sort(key=os.path.getctime)
            
            if len(archivos) >= 5:
                archivo_a_borrar = archivos[0]
                db.muro.delete_one({"file_path": archivo_a_borrar})
                os.remove(archivo_a_borrar)
                logger.debug(f"Gráfica antigua eliminada del muro: {archivo_a_borrar}")

            timestamp = datetime.now()
            nombre_archivo = f"air_quality_chart_{timestamp.strftime('%Y%m%d_%H%M%S')}.png"
            ruta_completa = os.path.join(graficos_base_path, nombre_archivo)
            
            download_image = requests.get(url_grafico_aire)

            if download_image.status_code == 200:
                with open(ruta_completa, 'wb') as f:
                    f.write(download_image.content)
                    
                entry = WallEntry(
                    content_type="image", 
                    description="Gráfica de categoría 'air_quality' sobre partículas de aire", 
                    file_path=ruta_completa,
                    timestamp=timestamp
                )
                
                db.muro.insert_one(entry.model_dump())

                logger.info("Nueva gráfica registrada en el muro en path " + str(ruta_completa))
            else: 
                logger.error(f"Error al descargar la imagen: Código {download_image.status_code}")
        
        except Exception as e:
            logger.error(f"Error generando gráfico: {e}")
            url_grafico_aire = None

    # =========================================================================
    # TRÁFICO DESACTIVADO
    # -------------------------------------------------------------------------
    # La recogida de datos de tráfico y la generación de sus gráficas están
    # temporalmente desactivadas. NO se realiza la petición al servicio de
    # tráfico (URL_TRAFFIC) ni se generan/descargan sus gráficas.
    #
    # El prompt enviado a la IA (más abajo) ya NO incluye datos de tráfico.
    #
    # Para REACTIVARLO en el futuro:
    #   1. Descomentar el bloque siguiente.
    #   2. Reactivar la parte de tráfico en el prompt dinámico (ver sección 2).
    #
    # El código se conserva comentado a propósito para uso futuro.
    # Las funciones auxiliares procesar_trafico(), obtener_valor_trafico() y
    # obtener_urls_graficos_trafico() siguen definidas pero no se usan.
    # =========================================================================
    # datos_trafico = procesar_trafico(URL_TRAFFIC)
    #
    # urls_graficos_trafico = []
    #
    # if isinstance(datos_trafico, list) and len(datos_trafico) > 0:
    #     urls_graficos_trafico = obtener_urls_graficos_trafico(datos_trafico)
    #     for i, url_grafico_trafico in enumerate(urls_graficos_trafico):
    #         try:
    #             tipo = "intensidad" if i == 0 else "velocidad"
    #
    #             graficos_base_path = os.path.join("uploads", "ai_context", "traffic", "charts", tipo)
    #             os.makedirs( graficos_base_path, exist_ok=True)
    #
    #             archivos = [os.path.join(graficos_base_path, f) for f in os.listdir(graficos_base_path)]
    #             archivos.sort(key=os.path.getctime)
    #
    #             if len(archivos) >= 5:
    #                 archivo_a_borrar = archivos[0]
    #                 db.muro.delete_one({"file_path": archivo_a_borrar})
    #                 os.remove(archivo_a_borrar)
    #                 logger.debug(f"Gráfica antigua eliminada del muro: {archivo_a_borrar}")
    #
    #             timestamp = datetime.now()
    #             nombre_archivo = f"traffic_{tipo}_chart_{timestamp.strftime('%Y%m%d_%H%M%S')}.png"
    #             ruta_completa = os.path.join(graficos_base_path, nombre_archivo)
    #
    #             if tipo == "intensidad":
    #                 descripcion = "Gráfica de categoría 'traffic' sobre intensidad media de tráfico"
    #             elif tipo == "velocidad":
    #                 descripcion = "Gráfica de categoría 'traffic' sobre velocidad media interurbana"
    #
    #             download_image = requests.get(url_grafico_trafico)
    #
    #             if download_image.status_code == 200:
    #                 with open(ruta_completa, 'wb') as f:
    #                     f.write(download_image.content)
    #
    #                 entry = WallEntry(
    #                     content_type="image",
    #                     description=descripcion,
    #                     file_path=ruta_completa,
    #                     timestamp=timestamp
    #                 )
    #
    #                 db.muro.insert_one(entry.model_dump())
    #
    #                 logger.info("Nueva gráfica registrada en el muro en path " + str(ruta_completa))
    #             else:
    #                 logger.error(f"Error al descargar la imagen: Código {download_image.status_code}")
    #
    #         except Exception as e:
    #             logger.error(f"Error generando gráfico: {e}")
    #             url_grafico_trafico = None
    
    # 2. Creamos el prompt dinámico
    # NOTA: el tráfico está DESACTIVADO, por lo que el prompt activo solo
    # incluye datos meteorológicos y de calidad del aire. Para reactivar el
    # tráfico, descomentar arriba la recogida de 'datos_trafico' y usar la
    # variante de prompt que incluye "Datos sobre tráfico actuales".
    #prompt = f"Datos meteorológicos actuales: {json.dumps(datos_meteo)}. Datos sobre calidad aire actuales: {json.dumps(datos_calidad_aire)}. Datos sobre tráfico actuales: {json.dumps(datos_trafico)}. Pregunta del usuario: '{pregunta}'"
    prompt = f"Datos meteorológicos actuales: {json.dumps(datos_meteo)}. Datos sobre calidad aire actuales: {json.dumps(datos_calidad_aire)}. Pregunta del usuario: '{pregunta}'"
    #prompt = f"Datos meteorológicos actuales: {json.dumps(datos_meteo)}. Datos sobre tráfico actuales: {json.dumps(datos_trafico)}. Pregunta del usuario: '{pregunta}'"
    #prompt = f"Datos sobre tráfico actuales: {json.dumps(datos_trafico)}. Pregunta del usuario: '{pregunta}'"

    #logger.info("Prompt utilizado: " + prompt)
    #with open("prompt.txt", "w") as f:
    #    f.write(prompt)

    logger.info("Pregunta del usuario: " + pregunta)

    # * Posibilidad de ver los modelos de IA que dispone el API de Gemini:
    '''
    for model in client.models.list():
        logger.debug(f"Modelo: {model.name}")
    '''

    try: 
        # 3. Consultamos a Gemini
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=config_agente
        )
        
        logger.info("Respuesta de la IA: " + response.text)
        
        return {"respuesta": response.text, "disclaimer": "El contenido ha sido generado por IA y puede ser incorrecto."}
    
    except ClientError as e:
        
        logger.debug(f"Error de la API de Gemini: {e}")
        
        status_code = e.code if e.code else status.HTTP_502_BAD_GATEWAY

        error_message = str(e.message) if e.message else str(e)
        
        # Verificamos si el código de error es 429 (Resource Exhausted)
        if status_code == 429:
            logger.error(f"Límite de cuota alcanzado (Gemini): {error_message}")
            message = "Hemos superado el límite temporal de consultas a la IA. Por favor, inténtalo de nuevo más tarde."
        else: 
            logger.exception(f"Error de la API de Gemini: {error_message}")
            message = "Ha ocurrido un error al conectar con el servicio de IA."
            
        raise HTTPException(
            status_code=status_code,
            detail = {
                "message": message,
                "error": error_message
            }
        )
    
    except Exception as e:
        logger.exception(f"Error interno inesperado: {e}")
        # Manejo de cualquier otro error inesperado
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail = { 
                "message": "Ocurrió un error inesperado al procesar tu solicitud.",
                "error": "Internal Server Error"
            }
        )

@app.post("/wall/update", summary="Subir contenido personal al muro", description="Publicar contenido personal en el muro que será almacenado en base de datos y en disco.", tags=["Muro"])
async def subir_contenido(
    content_type: str = Form(None, description="Tipo de contenido (ej: text, image, audio, pcap, pdf, etc.).", examples=[""]),
    description: str = Form(None, description="Descripción breve del contenido (opcional).", examples=[""]),
    file: UploadFile = File(..., description="Seleccione un archivo a publicar en el muro."),
    usuario: str = Depends(get_current_user)
):
    """
    Operación para que el usuario pueda subir contenido digital al muro. 
    """

    if not content_type:
        # Esto intenta adivinar el tipo (ej: image/jpeg, text/plain)
        content_type, _ = mimetypes.guess_type(file.filename)
        # mimetypes no reconoce ciertas extensiones (ej: .pcapng, .pcap),
        # en cuyo caso devuelve None. Usamos un tipo binario genérico por defecto.
        if not content_type:
            content_type = "application/octet-stream"

    file_path = None
    
    # Si viene un archivo, lo guardamos en disco
    if file:
        file_path = f"{UPLOAD_DIR}/personal/{file.filename}"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
    # Guardamos el registro en MongoDB
    entry = WallEntry(
        content_type=content_type, 
        description=description, 
        file_path=file_path
    )
    await guardar_en_muro(entry)
    
    return {"message": "Contenido publicado en el muro con éxito"}

class ContentCategory(str, Enum):
    AIR_QUALITY = "air_quality"
    METEO = "meteo"
    TRAFFIC = "traffic"
    PERSONAL = "personal"

@app.get("/wall/query", summary="Consultar el muro", description="Consulta el contenido publicado en el muro. Puedes filtrar por categoría (air_quality, meteo, traffic o personal).", tags=["Muro"])
async def consultar_muro(
    limit: int = Query(10, description="Número máximo de entradas a recuperar."),
    categoria: Optional[ContentCategory] = Query(None, description="Selecciona la categoría para filtrar el muro."),
    usuario: str = Depends(get_current_user)
):
    """
    Operación para que el usuario pueda consultar el contenido digital disponible en el muro. 
    """
    filtro = {}

    if categoria:
        filtro["file_path"] = {"$regex": categoria.value}

    entradas = await obtener_entradas_muro(filtro=filtro, limit=limit)
    
    for e in entradas:
        # Convertimos el ObjectId de MongoDB a string para que sea legible en JSON
        e["_id"] = str(e["_id"])
    return entradas

@app.delete("/wall/{entry_id}", summary="Borrar entrada del muro", description="Eliminar contenido del muro que haya sido previamente publicado.", tags=["Muro"])
async def borrar_contenido(entry_id: str, usuario: str = Depends(get_current_user)):
    """
    Operación para que el usuario pueda eliminar el contenido digital disponible en el muro. 
    """
    # 1. Validar que el ID sea un formato válido de MongoDB
    try:
        object_id = ObjectId(entry_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Formato de ID inválido")
    
    # 2. Buscar el registro en MongoDB
    entry = await db.muro.find_one({"_id": object_id})
    if not entry:
        raise HTTPException(status_code=404, detail="Entrada no encontrada")
    
    # 3. Borrar archivo físico si existe
    file_path = entry.get("file_path")
    
    if file_path:
        if "ai_context/" in file_path:
            raise HTTPException(status_code=403, detail="No tienes permiso para borrar contenido que sirve de contexto para la IA.")
        else:
            if os.path.exists(file_path):
                os.remove(file_path)
            else: 
                logger.error(f"Advertencia: Archivo no encontrado en disco: {file_path}")
            
    # 4. Borrar registro en MongoDB
    result = await db.muro.delete_one({"_id": object_id})
    
    if result.deleted_count == 1:
        return {"message": "Entrada y archivo eliminados correctamente"}
    else:
        raise HTTPException(status_code=500, detail="Error al borrar en la base de datos")
    
@app.get("/wall/query/{entry_id}", summary="Consultar contenido específico del muro", description="Consultar contenido específico publicado en el muro.", tags=["Muro"])
async def consultar_contenido(entry_id: str, usuario: str = Depends(get_current_user)):
    try:
        # Buscamos la entrada en MongoDB
        entry = await db.muro.find_one({"_id": ObjectId(entry_id)})
        
        if not entry:
            raise HTTPException(status_code=404, detail="Entrada no encontrada")
        
        # MongoDB devuelve el _id como objeto, lo convertimos a string para el JSON
        entry["_id"] = str(entry["_id"])
        
        return entry
        
    except Exception as e:
        # Esto captura errores de formato si el ID no es un ObjectId válido u otros errores
        raise HTTPException(status_code=400, detail="Formato de ID inválido o error interno")
    
@app.get("/wall/view/{entry_id}", summary="Obtener contenido específico del muro", description="Obtener contenido específico publicado en el muro.", tags=["Muro"])
async def ver_contenido(entry_id: str, usuario: str = Depends(get_current_user)):
    # Validar que el ID sea un formato válido de MongoDB
    try:
        object_id = ObjectId(entry_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Formato de ID inválido")

    # Buscamos la entrada en MongoDB
    entry = await db.muro.find_one({"_id": object_id})
    if not entry or not os.path.exists(entry["file_path"]):
        return {"error": "Archivo no encontrado"}
    
    # Esto intenta adivinar el tipo (ej: image/jpeg, text/plain)
    content_type, _ = mimetypes.guess_type(entry["file_path"])

    logger.debug("content-type: " + str(content_type))

    nombre_archivo = os.path.basename(entry["file_path"])

    if entry["file_path"]:
        if "ai_context/" in entry["file_path"] and "text" in entry["content_type"]:
            raise HTTPException(status_code=403, detail="No tienes permiso para ver el contenido de muestras de datos que sirven de contexto para la IA.")
    
    if content_type == "application/pdf":
        content_disposition_type = "inline"
    else: 
        content_disposition_type = "attachment"
        
    # Devolvemos el archivo directamente
    return FileResponse(entry["file_path"], media_type=content_type, filename=nombre_archivo, content_disposition_type=content_disposition_type)

def obtener_url_grafico_temperaturas(datos_meteo):

    # 1. Extraemos las listas de datos
    fechas = []
    temp_max = []
    temp_min = []
    
    for d in datos_meteo:
        # Convertimos el string a objeto datetime de forma unificada
        dt = datetime.fromisoformat(d['fecha'])
        
        # Formateamos fecha como día-mes-año (ej: 11-06-2026)
        fechas.append(dt.strftime("%d-%m-%Y"))
        
        temp_max.append(d['temp_max'])
        temp_min.append(d['temp_min'])

    # 2. Creamos la configuración para QuickChart
    chart_config = {
        "type": "line",
        "data": {
            "labels": fechas,
            "datasets": [
                {"label": "Temp. Máx (°C)", "data": temp_max, "borderColor": "red", "fill": False},
                {"label": "Temp. Mín (°C)", "data": temp_min, "borderColor": "blue", "fill": False}
            ]
        },
        "options": {
            "title": {"display": True, "text": "Evolución de temperatura en Madrid (7 días)"},
            "scales": {
                "xAxes": [{"scaleLabel": {"display": True, "labelString": "Fecha"}}],
                "yAxes": [{"scaleLabel": {"display": True, "labelString": "Grados Celsius"}}]
            }
        }
    }

    # 3. Codificamos la URL
    json_string = json.dumps(chart_config)
    encoded_url = urllib.parse.quote(json_string)
    return f"https://quickchart.io/chart?c={encoded_url}"

def obtener_url_grafico_aire(datos_aire):
    # Definimos las magnitudes a representar
    magnitudes_objetivo = ["Dioxido de Nitrogeno", "Particulas < 2.5 micras", "Particulas < 10 micras", "Ozono"]
    
    # Estructura: {magnitud: {hora: [lista_valores]}}
    datos_agrupados = {m: {h: [] for h in range(24)} for m in magnitudes_objetivo}

    for d in datos_aire:
        magnitud = d.get('MAGNITUD')
        if magnitud in magnitudes_objetivo:
            try:
                valor = float(d['VALOR'])
                hora = datetime.fromisoformat(d['FECHA_HORA']).hour
                datos_agrupados[magnitud][hora].append(valor)
            except (ValueError, TypeError):
                continue

    # Calcular medias por hora para cada magnitud
    # Si no hay datos en una hora, dejamos None para que el gráfico no los una (o los trate como vacío)
    datasets = []
    colores = ["red", "blue", "green", "black"]
    
    for i, mag in enumerate(magnitudes_objetivo):
        medias_horarias = []
        for h in range(24):
            valores = datos_agrupados[mag][h]
            medias_horarias.append(sum(valores) / len(valores) if valores else None)
        
        datasets.append({
            "label": mag,
            "data": medias_horarias,
            "borderColor": colores[i % len(colores)],
            "fill": False,
            "spanGaps": False # No unir puntos si falta una hora
        })

    chart_config = {
        "type": "line",
        "data": {
            "labels": [f"{h:02d}:00" for h in range(24)],
            "datasets": datasets
        },
        "options": {
            "title": {"display": True, "text": "Evolución Calidad del Aire (24h)"},
            "scales": {
                "yAxes": [{"scaleLabel": {"display": True, "labelString": "Concentración (µg/m³)"}}]
            }
        }
    }

    url = f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}"
    return url

def obtener_urls_graficos_trafico(datos_trafico):
    # Listas para acumular los valores de intensidad de tráfico y velocidad en vía urbanas e interurbanas
    intensidad_urbana = []
    intensidad_interurbana = []
    velocidad_interurbana = []

    for d in datos_trafico:
        # Clasificación basada en VELOCIDAD
        vel = d.get('VELOCIDAD')
        es_urbano = (vel == 'N/A')
        
        # Procesar Intensidad (Solo valores numéricos)
        # Nota: Asumimos que INTENSIDAD puede ser número o string
        try:
            val_int = float(d['INTENSIDAD'])
            if es_urbano:
                intensidad_urbana.append(val_int)
            else:
                # Solo si no es "DESCONOCIDO" o "N/A" (ya filtrado por es_urbano)
                intensidad_interurbana.append(val_int)
        except (ValueError, TypeError):
            pass # Ignoramos valores no numéricos en intensidad

        # Procesar Velocidad (Solo interurbano, excluyendo DESCONOCIDO)
        if not es_urbano and isinstance(vel, (int, float)):
            velocidad_interurbana.append(float(vel))

    # Cálculo de medias
    media_int_urbana = sum(intensidad_urbana) / len(intensidad_urbana) if intensidad_urbana else 0
    media_int_inter = sum(intensidad_interurbana) / len(intensidad_interurbana) if intensidad_interurbana else 0
    media_vel_inter = sum(velocidad_interurbana) / len(velocidad_interurbana) if velocidad_interurbana else 0

    # Configuración de los gráficos
    # Gráfico 1: Intensidad Media (Urbana vs Interurbana)
    chart_int = {
        "type": "bar",
        "data": {
            "labels": ["Intensidad Media"],
            "datasets": [
                {"label": "Urbana (veh/h)", "data": [media_int_urbana], "backgroundColor": "blue"},
                {"label": "Interurbana (veh/h)", "data": [media_int_inter], "backgroundColor": "red"}
            ]
        }
    }

    # Gráfico 2: Velocidad Media Interurbana
    chart_vel = {
        "type": "bar",
        "data": {
            "labels": ["Velocidad"],
            "datasets": [{"label": "Media Interurbana (km/h)", "data": [media_vel_inter], "backgroundColor": "green"}]
        }
    }

    # Generación de URLs
    def gen_url(c): return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(c))}"
    
    return gen_url(chart_int), gen_url(chart_vel)
