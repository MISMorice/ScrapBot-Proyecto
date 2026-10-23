"""Validación de formularios de fuentes y parámetros (Tabla 5, fila 2)."""
from __future__ import annotations

from urllib.parse import urlsplit

from .engine.extractor import SelectorError, validate_selector
from .engine.scheduler import VALID_INTERVALS

SELECTOR_FIELDS = ("sku", "ean", "name", "price", "stock")


def _valid_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname) and " " not in url


def validate_source_form(form) -> tuple[dict, dict]:
    """Devuelve (datos_limpios, errores). `errores` mapea campo -> mensaje."""
    errors: dict[str, str] = {}
    data: dict = {}

    data["name"] = " ".join(form.get("name", "").split())
    if not 1 <= len(data["name"]) <= 80:
        errors["name"] = "El nombre es obligatorio (máximo 80 caracteres)"

    urls = [u.strip() for u in form.get("start_urls", "").splitlines() if u.strip()]
    data["start_urls"] = "\n".join(urls)
    if not urls:
        errors["start_urls"] = "Ingrese al menos una URL"
    elif len(urls) > 50:
        errors["start_urls"] = "Máximo 50 URLs por fuente"
    else:
        bad = [u for u in urls if not _valid_url(u)]
        if bad:
            errors["start_urls"] = f"URL inválida (use http:// o https://): {bad[0]}"

    try:
        data["delay_min"] = float(form.get("delay_min", "2"))
        data["delay_max"] = float(form.get("delay_max", "5"))
        if not (1 <= data["delay_min"] <= data["delay_max"] <= 60):
            raise ValueError
    except ValueError:
        errors["delay"] = "La pausa entre peticiones debe estar entre 1 y 60 s (mínimo ≤ máximo)"
    data["respect_robots"] = 1 if form.get("respect_robots") else 0

    kind = form.get("selector_type", "css")
    data["selector_type"] = kind
    if kind not in ("css", "xpath"):
        errors["selector_type"] = "Tipo de selector inválido"
        kind = None

    for fld in ("item_selector", *[f"{f}_selector" for f in SELECTOR_FIELDS], "next_page_selector"):
        data[fld] = form.get(fld, "").strip() or None

    if kind:
        required = {"name_selector": "nombre", "price_selector": "precio"}
        for fld, label in required.items():
            if not data[fld]:
                errors[fld] = f"El selector de {label} es obligatorio"
        if not data["sku_selector"] and not data["ean_selector"]:
            errors["sku_selector"] = "Configure al menos un selector de identificador (SKU o EAN)"
        for fld in ("item_selector", *[f"{f}_selector" for f in SELECTOR_FIELDS], "next_page_selector"):
            if data[fld] and fld not in errors:
                try:
                    validate_selector(kind, data[fld], allow_attr=(fld != "item_selector"))
                except SelectorError as exc:
                    errors[fld] = str(exc)

    try:
        data["max_pages"] = int(form.get("max_pages", "20"))
        if not 1 <= data["max_pages"] <= 200:
            raise ValueError
    except ValueError:
        errors["max_pages"] = "Páginas máximas: entre 1 y 200"
    data["price_includes_vat"] = 1 if form.get("price_includes_vat") else 0
    return data, errors


def validate_settings_form(form) -> tuple[dict, dict]:
    errors, data = {}, {}
    try:
        data["vat_rate"] = float(form.get("vat_rate", ""))
        if not 0 <= data["vat_rate"] <= 50:
            raise ValueError
    except ValueError:
        errors["vat_rate"] = "El IVA debe estar entre 0 y 50 %"
    try:
        data["variation_threshold"] = float(form.get("variation_threshold", ""))
        if not 1 <= data["variation_threshold"] <= 100:
            raise ValueError
    except ValueError:
        errors["variation_threshold"] = "El umbral debe estar entre 1 y 100 %"
    try:
        interval = int(form.get("scheduler_interval_minutes", ""))
        if interval not in VALID_INTERVALS:
            raise ValueError
        data["scheduler_interval_minutes"] = str(interval)
    except ValueError:
        errors["scheduler_interval_minutes"] = "Seleccione un intervalo válido (5, 15 o 60 minutos)"
    data["scheduler_enabled"] = "1" if form.get("scheduler_enabled") else "0"
    return data, errors
