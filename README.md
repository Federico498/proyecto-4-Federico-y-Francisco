
import json
import sqlite3
import heapq
import datetime
import os
import threading
import asyncio
import queue
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# Intenta importar el websockets
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except Exception:
    WEBSOCKETS_AVAILABLE = False

DB_FILE = "correo.db"
DEFAULT_WS_HOST = "localhost"
DEFAULT_WS_PORT = 8765

# ==========================
# Modelos
# ==========================
class Usuario:
    def __init__(self, id_usuario, nombre, correo, contraseña):
        self.id_usuario = id_usuario
        self.nombre = nombre
        self.correo = correo
        self.contraseña = contraseña

    def __str__(self):
        return f"Usuario {self.nombre} ({self.correo})"


class Mensaje:
    def __init__(self, id_mensaje, asunto, cuerpo, remitente_id, destinatario_id, fecha_envio=None, metadata=None, prioridad=5):
        self.id_mensaje = id_mensaje
        self.asunto = asunto
        self.cuerpo = cuerpo
        self.remitente_id = remitente_id
        self.destinatario_id = destinatario_id
        self.fecha_envio = fecha_envio or datetime.datetime.now().isoformat()
        self.metadata = metadata or {}
        self.prioridad = prioridad

    def to_json(self):
        return json.dumps({
            "id_mensaje": self.id_mensaje,
            "asunto": self.asunto,
            "cuerpo": self.cuerpo,
            "remitente_id": self.remitente_id,
            "destinatario_id": self.destinatario_id,
            "fecha_envio": self.fecha_envio,
            "metadata": self.metadata,
            "prioridad": self.prioridad
        }, ensure_ascii=False)

    def from_row(row):
        id_db = row[0]
        remitente_id = row[1]
        destinatario_id = row[2]
        asunto = row[3]
        cuerpo_json = row[4]
        fecha = row[5]
        prioridad = row[6] if len(row) > 6 and row[6] is not None else 5
        try:
            parsed = json.loads(cuerpo_json)
            cuerpo = parsed.get("cuerpo") if isinstance(parsed, dict) else cuerpo_json
            metadata = parsed.get("metadata", {}) if isinstance(parsed, dict) else {}
        except Exception:
            cuerpo = cuerpo_json
            metadata = {}
        return Mensaje(id_db, asunto, cuerpo, remitente_id, destinatario_id, fecha, metadata, prioridad)

    def __str__(self):
        return f"[{self.asunto}] De: {self.remitente_id} → {self.destinatario_id} ({self.fecha_envio})"


# ==========================
# Base de datos (SQLite)
# ==========================
class BaseDatos:
    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        first_time = not os.path.exists(self.db_file)
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self._crear_tablas()

    def _crear_tablas(self):
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                correo TEXT UNIQUE NOT NULL,
                contraseña TEXT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                remitente_id INTEGER NOT NULL,
                destinatario_id INTEGER NOT NULL,
                asunto TEXT,
                cuerpo_json TEXT,
                fecha_envio TEXT,
                prioridad INTEGER DEFAULT 5,
                eliminado_en TEXT,
                procesado_prioridad INTEGER DEFAULT 0,
                FOREIGN KEY(remitente_id) REFERENCES usuarios(id),
                FOREIGN KEY(destinatario_id) REFERENCES usuarios(id)
            )
        """)
        # salvaguardias para bases de datos 
        try:
            c.execute("ALTER TABLE mensajes ADD COLUMN eliminado_en TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE mensajes ADD COLUMN procesado_prioridad INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        self.conn.commit()

    # Usuarios
    def crear_usuario(self, nombre, correo, contraseña):
        c = self.conn.cursor()
        try:
            c.execute("INSERT INTO usuarios (nombre, correo, contraseña) VALUES (?, ?, ?)", (nombre, correo, contraseña))
            self.conn.commit()
            return c.lastrowid
        except sqlite3.IntegrityError:
            return None

    def obtener_usuario_por_id(self, uid):
        c = self.conn.cursor()
        c.execute("SELECT id, nombre, correo, contraseña FROM usuarios WHERE id = ?", (uid,))
        row = c.fetchone()
        if not row:
            return None
        return Usuario(row[0], row[1], row[2], row[3])

    def obtener_usuario_por_correo(self, correo):
        c = self.conn.cursor()
        c.execute("SELECT id, nombre, correo, contraseña FROM usuarios WHERE correo = ?", (correo,))
        row = c.fetchone()
        if not row:
            return None
        return Usuario(row[0], row[1], row[2], row[3])

    def listar_usuarios(self):
        c = self.conn.cursor()
        c.execute("SELECT id, nombre, correo, contraseña FROM usuarios")
        return [Usuario(r[0], r[1], r[2], r[3]) for r in c.fetchall()]

    # Mensajes
    def guardar_mensaje(self, mensaje: Mensaje, prioridad:int=5):
        c = self.conn.cursor()
        cuerpo = json.dumps({"cuerpo": mensaje.cuerpo, "metadata": mensaje.metadata}, ensure_ascii=False)
        c.execute(
            "INSERT INTO mensajes (remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad) VALUES (?, ?, ?, ?, ?, ?)",
            (mensaje.remitente_id, mensaje.destinatario_id, mensaje.asunto, cuerpo, mensaje.fecha_envio, prioridad)
        )
        self.conn.commit()
        return c.lastrowid

    def obtener_mensajes_para_usuario(self, uid):
        try:
            self.limpiar_papelera()
        except Exception:
            pass

        c = self.conn.cursor()
        c.execute("""
            SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad
            FROM mensajes
            WHERE destinatario_id = ? AND eliminado_en IS NULL AND (procesado_prioridad IS NULL OR procesado_prioridad = 0)
            ORDER BY fecha_envio DESC
        """, (uid,))
        rows = c.fetchall()
        resultado = []
        for r in rows:
            resultado.append(Mensaje.from_row(r))
        return resultado

    def buscar_mensajes(self, uid, criterio, valor):
        try:
            self.limpiar_papelera()
        except Exception:
            pass

        c = self.conn.cursor()
        if criterio == "asunto":
            c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE destinatario_id = ? AND eliminado_en IS NULL AND (procesado_prioridad IS NULL OR procesado_prioridad = 0) AND asunto LIKE ? ORDER BY fecha_envio DESC", (uid, f"%{valor}%"))
        else:
            c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE destinatario_id = ? AND eliminado_en IS NULL AND (procesado_prioridad IS NULL OR procesado_prioridad = 0) ORDER BY fecha_envio DESC", (uid,))
        rows = c.fetchall()
        resultado = []
        for r in rows:
            resultado.append(Mensaje.from_row(r))
        return resultado

    def obtener_mensajes_prioritarios(self, uid=None):
        c = self.conn.cursor()
        if uid is None:
            c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE procesado_prioridad = 1 ORDER BY prioridad ASC, fecha_envio DESC")
        else:
            c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE destinatario_id = ? AND procesado_prioridad = 1 ORDER BY prioridad ASC, fecha_envio DESC", (uid,))
        rows = c.fetchall()
        resultado = []
        for r in rows:
            resultado.append(Mensaje.from_row(r))
        return resultado

    def marcar_eliminado(self, mid):
        c = self.conn.cursor()
        c.execute("UPDATE mensajes SET eliminado_en = ? WHERE id = ?", (datetime.datetime.now().isoformat(), mid))
        self.conn.commit()

    def recuperar_mensaje(self, mid):
        c = self.conn.cursor()
        c.execute("UPDATE mensajes SET eliminado_en = NULL WHERE id = ?", (mid,))
        self.conn.commit()

    def marcar_prioritario(self, mid):
        c = self.conn.cursor()
        c.execute("UPDATE mensajes SET procesado_prioridad = 1 WHERE id = ?", (mid,))
        self.conn.commit()

    def desmarcar_prioritario(self, mid):
        c = self.conn.cursor()
        c.execute("UPDATE mensajes SET procesado_prioridad = 0 WHERE id = ?", (mid,))
        self.conn.commit()

    def borrar_mensaje_definitivo(self, mid):
        c = self.conn.cursor()
        c.execute("DELETE FROM mensajes WHERE id = ?", (mid,))
        self.conn.commit()

    def limpiar_papelera(self):
        c = self.conn.cursor()
        limite = datetime.datetime.now() - datetime.timedelta(days=4, hours=20)
        c.execute("DELETE FROM mensajes WHERE eliminado_en IS NOT NULL AND eliminado_en < ?", (limite.isoformat(),))
        self.conn.commit()


# ==========================
# Filtro simple de reglas
# ==========================
class Filtro:
    def __init__(self):
        self.reglas = {}

    def agregar_regla(self, regla, accion):
        self.reglas[regla] = accion

    def aplicar_filtro(self, texto):
        for regla, accion in self.reglas.items():
            if regla.lower() in texto.lower():
                return accion
        return None


# ==========================
# Cola de prioridades en memoria (heap)
# ==========================
class ColaPrioridadesMem:
    def __init__(self):
        self.cola = []  # (prioridad, id_mensaje)

    def agregar(self, prioridad, mensaje_id):
        heapq.heappush(self.cola, (prioridad, mensaje_id))

    def obtener(self):
        if not self.cola:
            return None
        return heapq.heappop(self.cola)

    def listar(self):
        return list(self.cola)

    def vacia(self):
        return len(self.cola) == 0


# ==========================
# Sistema que unifica todo
# ==========================
class SistemaCorreo:
    def __init__(self, db: BaseDatos):
        self.db = db
        self.filtro = Filtro()
        self.cola_mem = ColaPrioridadesMem()

        # Reglas por defecto
        self.filtro.agregar_regla("urgente", "prioridad")
        self.filtro.agregar_regla("spam", "eliminar")

    def crear_usuario(self, nombre, correo, contraseña):
        return self.db.crear_usuario(nombre, correo, contraseña)

    def enviar(self, mensaje: Mensaje):
        accion = self.filtro.aplicar_filtro(mensaje.cuerpo)
        prioridad = mensaje.prioridad or 5
        if accion == "prioridad":
            prioridad = 1
            mid = self.db.guardar_mensaje(mensaje, prioridad=prioridad)
            self.cola_mem.agregar(prioridad, mid)
            return ("cola", mid)
        elif accion == "eliminar":
            return ("eliminado", None)
        else:
            mid = self.db.guardar_mensaje(mensaje, prioridad=prioridad)
            return ("enviado", mid)

    def procesar_proximo_prioritario(self):
        item = self.cola_mem.obtener()
        if not item:
            return None
        prioridad, mid = item
        self.db.marcar_prioritario(mid)
        c = self.db.conn.cursor()
        c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE id = ?", (mid,))
        row = c.fetchone()
        if not row:
            return None
        mensaje = Mensaje.from_row(row)
        return mensaje

    def priorizar_mensaje(self, mid):
        self.db.marcar_prioritario(mid)


# ==========================
# Broadcast WebSocket server (todos reciben lo mismo)
# ==========================
class BroadcastServer:
    """
    Simple broadcast server: every received message is forwarded to all connected clients.
    Runs in its own thread with its own asyncio loop.
    """
    def __init__(self, host=DEFAULT_WS_HOST, port=DEFAULT_WS_PORT):
        self.host = host
        self.port = port
        self.clients = set()
        self.loop = None
        self._thread = None
        self._lock = threading.Lock()

    async def handler(self, websocket, *args):
        # Register
        with self._lock:
            self.clients.add(websocket)
        print("Cliente conectado (broadcast). Total:", len(self.clients))

        try:
            async for msg in websocket:
                # Difundir mensaje en bruto a todos los clientes
                await self.broadcast(msg)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            print("Handler error:", e)
        finally:
            with self._lock:
                if websocket in self.clients:
                    self.clients.remove(websocket)
            print("Cliente desconectado (broadcast). Total:", len(self.clients))

    async def _safe_send(self, ws, message):
        try:
            await ws.send(message)
        except Exception:
            pass

    async def broadcast(self, message):
        with self._lock:
            clients = list(self.clients)
        if not clients:
            return
        await asyncio.gather(*(self._safe_send(c, message) for c in clients), return_exceptions=True)

    async def start_async(self):
        print(f"Starting Broadcast WS server on {self.host}:{self.port}")
        async with websockets.serve(self.handler, self.host, self.port):
            await asyncio.Future()  # correr para siempre

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.start_async())
        except Exception as e:
            print("WS server error:", e)

    def start_in_background(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print("Broadcast WS server thread started")


# ==========================
# Cliente WebSocket (para la UI)
# ==========================
class WSClient:
    """
    Client that receives broadcast messages. Runs a simple asyncio loop in a thread.
    Note: uses websockets.connect and listens for incoming messages.
    """
    def __init__(self, uri, incoming_queue: queue.Queue, sender_name="anon"):
        self.uri = uri
        self.incoming = incoming_queue
        self.sender_name = sender_name
        self._stop = threading.Event()
        self._thread = None
        self._ws = None
        self._loop = None

    def start(self):
        if not WEBSOCKETS_AVAILABLE:
            raise RuntimeError("websockets library not available")
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run_loop(self):
        # Crea y ejecuta un bucle asyncio en este hilo.
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            self.incoming.put({"type":"error","msg":str(e)})

    async def _main(self):
        try:
            async with websockets.connect(self.uri) as ws:
                self._ws = ws
                # informa al servidor sobre el remitente
                try:
                    await ws.send(json.dumps({"type":"join","sender":self.sender_name,"ts":datetime.datetime.now().isoformat()}))
                except Exception:
                    pass
                async for message in ws:
                    # Intentar analizar json, de lo contrario entrega mensaje crudo
                    try:
                        data = json.loads(message)
                    except Exception:
                        data = {"type":"raw","raw":message}
                    self.incoming.put(data)
                    if self._stop.is_set():
                        break
        except Exception as e:
            self.incoming.put({"type":"error","msg":str(e)})

    def send(self, text):
        # Enviar a través de la conexión ws abierta si está disponible 
        if self._loop and self._ws:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.send(text), self._loop)
            except Exception as e:
                self.incoming.put({"type":"error","msg":str(e)})
        else:
            # abre una conexión de corta duración para enviar
            threading.Thread(target=self._short_send, args=(text,), daemon=True).start()

    def _short_send(self, text):
        async def _s():
            try:
                async with websockets.connect(self.uri) as ws:
                    await ws.send(text)
            except Exception as e:
                self.incoming.put({"type":"error","msg":str(e)})
        try:
            asyncio.run(_s())
        except Exception as e:
            self.incoming.put({"type":"error","msg":str(e)})


# Asistente para enviar mensajes desde la interfaz de usuario en una tarea en segundo plano (conexión de corta duración)
def ws_send_in_thread(uri, text, incoming):
    async def _s():
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(text)
        except Exception as e:
            incoming.put({"type":"error","msg":str(e)})
    try:
        asyncio.run(_s())
    except Exception:
        pass


# ==========================
# Interfaz gráfica (Tkinter) extendida con Chat broadcast
# ==========================
class App(tk.Tk):
    def __init__(self, sistema: SistemaCorreo, rt_server: BroadcastServer = None):
        super().__init__()
        self.title("Sistema de Correo + Chat (Broadcast)")
        self.geometry("1000x650")
        self.sistema = sistema
        self.db = sistema.db
        self.usuario_actual = None

        # WebSocket client state
        self.ws_client = None
        self.ws_incoming = queue.Queue()
        self.rt_server = rt_server

        self._crear_widgets_inicio()

    def _crear_widgets_inicio(self):
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Sistema de Correo - Login / Registro", font=(None, 16)).pack(pady=8)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=12)

        ttk.Button(btn_frame, text="Iniciar sesión", command=self._login).grid(row=0, column=0, padx=6)
        ttk.Button(btn_frame, text="Registrarse", command=self._registrar).grid(row=0, column=1, padx=6)
        ttk.Button(btn_frame, text="Recuperar contraseña", command=self._recuperar_contraseña).grid(row=0, column=2, padx=6)
        ttk.Button(btn_frame, text="Salir", command=self.destroy).grid(row=0, column=3, padx=6)

        ttk.Separator(frame).pack(fill=tk.X, pady=8)

        ttk.Label(frame, text="Usuarios existentes:").pack(anchor=tk.W)
        self.lst_users = tk.Listbox(frame, height=6)
        self.lst_users.pack(fill=tk.X)
        self._recargar_usuarios()

    def _recargar_usuarios(self):
        self.lst_users.delete(0, tk.END)
        for u in self.db.listar_usuarios():
            self.lst_users.insert(tk.END, f"{u.id_usuario} - {u.nombre} ({u.correo})")

    def _login(self):
        correo = simpledialog.askstring("Login", "Correo:", parent=self)
        if not correo:
            return
        usuario = self.db.obtener_usuario_por_correo(correo)
        if not usuario:
            messagebox.showerror("Error", "Usuario no encontrado")
            return
        pwd = simpledialog.askstring("Login", "Contraseña:", show='*', parent=self)
        if pwd != usuario.contraseña:
            messagebox.showerror("Error", "Contraseña incorrecta")
            return
        self.usuario_actual = usuario
        self._abrir_panel_principal()

    def _registrar(self):
        nombre = simpledialog.askstring("Registro", "Nombre:", parent=self)
        correo = simpledialog.askstring("Registro", "Correo:", parent=self)
        contraseña = simpledialog.askstring("Registro", "Contraseña:", show='*', parent=self)
        if not (nombre and correo and contraseña):
            return
        uid = self.sistema.crear_usuario(nombre, correo, contraseña)
        if not uid:
            messagebox.showerror("Error", "No se pudo crear usuario (correo ya existe)")
            return
        messagebox.showinfo("OK", "Usuario creado")
        self._recargar_usuarios()

    def _recuperar_contraseña(self):
        nombre = simpledialog.askstring("Recuperar contraseña", "Ingrese su nombre:", parent=self)
        if not nombre:
            return

        c = self.db.conn.cursor()
        c.execute("SELECT nombre, contraseña FROM usuarios WHERE nombre = ?", (nombre,))
        row = c.fetchone()

        if not row:
            messagebox.showerror("Error", "No existe un usuario con ese nombre")
            return

        messagebox.showinfo("Recuperación de contraseña", f"Su contraseña es: {row[1]}")

    def _abrir_panel_principal(self):
        for widget in self.winfo_children():
            widget.destroy()

        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text=f"Conectado como: {self.usuario_actual.nombre}").pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Nuevo mensaje", command=self._ventana_enviar).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Mensajes Prioritarios", command=self._abrir_ventana_prioritarios).pack(side=tk.RIGHT, padx=6)
        ttk.Button(toolbar, text="Cerrar sesión", command=self._cerrar_sesion).pack(side=tk.RIGHT, padx=6)
        ttk.Button(toolbar, text="Eliminar usuario", command=self._eliminar_usuario).pack(side=tk.RIGHT, padx=6)

        content = ttk.Frame(self, padding=8)
        content.pack(fill=tk.BOTH, expand=True)

        # Left: Bandeja
        left = ttk.Frame(content)
        left.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        cols = ("id","asunto","remitente","fecha","prioridad")
        self.tree = ttk.Treeview(left, columns=cols, show='headings')
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=120)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        side = ttk.Frame(left, width=200)
        side.pack(fill=tk.Y, side=tk.RIGHT)
        ttk.Button(side, text="Refrescar", command=self._cargar_bandeja).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Priorizar seleccionado", command=self._priorizar_seleccionado).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Buscar asunto", command=self._buscar_asunto).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Ver detalle", command=self._ver_detalle).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Eliminar", command=self._eliminar_mensaje).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Papelera", command=self._abrir_papelera).pack(fill=tk.X, pady=4)

        # Right: Chat panel
        chat_frame = ttk.Frame(content, width=360, padding=6)
        chat_frame.pack(fill=tk.Y, side=tk.RIGHT)
        ttk.Label(chat_frame, text="Chat en tiempo real (broadcast)").pack(anchor=tk.W)

        ctrl = ttk.Frame(chat_frame)
        ctrl.pack(fill=tk.X, pady=4)
        ttk.Label(ctrl, text="Host:Port").grid(row=0, column=0)
        self.ent_host = ttk.Entry(ctrl)
        self.ent_host.insert(0, f"{DEFAULT_WS_HOST}:{DEFAULT_WS_PORT}")
        self.ent_host.grid(row=0, column=1)

        btns = ttk.Frame(chat_frame)
        btns.pack(fill=tk.X, pady=4)
        ttk.Button(btns, text="Start local server", command=self._start_local_server).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Connect", command=self._connect_ws).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Disconnect", command=self._disconnect_ws).pack(side=tk.LEFT, padx=2)

        # Chat text area
        self.txt_chat = tk.Text(chat_frame, height=20)
        self.txt_chat.pack(fill=tk.BOTH, expand=True)
        self.ent_msg = ttk.Entry(chat_frame)
        self.ent_msg.pack(fill=tk.X, pady=4)
        ttk.Button(chat_frame, text="Enviar chat", command=self._enviar_chat).pack(fill=tk.X)

        # actualizar bandeja
        self._cargar_bandeja()
        # iniciar polling de mensajes WS si correspon
        self.after(200, self._poll_ws_incoming)

    def _start_local_server(self):
        if not WEBSOCKETS_AVAILABLE:
            messagebox.showerror("Error", "La librería 'websockets' no está instalada. Ejecuta: pip install websockets")
            return
        host_port = self.ent_host.get().strip()
        if ':' in host_port:
            host, port = host_port.split(':', 1)
            port = int(port)
        else:
            host = DEFAULT_WS_HOST
            port = DEFAULT_WS_PORT

        if not self.rt_server:
            self.rt_server = BroadcastServer(host=host, port=port)
        try:
            self.rt_server.start_in_background()
            messagebox.showinfo("Servidor", f"Servidor WebSocket local intentando iniciar en {host}:{port}")
        except Exception as e:
            messagebox.showerror("Error servidor", str(e))

    def _connect_ws(self):
        if not WEBSOCKETS_AVAILABLE:
            messagebox.showerror("Error", "La librería 'websockets' no está instalada. Ejecuta: pip install websockets")
            return
        host = self.ent_host.get().strip()
        if ':' not in host:
            messagebox.showerror("Error", "Host debe estar en formato host:port")
            return
        uri = f"ws://{host}"
        sender = self.usuario_actual.nombre if self.usuario_actual else 'anon'

        if self.ws_client is not None:
            messagebox.showinfo("Info", "Ya conectado (desconéctese primero)")
            return
        self.ws_client = WSClient(uri, self.ws_incoming, sender_name=sender)
        try:
            self.ws_client.start()
            messagebox.showinfo("Conectado", f"Conectando a {uri} (broadcast)")
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.ws_client = None

    def _disconnect_ws(self):
        if not self.ws_client:
            messagebox.showinfo("Info", "No hay conexión activa")
            return
        self.ws_client.stop()
        self.ws_client = None
        messagebox.showinfo("Desconectado", "Cliente WebSocket detenido")

    def _enviar_chat(self):
        text = self.ent_msg.get().strip()
        if not text:
            return
        host = self.ent_host.get().strip()
        if ':' not in host:
            messagebox.showerror("Error", "Host debe estar en formato host:port")
            return
        uri = f"ws://{host}"

        # Enviamos JSON para que los clientes receptores puedan analizarlo
        payload = json.dumps({
            "type": "msg",
            "sender": self.usuario_actual.nombre if self.usuario_actual else "anon",
            "text": text,
            "ts": datetime.datetime.now().isoformat()
        }, ensure_ascii=False)

        # Si tenemos una conexión ws_client en vivo, usamos su método de envío 
        if self.ws_client:
            self.ws_client.send(payload)
        else:
            # usa conexión de corta duración
            threading.Thread(target=ws_send_in_thread, args=(uri, payload, self.ws_incoming), daemon=True).start()

        # Mostrar localmente también
        ts = datetime.datetime.now().isoformat()
        self._append_chat(f"(you) {self.usuario_actual.nombre if self.usuario_actual else 'anon'} [{ts}]: {text}\n")
        self.ent_msg.delete(0, tk.END)

    def _append_chat(self, text):
        self.txt_chat.config(state=tk.NORMAL)
        self.txt_chat.insert(tk.END, text)
        self.txt_chat.see(tk.END)
        self.txt_chat.config(state=tk.DISABLED)

    def _poll_ws_incoming(self):
        while not self.ws_incoming.empty():
            data = self.ws_incoming.get()
            if not isinstance(data, dict):
                # si es texto sin formato
                self._append_chat(f"{data}\n")
                continue
            if data.get('type') == 'msg':
                sender = data.get('sender')
                text = data.get('text')
                ts = data.get('ts')
                self._append_chat(f"{sender} [{ts}]: {text}\n")
            elif data.get('type') == 'raw':
                raw = data.get('raw')
                self._append_chat(f"[RAW] {raw}\n")
            elif data.get('type') == 'join':
                sender = data.get('sender')
                ts = data.get('ts')
                self._append_chat(f"*** {sender} joined at {ts}\n")
            elif data.get('type') == 'error':
                self._append_chat(f"*** ERROR: {data.get('msg')}\n")
            else:
                self._append_chat(f"*** {data}\n")
        self.after(200, self._poll_ws_incoming)

    # Los ayudantes de la interfaz de usuario de correo electrónico restantes (bandeja, enviar correo, etc.)
    def _cargar_bandeja(self):
        try:
            self.db.limpiar_papelera()
        except Exception:
            pass
        if not hasattr(self, 'tree'):
            return
        for i in self.tree.get_children():
            self.tree.delete(i)
        mensajes = self.db.obtener_mensajes_para_usuario(self.usuario_actual.id_usuario)
        for m in mensajes:
            self.tree.insert('', tk.END, values=(m.id_mensaje, m.asunto, m.remitente_id, m.fecha_envio, m.prioridad))

    def _ventana_enviar(self):
        top = tk.Toplevel(self)
        top.title("Enviar mensaje")
        top.geometry("420x360")

        ttk.Label(top, text="Destinatario (ID):").pack(pady=4)
        ent_dest = ttk.Entry(top)
        ent_dest.pack(fill=tk.X, padx=8)

        ttk.Label(top, text="Asunto:").pack(pady=4)
        ent_asunto = ttk.Entry(top)
        ent_asunto.pack(fill=tk.X, padx=8)

        ttk.Label(top, text="Cuerpo:").pack(pady=4)
        txt_cuerpo = tk.Text(top, height=10)
        txt_cuerpo.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        ttk.Label(top, text="Prioridad (1 alta ... 9 baja):").pack(pady=4)
        ent_prio = ttk.Entry(top)
        ent_prio.insert(0, "5")
        ent_prio.pack(fill=tk.X, padx=8)

        def enviar_accion():
            try:
                dest_id = int(ent_dest.get())
            except Exception:
                messagebox.showerror("Error", "ID destinatario inválido")
                return
            asunto = ent_asunto.get()
            cuerpo = txt_cuerpo.get("1.0", tk.END).strip()
            try:
                prioridad = int(ent_prio.get())
            except Exception:
                prioridad = 5
            if not cuerpo:
                messagebox.showerror("Error", "Cuerpo vacío")
                return
            m = Mensaje(id_mensaje=None, asunto=asunto, cuerpo=cuerpo, remitente_id=self.usuario_actual.id_usuario, destinatario_id=dest_id, prioridad=prioridad)
            estado, mid = self.sistema.enviar(m)
            if estado == "eliminado":
                messagebox.showinfo("Filtro", "Mensaje eliminado por filtro")
            elif estado == "cola":
                messagebox.showinfo("Enviado", f"Mensaje guardado y puesto en cola prioritaria (id={mid})")
            else:
                messagebox.showinfo("Enviado", "Mensaje enviado y guardado")
            top.destroy()
            self._cargar_bandeja()

        ttk.Button(top, text="Enviar", command=enviar_accion).pack(pady=6)

    def _buscar_asunto(self):
        termino = simpledialog.askstring("Buscar", "Asunto contiene:", parent=self)
        if termino is None:
            return
        resultados = self.db.buscar_mensajes(self.usuario_actual.id_usuario, "asunto", termino)
        for i in self.tree.get_children():
            self.tree.delete(i)
        for m in resultados:
            self.tree.insert('', tk.END, values=(m.id_mensaje, m.asunto, m.remitente_id, m.fecha_envio, m.prioridad))

    def _eliminar_mensaje(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleccione un mensaje")
            return
        mid = self.tree.item(sel[0])['values'][0]
        self.db.marcar_eliminado(mid)
        messagebox.showinfo("Eliminado", "Mensaje movido a papelera (4 días y 20 hs)")
        self._cargar_bandeja()

    def _ver_detalle(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleccione un mensaje")
            return
        item = self.tree.item(sel[0])
        mid = item['values'][0]
        c = self.db.conn.cursor()
        c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE id = ?", (mid,))
        row = c.fetchone()
        if not row:
            messagebox.showerror("Error", "Mensaje no encontrado")
            return
        m = Mensaje.from_row(row)
        top = tk.Toplevel(self)
        top.title(f"Mensaje {m.id_mensaje}")
        ttk.Label(top, text=f"Asunto: {m.asunto}").pack(anchor=tk.W, padx=8, pady=4)
        ttk.Label(top, text=f"De (ID): {m.remitente_id}").pack(anchor=tk.W, padx=8)
        ttk.Label(top, text=f"Fecha: {m.fecha_envio}").pack(anchor=tk.W, padx=8, pady=4)
        ttk.Label(top, text=f"Prioridad: {m.prioridad}").pack(anchor=tk.W, padx=8, pady=2)
        txt = tk.Text(top, height=15)
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        txt.insert(tk.END, m.cuerpo)
        txt.config(state=tk.DISABLED)

    def _priorizar_seleccionado(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleccione un mensaje para priorizar")
            return
        mid = self.tree.item(sel[0])['values'][0]
        try:
            self.db.marcar_prioritario(mid)
            messagebox.showinfo("OK", "Mensaje marcado como prioritario y movido a la ventana de Prioritarios")
            self._cargar_bandeja()
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo priorizar el mensaje")

    def _abrir_ventana_prioritarios(self):
        messagebox.showinfo("Info", "Ventana de prioritarios (implementación mínima en este ejemplo)")

    def _abrir_papelera(self):
        messagebox.showinfo("Info", "Papelera (implementación mínima en este ejemplo)")

    def _cerrar_sesion(self):
        self.usuario_actual = None
        for widget in self.winfo_children():
            widget.destroy()
        self._crear_widgets_inicio()

    def _eliminar_usuario(self):
        if not self.usuario_actual:
            return
        if not messagebox.askyesno("Confirmar", "¿Seguro que desea eliminar su usuario?\nSe borrarán TODOS sus mensajes enviados y recibidos.\nEsta acción no se puede deshacer."):
            return
        uid = self.usuario_actual.id_usuario
        c = self.db.conn.cursor()
        c.execute("DELETE FROM mensajes WHERE remitente_id = ? OR destinatario_id = ?", (uid, uid))
        c.execute("DELETE FROM usuarios WHERE id = ?", (uid,))
        self.db.conn.commit()
        messagebox.showinfo("Cuenta eliminada", "El usuario y sus mensajes han sido eliminados.")
        self.usuario_actual = None
        for widget in self.winfo_children():
            widget.destroy()
        self._crear_widgets_inicio()


# ==========================
# Inicialización y ejecución
# ==========================
def crear_usuarios_demo(db: BaseDatos):
    if not db.listar_usuarios():
        db.crear_usuario("Alice", "alice@example.com", "1234")
        db.crear_usuario("Bob", "bob@example.com", "abcd")
        db.crear_usuario("Carlos", "carlos@example.com", "pass")


def main():
    db = BaseDatos()
    crear_usuarios_demo(db)
    sistema = SistemaCorreo(db)

    # cargar mensajes demo si la tabla está vacía
    c = db.conn.cursor()
    c.execute("SELECT COUNT(*) FROM mensajes")
    total = c.fetchone()[0]
    if total == 0:
        m1 = Mensaje(None, "Hola Bob", "Hola Bob! ¿Cómo andás?", remitente_id=1, destinatario_id=2, prioridad=5)
        m2 = Mensaje(None, "URGENTE: Reunión", "Esto es urgente, reunión a las 10", remitente_id=3, destinatario_id=2, prioridad=1)
        m3 = Mensaje(None, "Spam oferta", "Compra ya", remitente_id=1, destinatario_id=2, prioridad=9)
        sistema.enviar(m1)
        sistema.enviar(m2)
        sistema.enviar(m3)

    # Opcionalmente, cree un servidor automáticamente al iniciar:
    # rt_server = BroadcastServer(host='0.0.0.0', puerto=8765)
    # rt_server.start_in_background()

    app = App(sistema)
    app.mainloop()


if __name__ == "__main__":
    main()
