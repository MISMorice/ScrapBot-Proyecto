"""Normalización y homologación (Tabla 5, filas 5 y 6).

* Precios en formato chileno ("$12.990", "12.990,50") -> entero CLP.
* Validación de SKU y EAN/UPC/GTIN (dígito verificador GS1).
* Desglose de IVA: el precio de origen puede venir con IVA incluido (se
  desglosa) o sin IVA (se recarga).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_PRICE_CLP = 500_000_000
MAX_STOCK = 10_000_000
SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9 \-_./]{0,63}$")
EAN_LENGTHS = {8, 12, 13, 14}

_NUM_TOKEN = re.compile(r"\d[\d.,]*")
_OUT_OF_STOCK = ("agotado", "sin stock", "sin existencias", "no disponible", "out of stock", "sold out")
_IN_STOCK = ("en stock", "disponible", "available", "in stock")


class InvalidRecord(Exception):
    """El valor capturado no es utilizable. `field` indica el campo afectado."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


# --- Precio ---------------------------------------------------------------

def _decimal_from_token(token: str) -> Decimal:
    token = token.rstrip(".,")
    has_dot, has_comma = "." in token, "," in token
    if has_dot and has_comma:
        decimal_sep = "." if token.rfind(".") > token.rfind(",") else ","
        thousands = "," if decimal_sep == "." else "."
        token = token.replace(thousands, "").replace(decimal_sep, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = token.split(sep)
        if len(parts) > 2 or len(parts[-1]) == 3:
            token = "".join(parts)            # separador de miles
        else:
            token = parts[0] + "." + parts[1]  # separador decimal
    return Decimal(token)


def parse_price(text: str | None) -> int:
    """Texto de precio -> entero CLP. Rechaza vacíos, rangos y valores absurdos."""
    if text is None or not str(text).strip():
        raise InvalidRecord("price", "Precio ausente o vacío")
    cleaned = str(text).replace("\xa0", " ")
    cleaned = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", cleaned)  # "12 990" -> "12990"
    tokens = _NUM_TOKEN.findall(cleaned)
    if not tokens:
        raise InvalidRecord("price", f"Precio sin valor numérico: {text!r}")
    if len(tokens) > 1:
        raise InvalidRecord("price", f"Precio ambiguo (varios valores numéricos): {text!r}")
    try:
        value = _decimal_from_token(tokens[0])
    except InvalidOperation:
        raise InvalidRecord("price", f"Precio con formato inválido: {text!r}") from None
    price = int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if price <= 0 or price > MAX_PRICE_CLP:
        raise InvalidRecord("price", f"Precio fuera de rango: {price}")
    return price


@dataclass(frozen=True)
class PriceBreakdown:
    net: int
    vat: int
    final: int


def compute_breakdown(price: int, includes_vat: bool, vat_rate: float) -> PriceBreakdown:
    """Desglosa (IVA incluido) o recarga (precio neto) el IVA. Todo en CLP enteros."""
    rate = Decimal(str(vat_rate)) / 100
    one = Decimal(1)
    if includes_vat:
        final = price
        net = int((Decimal(final) / (1 + rate)).quantize(one, rounding=ROUND_HALF_UP))
        vat = final - net
    else:
        net = price
        vat = int((Decimal(net) * rate).quantize(one, rounding=ROUND_HALF_UP))
        final = net + vat
    return PriceBreakdown(net=net, vat=vat, final=final)


# --- Stock ----------------------------------------------------------------

def parse_stock(text: str | None) -> tuple[int | None, bool | None]:
    """Devuelve (unidades, disponible). `unidades` es None si solo se sabe que hay stock."""
    s = " ".join(str(text or "").lower().split())
    if not s:
        raise InvalidRecord("stock", "Stock ausente o vacío")
    if any(k in s for k in _OUT_OF_STOCK):
        return 0, False
    match = re.search(r"\d[\d.]*", s)
    if match:
        units = int(match.group().replace(".", ""))
        if units > MAX_STOCK:
            raise InvalidRecord("stock", f"Stock fuera de rango: {units}")
        return units, units > 0
    if any(k in s for k in _IN_STOCK):
        return None, True
    raise InvalidRecord("stock", f"Stock no interpretable: {text!r}")


# --- Identificadores -------------------------------------------------------

def ean_checksum_ok(code: str) -> bool:
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalize_ean(text: str | None) -> str | None:
    if text is None or not str(text).strip():
        return None
    code = re.sub(r"[\s\-]", "", str(text))
    if not code.isdigit() or len(code) not in EAN_LENGTHS:
        raise InvalidRecord("ean", f"EAN con formato inválido: {text!r}")
    if not ean_checksum_ok(code):
        raise InvalidRecord("ean", f"EAN con dígito verificador incorrecto: {code}")
    return code


def normalize_sku(text: str | None) -> str | None:
    if text is None or not str(text).strip():
        return None
    sku = " ".join(str(text).upper().split())
    if not SKU_RE.match(sku):
        raise InvalidRecord("sku", f"SKU con formato inválido: {text!r}")
    return sku


def normalize_name(text: str | None) -> str:
    name = " ".join(str(text or "").split())
    if not name:
        raise InvalidRecord("name", "Nombre ausente o vacío")
    return name[:300]


# --- Registro completo ---------------------------------------------------

@dataclass
class NormalizedProduct:
    sku: str | None
    ean: str | None
    name: str
    net_price: int
    vat_amount: int
    final_price: int
    stock: int | None
    available: bool | None
    content_hash: str
    warnings: list[tuple[str, str]] = field(default_factory=list)


def normalize_record(values: dict, *, configured: set[str], includes_vat: bool,
                     vat_rate: float, content_hash: str) -> NormalizedProduct:
    """Convierte los textos capturados en un producto válido o lanza InvalidRecord.

    Obligatorios: nombre, precio y al menos un identificador válido (SKU o EAN).
    El stock es opcional: si falla se registra una advertencia y no se modifica.
    """
    warnings: list[tuple[str, str]] = []

    name = normalize_name(values.get("name"))
    if values.get("price") is None:
        raise InvalidRecord("price", "Campo obligatorio 'price' ausente en el DOM (selector sin resultados)")
    price = parse_price(values.get("price"))

    sku = ean = None
    id_errors: list[InvalidRecord] = []
    for fld, fn in (("sku", normalize_sku), ("ean", normalize_ean)):
        try:
            result = fn(values.get(fld))
        except InvalidRecord as exc:
            id_errors.append(exc)
            continue
        if fld == "sku":
            sku = result
        else:
            ean = result
    if sku is None and ean is None:
        detail = "; ".join(e.message for e in id_errors) or "sin SKU ni EAN en el DOM"
        raise InvalidRecord("sku/ean", f"Sin identificador válido: {detail}")
    warnings.extend((e.field, e.message) for e in id_errors)  # identificador malo pero hay otro bueno

    stock = available = None
    if "stock" in configured:
        try:
            stock, available = parse_stock(values.get("stock"))
        except InvalidRecord as exc:
            warnings.append((exc.field, exc.message))

    breakdown = compute_breakdown(price, includes_vat, vat_rate)
    return NormalizedProduct(
        sku=sku, ean=ean, name=name, net_price=breakdown.net, vat_amount=breakdown.vat,
        final_price=breakdown.final, stock=stock, available=available,
        content_hash=content_hash, warnings=warnings,
    )
