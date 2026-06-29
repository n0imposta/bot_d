# DealerBot

Bot de Telegram con un panel de administrador unico. No existe dashboard publico para usuarios: todo se crea y se controla internamente desde admin.

## URL

Panel administrador:

```text
http://127.0.0.1:8000/admin/login
```

Si no defines `ADMIN_EMAIL` y `ADMIN_PASSWORD`, el sistema crea un administrador inicial y guarda sus credenciales en `admin_credentials.txt`.

## Ngrok

Para exponer el panel admin:

```powershell
ngrok http 127.0.0.1:8000
```

La URL publica lleva directo al login del administrador.

## Flujo

1. El administrador crea usuarios internos desde `/admin`.
2. A cada usuario se le asigna un `Bot ID`, un `Telegram ID` y un `access token` interno.
3. El usuario entra al bot de Telegram `@Mi_Info_Service_bot`.
4. Si su `Telegram ID` fue registrado por admin, el bot lo reconoce.
5. El usuario envia un RUT y el bot descuenta `QUERY_COST` token(s).
6. Si la consulta falla, el sistema devuelve automaticamente el costo.

## Administracion

En el panel admin puedes:

- Crear usuarios internos.
- Asignar o limpiar `Telegram ID`.
- Ver saldo, estado, Bot ID y token interno.
- Sumar, restar o fijar tokens.
- Activar o desactivar cuentas.
- Eliminar usuarios de forma definitiva.
- Crear nuevos administradores.

## Oracle Cloud

La ruta recomendada para correr el bot completo es una VM Always Free en Oracle Cloud.

1. Crea una instancia Linux Always Free.
2. Abre puertos `22` para SSH y `8000` para el panel admin.
3. Conecta por SSH y ejecuta:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
git clone https://github.com/n0imposta/bot_d.git
cd bot_d
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env
```

4. Edita `.env` con tus credenciales reales.
5. Copia `deploy/oracle/dealerbot.service` a `/etc/systemd/system/dealerbot.service`.
6. Activa el servicio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dealerbot
sudo journalctl -u dealerbot -f
```

El panel quedara en `http://IP_PUBLICA:8000/admin/login`.
