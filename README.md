# ScrapBot

Sistema automatizado de extracción, validación y sincronización de catálogos e-commerce desde **fuentes autorizadas**.
Implementa lo descrito en los Capítulos I–III del proyecto de título *“Diseño de un Bot para automatizar la recolección de datos de catálogos web”*.

- **Presentación:** Flask 3 + Jinja2 (panel de administración)
- **Lógica / motor:** Python 3.11+ · Requests · BeautifulSoup4 (CSS) · lxml (XPath)
- **Persistencia:** SQLite 3 en modo WAL, escrituras atómicas (ACID)
- **Pruebas:** pytest (90 pruebas)

## Puesta en marcha

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python manage.py init-db
python manage.py create-user martin --role admin     # pide la contraseña (mín. 10 caracteres, letras y números)
python manage.py serve                               # http://127.0.0.1:5000
```

### Catálogo público

Con el servidor activo, visite `http://127.0.0.1:5000/catalogo`. Esta vista de solo lectura reúne las ofertas de fuentes activas, permite buscar y ordenar, y enlaza al catálogo configurado para cada tienda. El panel interno continúa protegido en `/`.

### Probarlo sin tocar sitios reales (tienda de demostración)

```bash
python -m demo_site.app              # terminal 1 -> tiendas en el puerto 5001
python manage.py seed-demo           # terminal 2: registra Ferretería, Electro y Jardín Demo
python manage.py run --all           # o use «Ejecutar ahora» en el panel
```

Catálogos: `/catalogo` (ferretería), `/electronica` (tecnología) y `/jardin` (jardinería). Cada tienda tiene paginación y un panel local para cambiar precios; las tres comparten `robots.txt`.
Ferretería Demo conserva los casos de prueba de precio “Consultar”, EAN inválido y SKU duplicado. Para provocar una **variación anómala** (R3): abra `http://127.0.0.1:5001/_demo/precio?sku=DEMO-003&valor=39990`, vuelva a ejecutar y revise **Alertas de precio**.

### Ejecuciones intradiarias

`manage.py serve` inicia un programador interno con 4 horarios diarios (00:00, 06:00, 12:00, 18:00, hora de Chile), editables en **Parámetros**.
Alternativa sin proceso residente: programar `python manage.py run --all` con cron o el Programador de tareas de Windows.

### Producción

```bash
pip install waitress
waitress-serve --call wsgi:app          # o: gunicorn wsgi:app
```

Definir `SCRAPBOT_SECRET_KEY`, `SCRAPBOT_COOKIE_SECURE=1` (detrás de HTTPS) y `SCRAPBOT_USER_AGENT` con un contacto real de la organización.
Si no se define la clave secreta, se genera en `instance/secret.key`.

## Pruebas

```bash
pytest -q            # o: python -m unittest discover -s tests -t .
```

Las pruebas levantan servidores HTTP locales; no usan internet.

## Estructura

```
manage.py              CLI: init-db, create-user, seed-demo, run, serve
config.py              parámetros técnicos (timeout, reintentos, pausas, sesión…)
scrapbot/
  __init__.py          create_app (fábrica Flask)
  auth.py              login, bloqueo, expiración de sesión, CSRF
  web.py, forms.py     rutas del panel y validación de formularios
  db.py, schema.sql    conexión SQLite, transacciones, esquema
  audit.py             bitácora de auditoría
  engine/
    http_client.py     cliente HTTP “educado” (pausas, timeout, reintentos, robots.txt)
    extractor.py       parsing CSS/XPath + hash SHA-256 + validación de selectores
    normalizer.py      precios CLP, SKU/EAN, stock, desglose de IVA
    detector.py        variación porcentual contra el precio publicado
    store.py           escritura transaccional, cola de discrepancias, aprobación
    pipeline.py        orquestación de una ejecución
    scheduler.py       ejecuciones intradiarias
  templates/, static/  interfaz
demo_site/             tienda de ejemplo
tests/                 pruebas unitarias, de integración y de aceptación
```

## Trazabilidad con el documento

### Tabla 5 — controles y evidencia

| Flujo / módulo | Implementación | Evidencia en el sistema |
|---|---|---|
| Autenticación y acceso | `auth.py` | Tabla `login_log`; pantalla **Accesos**; bloqueo tras 5 fallos; sesión expira a los 15 min |
| Configuración de fuentes | `forms.py`, `extractor.validate_selector`, `source_rules` | Reglas versionadas con fecha y usuario; pantalla **Fuentes** |
| Petición HTTP | `engine/http_client.py` | Tabla `http_log` (URL, código, tiempo, intento, SHA-256) |
| Parsing de etiquetas | `engine/extractor.py` | Tabla `parse_errors`; `content_hash` SHA-256 por producto |
| Homologación SKU/EAN | `engine/normalizer.py`, `store.find_product` | Validación GS1 del EAN; descarte de duplicados e identificadores inválidos |
| IVA 19 % | `normalizer.compute_breakdown` | `price_history`: neto, IVA, final y tasa aplicada |
| Fluctuaciones anómalas | `engine/detector.py`, `store.apply_products` | Cola `discrepancies` (pendiente) y alerta en el panel |
| Aprobación manual | `store.resolve_discrepancy` | Decisión, motivo obligatorio, usuario y fecha |
| Persistencia transaccional | `db.transaction`, `pipeline.py` | `audit_log` `catalog.commit` con el número de productos escritos |
| Trazabilidad general | `audit.py`, pantallas **Auditoría** y **Ejecuciones** | Historial desde la captura hasta la publicación |

### Tabla 9 — mitigación de riesgos

| Riesgo | Mitigación implementada |
|---|---|
| R3 precios erróneos | Variación > 15 % (editable) congela el precio y deriva a revisión |
| R5 bloqueo de IP | Pausa aleatoria de 2–5 s, `User-Agent` identificado, respeto de `robots.txt` y `Crawl-delay`, sin técnicas de evasión |
| R1 cambio del DOM | Selectores en la BD, editables y versionados; un selector de ítems sin resultados falla la ejecución sin tocar el catálogo |
| R4 corrupción en SQLite | WAL, `synchronous=FULL`, escritura del catálogo en una sola transacción con rollback |
| R2 red / latencia | Timeout de 10 s, 3 reintentos con espera exponencial (1, 2, 4 s) y respeto de `Retry-After` |

## Decisiones de diseño

- **El stock se actualiza aunque el precio quede congelado.** Lo que se retiene es la publicación del precio; mostrar un producto agotado como disponible es otro de los problemas del documento.
- **Una variación exactamente igual al umbral se publica;** se congela solo si lo supera.
- **Si se rechaza una variación y la fuente sigue mostrando ese precio,** la siguiente ejecución genera una nueva alerta (el precio publicado no cambió).
- **`robots.txt` inaccesible (5xx o red) bloquea la captura** por precaución, según RFC 9309. Un 404 equivale a “todo permitido”.
- **Solo se siguen enlaces dentro del dominio de la fuente;** una redirección a otro dominio se rechaza.
- **Un precio con varios valores numéricos** (p. ej. precio normal y precio de oferta juntos) se descarta como ambiguo en vez de adivinar.
- **Las fuentes no se eliminan,** solo se desactivan, para no perder el historial.
- El formulario exige una pausa mínima de 1 s entre peticiones.
