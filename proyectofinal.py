import json
import sqlite3
import heapq
import datetime
import os
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

DB_FILE = "correo.db"

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

    @staticmethod
    def from_row(row):
        # row positions:
        # 0:id,1:remitente_id,2:destinatario_id,3:asunto,4:cuerpo_json,5:fecha_envio,6:prioridad,7:eliminado_en,8:procesado_prioridad(optional)
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
        self.conn = sqlite3.connect(self.db_file)
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
        # limpiar papelera antes de mostrar
        try:
            self.limpiar_papelera()
        except Exception:
            pass

        c = self.conn.cursor()
        # Excluir mensajes eliminados y priorizados (procesado_prioridad=1)
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
        # limpiar papelera antes de buscar
        try:
            self.limpiar_papelera()
        except Exception:
            pass

        c = self.conn.cursor()
        # Al buscar, también excluir mensajes eliminados y priorizados
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
        # Obtener mensajes que fueron marcados como procesado_prioridad = 1
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
        """Borra definitivamente los mensajes cuya marca eliminado_en excede 4 días y 20 horas."""
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
            # guardamos y añadimos a cola en memoria
            mid = self.db.guardar_mensaje(mensaje, prioridad=prioridad)
            self.cola_mem.agregar(prioridad, mid)
            return ("cola", mid)
        elif accion == "eliminar":
            # No guardar
            return ("eliminado", None)
        else:
            mid = self.db.guardar_mensaje(mensaje, prioridad=prioridad)
            return ("enviado", mid)

    def procesar_proximo_prioritario(self):
        item = self.cola_mem.obtener()
        if not item:
            return None
        prioridad, mid = item
        # marcar como priorizado en DB
        self.db.marcar_prioritario(mid)
        # recuperar mensaje desde DB y devolverlo
        c = self.db.conn.cursor()
        c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE id = ?", (mid,))
        row = c.fetchone()
        if not row:
            return None
        mensaje = Mensaje.from_row(row)
        return mensaje

    # Método práctico para priorizar (cuando se quiere priorizar directamente)
    def priorizar_mensaje(self, mid):
        self.db.marcar_prioritario(mid)


# ==========================
# Interfaz gráfica (Tkinter)
# ==========================
class App(tk.Tk):
    def __init__(self, sistema: SistemaCorreo):
        super().__init__()
        self.title("Sistema de Correo")
        self.geometry("900x600")
        self.sistema = sistema
        self.db = sistema.db
        self.usuario_actual = None

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

        # Buscar usuario por nombre
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

        # Bandeja: Treeview
        cols = ("id","asunto","remitente","fecha","prioridad")
        self.tree = ttk.Treeview(content, columns=cols, show='headings')
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=120)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        side = ttk.Frame(content, width=200)
        side.pack(fill=tk.Y, side=tk.RIGHT)
        ttk.Button(side, text="Refrescar", command=self._cargar_bandeja).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Priorizar seleccionado", command=self._priorizar_seleccionado).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Buscar asunto", command=self._buscar_asunto).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Ver detalle", command=self._ver_detalle).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Eliminar", command=self._eliminar_mensaje).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="Papelera", command=self._abrir_papelera).pack(fill=tk.X, pady=4)

        self._cargar_bandeja()

    def _cerrar_sesion(self):
        self.usuario_actual = None
        for widget in self.winfo_children():
            widget.destroy()
        self._crear_widgets_inicio()
    
    def _eliminar_usuario(self):
      if not self.usuario_actual:
        return

      if not messagebox.askyesno(
        "Confirmar",
        "¿Seguro que desea eliminar su usuario?\n"
        "Se borrarán TODOS sus mensajes enviados y recibidos.\n"
        "Esta acción no se puede deshacer."
    ):
        return

      uid = self.usuario_actual.id_usuario

    # Eliminar mensajes enviados o recibidos
      c = self.db.conn.cursor()
      c.execute("DELETE FROM mensajes WHERE remitente_id = ? OR destinatario_id = ?", (uid, uid))

    # Eliminar usuario
      c.execute("DELETE FROM usuarios WHERE id = ?", (uid,))
      self.db.conn.commit()

      messagebox.showinfo("Cuenta eliminada", "El usuario y sus mensajes han sido eliminados.")

    # Cerrar sesión y volver al inicio
      self.usuario_actual = None
      for widget in self.winfo_children():
        widget.destroy()
      self._crear_widgets_inicio()

    def _cargar_bandeja(self):
        # limpiar papelera para mantener DB ordenada
        try:
            self.db.limpiar_papelera()
        except Exception:
            pass

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
            # Marcar en DB
            self.db.marcar_prioritario(mid)
            messagebox.showinfo("OK", "Mensaje marcado como prioritario y movido a la ventana de Prioritarios")
            # Refrescar ambas vistas (bandeja pierde el mensaje; prioritarios lo gana)
            self._cargar_bandeja()
            if hasattr(self, "ventana_prioritarios") and getattr(self, "ventana_prioritarios").winfo_exists():
                self._cargar_prioritarios()
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo priorizar el mensaje")
                                                      #{e}")
                                 
    def _procesar_prioridad(self):
        # Procesa el próximo mensaje de la cola en memoria y lo marca como priorizado en DB,
        # luego actualiza vistas.
        item = self.sistema.procesar_proximo_prioritario()
        if not item:
            messagebox.showinfo("Cola", "No hay mensajes prioritarios en memoria")
            return

        # Refrescar bandeja principal (el mensaje ya no debería aparecer ahí)
        self._cargar_bandeja()
        # Si la ventana de prioritarios está abierta, recargarla
        if hasattr(self, "ventana_prioritarios") and self.ventana_prioritarios.winfo_exists():
            self._cargar_prioritarios()
        # Mostrar el mensaje procesado (opcional)
        top = tk.Toplevel(self)
        top.title("Procesado - Mensaje Priorizado")
        ttk.Label(top, text=f"Asunto: {item.asunto}").pack(anchor=tk.W, padx=8, pady=4)
        ttk.Label(top, text=f"De (ID): {item.remitente_id}").pack(anchor=tk.W, padx=8)
        ttk.Label(top, text=f"Para (ID): {item.destinatario_id}").pack(anchor=tk.W, padx=8, pady=4)
        ttk.Label(top, text=f"Prioridad: {item.prioridad}").pack(anchor=tk.W, padx=8, pady=2)
        txt = tk.Text(top, height=12)
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        txt.insert(tk.END, item.cuerpo)
        txt.config(state=tk.DISABLED)

    # =========================
    # Ventana de Prioritarios
    # =========================
    def _abrir_ventana_prioritarios(self):
        # Evita abrir dos ventanas iguales
        if hasattr(self, "ventana_prioritarios") and self.ventana_prioritarios.winfo_exists():
            self.ventana_prioritarios.lift()
            return

        self.ventana_prioritarios = tk.Toplevel(self)
        self.ventana_prioritarios.title("Mensajes Prioritarios")
        self.ventana_prioritarios.geometry("800x500")

        cols = ("id","asunto","remitente","fecha","prioridad")
        self.tree_prioritarios = ttk.Treeview(self.ventana_prioritarios, columns=cols, show='headings', height=18)
        for c in cols:
            self.tree_prioritarios.heading(c, text=c.capitalize())
            self.tree_prioritarios.column(c, width=140)
        self.tree_prioritarios.pack(fill=tk.BOTH, expand=True, side=tk.TOP, padx=8, pady=8)

        btn_frame = ttk.Frame(self.ventana_prioritivos if False else self.ventana_prioritarios, padding=6)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Refrescar", command=self._cargar_prioritarios).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Ver detalle", command=self._ver_detalle_prioritario).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Restaurar a bandeja", command=self._restaurar_prioritario).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Borrar definitivamente", command=self._borrar_definitivo_prioritario).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Cerrar", command=self.ventana_prioritarios.destroy).pack(side=tk.RIGHT, padx=6)

        # Carga inicial
        self._cargar_prioritarios()

    def _cargar_prioritarios(self):
        # Cargar mensajes marcados como priorizados (procesado_prioridad = 1)
        if not hasattr(self, "tree_prioritarios"):
            return
        for i in self.tree_prioritarios.get_children():
            try:
                self.tree_prioritarios.delete(i)
            except Exception:
                pass

        mensajes = self.db.obtener_mensajes_prioritarios(self.usuario_actual.id_usuario)
        for m in mensajes:
            self.tree_prioritarios.insert('', tk.END, values=(m.id_mensaje, m.asunto, m.remitente_id, m.fecha_envio, m.prioridad))

    def _ver_detalle_prioritario(self):
        sel = self.tree_prioritarios.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleccione un mensaje prioritario")
            return
        mid = self.tree_prioritarios.item(sel[0])['values'][0]
        c = self.db.conn.cursor()
        c.execute("SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad FROM mensajes WHERE id = ?", (mid,))
        row = c.fetchone()
        if not row:
            messagebox.showerror("Error", "Mensaje no encontrado")
            return
        m = Mensaje.from_row(row)
        top = tk.Toplevel(self)
        top.title(f"Mensaje Prioritario {m.id_mensaje}")
        ttk.Label(top, text=f"Asunto: {m.asunto}").pack(anchor=tk.W, padx=8, pady=4)
        ttk.Label(top, text=f"De (ID): {m.remitente_id}").pack(anchor=tk.W, padx=8)
        ttk.Label(top, text=f"Fecha: {m.fecha_envio}").pack(anchor=tk.W, padx=8, pady=4)
        ttk.Label(top, text=f"Prioridad: {m.prioridad}").pack(anchor=tk.W, padx=8, pady=2)
        txt = tk.Text(top, height=15)
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        txt.insert(tk.END, m.cuerpo)
        txt.config(state=tk.DISABLED)

    def _restaurar_prioritario(self):
        sel = self.tree_prioritarios.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleccione un mensaje prioritario")
            return
        mid = self.tree_prioritarios.item(sel[0])['values'][0]
        # Desmarcar en DB
        self.db.desmarcar_prioritario(mid)
        messagebox.showinfo("OK", "Mensaje restaurado a bandeja principal")
        # Refrescar ambas vistas
        self._cargar_prioritarios()
        self._cargar_bandeja()

    def _borrar_definitivo_prioritario(self):
        sel = self.tree_prioritarios.selection()
        if not sel:
            messagebox.showinfo("Info", "Seleccione un mensaje prioritario")
            return
        mid = self.tree_prioritarios.item(sel[0])['values'][0]
        if messagebox.askyesno("Confirmar", "¿Borrar definitivamente este mensaje?"):
            self.db.borrar_mensaje_definitivo(mid)
            messagebox.showinfo("OK", "Mensaje borrado definitivamente")
            self._cargar_prioritarios()

    # =========================
    # Papelera
    # =========================
    def _abrir_papelera(self):
        # limpiar primero
        try:
            self.db.limpiar_papelera()
        except Exception:
            pass

        top = tk.Toplevel(self)
        top.title("Papelera")
        top.geometry("700x500")

        cols = ("id","asunto","remitente","eliminado_en")
        tree = ttk.Treeview(top, columns=cols, show='headings')
        for c in cols:
            tree.heading(c, text=c.capitalize())
        tree.pack(fill=tk.BOTH, expand=True)

        # cargar mensajes eliminados
        c = self.db.conn.cursor()
        c.execute("""
            SELECT id, remitente_id, destinatario_id, asunto, cuerpo_json, fecha_envio, prioridad, eliminado_en, procesado_prioridad
            FROM mensajes
            WHERE eliminado_en IS NOT NULL
            ORDER BY eliminado_en DESC
        """)
        rows = c.fetchall()

        limite = datetime.timedelta(days=4, hours=20)
        ahora = datetime.datetime.now()

        for r in rows:
            eliminado_en = r[7]
            if eliminado_en:
                try:
                    dt = datetime.datetime.fromisoformat(eliminado_en)
                except Exception:
                    continue
                if ahora - dt <= limite:  # Aún no vence
                    tree.insert('', tk.END, values=(r[0], r[3], r[1], eliminado_en))

        # Botón Restaurar
        def restaurar():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Info", "Seleccione un mensaje")
                return
            mid = tree.item(sel[0])['values'][0]
            self.db.recuperar_mensaje(mid)
            messagebox.showinfo("OK", "Mensaje restaurado")
            tree.delete(sel[0])
            self._cargar_bandeja()

        def borrar_definitivo():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Info", "Seleccione un mensaje")
                return
            mid = tree.item(sel[0])['values'][0]
            if messagebox.askyesno("Confirmar", "¿Borrar definitivamente este mensaje?"):
                c2 = self.db.conn.cursor()
                c2.execute("DELETE FROM mensajes WHERE id = ?", (mid,))
                self.db.conn.commit()
                messagebox.showinfo("OK", "Mensaje borrado definitivamente")
                tree.delete(sel[0])

        btn_frame = ttk.Frame(top, padding=6)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Restaurar", command=restaurar).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Borrar definitivamente", command=borrar_definitivo).pack(side=tk.LEFT, padx=6)


# ==========================
# Inicialización y ejecución
# ==========================
def crear_usuarios_demo(db: BaseDatos):
    # crear un par de usuarios si la DB está vacía
    if not db.listar_usuarios():
        db.crear_usuario("Ana", "Ana@gmail.com", "1234")
        db.crear_usuario("Bob", "Bob@gmail.com", "5678")
        
        

def main():
    db = BaseDatos()
    crear_usuarios_demo(db)
    sistema = SistemaCorreo(db)

    # cargar mensajes demo si la tabla está vacía
    c = db.conn.cursor()
    c.execute("SELECT COUNT(*) FROM mensajes")
    total = c.fetchone()[0]
    if total == 0:
        # agregar algunos mensajes de prueba
        m1 = Mensaje(None, "Hola Bob", "Hola Bob! ¿Cómo andás?", remitente_id=1, destinatario_id=2, prioridad=5)
        m2 = Mensaje(None, "URGENTE: Reunión", "Esto es urgente, reunión a las 10", remitente_id=3, destinatario_id=2, prioridad=1)
        m3 = Mensaje(None, "Spam oferta", "Compra ya", remitente_id=1, destinatario_id=2, prioridad=9)
        sistema.enviar(m1)
        sistema.enviar(m2)
        sistema.enviar(m3)

    app = App(sistema)
    app.mainloop()


if __name__ == "__main__":
    main()
