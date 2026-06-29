import asyncio
import hashlib
import html
import json
import os
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


BASE_DIR = Path(__file__).resolve().parent
load_env_file(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DEALERBOT_DB", BASE_DIR / "dealerbot.sqlite3"))
DOWNLOAD_DIR = Path(os.getenv("DEALERBOT_DOWNLOAD_DIR", BASE_DIR / "reportes"))
SESSION_SECRET_FILE = BASE_DIR / "session.secret"
ADMIN_CREDENTIALS_FILE = BASE_DIR / "admin_credentials.txt"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
URL_WEB = os.getenv("DEALERNET_URL", "https://suite.dealernet.cl/")
USUARIO_WEB = os.getenv("DEALERNET_USER", "")
CLAVE_WEB = os.getenv("DEALERNET_PASSWORD", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "Mi_Info_Service_bot").lstrip("@")

DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
INITIAL_ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@dealerbot.local")
INITIAL_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
QUERY_COST = int(os.getenv("QUERY_COST", "1"))
INITIAL_TOKENS = int(os.getenv("INITIAL_TOKENS", "0"))
DEALERNET_READY = bool(USUARIO_WEB and CLAVE_WEB)

cola_de_trabajo = asyncio.Queue()


def load_session_secret() -> str:
    env_secret = os.getenv("SESSION_SECRET")
    if env_secret:
        return env_secret
    if SESSION_SECRET_FILE.exists():
        return SESSION_SECRET_FILE.read_text().strip()
    secret = secrets.token_hex(32)
    SESSION_SECRET_FILE.write_text(secret)
    return secret


SESSION_SECRET = load_session_secret()


def utc_now() -> int:
    return int(time.time())


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT,
                telegram_user_id INTEGER UNIQUE,
                telegram_username TEXT UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                bot_id TEXT NOT NULL UNIQUE,
                access_token TEXT NOT NULL UNIQUE,
                telegram_chat_id INTEGER UNIQUE,
                token_balance INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                metadata TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );
            """
        )
        conn.execute("DROP TABLE IF EXISTS topups")
        migrate_users(conn)
        seed_initial_admin(conn)


def migrate_users(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    migrations = {
        "display_name": "ALTER TABLE users ADD COLUMN display_name TEXT",
        "telegram_user_id": "ALTER TABLE users ADD COLUMN telegram_user_id INTEGER",
        "telegram_username": "ALTER TABLE users ADD COLUMN telegram_username TEXT",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)


def seed_initial_admin(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT id FROM admins LIMIT 1").fetchone()
    if existing:
        return
    password = INITIAL_ADMIN_PASSWORD or secrets.token_urlsafe(14)
    salt, digest = hash_password(password)
    conn.execute(
        """
        INSERT INTO admins (email, password_salt, password_hash, active, created_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        (INITIAL_ADMIN_EMAIL.strip().lower(), salt, digest, utc_now()),
    )
    if not INITIAL_ADMIN_PASSWORD:
        ADMIN_CREDENTIALS_FILE.write_text(
            f"Admin URL: http://127.0.0.1:{DASHBOARD_PORT}/admin/login\n"
            f"Email: {INITIAL_ADMIN_EMAIL.strip().lower()}\n"
            f"Password: {password}\n"
        )


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 150_000)
    return salt, digest.hex()


def check_password(password: str, salt: str, expected_hash: str) -> bool:
    _, digest = hash_password(password, salt)
    return secrets.compare_digest(digest, expected_hash)


def make_access_token() -> str:
    return "dt_" + secrets.token_urlsafe(32)


def make_bot_id() -> str:
    return "BOT-" + secrets.token_hex(4).upper()


def create_user(
    display_name: str,
    telegram_user_id: int | None = None,
    telegram_username: str | None = None,
    initial_tokens: int = INITIAL_TOKENS,
) -> sqlite3.Row:
    display_name = display_name.strip()
    if not display_name:
        raise ValueError("Ingresa un nombre o alias valido.")

    slug = "".join(ch for ch in display_name.lower().replace(" ", "_") if ch.isalnum() or ch == "_") or "user"
    internal_email = f"{slug}_{secrets.token_hex(3)}@internal.local"
    salt, digest = hash_password(secrets.token_urlsafe(16))
    if telegram_user_id is not None and telegram_user_id != "":
        telegram_user_id = int(str(telegram_user_id).strip())
    else:
        telegram_user_id = None
    telegram_username = telegram_username.strip().lstrip("@") if telegram_username else None
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users
                (email, display_name, telegram_user_id, telegram_username, password_salt, password_hash, bot_id, access_token, token_balance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                internal_email,
                display_name,
                telegram_user_id,
                telegram_username,
                salt,
                digest,
                make_bot_id(),
                make_access_token(),
                initial_tokens,
                utc_now(),
            ),
        )
        user = conn.execute("SELECT * FROM users WHERE email = ?", (internal_email,)).fetchone()
        if initial_tokens:
            add_ledger(conn, user["id"], initial_tokens, "initial_grant", {})
        return user


def authenticate_admin(email: str, password: str) -> sqlite3.Row | None:
    with db() as conn:
        admin = conn.execute(
            "SELECT * FROM admins WHERE email = ? AND active = 1",
            (email.strip().lower(),),
        ).fetchone()
    if admin and check_password(password, admin["password_salt"], admin["password_hash"]):
        return admin
    return None


def create_admin(email: str, password: str) -> None:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Ingresa un email valido.")
    if len(password) < 10:
        raise ValueError("La clave admin debe tener al menos 10 caracteres.")
    salt, digest = hash_password(password)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO admins (email, password_salt, password_hash, active, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (email, salt, digest, utc_now()),
        )


def add_ledger(conn: sqlite3.Connection, user_id: int, delta: int, reason: str, metadata: dict) -> None:
    conn.execute(
        "INSERT INTO ledger (user_id, delta, reason, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, delta, reason, json.dumps(metadata, ensure_ascii=True), utc_now()),
    )


def get_user_by_telegram_id(telegram_user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM users
            WHERE active = 1 AND telegram_user_id = ?
            """,
            (telegram_user_id,),
        ).fetchone()


def get_user_by_username(username: str) -> sqlite3.Row | None:
    username = username.strip().lstrip("@").lower()
    if not username:
        return None
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM users
            WHERE active = 1 AND lower(telegram_username) = ?
            """,
            (username,),
        ).fetchone()


def get_authorized_user(telegram_user_id: int | None, chat_id: int | None = None) -> sqlite3.Row | None:
    if telegram_user_id is not None:
        with db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE active = 1 AND telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if user:
                if chat_id is not None and user["telegram_chat_id"] != chat_id:
                    conn.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat_id, user["id"]))
                return user
    return None


def charge_query(user_id: int, rut: str) -> tuple[bool, str]:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT token_balance FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            conn.rollback()
            return False, "Usuario no encontrado."
        if user["token_balance"] < QUERY_COST:
            conn.rollback()
            return False, f"Saldo insuficiente. Cada consulta cuesta {QUERY_COST} token(s)."
        conn.execute(
            "UPDATE users SET token_balance = token_balance - ? WHERE id = ?",
            (QUERY_COST, user_id),
        )
        add_ledger(conn, user_id, -QUERY_COST, "rut_query", {"rut": rut})
        conn.commit()
        return True, "ok"


def refund_query(user_id: int, rut: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET token_balance = token_balance + ? WHERE id = ?",
            (QUERY_COST, user_id),
        )
        add_ledger(conn, user_id, QUERY_COST, "query_refund", {"rut": rut})


def limpiar_rut(rut_sucio: str) -> str:
    rut_limpio = rut_sucio.replace(".", "").replace("-", "").replace(" ", "").upper()
    if len(rut_limpio) > 1:
        return f"{rut_limpio[:-1]}-{rut_limpio[-1]}"
    return rut_limpio


def looks_like_rut(rut: str) -> bool:
    if "-" not in rut:
        return False
    cuerpo, dv = rut.split("-", 1)
    return cuerpo.isdigit() and len(cuerpo) >= 7 and len(dv) == 1 and dv.isalnum()


async def buscar_rut_en_web(rut_consultado: str) -> Path | None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            print(f"[~] Conectando a DealerNet para consultar RUT: {rut_consultado}...")
            await page.goto(URL_WEB, timeout=30000)
            await page.wait_for_load_state("networkidle")

            if await page.is_visible("#uname"):
                print("[~] Iniciando sesion en DealerNet...")
                await page.fill("#uname", USUARIO_WEB)
                await page.fill("#psw", CLAVE_WEB)
                await page.press("#psw", "Enter")
                await page.wait_for_load_state("networkidle")

            print("[~] Navegando por el menu lateral...")
            central_info_selector = "span.sidebar-item__label:has-text('Central de Informacion')"
            await page.wait_for_selector(central_info_selector, timeout=15000)
            await page.click(central_info_selector)

            consulta_rut_menu = "button[title='Consulta por RUT']"
            await page.wait_for_selector(consulta_rut_menu, timeout=15000)
            await page.click(consulta_rut_menu)
            await page.wait_for_load_state("networkidle")

            print("[~] Ingresando RUT...")
            await page.wait_for_selector("#rut", timeout=20000)
            await page.fill("#rut", rut_consultado)

            boton_agregar = "span.btn_mas_blue"
            await page.wait_for_selector(boton_agregar, timeout=10000)
            await page.click(boton_agregar)

            selector_boletines = "li.prodcom"
            await page.wait_for_selector(selector_boletines, timeout=10000)
            boletines = await page.locator(selector_boletines).all()
            print(f"[~] Se encontraron {len(boletines)} boletines. Seleccionando todos...")
            for index, boletin in enumerate(boletines):
                try:
                    await boletin.click()
                    print(f"[+] Boletin #{index + 1} seleccionado.")
                except Exception as click_err:
                    print(f"[!] No se pudo seleccionar boletin #{index + 1}: {click_err}")

            boton_consultar_final = "div.consultar_btn.enabled"
            await page.wait_for_selector(boton_consultar_final, timeout=10000)
            await page.click(boton_consultar_final)

            boton_ver_selector = "span.btn_crea_sol"
            await page.wait_for_selector(boton_ver_selector, timeout=25000)

            async with page.expect_popup() as popup_info:
                await page.click(boton_ver_selector)
            page_pdf = await popup_info.value
            await page_pdf.wait_for_load_state("networkidle")

            boton_exportar_pdf = "span.printpreview-spn_pdf"
            await page_pdf.wait_for_selector(boton_exportar_pdf, timeout=20000)
            async with page_pdf.expect_download() as download_info:
                await page_pdf.click(boton_exportar_pdf)
            download = await download_info.value

            nombre_archivo = f"reporte_{rut_consultado.replace('-', '_')}_{utc_now()}.pdf"
            ruta_temporal = DOWNLOAD_DIR / nombre_archivo
            await download.save_as(str(ruta_temporal))
            print(f"[+] PDF descargado exitosamente para RUT: {rut_consultado}")
            return ruta_temporal
        except Exception as e:
            print(f"[-] Error en automatizacion para RUT {rut_consultado}: {e}")
            return None
        finally:
            await browser.close()


async def procesador_de_cola(bot_real):
    while True:
        chat_id, user_id, rut_usuario, message_id = await cola_de_trabajo.get()
        try:
            await bot_real.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"Procesando RUT {rut_usuario}.\n"
                    "El costo ya fue reservado; si la consulta falla se devuelve automaticamente."
                ),
            )

            ruta_pdf = await buscar_rut_en_web(rut_usuario)
            if ruta_pdf and ruta_pdf.exists():
                with ruta_pdf.open("rb") as pdf:
                    await bot_real.send_document(
                        chat_id=chat_id,
                        document=pdf,
                        filename=ruta_pdf.name,
                    )
                ruta_pdf.unlink(missing_ok=True)
                await bot_real.delete_message(chat_id=chat_id, message_id=message_id)
            else:
                refund_query(user_id, rut_usuario)
                await bot_real.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="No se pudo obtener el reporte. Se devolvieron los token(s) de esta consulta.",
                )
        except Exception as e:
            refund_query(user_id, rut_usuario)
            print(f"[-] Error en procesador de cola: {e}")
        finally:
            cola_de_trabajo.task_done()


async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat:
        return
    telegram_user_id = update.effective_user.id if update.effective_user else None
    user = get_authorized_user(telegram_user_id, update.effective_chat.id)
    if not user:
        await update.effective_message.reply_text("Tu cuenta aun no esta habilitada por administracion.")
        return
    await update.effective_message.reply_text(
        f"Bot ID: {user['bot_id']}\nSaldo: {user['token_balance']} token(s)\nCosto por consulta: {QUERY_COST}"
    )


async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id
    telegram_user_id = update.effective_user.id if update.effective_user else None
    user = get_authorized_user(telegram_user_id, chat_id)
    if not user:
        await update.message.reply_text(
            "Acceso restringido. Tu cuenta debe ser creada y habilitada por administracion, y tu ID de Telegram debe estar registrado."
        )
        return
    if not DEALERNET_READY:
        await update.message.reply_text(
            "El bot esta online, pero el acceso a DealerNet aun no esta configurado por administracion."
        )
        return

    rut_usuario = limpiar_rut(update.message.text.strip())
    if not looks_like_rut(rut_usuario):
        await update.message.reply_text("El texto enviado no parece un RUT valido. Ejemplo: 12345678-K")
        return

    ok, message = charge_query(user["id"], rut_usuario)
    if not ok:
        await update.message.reply_text(message)
        return

    posicion = cola_de_trabajo.qsize() + 1
    mensaje_espera = await update.message.reply_text(
        f"RUT {rut_usuario} recibido.\n"
        f"Posicion #{posicion} en la fila. Costo reservado: {QUERY_COST} token(s)."
    )
    await cola_de_trabajo.put((chat_id, user["id"], rut_usuario, mensaje_espera.message_id))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        telegram_user_id = update.effective_user.id if update.effective_user else None
        user = get_authorized_user(telegram_user_id, update.effective_chat.id if update.effective_chat else None)
        if user:
            await update.effective_message.reply_text(
                f"Hola, {user['display_name'] or user['email']}.\n"
                "Tu acceso ya esta habilitado. Envía un RUT para generar el PDF."
            )
            return
        await update.effective_message.reply_text(
            "Bienvenido al Bot DealerNet.\n\n"
            "Tu acceso lo activa administracion. Si ya fuiste creado y tu ID de Telegram esta registrado, envia un RUT para generar el PDF."
        )


async def iniciar_procesador(application: Application):
    asyncio.create_task(procesador_de_cola(application.bot))


def sign_admin_session(admin_id: int) -> str:
    payload = str(admin_id)
    sig = hashlib.sha256(f"admin.{payload}.{SESSION_SECRET}".encode()).hexdigest()
    return f"{payload}.{sig}"


def verify_admin_session(value: str | None) -> int | None:
    if not value or "." not in value:
        return None
    payload, sig = value.split(".", 1)
    expected = hashlib.sha256(f"admin.{payload}.{SESSION_SECRET}".encode()).hexdigest()
    if not secrets.compare_digest(sig, expected):
        return None
    return int(payload) if payload.isdigit() else None


def page_shell(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light; font-family: Arial, sans-serif; }}
    body {{ margin: 0; background: #f5f7fb; color: #1d2433; }}
    header {{ background: #111827; color: white; padding: 18px 24px; }}
    main {{ max-width: 980px; margin: 28px auto; padding: 0 18px; }}
    .panel {{ background: white; border: 1px solid #d8dee9; border-radius: 8px; padding: 20px; margin-bottom: 18px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; }}
    label {{ display: block; font-weight: 700; margin: 12px 0 6px; }}
    input, select {{ box-sizing: border-box; width: 100%; padding: 10px; border: 1px solid #bbc4d4; border-radius: 6px; }}
    button, .button {{ display: inline-block; border: 0; border-radius: 6px; padding: 10px 14px; background: #2563eb; color: white; text-decoration: none; cursor: pointer; }}
    .button.secondary {{ background: #0f766e; }}
    .muted {{ color: #667085; }}
    .token {{ font-family: Consolas, monospace; word-break: break-all; background: #eef2ff; padding: 10px; border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; border-bottom: 1px solid #e5e7eb; padding: 10px; vertical-align: top; }}
    .error {{ color: #b42318; }}
  </style>
</head>
<body>
  <header><strong>DealerBot Dashboard</strong></header>
  <main>{body}</main>
</body>
</html>""".encode()


class DashboardHandler(BaseHTTPRequestHandler):
    allow_admin = True

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.home()
        elif self.allow_admin and parsed.path == "/admin/login":
            self.admin_login_page()
        elif self.allow_admin and parsed.path == "/admin/logout":
            self.admin_logout()
        elif self.allow_admin and parsed.path == "/admin":
            self.admin(parsed)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        if self.allow_admin and parsed.path == "/admin/login":
            self.admin_login_post()
        elif self.allow_admin and parsed.path == "/admin/user-action":
            self.admin_user_action(parsed)
        elif self.allow_admin and parsed.path == "/admin/create-admin":
            self.admin_create_admin(parsed)
        elif self.allow_admin and parsed.path == "/admin/create-user":
            self.admin_create_user(parsed)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def current_admin(self) -> sqlite3.Row | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        raw = cookie.get("admin_session")
        admin_id = verify_admin_session(raw.value if raw else None)
        if not admin_id:
            return None
        with db() as conn:
            return conn.execute("SELECT * FROM admins WHERE id = ? AND active = 1", (admin_id,)).fetchone()

    def require_admin(self) -> sqlite3.Row | None:
        admin = self.current_admin()
        if not admin:
            self.redirect("/admin/login")
            return None
        return admin

    def admin_nav(self) -> str:
        return """
        <section class='panel'>
          <a class="button" href="/admin/logout">Salir</a>
        </section>
        """

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        raw = body.decode()
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def respond(self, body: bytes, status: HTTPStatus = HTTPStatus.OK, cookie: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path: str, cookie: str | None = None, clear_cookie: bool = False):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", path)
        if clear_cookie:
            self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        elif cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def home(self, error: str = ""):
        self.redirect("/admin/login")

    def admin_login_page(self, error: str = ""):
        if self.current_admin():
            self.redirect("/admin")
            return
        err = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        self.respond(
            page_shell(
                "Admin Login",
                f"""
                <section class="panel">
                  <h2>Acceso administrador</h2>
                  {err}
                  <form method="post" action="/admin/login">
                    <label>Email</label><input name="email" type="email" required>
                    <label>Clave</label><input name="password" type="password" required>
                    <p><button>Entrar</button></p>
                  </form>
                </section>
                """,
            )
        )

    def admin_login_post(self):
        form = self.read_form()
        admin = authenticate_admin(form.get("email", ""), form.get("password", ""))
        if not admin:
            self.admin_login_page("Credenciales de administrador invalidas.")
            return
        self.redirect(
            "/admin",
            cookie=f"admin_session={sign_admin_session(admin['id'])}; Path=/; HttpOnly; SameSite=Lax",
        )

    def admin_logout(self):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/admin/login")
        self.send_header("Set-Cookie", "admin_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.end_headers()

    def admin(self, parsed):
        if not self.require_admin():
            return
        with db() as conn:
            users = conn.execute(
                """
                SELECT id, email, display_name, telegram_user_id, telegram_username, bot_id, access_token, telegram_chat_id, token_balance, active, created_at
                FROM users
                ORDER BY id DESC
                """
            ).fetchall()
            admins = conn.execute(
                "SELECT id, email, active, created_at FROM admins ORDER BY id DESC"
            ).fetchall()
        rows = "".join(
            f"""
            <tr>
              <td>{u['id']}</td>
              <td>{html.escape(u['display_name'] or u['email'])}</td>
              <td>{u['telegram_user_id'] or '-'}</td>
              <td>{html.escape('@' + u['telegram_username']) if u['telegram_username'] else '-'}</td>
              <td>{html.escape(u['bot_id'])}</td>
              <td>{u['token_balance']}</td>
              <td>{'Activo' if u['active'] else 'Inactivo'}</td>
              <td class="token">{html.escape(u['access_token'])}</td>
              <td>
                <form method="post" action="/admin/user-action">
                  <input type="hidden" name="user_id" value="{u['id']}">
                  <input name="tokens" type="number" placeholder="Tokens">
                  <button name="action" value="add_tokens">Sumar</button>
                  <button name="action" value="subtract_tokens">Restar</button>
                  <button name="action" value="set_tokens">Fijar</button>
                </form>
                <form method="post" action="/admin/user-action">
                  <input type="hidden" name="user_id" value="{u['id']}">
                  <input name="telegram_user_id" type="number" placeholder="Telegram ID">
                  <button name="action" value="set_telegram_id">Asignar ID</button>
                  <button name="action" value="clear_telegram_id">Limpiar ID</button>
                </form>
                <form method="post" action="/admin/user-action">
                  <input type="hidden" name="user_id" value="{u['id']}">
                  <button name="action" value="toggle_active">{'Desactivar' if u['active'] else 'Activar'}</button>
                  <button name="action" value="regenerate_token">Nuevo access token</button>
                  <button name="action" value="delete_user">Eliminar</button>
                </form>
              </td>
            </tr>
            """
            for u in users
        )
        admin_rows = "".join(
            f"<tr><td>{a['id']}</td><td>{html.escape(a['email'])}</td><td>{'Activo' if a['active'] else 'Inactivo'}</td></tr>"
            for a in admins
        )
        self.respond(
            page_shell(
                "Admin",
                f"""
                {self.admin_nav()}
                <section class='panel'>
                  <h2>Administracion de usuarios</h2>
                  <form method="post" action="/admin/create-user">
                    <label>Nombre o alias</label><input name="display_name" required>
                    <label>Telegram ID</label><input name="telegram_user_id" type="number" placeholder="8325399900">
                    <label>Telegram username opcional</label><input name="telegram_username" placeholder="@usuario">
                    <label>Tokens iniciales</label><input name="initial_tokens" type="number" min="0" value="0">
                    <p><button>Crear usuario</button></p>
                  </form>
                </section>
                <section class='panel'>
                  <h2>Usuarios</h2>
                  <table>
                    <tr><th>ID</th><th>Nombre</th><th>Telegram ID</th><th>Username</th><th>Bot ID</th><th>Saldo</th><th>Estado</th><th>Token interno</th><th>Acciones</th></tr>
                    {rows}
                  </table>
                </section>
                <section class='panel'>
                  <h2>Administradores</h2>
                  <form method="post" action="/admin/create-admin">
                    <label>Email</label><input name="email" type="email" required>
                    <label>Clave</label><input name="password" type="password" minlength="10" required>
                    <p><button>Crear administrador</button></p>
                  </form>
                  <table><tr><th>ID</th><th>Email</th><th>Estado</th></tr>{admin_rows}</table>
                </section>
                """,
            )
        )

    def admin_user_action(self, parsed):
        if not self.require_admin():
            return
        form = self.read_form()
        try:
            user_id = int(form.get("user_id", "0"))
        except ValueError:
            self.redirect("/admin")
            return
        action = form.get("action", "")
        try:
            token_value = int(form.get("tokens", "0") or "0")
        except ValueError:
            token_value = 0
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if user:
                if action == "add_tokens" and token_value > 0:
                    conn.execute("UPDATE users SET token_balance = token_balance + ? WHERE id = ?", (token_value, user_id))
                    add_ledger(conn, user_id, token_value, "admin_add_tokens", {})
                elif action == "subtract_tokens" and token_value > 0:
                    delta = -min(token_value, user["token_balance"])
                    conn.execute("UPDATE users SET token_balance = token_balance + ? WHERE id = ?", (delta, user_id))
                    add_ledger(conn, user_id, delta, "admin_subtract_tokens", {})
                elif action == "set_tokens" and token_value >= 0:
                    delta = token_value - user["token_balance"]
                    conn.execute("UPDATE users SET token_balance = ? WHERE id = ?", (token_value, user_id))
                    add_ledger(conn, user_id, delta, "admin_set_tokens", {"new_balance": token_value})
                elif action == "toggle_active":
                    new_active = 0 if user["active"] else 1
                    conn.execute("UPDATE users SET active = ? WHERE id = ?", (new_active, user_id))
                    add_ledger(conn, user_id, 0, "admin_toggle_active", {"active": bool(new_active)})
                elif action == "regenerate_token":
                    new_token = make_access_token()
                    conn.execute("UPDATE users SET access_token = ?, telegram_chat_id = NULL WHERE id = ?", (new_token, user_id))
                    add_ledger(conn, user_id, 0, "admin_regenerate_access_token", {})
                elif action == "set_telegram_id":
                    telegram_user_id_raw = form.get("telegram_user_id", "").strip()
                    if telegram_user_id_raw:
                        try:
                            telegram_user_id = int(telegram_user_id_raw)
                        except ValueError:
                            telegram_user_id = None
                        if telegram_user_id is not None:
                            conn.execute(
                                "UPDATE users SET telegram_user_id = ?, telegram_chat_id = NULL WHERE id = ?",
                                (telegram_user_id, user_id),
                            )
                            add_ledger(conn, user_id, 0, "admin_set_telegram_id", {"telegram_user_id": telegram_user_id})
                elif action == "clear_telegram_id":
                    conn.execute(
                        "UPDATE users SET telegram_user_id = NULL, telegram_chat_id = NULL WHERE id = ?",
                        (user_id,),
                    )
                    add_ledger(conn, user_id, 0, "admin_clear_telegram_id", {})
                elif action == "delete_user":
                    conn.execute("DELETE FROM ledger WHERE user_id = ?", (user_id,))
                    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
        self.redirect("/admin")

    def admin_create_admin(self, parsed):
        if not self.require_admin():
            return
        form = self.read_form()
        try:
            create_admin(form.get("email", ""), form.get("password", ""))
        except (ValueError, sqlite3.IntegrityError):
            pass
        self.redirect("/admin")

    def admin_create_user(self, parsed):
        if not self.require_admin():
            return
        form = self.read_form()
        display_name = form.get("display_name", "")
        telegram_user_id_raw = form.get("telegram_user_id", "")
        telegram_username = form.get("telegram_username", "")
        initial_tokens = int(form.get("initial_tokens", "0") or "0")
        try:
            telegram_user_id = int(telegram_user_id_raw) if telegram_user_id_raw.strip() else None
            create_user(display_name, telegram_user_id, telegram_username, initial_tokens)
        except (ValueError, sqlite3.IntegrityError):
            pass
        self.redirect("/admin")

    def log_message(self, format, *args):
        print(f"[dashboard] {self.address_string()} - {format % args}")


def start_http_server(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def start_dashboard() -> ThreadingHTTPServer:
    server = start_http_server(DASHBOARD_HOST, DASHBOARD_PORT, DashboardHandler)
    print(f"[+] Dashboard admin: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/admin/login")
    return server


def main():
    init_db()
    start_dashboard()

    if not TELEGRAM_TOKEN:
        print("[!] Bot de Telegram deshabilitado por falta de TELEGRAM_TOKEN.")
        print("[+] El panel admin sigue activo en modo local.")
        print("[->] Presiona Ctrl+C para detener.")
        threading.Event().wait()
        return

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(iniciar_procesador).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))

    print("[+] Bot de Telegram inicializado.")
    print("[+] Usuarios internos por admin y saldo por consulta activos.")
    print("[->] Presiona Ctrl+C para detener.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
