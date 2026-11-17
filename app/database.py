import json
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional

with open('config.json', 'r', encoding='utf-8') as f:
    config_data = json.load(f)

mongodb_credentials = config_data['database']['user'] + ":" + config_data['database']['password']
mongodb_user = config_data['database']['user']
mongodb_endpoint = config_data['database']['ip'] + ":" + config_data['database']['port']

# Conexión a MongoDB
MONGO_URL = f"mongodb://{mongodb_credentials}@{mongodb_endpoint}/?authSource=admin"
client = AsyncIOMotorClient(MONGO_URL)
db = client.ping_db

# Modelo Pydantic para el muro
class WallEntry(BaseModel):
    content_type: str  # text, image, audio, pcap, pdf, ...
    description: Optional[str] = None
    file_path: str
    timestamp: datetime = datetime.now()

# Almacenar contenido en el muro
async def guardar_en_muro(entry: WallEntry):
    return await db.muro.insert_one(entry.model_dump())

# Consultar contenido disponible en el muro
async def obtener_entradas_muro(filtro: dict = None, limit: int = 10):
    query = filtro if filtro is not None else {}
    cursor = db.muro.find(query).sort("timestamp", -1).limit(limit)
    return [doc async for doc in cursor]

# Modelo Pydantic para el usuario
class User(BaseModel):
    username: str
    hashed_password: str

# Obtener usuario por nombre de usuario
async def get_user_from_db(username: str):
    user_doc = await db.usuarios.find_one({"username": username})
    return user_doc

# Guardar nuevo usuario
async def create_user_in_db(user: User):
    return await db.usuarios.insert_one(user.model_dump())
