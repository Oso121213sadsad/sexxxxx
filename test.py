import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import asyncio
import time
from flask import Flask
import re
import webserver
import aiohttp
from typing import Optional, Dict, List, Union, Tuple, Set
from datetime import datetime, timedelta
from collections import defaultdict, deque
import random
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# ID del servidor autorizado
AUTHORIZED_SERVER_ID = 1322642350216052828

# Canales para historial
HISTORY_CHANNEL_ID = 231313

# ===== SISTEMA ANTI RATE-LIMIT MEJORADO =====
class RateLimitHandler:
    def __init__(self):
        # Usar deque para mejor rendimiento en operaciones de cola
        self.command_timestamps = defaultdict(lambda: deque(maxlen=100))
        self.global_cooldown = 0
        self.backoff_factor = 1.5
        self.max_retry_after = 15  # segundos máximos de espera
        self.recent_commands = defaultdict(int)
        
        # Nuevos parámetros para manejo avanzado de rate limits
        self.bucket_size = 5  # Número máximo de comandos en ventana de tiempo
        self.time_window = 60  # Ventana de tiempo en segundos
        self.command_specific_limits = {
            "promote": 3,  # Máximo 3 promotes en la ventana de tiempo
            "demote": 3,   # Máximo 3 demotes en la ventana de tiempo
            "whitelist": 10 # Máximo 10 whitelist en la ventana de tiempo
        }
        
        # Registro de errores 429 para ajuste dinámico
        self.rate_limit_incidents = deque(maxlen=10)
        self.adaptive_cooldown = False
        
    async def check_rate_limit(self, user_id: int, command_name: str) -> Tuple[bool, float]:
        """Verifica si un comando está en cooldown y aplica backoff exponencial con token bucket"""
        current_time = time.time()
        
        # Limpiar timestamps antiguos (fuera de la ventana de tiempo)
        while self.command_timestamps[user_id] and current_time - self.command_timestamps[user_id][0] > self.time_window:
            self.command_timestamps[user_id].popleft()
        
        # Verificar límite específico del comando
        cmd_limit = self.command_specific_limits.get(command_name, self.bucket_size)
        cmd_count = sum(1 for ts in self.command_timestamps[user_id] 
                        if current_time - ts < self.time_window)
        
        # Si hay demasiados comandos en la ventana de tiempo
        if cmd_count >= cmd_limit:
            # Calcular tiempo de espera basado en la cantidad de comandos y factor de backoff
            wait_time = min(
                (cmd_count - cmd_limit + 1) * self.backoff_factor,
                self.max_retry_after
            )
            
            # Ajuste dinámico si hemos tenido incidentes de rate limit
            if self.adaptive_cooldown and len(self.rate_limit_incidents) > 3:
                wait_time *= 1.5
                
            return False, wait_time
        
        # Verificar cooldown global (para todo el bot)
        if current_time < self.global_cooldown:
            return False, self.global_cooldown - current_time
        
        # Registrar uso del comando
        self.command_timestamps[user_id].append(current_time)
        self.recent_commands[user_id] += 1
        
        # Si hay muchos comandos recientes globales, aplicar cooldown global
        if self.recent_commands[user_id] > 15:  # Aumentado de 10 a 15
            self.global_cooldown = current_time + 3  # Reducido de 5 a 3 segundos
            self.recent_commands[user_id] = 0
            
        return True, 0
    
    def register_rate_limit_incident(self):
        """Registra un incidente de rate limit (error 429) para ajuste dinámico"""
        self.rate_limit_incidents.append(time.time())
        
        # Si hay muchos incidentes recientes, activar modo adaptativo
        if len(self.rate_limit_incidents) >= 3:
            recent_incidents = sum(1 for ts in self.rate_limit_incidents 
                                  if time.time() - ts < 300)  # 5 minutos
            if recent_incidents >= 3:
                self.adaptive_cooldown = True
                self.max_retry_after = 30  # Aumentar tiempo máximo de espera
                self.backoff_factor = 2.0  # Aumentar factor de backoff
            else:
                # Restaurar valores normales si no hay muchos incidentes recientes
                self.adaptive_cooldown = False
                self.max_retry_after = 15
                self.backoff_factor = 1.5

# ===== CACHÉ DE ROLES MEJORADA =====
class RoleCache:
    def __init__(self, roles_hierarchy: Dict):
        self.roles_hierarchy = roles_hierarchy
        self.member_roles_cache = {}
        self.cache_expiry = {}
        self.cache_duration = 300  # 5 minutos
        self.role_objects_cache = {}  # Caché de objetos de rol para evitar búsquedas repetidas
        self.last_cache_cleanup = time.time()
        self.cleanup_interval = 3600  # Limpiar caché cada hora
        
    def invalidate_cache(self, member_id: int):
        """Invalida la caché para un miembro específico"""
        if member_id in self.member_roles_cache:
            del self.member_roles_cache[member_id]
            if member_id in self.cache_expiry:
                del self.cache_expiry[member_id]
    
    def invalidate_all_cache(self):
        """Invalida toda la caché de roles"""
        self.member_roles_cache.clear()
        self.cache_expiry.clear()
        self.role_objects_cache.clear()
    
    def cleanup_cache(self, force=False):
        """Limpia entradas expiradas de la caché"""
        current_time = time.time()
        
        # Solo limpiar si ha pasado el intervalo o si se fuerza
        if force or current_time - self.last_cache_cleanup > self.cleanup_interval:
            expired_members = [
                member_id for member_id, expiry in self.cache_expiry.items()
                if current_time > expiry
            ]
            
            for member_id in expired_members:
                if member_id in self.member_roles_cache:
                    del self.member_roles_cache[member_id]
                if member_id in self.cache_expiry:
                    del self.cache_expiry[member_id]
            
            self.last_cache_cleanup = current_time
    
    def cache_role_object(self, guild: discord.Guild, role_id: int) -> Optional[discord.Role]:
        """Obtiene y cachea un objeto de rol"""
        if role_id in self.role_objects_cache:
            return self.role_objects_cache[role_id]
        
        role = discord.utils.get(guild.roles, id=role_id)
        if role:
            self.role_objects_cache[role_id] = role
        return role
    
    def get_staff_role(self, member: discord.Member) -> Optional[Dict]:
        """Obtiene el rol de staff de un miembro, usando caché si está disponible"""
        current_time = time.time()
        
        # Limpiar caché periódicamente
        self.cleanup_cache()
        
        # Verificar si hay caché válida
        if member.id in self.member_roles_cache and current_time < self.cache_expiry.get(member.id, 0):
            return self.member_roles_cache[member.id]
        
        # Si no hay caché o expiró, calcular el rol
        member_role_ids = [role.id for role in member.roles]
        
        # Crear lista de roles del staff que tiene el miembro
        member_staff_roles = []
        for role_name, role_info in self.roles_hierarchy.items():
            if role_info["role_id"] in member_role_ids:
                member_staff_roles.append({
                    "name": role_name,
                    "id": role_info["role_id"],
                    "index": role_info["index"],
                    "special": role_info.get("special", False)
                })
        
        if not member_staff_roles:
            result = None
        else:
            # Ordenar por índice (menor índice = rol más alto)
            member_staff_roles.sort(key=lambda x: x["index"])
            result = member_staff_roles[0]  # Devolver el rol más alto
        
        # Guardar en caché
        self.member_roles_cache[member.id] = result
        self.cache_expiry[member.id] = current_time + self.cache_duration
        
        return result
    
    def get_all_staff_roles(self, member: discord.Member) -> List[Dict]:
        """Obtiene todos los roles de staff que tiene un miembro"""
        member_role_ids = [role.id for role in member.roles]
        
        # Crear lista de roles del staff que tiene el miembro
        member_staff_roles = []
        for role_name, role_info in self.roles_hierarchy.items():
            if role_info["role_id"] in member_role_ids:
                member_staff_roles.append({
                    "name": role_name,
                    "id": role_info["role_id"],
                    "index": role_info["index"],
                    "special": role_info.get("special", False)
                })
        
        # Ordenar por índice (menor índice = rol más alto)
        member_staff_roles.sort(key=lambda x: x["index"])
        return member_staff_roles

# ===== SISTEMA DE FILTRADO DE WIKIPEDIA =====
class WikipediaFilter:
    def __init__(self):
        self.filtered_categories = {
            "ilegal": set(),
            "inapropiado": set(),
            "cp": set(),
            "narco": set(),
            "gore": set(),
            "porno": set(),
            "webs_ilegales": set(),
            "webs_18plus": set()
        }
        self.load_filtered_words()
        self.session = None
        
    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
        
    def load_filtered_words(self):
        """Carga las palabras filtradas desde archivos"""
        try:
            # Cargar categorías específicas
            for category in self.filtered_categories:
                file_path = f'filtered_{category}.json'
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        self.filtered_categories[category] = set(json.load(f))
        except Exception as e:
            print(f"Error al cargar palabras filtradas: {e}")
            # Inicializar con conjuntos de palabras por defecto
            self._initialize_default_filters()
    
    def _initialize_default_filters(self):
        """Inicializa filtros por defecto si no se pueden cargar los archivos"""
        # Palabras ilegales generales
        self.filtered_categories["ilegal"] = {
            "hackear", "ddos", "doxear", "phishing", "malware", "ransomware",
            "trojan", "botnet", "carding", "fraude", "estafa", "suplantación"
        }
        
        # Palabras inapropiadas
        self.filtered_categories["inapropiado"] = {
            "insulto1", "insulto2", "racista1", "racista2", "discriminación"
        }
        
        # Contenido CP
        self.filtered_categories["cp"] = {
            "cp", "childp", "pedo", "loli", "shota"
        }
        
        # Contenido narco
        self.filtered_categories["narco"] = {
            "drogas", "cocaína", "heroína", "metanfetamina", "narcotráfico"
        }
        
        # Contenido gore
        self.filtered_categories["gore"] = {
            "gore", "sangre", "decapitación", "mutilación", "tortura"
        }
        
        # Contenido pornográfico
        self.filtered_categories["porno"] = {
            "pornografía", "xxx", "nsfw", "hentai", "rule34"
        }
        
        # Webs ilegales
        self.filtered_categories["webs_ilegales"] = {
            "silkroad", "darkweb", "deepweb", "onion"
        }
        
        # Webs +18
        self.filtered_categories["webs_18plus"] = {
            "pornhub", "xvideos", "onlyfans", "brazzers", "xnxx", "youporn",
            "redtube", "xhamster", "spankbang", "tube8", "porntrex", "pornhd",
            "porn", "adult", "xxx", "sex", "nsfw", "hentai", "rule34"
        }
    
    def save_filtered_words(self):
        """Guarda las palabras filtradas en archivos"""
        try:
            # Guardar categorías específicas
            for category, words in self.filtered_categories.items():
                file_path = f'filtered_{category}.json'
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(list(words), f, indent=4)
        except Exception as e:
            print(f"Error al guardar palabras filtradas: {e}")
    
    def add_filtered_word(self, word: str, category: str = None):
        """Añade una palabra al filtro"""
        word = word.lower().strip()
        
        if category and category in self.filtered_categories:
            self.filtered_categories[category].add(word)
            
        self.save_filtered_words()
    
    def remove_filtered_word(self, word: str, category: str = None):
        """Elimina una palabra del filtro"""
        word = word.lower().strip()
        
        if category and category in self.filtered_categories:
            if word in self.filtered_categories[category]:
                self.filtered_categories[category].remove(word)
        
        self.save_filtered_words()
    
    async def check_wikipedia_content(self, query: str) -> Tuple[bool, List[str], List[str]]:
        """
        Verifica si el contenido de Wikipedia contiene palabras filtradas
        Retorna: (contiene_filtradas, palabras_encontradas, categorías_encontradas)
        """
        session = await self.get_session()
        
        try:
            # Buscar en Wikipedia
            search_url = f"https://es.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&format=json"
            async with session.get(search_url) as response:
                if response.status != 200:
                    return False, [], ["Error al buscar en Wikipedia"]
                
                data = await response.json()
                
                if "query" not in data or "search" not in data["query"] or not data["query"]["search"]:
                    return False, [], ["No se encontraron resultados en Wikipedia"]
                
                # Obtener el primer resultado
                page_id = data["query"]["search"][0]["pageid"]
                
                # Obtener el contenido de la página
                content_url = f"https://es.wikipedia.org/w/api.php?action=query&prop=extracts&exintro&explaintext&pageids={page_id}&format=json"
                async with session.get(content_url) as content_response:
                    if content_response.status != 200:
                        return False, [], ["Error al obtener contenido de Wikipedia"]
                    
                    content_data = await content_response.json()
                    
                    if "query" not in content_data or "pages" not in content_data["query"] or str(page_id) not in content_data["query"]["pages"]:
                        return False, [], ["Error al procesar contenido de Wikipedia"]
                    
                    # Obtener el texto de la página
                    page_content = content_data["query"]["pages"][str(page_id)].get("extract", "")
                    
                    # Verificar palabras filtradas
                    found_words = []
                    found_categories = []
                    
                    # Convertir a minúsculas para comparación
                    page_content_lower = page_content.lower()
                    
                    # Verificar cada categoría
                    for category, words in self.filtered_categories.items():
                        for word in words:
                            # Buscar la palabra con límites de palabra
                            pattern = r'\b' + re.escape(word) + r'\b'
                            if re.search(pattern, page_content_lower):
                                found_words.append(word)
                                if category not in found_categories:
                                    found_categories.append(category)
                    
                    return bool(found_words), found_words, found_categories
                    
        except Exception as e:
            print(f"Error al verificar contenido de Wikipedia: {e}")
            return False, [], [f"Error: {str(e)}"]
    
    async def get_safe_wikipedia_content(self, query: str) -> Tuple[str, bool, List[str]]:
        """
        Obtiene contenido seguro de Wikipedia, filtrando palabras inapropiadas
        Retorna: (contenido, es_seguro, categorías_filtradas)
        """
        session = await self.get_session()
        
        try:
            # Buscar en Wikipedia
            search_url = f"https://es.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&format=json"
            async with session.get(search_url) as response:
                if response.status != 200:
                    return "Error al buscar en Wikipedia", False, []
                
                data = await response.json()
                
                if "query" not in data or "search" not in data["query"] or not data["query"]["search"]:
                    return "No se encontraron resultados en Wikipedia", True, []
                
                # Obtener el primer resultado
                page_id = data["query"]["search"][0]["pageid"]
                page_title = data["query"]["search"][0]["title"]
                
                # Obtener el contenido de la página
                content_url = f"https://es.wikipedia.org/w/api.php?action=query&prop=extracts&exintro&explaintext&pageids={page_id}&format=json"
                async with session.get(content_url) as content_response:
                    if content_response.status != 200:
                        return "Error al obtener contenido de Wikipedia", False, []
                    
                    content_data = await content_response.json()
                    
                    if "query" not in content_data or "pages" not in content_data["query"] or str(page_id) not in content_data["query"]["pages"]:
                        return "Error al procesar contenido de Wikipedia", False, []
                    
                    # Obtener el texto de la página
                    page_content = content_data["query"]["pages"][str(page_id)].get("extract", "")
                    
                    # Verificar palabras filtradas
                    found_words = []
                    found_categories = []
                    
                    # Convertir a minúsculas para comparación
                    page_content_lower = page_content.lower()
                    
                    # Verificar cada categoría
                    for category, words in self.filtered_categories.items():
                        for word in words:
                            # Buscar la palabra con límites de palabra
                            pattern = r'\b' + re.escape(word) + r'\b'
                            if re.search(pattern, page_content_lower):
                                found_words.append(word)
                                if category not in found_categories:
                                    found_categories.append(category)
                    
                    # Si se encontraron palabras filtradas, censurar el contenido
                    if found_words:
                        for word in found_words:
                            # Reemplazar la palabra con asteriscos
                            censored_word = '*' * len(word)
                            page_content = re.sub(r'\b' + re.escape(word) + r'\b', censored_word, page_content, flags=re.IGNORECASE)
                        
                        return f"**{page_title}** (Contenido censurado):\n\n{page_content}", False, found_categories
                    
                    return f"**{page_title}**:\n\n{page_content}", True, []
                    
        except Exception as e:
            print(f"Error al obtener contenido de Wikipedia: {e}")
            return f"Error: {str(e)}", False, []
    
    async def close(self):
        """Cierra la sesión HTTP"""
        if self.session and not self.session.closed:
            await self.session.close()

# Configuración del bot
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        self.rate_limiter = RateLimitHandler()
        self.wiki_filter = WikipediaFilter()
        self.command_usage_stats = defaultdict(int)
        self.startup_time = datetime.now()
        
    async def setup_hook(self):
        await self.tree.sync()
        print(f"Comandos slash sincronizados - {datetime.now().strftime('%H:%M:%S')}")
        
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Este comando está en cooldown. Intenta de nuevo en {error.retry_after:.2f} segundos.", delete_after=5)
        elif isinstance(error, discord.errors.HTTPException) and error.status == 429:
            # Registrar incidente de rate limit
            self.rate_limiter.register_rate_limit_incident()
            await ctx.send("El bot está experimentando límites de tasa. Por favor, espera un momento.", delete_after=5)
        else:
            print(f"Error en comando: {error}")
    
    async def close(self):
        """Cierra recursos al cerrar el bot"""
        await self.wiki_filter.close()
        await super().close()

bot = MyBot()

# IDs de usuarios autorizados (solo estos pueden usar comandos de whitelist)
AUTHORIZED_USERS = [
    1351702657865093190,
    754063971799138366,
    1287879729306931331,
    1302078367108956200
]

# ID del rol restringido
RESTRICTED_ROLE_ID = 1329999196555579503

# ID del rol Owner (para verificación de permisos especiales)
OWNER_ROLE_ID = 1325450099077415004 # Reemplaza con tu ID de rol owner

# Archivos para la whitelist
WHITELIST_FILE = 'whitelist.json'

PROMOTE_CHANNEL_ID = 1360101112404902079
DEMOTE_CHANNEL_ID = 1360101062236700702

# URL de imagen para embeds
EMBED_IMAGE_URL = "https://i.pinimg.com/736x/86/9b/56/869b56a0d38bef096f93ae6bd6b80ade.jpg"

# Colores para los embeds
PROMOTE_COLOR = 0x39FF14  # Verde radioactivo
DEMOTE_COLOR = 0xFF073A   # Rojo fosforescente
BLACK_COLOR = 0x000000    # Color negro para los demás embeds

# Emojis personalizados para los embeds
EMOJIS = {
    "user": "<:sexo:1364009872714236006>",
    "role": "<:tester:1364009609039446107>",
    "reason": "<:librin:1364009560167415849>",
    "owner": "<:ownersin:1364009536134058130>"
}

# Definición de los roles y sus siguientes roles con jerarquía clara
roles_hierarchy = {
    "Co owner": {
        "role_id": 1329999164368486424,
        "index": 0,
        "special": True  # Solo el bot y owner pueden asignar
    },
    "Admin Head": {
        "role_id": 1337192894674501632,
        "index": 1
    },
    "Admin": {
        "role_id": 1329999170517598239,
        "index": 2
    },
    "Supervisor": {
        "role_id": 1337192515467739278,
        "index": 3
    },
    "Vxspot Head": {
        "role_id": 1329999165769650186,
        "index": 4
    },
    "Vxspot Staff": {
        "role_id": 1329999166801317908,
        "index": 5
    },
    "Head": {
        "role_id": 1337191398545559563,
        "index": 6
    },
    "Moderator": {
        "role_id": 1329999171851124736,
        "index": 7
    },
    "Trial Mod": {
        "role_id": 1329999176909455403,
        "index": 8
    },
    "Vxspot Helper": {
        "role_id": 1329999174179094590,
        "index": 9
    },
    "Helper": {
        "role_id": 1329999181674184724,
        "index": 10
    },
    "trial helper": {
        "role_id": 1360122561006014616,
        "index": 11
    }
}

# Crear listas ordenadas para facilitar el acceso
ROLES_ORDER = [role_name for role_name, _ in sorted(roles_hierarchy.items(), key=lambda x: x[1]["index"])]
ROLE_IDS = {role_name: role_info["role_id"] for role_name, role_info in roles_hierarchy.items()}

# Inicializar caché de roles
role_cache = RoleCache(roles_hierarchy)

# Función para cargar whitelist
def cargar_whitelist():
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print("El archivo whitelist.json está vacío o mal formado.")
                return []
    return []

# Guardar whitelist
def guardar_whitelist(whitelist):
    with open(WHITELIST_FILE, 'w') as f:
        json.dump(whitelist, f, indent=4)

# Verificar si un usuario tiene el rol restringido
def tiene_rol_restringido(member):
    return any(role.id == RESTRICTED_ROLE_ID for role in member.roles)

# Verificar si un usuario tiene el rol owner
def tiene_rol_owner(member):
    return any(role.id == OWNER_ROLE_ID for role in member.roles)

# Mejorado: Obtener el rol de staff actual del usuario (ahora usa caché)
def obtener_rol_staff_actual(member):
    return role_cache.get_staff_role(member)

# Obtener el siguiente rol en la jerarquía
def get_next_role(current_role_index):
    # Índices menores representan roles más altos en la jerarquía
    if current_role_index > 0:
        next_role_name = ROLES_ORDER[current_role_index - 1]
        next_role_info = roles_hierarchy[next_role_name]
        return {
            "name": next_role_name,
            "id": next_role_info["role_id"],
            "index": next_role_info["index"],
            "special": next_role_info.get("special", False)
        }
    return None

# Obtener el rol inferior
def get_lower_role(current_role_index):
    # Índices mayores representan roles más bajos en la jerarquía
    if current_role_index < len(ROLES_ORDER) - 1:
        lower_role_name = ROLES_ORDER[current_role_index + 1]
        lower_role_info = roles_hierarchy[lower_role_name]
        return {
            "name": lower_role_name,
            "id": lower_role_info["role_id"],
            "index": lower_role_info["index"],
            "special": lower_role_info.get("special", False)
        }
    return None

# FUNCIÓN MEJORADA: Verificación de jerarquía de roles
def puede_modificar_roles(autor, objetivo, accion="modificar"):
    # Verificación rápida para usuarios autorizados
    if autor.id in AUTHORIZED_USERS or autor.guild_permissions.administrator:
        return True, None

    # VERIFICACIÓN GENERAL DE DISCORD: Comparar el rol más alto de cada usuario
    # Esto es crucial para respetar la jerarquía general de Discord
    if objetivo.top_role >= autor.top_role:
        return False, f"No puedes modificar a {objetivo.name} porque tiene un rol ({objetivo.top_role.name}) más alto o igual que el tuyo."

    # Obtener roles de staff específicos
    autor_role = obtener_rol_staff_actual(autor)
    objetivo_role = obtener_rol_staff_actual(objetivo)

    # Si el autor no tiene rol de staff, no puede modificar
    if not autor_role:
        return False, "No tienes un rol de staff para hacer esto."

    # Si el objetivo no tiene rol de staff, verificar según la acción
    if not objetivo_role:
        if accion == "promote":
            # Para promover a alguien sin rol de staff, verificar si el autor puede asignar el rol más bajo
            lowest_role_name = ROLES_ORDER[-1]
            lowest_role_id = roles_hierarchy[lowest_role_name]["role_id"]
            lowest_role = discord.utils.get(autor.guild.roles, id=lowest_role_id)
            
            # Si el rol más bajo es más alto que el rol más alto del autor, denegar
            if lowest_role and lowest_role >= autor.top_role:
                return False, f"No puedes dar el rol {lowest_role.name} porque es de mayor o igual jerarquía que tu rol más alto."
            
            return True, None
        else:
            return True, None  # Otras acciones en usuarios sin rol de staff

    # A partir de aquí, ambos tienen roles de staff

    # Verificar jerarquía basada en el tipo de acción
    if accion == "promote":
        # Verificar si el autor tiene autoridad sobre el objetivo en jerarquía de staff
        if autor_role["index"] >= objetivo_role["index"]:
            return False, f"No puedes promotear a {objetivo.name} porque tiene un rol mayor o igual al tuyo ({objetivo_role['name']})."
        
        # Calcular el siguiente rol en la jerarquía
        next_role_index = objetivo_role["index"] - 1
        if next_role_index >= 0:
            next_role_name = ROLES_ORDER[next_role_index]
            next_role_id = roles_hierarchy[next_role_name]["role_id"]
            next_role = discord.utils.get(autor.guild.roles, id=next_role_id)
            
            # Verificar si el autor puede asignar ese rol específico (jerarquía de Discord)
            if next_role and next_role >= autor.top_role:
                return False, f"No puedes promotear a {objetivo.name} al rol {next_role.name} porque ese rol es de mayor o igual jerarquía que tu rol más alto."
            
            # Verificar si el autor tiene un rol de staff superior al que se va a asignar
            if autor_role["index"] >= next_role_index:
                return False, f"No puedes promotear a {objetivo.name} al rol {next_role_name} porque tu rol de staff ({autor_role['name']}) no es superior."
        
    elif accion == "demote":
        # Verificar si el autor tiene autoridad sobre el objetivo en jerarquía de staff
        if autor_role["index"] >= objetivo_role["index"]:
            return False, f"No puedes demotear a {objetivo.name} <:hijosdelavergaa:1364030086377898064> porque su rol ({objetivo_role['name']}) es mayor o igual al tuyo."
        
        # Calcular el rol inferior en la jerarquía
        lower_role_index = objetivo_role["index"] + 1
        if lower_role_index < len(ROLES_ORDER):
            lower_role_name = ROLES_ORDER[lower_role_index]
            lower_role_id = roles_hierarchy[lower_role_name]["role_id"]
            lower_role = discord.utils.get(autor.guild.roles, id=lower_role_id)
            
            # Para demote, verificar si el autor puede gestionar ambos roles (actual y futuro)
            current_role_obj = discord.utils.get(autor.guild.roles, id=objetivo_role["id"])
            if (current_role_obj and current_role_obj >= autor.top_role) or \
               (lower_role and lower_role >= autor.top_role):
                return False, f"No puedes demotear a {objetivo.name} porque no tienes permisos para gestionar los roles involucrados."
            
            # Verificar si el autor tiene un rol de staff superior al rol inferior
            if autor_role["index"] >= lower_role_index:
                return False, f"No puedes demotear a {objetivo.name} al rol {lower_role_name} porque tu rol de staff ({autor_role['name']}) no es superior."

    return True, None

# Función decoradora para manejar rate limits
def rate_limit_command():
    async def predicate(interaction: discord.Interaction):
        command_name = interaction.command.name
        can_execute, wait_time = await bot.rate_limiter.check_rate_limit(interaction.user.id, command_name)
        
        if not can_execute:
            await interaction.response.send_message(
                f"Espera {wait_time:.1f} segundos antes de usar este comando de nuevo.", 
                ephemeral=True
            )
            return False
        return True

    return app_commands.check(predicate)

# Función decoradora para verificar whitelist
def whitelist_check():
    async def predicate(interaction: discord.Interaction):
        whitelist = cargar_whitelist()
        if interaction.user.id not in whitelist and interaction.user.id not in AUTHORIZED_USERS:
            embed = discord.Embed(
                title="No puedes usar esto chabalin...",
                description="No estás autorizado para usar este comando",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        
        if tiene_rol_restringido(interaction.user):
            embed = discord.Embed(
                title="Rol Restringido",
                description="Tu rol no permite usar este comando.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
            
        return True

    return app_commands.check(predicate)

# de whitelist principal
@bot.tree.command(name="whitelist", description="Añade un usuario y permite que pueda usar el bot en su totalidad")
@app_commands.describe(member="El usuario que quieres añadir a la whitelist")
@rate_limit_command()
async def auth_whitelist(interaction: discord.Interaction, member: discord.Member):
    if interaction.user.id not in AUTHORIZED_USERS:
        embed = discord.Embed(
            title="Uhhh, no tienes permisos para usar esto chabalin...",
            description="No estás autorizado para usar este comando Solo los usuarios auth pueden modificar la whitelist",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    if tiene_rol_restringido(interaction.user):
        embed = discord.Embed(
            title="Rol Restringido",
            description="Tu rol no permite usar este comando.",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    whitelist = cargar_whitelist()
    if member.id not in whitelist:
        whitelist.append(member.id)
        guardar_whitelist(whitelist)
        
        embed = discord.Embed(
            title="Usuario agregado a Whitelist",
            description=f"{member.mention} ha sido agregado a la whitelist <a:Aeo_Purplefire:1324880686733332630>",
            color=BLACK_COLOR
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        embed = discord.Embed(
            title="Usuario ya en Whitelist",
            description=f"{member.mention} ya está en la whitelist.",
            color=BLACK_COLOR
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="promote", description="Asigna un promote a un staff")
@app_commands.describe(
    member="El usuario al que quieres promotear",
    reason="Razón del promote"
)
@whitelist_check()
@rate_limit_command()
async def promote(interaction: discord.Interaction, member: discord.Member, reason: str):
    # Código existente...
    
    # Enviar embed al canal de promotes
    promote_channel = bot.get_channel(PROMOTE_CHANNEL_ID)
    if promote_channel:
        await promote_channel.send(embed=embed)

    # Obtener el rol actual del usuario con detección mejorada
    current_role_info = obtener_rol_staff_actual(member)

    # Determinar el siguiente rol en la jerarquía
    if current_role_info:
        next_role_info = get_next_role(current_role_info["index"])
        
        if not next_role_info:
            embed = discord.Embed(
                title="Promote no posible",
                description=f"{member.mention} ya tiene el rol más alto en el orden de roles staff.",
                color=discord.Color.yellow()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
            
        # Obtener objetos de roles para quitar y añadir
        current_role_obj = discord.utils.get(interaction.guild.roles, id=current_role_info["id"])
        next_role_obj = discord.utils.get(interaction.guild.roles, id=next_role_info["id"])
        
        if not current_role_obj or not next_role_obj:
            embed = discord.Embed(
                title="Error de Rol",
                description="No se pudieron encontrar los roles necesarios",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
            
        # Realizar cambios de roles
        try:
            # Primero quitar el rol actual
            await member.remove_roles(current_role_obj, reason=f"Promovido por {interaction.user}")
            # Luego añadir el nuevo rol
            await member.add_roles(next_role_obj, reason=f"Promovido por {interaction.user}")
            
            # Invalidar caché para este miembro
            role_cache.invalidate_cache(member.id)
            
            # Crear embed para promoción
            embed = discord.Embed(
                title="<a:ButterfliesBlue:1364145802636951612> Promote",
                description=f"{EMOJIS['user']} User: {member.mention}\n"
                            f"{EMOJIS['role']} Rango anterior: <@&{current_role_info['id']}>\n"
                            f"{EMOJIS['role']} Nuevo rango: <@&{next_role_info['id']}>\n"
                            f"{EMOJIS['reason']} Razón: {reason}\n"
                            f"{EMOJIS['owner']} Owner: {interaction.user.mention}",
                color=PROMOTE_COLOR,
                timestamp=datetime.now()
            )
            embed.set_image(url=EMBED_IMAGE_URL)
            embed.set_footer(text=f"Staff • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
            
            # Enviar mensaje efímero al usuario que ejecutó el comando
            await interaction.response.send_message(
                f"**{member.display_name}** ha sido promoteado  de **{current_role_info['name']}** a **{next_role_info['name']}**", 
                ephemeral=True
            )
            
            # Enviar embed al canal de historial
            history_channel = bot.get_channel(PROMOTE_CHANNEL_ID)
            if history_channel:
                await history_channel.send(embed=embed)
                
        except Exception as e:
            embed = discord.Embed(
                title="Error al Pomotear",
                description=f"Error al modificar roles: {str(e)}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        # Si el usuario no tiene rol de staff, asignarle el rol más bajo
        lowest_role_name = ROLES_ORDER[-1]
        lowest_role_id = roles_hierarchy[lowest_role_name]["role_id"]
        lowest_role = discord.utils.get(interaction.guild.roles, id=lowest_role_id)
        
        if not lowest_role:
            embed = discord.Embed(
                title="Error de Rol",
                description="No se pudo encontrar el rol más bajo en el orden",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
            
        try:
            # Asignar el rol más bajo
            await member.add_roles(lowest_role, reason=f"Añadido al staff team por {interaction.user}")
            
            # Invalidar caché para este miembro
            role_cache.invalidate_cache(member.id)
            
            # Crear embed para promoción
            embed = discord.Embed(
                title="<a:ButterfliesGreen:1364019553834795008> Promote",
                description=f"{EMOJIS['user']} User: {member.mention}\n"
                            f"{EMOJIS['role']} Rango anterior: Ninguno\n"
                            f"{EMOJIS['role']} Nuevo rango: <@&{lowest_role_id}>\n"
                            f"{EMOJIS['reason']} Razón: {reason}\n"
                            f"{EMOJIS['owner']} Owner: {interaction.user.mention}",
                color=PROMOTE_COLOR,
                timestamp=datetime.now()
            )
            embed.set_image(url=EMBED_IMAGE_URL)
            embed.set_footer(text=f"Staff • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
            
            # Enviar mensaje efímero al usuario que ejecutó el comando
            await interaction.response.send_message(
                    f"**{member.display_name}** ha sido añadido como **{lowest_role_name}**", 
                ephemeral=True
            )
            
            # Enviar embed al canal de historial
            history_channel = bot.get_channel(PROMOTE_CHANNEL_ID)
            if history_channel:
                await history_channel.send(embed=embed)
                
        except Exception as e:
            embed = discord.Embed(
                title="Error al Promotear",
                description=f"Error al dar rol: {str(e)}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="demote", description="Baja de rol a un usuario")
@app_commands.describe(
    member="El usuario al que quieres demotear",
    reason="Razón del demote"
)
@whitelist_check()
@rate_limit_command()
async def demote(interaction: discord.Interaction, member: discord.Member, reason: str):
    # Verificación para usuarios autorizados
    if interaction.user.id in AUTHORIZED_USERS or interaction.user.guild_permissions.administrator:
        puede_modificar = True
        mensaje_error = None
    else:
        # Verificar jerarquía de roles
        puede_modificar, mensaje_error = puede_modificar_roles(interaction.user, member, "demote")

    if not puede_modificar:
        embed = discord.Embed(
            title="Error de orden",
            description=mensaje_error,
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # Obtener el rol actual del usuario con detección mejorada
    current_role_info = obtener_rol_staff_actual(member)

    if not current_role_info:
        embed = discord.Embed(
            title="No tiene rol para demotear",
            description=f"El usuario {member.mention} no tiene un rol  para ser demoteado.",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    try:
        # Obtener el rol inferior en la jerarquía
        lower_role_info = get_lower_role(current_role_info["index"])
        
        # Obtener objetos de roles para quitar
        current_role_obj = discord.utils.get(interaction.guild.roles, id=current_role_info["id"])
        
        if not current_role_obj:
            embed = discord.Embed(
                title="Error de Rol",
                description="No se pudo encontrar el rol del usuario",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        if not lower_role_info:
            # Si es el rol más bajo, eliminarlo completamente
            await member.remove_roles(current_role_obj, reason=f"Demoteado por {interaction.user}")
            
            # Invalidar caché para este miembro
            role_cache.invalidate_cache(member.id)
            
            # Crear embed para demote
            embed = discord.Embed(
                title="<a:ButterfliesRed:1364019555654963332> Demote",
                description=f"{EMOJIS['user']} User: {member.mention}\n"
                            f"{EMOJIS['role']} Rango anterior: <@&{current_role_info['id']}>\n"
                            f"{EMOJIS['role']} Nuevo rango: **Ninguno**\n"
                            f"{EMOJIS['reason']} Razón: {reason}\n"
                            f"{EMOJIS['owner']} Owner: {interaction.user.mention}",
                color=DEMOTE_COLOR,
                timestamp=datetime.now()
            )

            embed.set_image(url=EMBED_IMAGE_URL)
            embed.set_footer(text=f"Staff • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
            
            # Enviar mensaje efímero al usuario que ejecutó el comando
            await interaction.response.send_message(
                f"**{member.display_name}** ha sido demoteado de **{current_role_info['name']}** a **Ninguno**", 
                ephemeral=True
            )
            
            # Enviar embed al canal de historial
            history_channel = bot.get_channel(DEMOTE_CHANNEL_ID)
            if history_channel:
                await history_channel.send(embed=embed)
                
            return
        
        # Obtener objeto del rol inferior
        lower_role = discord.utils.get(interaction.guild.roles, id=lower_role_info["id"])

        if not lower_role:
            embed = discord.Embed(
                title="Error de Rol",
                description="No se pudo encontrar el rol inferior necesario",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # Primero, eliminar el rol actual
        await member.remove_roles(current_role_obj, reason=f"Demoteado por {interaction.user}")
        
        # Luego, asignar el rol inferior
        await member.add_roles(lower_role, reason=f"Demoteado por {interaction.user}")
        
        # Invalidar caché para este miembro
        role_cache.invalidate_cache(member.id)
        
        # Crear embed para demote
        embed = discord.Embed(
            title="<a:ButterfliesRed:1364019555654963332> Demote",
            description=f"{EMOJIS['user']} User: {member.mention}\n"
                        f"{EMOJIS['role']} Rango anterior: <@&{current_role_info['id']}>\n"
                        f"{EMOJIS['role']} Nuevo rango: <@&{lower_role_info['id']}>\n"
                        f"{EMOJIS['reason']} Razón: {reason}\n"
                        f"{EMOJIS['owner']} Owner: {interaction.user.mention}",
            color=DEMOTE_COLOR,
            timestamp=datetime.now()
        )
        embed.set_image(url=EMBED_IMAGE_URL)
        embed.set_footer(text=f"Staff • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        
        # Enviar mensaje efímero al usuario que ejecutó el comando
        await interaction.response.send_message(
            f"**{member.display_name}** ha sido demoteado de **{current_role_info['name']}** a **{lower_role_info['name']}**", 
            ephemeral=True
        )
        
        # Enviar embed al canal de historial
        history_channel = bot.get_channel(DEMOTE_CHANNEL_ID)
        if history_channel:
            await history_channel.send(embed=embed)
            
    except Exception as e:
        embed = discord.Embed(
            title="Error al Demotear",
            description=f"Error al modificar roles: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="removerstaff", description="Elimina todos los roles de staff de un usuario")
@app_commands.describe(
    member="El usuario al que quieres quitarle los roles de staff",
    reason="Razón para quitar roles"
)
@whitelist_check()
@rate_limit_command()
async def removerstaff(interaction: discord.Interaction, member: discord.Member, reason: str):
    # Verificar jerarquía de roles
    puede_modificar, mensaje_error = puede_modificar_roles(interaction.user, member)
    if not puede_modificar:
        embed = discord.Embed(
            title="Error de Orden de roles",
            description=mensaje_error,
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # Obtener todos los roles de staff que tiene el usuario
    roles_a_quitar = []
    roles_names = []
    member_role_ids = [role.id for role in member.roles]

    # Añadir roles de la jerarquía
    for role_name, role_info in roles_hierarchy.items():
        if role_info["role_id"] in member_role_ids:
            role_obj = discord.utils.get(interaction.guild.roles, id=role_info["role_id"])
            if role_obj:
                roles_a_quitar.append(role_obj)
                roles_names.append(role_name)

    # Añadir el rol específico 1329999197340045344 si lo tiene
    special_role = discord.utils.get(interaction.guild.roles, id=1329999197340045344)
    if special_role and special_role in member.roles:
        roles_a_quitar.append(special_role)
        roles_names.append(special_role.name)

    if not roles_a_quitar:
        embed = discord.Embed(
            title="Sin Roles de Staff",
            description=f"{member.mention} no tiene ningún rol de staff para quitarselo.",
            color=BLACK_COLOR
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # Obtener el rol más alto que tenía para el historial
    current_role_info = obtener_rol_staff_actual(member)

    try:
        await member.remove_roles(*roles_a_quitar, reason=f"Roles eliminados por {interaction.user}")
        
        # Invalidar caché para este miembro
        role_cache.invalidate_cache(member.id)
        
        # Enviar mensaje efímero al usuario que ejecutó el comando
        await interaction.response.send_message(
            f"**{member.display_name}** ha sido retirado del staff, roles quitados F chabal: **{', '.join(roles_names)}**", 
            ephemeral=False
        )
        
    except Exception as e:
        embed = discord.Embed(
            title="Error al quitar Roles <:MC:1337508350623223962>",
            description=f"Error: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="historial", description="Muestra el historial de cambios de roles")
@whitelist_check()
@rate_limit_command()
async def historial(interaction: discord.Interaction):
    # Obtener mensajes del canal de historial
    history_channel = bot.get_channel(HISTORY_CHANNEL_ID)
    if not history_channel:
        embed = discord.Embed(
            title="Error",
            description="No se pudo encontrar el canal de logs.",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # Recopilar los últimos 100 mensajes con embeds del historial
    historial_mensajes = []
    async for message in history_channel.history(limit=100):
        if message.embeds:
            historial_mensajes.append(message.embeds[0])

    # Si no hay mensajes, mostrar mensaje de error
    if not historial_mensajes:
        embed = discord.Embed(
            title="Uh, historial vacio",
            description="No hay logs XD",
            color=BLACK_COLOR
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # Dividir en páginas (10 entradas por página)
    paginas = [historial_mensajes[i:i+10] for i in range(0, len(historial_mensajes), 10)]
    total_paginas = len(paginas)
    pagina_actual = 0

    # Crear vista para botones de navegación
    class HistorialView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            
        @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary, disabled=True)
        async def anterior_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            nonlocal pagina_actual
            if pagina_actual > 0:
                pagina_actual -= 1
                # Actualizar estado de botones
                for child in self.children:
                    if child.label == "Anterior":
                        child.disabled = pagina_actual == 0
                    elif child.label == "Siguiente":
                        child.disabled = pagina_actual == total_paginas - 1
                
                await button_interaction.response.edit_message(
                    embed=crear_embed_pagina(pagina_actual), 
                    view=self
                )
            
        @discord.ui.button(label="Siguiente", style=discord.ButtonStyle.secondary, disabled=(total_paginas <= 1))
        async def siguiente_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            nonlocal pagina_actual
            if pagina_actual < total_paginas - 1:
                pagina_actual += 1
                # Actualizar estado de botones
                for child in self.children:
                    if child.label == "Anterior":
                        child.disabled = pagina_actual == 0
                    elif child.label == "Siguiente":
                        child.disabled = pagina_actual == total_paginas - 1
                
                await button_interaction.response.edit_message(
                    embed=crear_embed_pagina(pagina_actual), 
                    view=self
                )

    # Función para crear el embed de la página actual
    def crear_embed_pagina(pagina_num):
        embed = discord.Embed(
            title="Logs de Cambios de Roles",
            description=f"Página {pagina_num + 1}/{total_paginas}",
            color=BLACK_COLOR
        )
        
        for i, item_embed in enumerate(paginas[pagina_num], 1):
            # Usar la descripción del embed original con menciones de roles
            embed.add_field(
                name=f"Registro {i + pagina_num*10}",
                value=item_embed.description,
                inline=False
            )
            
        embed.set_footer(text=f"Staff • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        return embed

    # Enviar primer página
    view = HistorialView()
    await interaction.response.send_message(embed=crear_embed_pagina(0), view=view)

@bot.tree.command(name="roles", description="Muestra todos los roles de staff y su orden")
@whitelist_check()
@rate_limit_command()
async def roles_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Roles de Staff <a:minecraft_xp_orb:1346322830341574697>",
        description="Lista de roles ordenados de menor a mayor:",
        color=BLACK_COLOR
    )

    for i, role_name in enumerate(ROLES_ORDER, 1):
        role_id = roles_hierarchy[role_name]["role_id"]
        embed.add_field(
            name=f"{i}. {role_name}",
            value=f"<@&{role_id}>",
            inline=False
        )

    embed.set_footer(text=f"Staff • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="checkstaff", description="Ve el rol de staff de un usuario")
@app_commands.describe(member="El usuario que quieres ver")
@rate_limit_command()
async def checkstaff(interaction: discord.Interaction, member: discord.Member = None):
    if interaction.user.id not in cargar_whitelist() and interaction.user.id not in AUTHORIZED_USERS:
        embed = discord.Embed(
            title="No puedes usar esto chabalin...",
            description="No tienes permisos para usar este comando.",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # Si no se especifica un miembro, usar el autor del comando
    if member is None:
        member = interaction.user

    # Obtener el rol de staff actual
    current_role_info = obtener_rol_staff_actual(member)

    embed = discord.Embed(
        title="Información de Staff",
        color=BLACK_COLOR
    )

    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)

    if current_role_info:
        # Si tiene rol de staff, mostrar información detallada
        role_obj = discord.utils.get(interaction.guild.roles, id=current_role_info["id"])
        
        embed.description = f"**Usuario:** {member.mention}\n**Rol de Staff:** <@&{current_role_info['id']}>\n**Top en el orden:** #{current_role_info['index'] + 1}"
        
        # Mostrar rol superior (si existe)
        if current_role_info["index"] > 0:
            superior_role = ROLES_ORDER[current_role_info["index"] - 1]
            superior_id = roles_hierarchy[superior_role]["role_id"]
            embed.add_field(
                name="Rol Superior",
                value=f"<@&{superior_id}>",
                inline=True
            )
        else:
            embed.add_field(
                name="Rol Superior",
                value="Ninguno (es el rol más alto)",
                inline=True
            )
        
        # Mostrar rol inferior (si existe)
        if current_role_info["index"] < len(ROLES_ORDER) - 1:
            inferior_role = ROLES_ORDER[current_role_info["index"] + 1]
            inferior_id = roles_hierarchy[inferior_role]["role_id"]
            embed.add_field(
                name="Rol Inferior",
                value=f"<@&{inferior_id}>",
                inline=True
            )
        else:
            embed.add_field(
                name="Rol Inferior",
                value="Ninguno (es el rol más bajo)",
                inline=True
            )
        
        # Verificar si el miembro puede ser promoteado o demoteado por el autor
        puede_modificar, _ = puede_modificar_roles(interaction.user, member)
        
        estado = "**Puedes mover sus roles**" if puede_modificar else "**No puedes mover sus roles**"
        embed.add_field(
            name="Estado",
            value=estado,
            inline=False
        )
        
    else:
        embed.description = f"**Usuario:** {member.mention}\n**No tiene roles de staff**"
        
        # Ver si podría asignarle un rol
        autor_role = obtener_rol_staff_actual(interaction.user)
        if autor_role:
            embed.add_field(
                name="Estado",
                value="**Puedes asignarle un rol de staff**",
                inline=False
            )
        else:
            embed.add_field(
                name="Estado",
                value="**No puedes darle roles (No tienes rol staff)**",
                inline=False
            )

    embed.set_footer(text=f"Staff • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ayuda", description="Muestra los comandos del bot ")
@rate_limit_command()
async def ayuda(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Comandos del Bot ",
        description="Aquí tienes los comandos chabal:",
        color=discord.Color.from_str("#0d0808")
    )

    # Comandos para todos los usuarios
    embed.add_field(
        name="Comandos <a:GhibliCash:1364425647123861656>",
        value="- **/ayuda**: Muestra este cmd XD\n"
              "- **/checkstaff** : Verifica el rol de staff de un member\n"
              "- **/ping**: Muestra la latencia del bot\n"
              "- **/wiki** : Busca información en Wikipedia",
        inline=False
    )

    # Comandos para usuarios en whitelist
    embed.add_field(
        name="Comandos para users en whitelist <:star:1337996809737474048>",
        value="- **/promote**: Promotea un miembro del staff a un rol mas arriba\n"
              "- **/demote**: baja de rol a un user staff\n"
              "- **/removerstaff**: Elimina todos los roles de staff a el user puesto\n"
              "- **/roles**: Muestra el orden de roles de el staff\n"
              "- **/historial**: Muestra el log de cambios de roles\n"
              "- **/roleadd**: Da un rol a un usuario",
        inline=False
    )

    # Notificar si el usuario está autorizado o no
    whitelist = cargar_whitelist()
    if interaction.user.id in whitelist or interaction.user.id in AUTHORIZED_USERS:
        embed.set_footer(text="Tienes acceso a comandos de staff ")
    else:
        embed.set_footer(text="Solo tienes acceso a comandos para members <:angel_wing:1364031541608579196>")

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="roleadd", description="Da un rol a un usuario")
@app_commands.describe(
    member="El usuario al que quieres dar el rol",
    rol="El rol que quieres dar",
    reason="motivo para darlo"
)
@rate_limit_command()
async def roleadd(
    interaction: discord.Interaction, 
    member: discord.Member, 
    rol: discord.Role, 
    reason: Optional[str] = "No seleccionado"
):
    # Verificar si el usuario tiene permisos para gestionar roles
    if not interaction.user.guild_permissions.manage_roles and interaction.user.id not in AUTHORIZED_USERS:
        return await interaction.response.send_message(
            "No tienes perms para dar roles.",
            ephemeral=True
        )

    # Verificar que el rol a dar no sea igual o superior al del autor
    if rol >= interaction.user.top_role and not interaction.user.guild_permissions.administrator and interaction.user.id not in AUTHORIZED_USERS:
        return await interaction.response.send_message(
            "No puedes dar un rol igual o mas arriba del tuyo <:hijosdelavergaa:1364030086377898064>.",
            ephemeral=True
        )

    try:
        await member.add_roles(rol, reason=f"Otorgado por {interaction.user}: {reason}")

        embed = discord.Embed(
            title="Rol dado jijij ",
            description=f"{member.mention} ha recibido el rol {rol.mention}\nRazón de: {reason}",
            color=BLACK_COLOR
        )
        await interaction.response.send_message(embed=embed)

    except discord.Forbidden:
        embed = discord.Embed(
            title="Error al dar rol",
            description="No tengo perms para dar este rol.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException as e:
        embed = discord.Embed(
            title="Error al dar rol",
            description=f"Ocurrió un error al dar el rol chabalin: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Muestra la latencia del bot")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)

    # Determinar el estado basado en la latencia
    if latency < 100:
        status = "guud"
        color = discord.Color.green()
    elif latency < 200:
        status = "Buena"
        color = discord.Color.blue()
    elif latency < 400:
        status = "maso"
        color = discord.Color.gold()
    else:
        status = "Mala"
        color = discord.Color.red()

        embed = discord.Embed(
    title="Pong, jeje me pase",
    description=f"**Latencia  <:hardcore:1364422138320125953>:** {latency}ms\n**Estado:** {status}",
    color=discord.Color.from_rgb(15, 15, 15)
)

        embed.set_footer(text=f"Hora actual • {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")


    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wiki", description="Busca información en Wikipedia")
@app_commands.describe(consulta="Lo que quieres buscar en Wikipedia")
async def wiki(interaction: discord.Interaction, consulta: str):
    await interaction.response.defer(thinking=True)

    try:
        # Verificar si el contenido es apropiado
        tiene_filtradas, palabras_encontradas, categorias_encontradas = await bot.wiki_filter.check_wikipedia_content(consulta)
        
        if tiene_filtradas:
            embed = discord.Embed(
                title="Contenido Inapropiado Detectado",
                description=f"Lo que buscaste '{consulta}' tiene contenido en contra de las reglas del server.",
                color=discord.Color.red()
            )
            embed.add_field(
                name="Categorías detectadas",
                value=", ".join(categorias_encontradas),
                inline=False
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)
        
        # Obtener contenido seguro
        contenido, es_seguro, categorias_filtradas = await bot.wiki_filter.get_safe_wikipedia_content(consulta)
        
        if es_seguro:
            # Limitar el contenido a 4000 caracteres para evitar mensajes demasiado largos
            if len(contenido) > 4000:
                contenido = contenido[:3997] + "..."
            
            embed = discord.Embed(
                title=f"Resultados para: {consulta}",
                description=contenido,
                color=BLACK_COLOR
            )
            embed.set_footer(text="Fuente: Wikipedia")
            
            await interaction.followup.send(embed=embed)
        else:
            if categorias_filtradas:
                embed = discord.Embed(
                    title=f"Resultados para: {consulta} (Contenido Filtrado)",
                    description="El contenido ha sido filtrado debido a temas mas legales y reglas del server.",
                    color=BLACK_COLOR
                )
                embed.add_field(
                    name="que",
                    value=", ".join(categorias_filtradas),
                    inline=False
                )
                embed.add_field(
                    name="Contenido no permitido",
                    value=contenido[:4000] if len(contenido) > 4000 else contenido,
                    inline=False
                )
                embed.set_footer(text="Fuente: Wikipedia")
                
                await interaction.followup.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="Error en la búsqueda",
                    description=contenido,
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)

    except Exception as e:
        embed = discord.Embed(
            title="Error al buscar en Wikipedia",
            description=f"Ocurrió un error XDDDD: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)

@bot.event
async def on_ready():
    print(f'Bot iniciado como {bot.user}')
    await bot.change_presence(status=discord.Status.do_not_disturb)

webserver.keep_alive()
bot.run(DISCORD_TOKEN)
