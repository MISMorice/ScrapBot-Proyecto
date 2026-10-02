"""Tres tiendas locales para probar la captura de ScrapBot.

Ejecutar con ``python -m demo_site.app``. Catálogos: /catalogo,
/electronica y /jardin.
"""
from __future__ import annotations

from flask import Flask, abort, redirect, request, url_for

PAGE_SIZE = 8

STORE_STYLES = """
<style>
:root{color-scheme:light;--forest:#153f34;--forest-deep:#102f28;--lime:#d3e565;--ink:#192620;--muted:#68766e;--paper:#f1f3ed;--line:#dce2d9;--white:#fff;--alert:#b84734;--font:Arial,Helvetica,sans-serif;--display:Georgia,'Times New Roman',serif}
body.store-theme-electronica{--forest:#24576b;--forest-deep:#163b4b;--lime:#c8e4e8}
body.store-theme-jardin{--forest:#566440;--forest-deep:#35422b;--lime:#e4d990}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--font)}
a{color:inherit}
.utility-bar{background:var(--forest-deep);color:#e6eee7;padding:7px 24px;text-align:center;font-size:.72rem;letter-spacing:1.2px;text-transform:uppercase}
.site-header{max-width:1280px;margin:auto;min-height:78px;padding:14px 28px;display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{display:flex;align-items:center;gap:11px;text-decoration:none}
.brand-mark{width:42px;height:42px;display:grid;place-items:center;background:var(--lime);color:var(--forest-deep);font-weight:900;font-size:.9rem}
.brand-copy strong{display:block;font:700 1.12rem var(--display)}
.brand-copy small{display:block;color:var(--muted);font-size:.74rem}
.header-note{color:var(--muted);font-size:.74rem;font-weight:700;letter-spacing:1.2px;text-align:right;text-transform:uppercase}
.demo-nav{display:flex;align-items:center;gap:15px}
.demo-nav a{padding:7px 0;border-bottom:2px solid transparent;color:var(--muted);font-size:.78rem;font-weight:700;text-decoration:none}
.demo-nav a[aria-current="page"]{border-bottom-color:var(--forest);color:var(--forest)}
.store-main{width:min(100% - 40px,1224px);margin:0 auto 56px}
.store-hero{position:relative;min-height:340px;display:flex;align-items:center;overflow:hidden;background:var(--forest);color:var(--white)}
.hero-photo{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:center 48%}
.store-hero:after{position:absolute;inset:0;background:rgba(11,39,31,.66);content:""}
.hero-copy{position:relative;z-index:1;max-width:570px;padding:38px 48px}
.hero-eyebrow{margin:0 0 12px;color:var(--lime);font-size:.73rem;font-weight:700;letter-spacing:2px;text-transform:uppercase}
.hero-copy h1{margin:0;font:700 3.2rem/1.04 var(--display)}
.hero-copy p:not(.hero-eyebrow){max-width:390px;margin:14px 0 22px;color:#ecf0ea;font-size:1rem}
.hero-link{display:inline-flex;align-items:center;gap:16px;padding:10px 14px;background:var(--lime);color:var(--forest-deep);font-size:.84rem;font-weight:700;text-decoration:none}
.hero-link span{font-size:1.15rem;line-height:1}
.catalogue-bar{display:flex;justify-content:space-between;gap:12px;padding:11px 2px;border-bottom:1px solid var(--line);color:var(--muted);font-size:.77rem;letter-spacing:.8px;text-transform:uppercase}
.catalogue-bar strong{color:var(--forest);font-weight:700}
.products-section{padding-top:28px;scroll-margin-top:18px}
.section-heading{display:flex;justify-content:space-between;align-items:end;gap:16px;margin-bottom:16px}
.section-heading p{margin:0 0 3px;color:var(--forest);font-size:.7rem;font-weight:700;letter-spacing:1.8px;text-transform:uppercase}
.section-heading h2{margin:0;font:700 1.85rem/1.15 var(--display)}
.result-count{color:var(--muted);font-size:.83rem;white-space:nowrap}
.product-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.producto{position:relative;min-width:0;min-height:226px;padding:16px 16px 14px;background:var(--white);border:1px solid var(--line);border-radius:5px;display:flex;flex-direction:column}
.producto:before{height:3px;margin:-16px -16px 15px;background:var(--forest);content:""}
.card-kicker{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:.67rem;letter-spacing:.5px;text-transform:uppercase}
.card-kicker span:last-child{font-family:Consolas,monospace;letter-spacing:0}
.producto .nombre{margin:13px 0 8px;font-size:1.04rem;line-height:1.3;font-weight:700}
.ean-line{display:flex;gap:7px;align-items:center;color:var(--muted);font-size:.7rem}
.ean-line strong{font-weight:500;letter-spacing:.6px;text-transform:uppercase}
.ean{font-family:Consolas,monospace;font-size:.74rem}
.card-footer{display:flex;justify-content:space-between;align-items:end;gap:8px;margin-top:auto;padding-top:14px}
.price-label{display:block;margin-bottom:1px;color:var(--muted);font-size:.68rem;text-transform:uppercase}
.precio{display:block;color:var(--forest);font-size:1.22rem;font-weight:800;font-variant-numeric:tabular-nums}
.stock{display:inline-block;padding:4px 7px;background:#edf4df;color:#36592f;font-size:.68rem;font-weight:700;text-align:center}
.stock.sold-out{background:#f8e8e3;color:var(--alert)}
.pagination{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:18px;padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.pagination-note{color:var(--muted);font-size:.84rem}
.page-link{min-width:94px;padding:8px 12px;border:1px solid var(--forest);color:var(--forest);font-size:.8rem;font-weight:700;text-align:center;text-decoration:none}
.page-link:hover{background:var(--forest);color:var(--white)}
.page-link.disabled{border-color:var(--line);color:#98a29b;pointer-events:none}
.admin-section{margin-top:34px;padding:20px 22px;background:#e7ece3;border-top:4px solid var(--forest)}
.admin-heading{display:flex;justify-content:space-between;align-items:end;gap:18px;margin-bottom:14px}
.admin-heading p{margin:0 0 3px;color:var(--forest);font-size:.7rem;font-weight:700;letter-spacing:1.6px;text-transform:uppercase}
.admin-heading h2{margin:0;font:700 1.55rem/1.2 var(--display)}
.admin-heading span{color:var(--muted);font-size:.78rem}
.admin-form{display:grid;grid-template-columns:minmax(240px,1.5fr) minmax(170px,.8fr) auto;align-items:end;gap:12px}
.admin-field label{display:block;margin-bottom:5px;color:var(--muted);font-size:.78rem;font-weight:700}
.admin-field select,.admin-field input{width:100%;min-height:42px;padding:8px 10px;border:1px solid #b9c6b8;border-radius:2px;background:var(--white);color:var(--ink);font:inherit}
.admin-field input{font-variant-numeric:tabular-nums}
.admin-submit{min-height:42px;padding:9px 15px;border:0;background:var(--forest);color:var(--white);font:700 .82rem var(--font);cursor:pointer}
.admin-submit:hover{background:var(--forest-deep)}
.admin-confirmation{margin-bottom:14px;padding:12px 14px;border-left:4px solid var(--forest);background:var(--white);color:var(--ink);font-size:.87rem}
.admin-confirmation p{margin:0 0 4px}
.admin-confirmation a{color:var(--forest);font-weight:700}
.site-footer{max-width:1224px;margin:0 auto;padding:18px 0 28px;color:var(--muted);font-size:.74rem}
.site-footer strong{color:var(--forest)}
@media(max-width:900px){.product-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.store-hero{min-height:310px}}
@media(max-width:560px){.utility-bar{font-size:.62rem}.site-header{min-height:68px;padding:12px 16px;flex-wrap:wrap}.header-note{display:none}.demo-nav{order:3;width:100%;justify-content:center;gap:24px}.store-main{width:calc(100% - 24px);margin-bottom:32px}.store-hero{min-height:300px}.hero-photo{object-position:58% center}.hero-copy{padding:28px 22px}.hero-copy h1{font-size:2.55rem}.catalogue-bar{font-size:.64rem}.products-section{padding-top:23px}.section-heading{align-items:start;flex-direction:column;gap:5px}.section-heading h2{font-size:1.6rem}.product-grid{grid-template-columns:1fr;gap:9px}.producto{min-height:200px;padding:15px}.producto:before{margin:-15px -15px 14px}.admin-section{padding:17px 14px}.admin-heading{align-items:start;flex-direction:column;gap:5px}.admin-heading h2{font-size:1.35rem}.admin-form{grid-template-columns:1fr}.admin-submit{width:100%}.site-footer{padding:16px 12px 24px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
</style>
"""


def ean13(body12: str) -> str:
    total = sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


def _base_products() -> list[dict]:
    names = [
        "Taladro percutor 650 W", "Sierra circular 7 1/4", "Lijadora orbital", "Caja de herramientas 19\"",
        "Set de destornilladores 32 pzs", "Nivel láser autonivelante", "Atornillador inalámbrico 12 V",
        "Esmeril angular 4 1/2", "Cinta métrica 8 m", "Alicate universal 8\"", "Pistola de calor 2000 W",
        "Compresor de aire 24 L", "Generador eléctrico 2500 W", "Martillo carpintero 16 oz", "Llave ajustable 10\"",
        "Escalera de aluminio 6 peldaños", "Guantes de seguridad", "Casco de seguridad", "Lentes protectores",
        "Extensión eléctrica 10 m", "Taladro de banco", "Soldadora inverter 200 A", "Disco de corte 7\" (x10)",
        "Pintura látex blanco 1 gl",
    ]
    products = [
        {"sku": f"DEMO-{index:03d}", "ean": ean13(f"780123456{index:03d}"), "name": name,
         "price": 4990 + index * 3170, "stock": (index * 7) % 23}
        for index, name in enumerate(names, start=1)
    ]
    products[4]["price_text"] = "Consultar"
    products[9]["ean"] = "7801234567890"
    products.append(dict(products[1]))
    return products


def _category_products(prefix: str, names: list[str], ean_prefix: str,
                       base_price: int, price_step: int) -> list[dict]:
    return [
        {"sku": f"{prefix}-{index:03d}", "ean": ean13(f"{ean_prefix}{index:03d}"), "name": name,
         "price": base_price + index * price_step, "stock": (index * 7) % 23}
        for index, name in enumerate(names, start=1)
    ]


def create_demo_app() -> Flask:
    app = Flask(__name__)
    stores = {
        "ferreteria": {
            "name": "Ferretería Demo", "mark": "FD", "subtitle": "Herramientas y materiales",
            "header_note": "Equipo para cada proyecto<br>Catálogo online",
            "eyebrow": "Herramientas · Construcción · Hogar", "hero_title": "Ferretería<br>Demo",
            "description": "Equipo y materiales para construir, reparar y darle forma a tu próximo proyecto.",
            "image": "https://images.unsplash.com/photo-1504148455328-c376907d081c?auto=format&fit=crop&w=2000&q=85",
            "image_alt": "Taladro eléctrico sobre una mesa de trabajo", "department": "Ferretería",
            "catalogue_meta": "Precios en CLP · IVA incluido", "section_label": "Selección ferretera",
            "section_title": "Encuentra lo que necesitas", "endpoint": "catalogo",
            "price_path": "/_demo/precio", "default_sku": "DEMO-003", "default_price": 15990,
            "products": _base_products(),
        },
        "electronica": {
            "name": "Electro Demo", "mark": "ED", "subtitle": "Tecnología para todos los días",
            "header_note": "Tecnología útil<br>Diseño cotidiano",
            "eyebrow": "Audio · Computación · Conectividad", "hero_title": "Tecnología<br>en movimiento",
            "description": "Dispositivos y accesorios para trabajar, escuchar y conectar mejor.",
            "image": "https://images.unsplash.com/photo-1498049794561-7780e7231661?auto=format&fit=crop&w=2000&q=85",
            "image_alt": "Selección de dispositivos electrónicos sobre una mesa", "department": "Tecnología",
            "catalogue_meta": "Audio · Computación · Accesorios", "section_label": "Tecnología seleccionada",
            "section_title": "Conecta con lo que viene", "endpoint": "electronica",
            "price_path": "/_demo/electronica/precio", "default_sku": "ELEC-003", "default_price": 28000,
            "products": _category_products("ELEC", [
                "Audífonos inalámbricos", "Parlante Bluetooth portátil", "Monitor LED 27 pulgadas",
                "Teclado mecánico compacto", "Mouse ergonómico inalámbrico", "Webcam Full HD",
                "Router Wi-Fi 6", "Disco SSD externo 1 TB", "Tablet 10 pulgadas", "Cargador USB-C 65 W",
                "Barra de sonido compacta", "Power bank 20.000 mAh", "Micrófono USB de escritorio",
                "Hub USB-C multipuerto", "Lector electrónico 6 pulgadas", "Proyector portátil HD",
            ], "780987654", 6990, 6200),
        },
        "jardin": {
            "name": "Jardín Demo", "mark": "JD", "subtitle": "Cultiva tu espacio",
            "header_note": "Ideas para el jardín<br>Todo en temporada",
            "eyebrow": "Exterior · Cultivo · Decoración", "hero_title": "Haz espacio<br>para crecer",
            "description": "Herramientas y detalles para cuidar tus plantas y disfrutar el aire libre.",
            "image": "https://images.unsplash.com/photo-1416879595882-3373a0480b5b?auto=format&fit=crop&w=2000&q=85",
            "image_alt": "Plantas y herramientas para el cuidado del jardín", "department": "Jardín",
            "catalogue_meta": "Cultivo · Riego · Exterior", "section_label": "Vida al aire libre",
            "section_title": "Dale vida a tus espacios", "endpoint": "jardin",
            "price_path": "/_demo/jardin/precio", "default_sku": "JARD-003", "default_price": 12290,
            "products": _category_products("JARD", [
                "Tijera de podar bypass", "Guantes de jardinería", "Regadera metálica 5 L",
                "Set de herramientas de cultivo", "Manguera reforzada 20 m", "Macetero cerámico mediano",
                "Sustrato universal 20 L", "Semillas de huerto surtidas", "Aspersor giratorio",
                "Pala de jardín acero", "Kit de riego por goteo", "Farol solar para exterior",
                "Compostera doméstica 40 L", "Jardinera de madera", "Rastrillo de mano",
                "Lámpara solar de sendero",
            ], "780987653", 2990, 2380),
        },
    }

    @app.route("/robots.txt")
    def robots():
        return "User-agent: *\nDisallow: /privado/\nCrawl-delay: 1\n", 200, {"Content-Type": "text/plain"}

    def render_catalogue(store_key: str):
        store = stores[store_key]
        products = store["products"]
        page = max(1, request.args.get("page", 1, type=int))
        chunk = products[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        if not chunk:
            abort(404)
        total_pages = (len(products) + PAGE_SIZE - 1) // PAGE_SIZE
        updated_sku = request.args.get("actualizado", "")
        updated_value = request.args.get("valor", type=int)
        available_skus = {product["sku"] for product in products}
        selected_sku = updated_sku if updated_sku in available_skus else store["default_sku"]
        cards, options, option_skus = [], [], set()
        for product in chunk:
            price = product.get("price_text") or "${:,}".format(product["price"]).replace(",", ".")
            stock = "Agotado" if product["stock"] == 0 else f"Stock: {product['stock']} unidades"
            stock_class = "stock sold-out" if product["stock"] == 0 else "stock"
            cards.append(
                f'<article class="producto" data-sku="{product["sku"]}">'
                f'<div class="card-kicker"><span>{store["department"]}</span><span>Ref. {product["sku"]}</span></div>'
                f'<h2 class="nombre">{product["name"]}</h2>'
                f'<div class="ean-line"><strong>EAN</strong><span class="ean">{product["ean"]}</span></div>'
                f'<div class="card-footer"><div><span class="price-label">Precio</span>'
                f'<span class="precio">{price}</span></div>'
                f'<span class="{stock_class}">{stock}</span></div></article>')
        for product in products:
            if product["sku"] in option_skus:
                continue
            option_skus.add(product["sku"])
            price = product.get("price_text") or "${:,}".format(product["price"]).replace(",", ".")
            selected = " selected" if product["sku"] == selected_sku else ""
            options.append(f'<option value="{product["sku"]}"{selected}>{product["sku"]} · {product["name"]} · {price}</option>')

        changed_product = next((product for product in products if product["sku"] == updated_sku), None)
        changed = (changed_product is not None and updated_value is not None
                   and changed_product["price"] == updated_value and "price_text" not in changed_product)
        confirmation = (
            '<div class="admin-confirmation" role="status"><p>Precio guardado en la tienda demo. '
            'Vuelve a ejecutar la fuente para que ScrapBot capture el cambio.</p>'
            '<a href="http://127.0.0.1:5000/sources">Abrir Fuentes en ScrapBot</a></div>'
            if changed else "")
        default_product = next(product for product in products if product["sku"] == store["default_sku"])
        price_value = updated_value if changed else store.get("default_price", int(default_product["price"] * 1.1))
        previous = (f'<a class="page-link anterior" href="{url_for(store["endpoint"], page=page - 1)}" rel="prev">Anterior</a>'
                    if page > 1 else '<span class="page-link disabled" aria-disabled="true">Anterior</span>')
        following = (f'<a class="page-link siguiente" href="{url_for(store["endpoint"], page=page + 1)}" rel="next">Siguiente</a>'
                     if page < total_pages else '<span class="page-link disabled" aria-disabled="true">Siguiente</span>')

        return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#153f34">
  <title>{store['name']} · Catálogo online</title>
  {STORE_STYLES}
</head>
<body class="store-theme-{store_key}">
  <div class="utility-bar">Catálogo de demostración · {store['name']}</div>
  <header class="site-header">
    <a class="brand" href="{url_for(store['endpoint'])}" aria-label="{store['name']}, inicio">
      <span class="brand-mark" aria-hidden="true">{store['mark']}</span>
      <span class="brand-copy"><strong>{store['name']}</strong><small>{store['subtitle']}</small></span>
    </a>
    <span class="header-note">{store['header_note']}</span>
    <nav class="demo-nav" aria-label="Tiendas de demostración">
      <a href="/catalogo"{' aria-current="page"' if store_key == 'ferreteria' else ''}>Ferretería</a>
      <a href="/electronica"{' aria-current="page"' if store_key == 'electronica' else ''}>Tecnología</a>
      <a href="/jardin"{' aria-current="page"' if store_key == 'jardin' else ''}>Jardín</a>
    </nav>
  </header>
  <main class="store-main">
    <section class="store-hero" aria-labelledby="store-title">
      <img class="hero-photo" src="{store['image']}" alt="{store['image_alt']}">
      <div class="hero-copy">
        <p class="hero-eyebrow">{store['eyebrow']}</p>
        <h1 id="store-title">{store['hero_title']}</h1>
        <p>{store['description']}</p>
        <a class="hero-link" href="#productos">Ver productos <span aria-hidden="true">↓</span></a>
      </div>
    </section>
    <div class="catalogue-bar"><strong>Catálogo de prueba</strong><span>{store['catalogue_meta']}</span></div>
    <section class="products-section" id="productos" aria-labelledby="products-title">
      <div class="section-heading">
        <div><p>{store['section_label']}</p><h2 id="products-title">{store['section_title']}</h2></div>
        <span class="result-count">Página {page} de {total_pages}</span>
      </div>
      <div class="product-grid">{''.join(cards)}</div>
      <nav class="pagination" aria-label="Páginas del catálogo">
        {previous}<span class="pagination-note">Página {page} / {total_pages}</span>{following}
      </nav>
    </section>
    <section class="admin-section" id="administracion" aria-labelledby="admin-title">
      <div class="admin-heading">
        <div><p>Administración local</p><h2 id="admin-title">Cambiar un precio de prueba</h2></div>
        <span>Solo modifica la tienda de demostración</span>
      </div>
      {confirmation}
      <form class="admin-form" method="post" action="{store['price_path']}">
        <div class="admin-field">
          <label for="sku">Producto</label>
          <select id="sku" name="sku" required>{''.join(options)}</select>
        </div>
        <div class="admin-field">
          <label for="valor">Nuevo precio (CLP)</label>
          <input id="valor" name="valor" type="number" min="1" step="1" value="{price_value}" required>
        </div>
        <button class="admin-submit" type="submit">Guardar precio</button>
      </form>
    </section>
  </main>
  <footer class="site-footer"><strong>{store['name']}</strong> · Entorno local de demostración de ScrapBot.</footer>
</body>
</html>"""

    @app.route("/catalogo")
    def catalogo():
        return render_catalogue("ferreteria")

    @app.route("/electronica")
    def electronica():
        return render_catalogue("electronica")

    @app.route("/jardin")
    def jardin():
        return render_catalogue("jardin")

    @app.route("/_demo/precio", defaults={"store_key": "ferreteria"}, methods=["GET", "POST"])
    @app.route("/_demo/<store_key>/precio", methods=["POST"])
    def set_price(store_key):
        store = stores.get(store_key)
        if store is None:
            abort(404)
        products = store["products"]
        if request.method == "POST":
            sku = request.form.get("sku", "")
            value = request.form.get("valor", type=int)
            product = next((item for item in products if item["sku"] == sku), None)
            if product is None or value is None or value < 1:
                abort(400)
            product["price"] = value
            product.pop("price_text", None)
            product_page = products.index(product) // PAGE_SIZE + 1
            return redirect(url_for(store["endpoint"], page=product_page, actualizado=sku,
                                    valor=value, _anchor="administracion"))
        sku, value = request.args.get("sku", ""), request.args.get("valor", type=int)
        for product in products:
            if product["sku"] == sku and value and value > 0:
                product["price"] = value
                product.pop("price_text", None)
        return f"{sku} -> {value}\n"

    @app.route("/_demo/reset")
    def reset():
        stores["ferreteria"]["products"] = _base_products()
        return "reiniciado\n"

    return app


if __name__ == "__main__":
    create_demo_app().run(host="127.0.0.1", port=5001)
