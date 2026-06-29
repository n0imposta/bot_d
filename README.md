# DealerBot

Bot de Telegram con un panel de administrador único. No existe dashboard público para usuarios: todo se crea y se controla internamente desde admin.

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

La URL pública lleva directo al login del administrador.

## Flujo

1. El administrador crea usuarios internos desde `/admin`.
2. A cada usuario se le asigna un `Bot ID` y un `access token` interno.
3. El usuario entra al bot de Telegram `@Mi_Info_Service_bot`.
4. Si su `@telegram` fue registrado por admin, el bot lo reconoce.
5. El usuario envía un RUT y el bot descuenta `QUERY_COST` token(s).
6. Si la consulta falla, el sistema devuelve automáticamente el costo.

## Administracion

En el panel admin puedes:

- Crear usuarios internos.
- Asignar o limpiar `@telegram`.
- Ver saldo, estado, Bot ID y token interno.
- Sumar, restar o fijar tokens.
- Activar o desactivar cuentas.
- Eliminar usuarios de forma definitiva.
- Crear nuevos administradores.
