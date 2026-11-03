# ScrapBot

ScrapBot es un prototipo web para recopilar y comparar precios de catálogos en línea. Permite registrar fuentes autorizadas, extraer productos, normalizar sus precios y mostrar las ofertas en un catálogo consolidado. También registra variaciones importantes para que una persona administradora las revise antes de aplicar el nuevo precio.

El proyecto incluye tiendas locales de demostración. Estas sirven para probar el flujo completo sin depender de tiendas externas; no son datos de comercios reales.

## Objetivo

Facilitar la consulta centralizada de precios y apoyar su seguimiento mediante capturas periódicas, historial y alertas de variación.

## Funcionalidades principales

- Inicio de sesión con selector **Usuario / Administrador**.
- **Edición manual de precio y stock** para administradores, con motivo obligatorio, historial y auditoría.
- **Automatización sin intervención:** captura cada N minutos (15 por defecto); las variaciones se publican al instante y generan alertas, con aviso en pantalla.
- Alertas por **cambio brusco** y por **variación acumulada** (subidas graduales que suman más que el umbral).
- **Exportación de la auditoría** de cambios de precio y de la bitácora completa (CSV que abre en Excel).
- Creación de usuarios desde el panel (para administradores) o desde la línea de comandos.
- Registro de fuentes y configuración de selectores CSS o XPath.
- Extracción de páginas HTML, incluyendo navegación por paginación.
- Normalización de SKU/EAN, nombre, precio, IVA y disponibilidad.
- Historial de precios; modo opcional de revisión manual de variaciones.
- Catálogo público con búsqueda, filtros y enlaces a las fuentes (oculta productos retirados de la tienda).
- Registro de ejecuciones, errores de descarga y errores de extracción.
- Tiendas locales de demostración: Ferretería, Electro y Jardín.

## Arquitectura: metodología por capas

La aplicación organiza sus responsabilidades en tres capas lógicas. Son partes de una misma aplicación Flask, no tres servidores o programas independientes.

| Capa | Componentes principales | Responsabilidad |
| --- | --- | --- |
| Presentación | `scrapbot/web.py`, `scrapbot/templates/`, `scrapbot/static/` | Recibe las solicitudes web, valida las acciones de la interfaz y muestra el panel, el catálogo, las alertas y los resultados. |
| Lógica de negocio | `scrapbot/engine/`, `scrapbot/forms.py`, `scrapbot/auth.py` | Ejecuta la captura y paginación, interpreta selectores, normaliza productos, detecta variaciones y aplica reglas de autenticación y validación. |
| Datos | `scrapbot/db.py`, `scrapbot/schema.sql`, SQLite | Define y consulta las tablas; conserva usuarios, fuentes, productos, ejecuciones, historial y alertas. |

### Flujo de una captura

```text
Usuario inicia una captura
        ↓
Presentación (web.py) solicita la ejecución
        ↓
Negocio (engine/pipeline.py) coordina la descarga y el procesamiento
        ↓
HTTP client → extractor → normalizador → detección de variaciones
        ↓
Datos (SQLite) guarda productos, historial, ejecución y alertas
        ↓
Presentación muestra el resultado en el panel y el catálogo
```

La separación es lógica y facilita entender las responsabilidades. Actualmente algunas rutas de `web.py` consultan la base de datos directamente para construir sus vistas; por eso las capas están diferenciadas, pero no completamente aisladas mediante una capa de servicios o repositorios.

**Explicación completa:** [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md) detalla cada capa, sus módulos, los objetos que cruzan cada frontera, recorridos completos (captura automática, edición de precio, exportación), el modelo de datos, la seguridad por capa y cómo se mejoraría la separación.

## Requisitos

- Python 3.11 o superior.
- Windows, macOS o Linux.
- Las dependencias indicadas en `requirements.txt`.

## Instalación e inicio en Windows

Abre PowerShell en la carpeta raíz del proyecto (donde están `manage.py` y `requirements.txt`) y ejecuta:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py init-db
.\.venv\Scripts\python.exe manage.py create-user profesor --role admin
```

El último comando solicita una contraseña de forma interactiva. Debe tener al menos 10 caracteres y cumplir las reglas de contraseña de la aplicación. El nombre `profesor` es solo un ejemplo: puedes reemplazarlo por el usuario que necesites. Si el usuario ya existe, elige otro nombre.

Inicia el panel:

```powershell
.\.venv\Scripts\python.exe manage.py serve
```

Abre [http://127.0.0.1:5000](http://127.0.0.1:5000) e inicia sesión con la cuenta creada.

> La base de datos se crea en `instance/scrapbot.db`. Los usuarios y los productos permanecen allí entre ejecuciones. No hay una contraseña predeterminada.

### Linux o macOS

Desde la raíz del proyecto:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py init-db
.venv/bin/python manage.py create-user profesor --role admin
.venv/bin/python manage.py serve
```

## Uso de la aplicación

- **Panel privado:** [http://127.0.0.1:5000/](http://127.0.0.1:5000/). Requiere iniciar sesión. Desde el panel se administran fuentes, productos, ejecuciones, alertas, usuarios y parámetros.
- **Catálogo público:** [http://127.0.0.1:5000/catalogo](http://127.0.0.1:5000/catalogo). No requiere iniciar sesión. Muestra ofertas activas y permite buscar y filtrar.
- **Usuarios:** `/users`. Solo disponible para administradores. Permite crear usuarios y activar, desactivar o desbloquear cuentas.

### Tipos de acceso

En la pantalla de inicio de sesión se elige **Usuario** o **Administrador**:

| Acceso | Cuentas que pueden usarlo | Qué permite |
| --- | --- | --- |
| Usuario | Cualquier cuenta activa | Consultar productos, catálogo, ejecuciones y auditoría; ver y marcar alertas; lanzar capturas; exportar. |
| Administrador | Solo cuentas con rol `admin` | Todo lo anterior, más **editar precio y stock**, fuentes, usuarios y parámetros. |

Una cuenta `analyst` que elige «Administrador» es rechazada. Una cuenta `admin` que elige «Usuario» entra sin permisos de edición.

### Editar precio y stock

Con acceso de administrador, en **Productos → Editar** (o en el detalle de un producto):

- El precio se ingresa con IVA incluido; el neto y el IVA se recalculan.
- El motivo es obligatorio y queda en el historial y en la auditoría.
- Con «Mantener este precio y stock», el bot no lo sobrescribe hasta que se pulse «Volver a seguir el precio de la tienda».

### Automatización y alertas

En **Parámetros → Automatización del bot**:

- **Frecuencia:** cada N minutos (5 a 1440; 15 por defecto) o en horarios fijos.
- **Alertas:** por defecto, toda variación se **publica al instante** y, si supera el umbral (15 %), se genera una alerta sin que nadie tenga que aprobar nada. Opcionalmente se puede volver al modo de revisión manual.
- **Variación acumulada:** detecta subidas o bajadas graduales que en la ventana configurada (7 días) superan el umbral.

Las alertas aparecen en **Alertas de precio**, en el panel y como aviso emergente en cualquier pantalla abierta (se consulta cada 20 segundos).

### Exportar auditoría

- **Auditoría de cambios de precio** (`Auditoría` o `Alertas de precio` → Exportar): cada cambio publicado con fecha, precio anterior y nuevo, variación, origen (bot, edición manual o aprobación), usuario, motivo y alerta asociada. Se puede filtrar por fechas y fuente.
- **Bitácora completa** (`Auditoría` → Exportar esta bitácora): todas las acciones del sistema con los filtros aplicados.

Los archivos usan `;` como separador para abrirse directamente en Excel con configuración regional de Chile.

## Demostración local

La demo tiene tres catálogos HTML locales que ScrapBot puede visitar. Para usarla, abre **dos terminales PowerShell** en la carpeta raíz del proyecto.

En la primera terminal, inicia las tiendas demo y déjala abierta:

```powershell
.\.venv\Scripts\python.exe -m demo_site.app
```

En la segunda terminal, registra las fuentes de demostración y ejecuta una captura:

```powershell
.\.venv\Scripts\python.exe manage.py seed-demo
.\.venv\Scripts\python.exe manage.py run --all
```

Las tiendas de demostración están disponibles en:

- [Ferretería](http://127.0.0.1:5001/catalogo)
- [Electro](http://127.0.0.1:5001/electronica)
- [Jardín](http://127.0.0.1:5001/jardin)

Después de una captura, consulta el [catálogo consolidado](http://127.0.0.1:5000/catalogo) y el panel de ejecuciones. Para probar una variación de precio en Ferretería, cambia el precio del SKU de demo y vuelve a ejecutar la captura:

```text
http://127.0.0.1:5001/_demo/precio?sku=DEMO-003&valor=39990
```

Con el modo por defecto, el nuevo precio se publica al instante y aparece una alerta en **Alertas de precio** (y como aviso en pantalla si el panel está abierto). Si en Parámetros se elige el modo de revisión manual, el cambio queda retenido hasta aprobarlo o rechazarlo con una justificación.

Para ver la automatización completa, deja `manage.py serve` corriendo: capturará las fuentes cada 15 minutos sin intervención.

### Capturar una fuente específica

Cada fuente tiene un identificador numérico, visible en la administración de fuentes. Para capturar solo una:

```powershell
.\.venv\Scripts\python.exe manage.py run --source ID
```

Reemplaza `ID` por el identificador de la fuente. Para capturar todas las fuentes activas, usa `--all`.

## Pruebas

Desde la raíz del proyecto:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

En Linux o macOS:

```bash
.venv/bin/python -m pytest -q
```

La suite (128 pruebas) incluye autenticación y rutas web, cliente HTTP, extracción, normalización, almacenamiento, programación y flujo completo. La prueba de aceptación procesa un lote de 100 productos y comprueba capturas repetidas. `tests/test_correcciones.py` cubre los bugs corregidos en la entrega del 50 % y `tests/test_entrega.py` las funcionalidades de la entrega 80-100 %.

> Si ya tenías una base de datos de una versión anterior, al iniciar la aplicación se agregan automáticamente las columnas y tablas nuevas, sin perder datos.

## Correcciones de la entrega del 50 %

| # | Problema | Corrección |
| --- | --- | --- |
| 1 | Un usuario inexistente respondía cientos de veces más rápido en el login, lo que permitía averiguar qué cuentas existen. | El hash de comparación ficticio usa el mismo costo que las contraseñas reales. |
| 2 | Al cambiar la tasa de IVA, los productos con IVA incluido conservaban el neto y el IVA calculados con la tasa anterior. | Un cambio de desglose también actualiza el producto y queda en el historial. |
| 3 | Un intento HTTP fallido que se recuperaba con un reintento marcaba la ejecución «Con observaciones». | Solo cuenta como error la página que finalmente no se pudo descargar; todos los intentos siguen en el registro HTTP. |
| 4 | Un producto que la tienda dejaba de publicar seguía en el catálogo público indefinidamente. | Tras una captura completa, los productos ausentes se marcan como retirados y se ocultan; vuelven si reaparecen. Una captura incompleta no retira nada. |
| 5 | Varias subidas pequeñas (14 % cada una) nunca superaban el umbral del 15 % y se publicaban sin revisión. | La variación también se compara con el precio de hace hasta 7 días; si el acumulado supera el umbral, se congela para revisión, igual que un salto brusco. |

Las bases de datos existentes se actualizan solas al iniciar, sin perder datos. Las pruebas de estas correcciones están en `tests/test_correcciones.py`.

> En la entrega 80-100 % las variaciones sobre el umbral (bruscas o acumuladas) se publican al instante y generan alertas; el congelamiento para revisión queda como modo opcional en Parámetros.

## Seguridad y uso responsable

- Las contraseñas se almacenan con hash PBKDF2; no se pueden consultar desde la base de datos.
- El panel usa sesiones con expiración, protección CSRF, bloqueo temporal tras intentos fallidos y una política CSP.
- Por defecto, el extractor respeta `robots.txt`, identifica sus solicitudes y aplica pausas, timeout y reintentos.
- Registra únicamente fuentes para las que tengas autorización. Respeta las condiciones de uso de cada sitio y evita sobrecargar sus servicios.
- Las tiendas incluidas son una demo local; antes de usar un sitio real, configura selectores apropiados y verifica que el acceso esté permitido.

## Estructura del proyecto

```text
manage.py                  Comandos de inicialización, usuarios, capturas y servidor
config.py                  Configuración de la aplicación
wsgi.py                    Punto de entrada WSGI
scrapbot/
  __init__.py              Creación y configuración de la aplicación Flask
  web.py                   Rutas del panel y del catálogo
  auth.py                  Autenticación, roles, sesiones y CSRF
  forms.py                 Validación de fuentes y parámetros
  db.py                    Conexiones, inicialización y utilidades SQLite
  schema.sql               Tablas e índices de la base de datos
  audit.py                 Registro de auditoría
  engine/
    http_client.py         Descargas HTTP, pausas, reintentos y robots.txt
    extractor.py           Extracción con selectores CSS o XPath
    normalizer.py          Normalización de precios, IVA e inventario
    detector.py            Detección de variaciones de precio
    store.py               Persistencia de productos e incidencias
    pipeline.py            Orquestación del proceso de captura
    scheduler.py           Ejecuciones programadas
  templates/               Plantillas HTML
  static/                  Estilos y recursos estáticos
docs/ARQUITECTURA.md       Explicación detallada del modelo de capas
demo_site/                  Catálogos locales de demostración
tests/                      Pruebas unitarias, de integración y aceptación
instance/                   Base de datos y clave local generada
```

## Alcance y próximos pasos

El estado actual es un prototipo funcional con fuentes de demostración locales. Para aplicarlo a tiendas reales, se deben configurar y validar las URL y selectores permitidos para cada fuente. La extracción está orientada a páginas HTML; sitios cuyo catálogo se genera exclusivamente con JavaScript podrían requerir una estrategia de integración adicional.

Como pasos siguientes se pueden considerar pruebas con fuentes reales autorizadas, documentación de selectores por tienda, despliegue en un servidor seguro y evaluación de la calidad y frecuencia de actualización de los datos.

## Referencias del proyecto

- Repositorio: [MISMorice/ScrapBot-Proyecto](https://github.com/MISMorice/ScrapBot-Proyecto)
