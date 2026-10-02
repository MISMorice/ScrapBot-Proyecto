"""Parsing de páginas con selectores CSS (BeautifulSoup) o XPath (lxml).

Los selectores viven en la base de datos (R1). En CSS, un sufijo `@atributo`
extrae un atributo en vez del texto (ej. `span.precio@data-price`); si no hay
selector antes de la `@`, se lee el atributo del propio ítem (`@data-sku`).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import soupsieve
from bs4 import BeautifulSoup, FeatureNotFound
from lxml import etree
from lxml import html as lxml_html

FIELD_NAMES = ("sku", "ean", "name", "price", "stock")
_ATTR_RE = re.compile(r"^(.*?)@([A-Za-z_][\w:.-]*)$", re.S)


class SelectorError(ValueError):
    pass


def split_css_attr(selector: str) -> tuple[str, str | None]:
    m = _ATTR_RE.match(selector.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    return selector.strip(), None


def validate_selector(kind: str, expr: str | None, *, allow_attr: bool = True) -> None:
    """Comprobación de sintaxis antes de guardar (Tabla 5, fila 2)."""
    expr = (expr or "").strip()
    if not expr:
        raise SelectorError("El selector está vacío")
    if kind == "css":
        base, attr = split_css_attr(expr) if allow_attr else (expr, None)
        if base == "" and attr:
            return
        try:
            soupsieve.compile(base)
        except Exception as exc:
            raise SelectorError(f"Selector CSS inválido: {exc}") from None
    elif kind == "xpath":
        try:
            etree.XPath(expr)
        except etree.XPathError as exc:
            raise SelectorError(f"XPath inválido: {exc}") from None
    else:
        raise SelectorError(f"Tipo de selector desconocido: {kind}")


@dataclass(frozen=True)
class RuleSet:
    selector_type: str
    item_selector: str | None
    field_selectors: dict
    next_page_selector: str | None
    max_pages: int
    price_includes_vat: bool
    version: int = 1

    @classmethod
    def from_row(cls, row) -> "RuleSet":
        fields = {f: row[f"{f}_selector"] for f in FIELD_NAMES if row[f"{f}_selector"]}
        return cls(
            selector_type=row["selector_type"],
            item_selector=(row["item_selector"] or None),
            field_selectors=fields,
            next_page_selector=(row["next_page_selector"] or None),
            max_pages=row["max_pages"],
            price_includes_vat=bool(row["price_includes_vat"]),
            version=row["version"],
        )

    @property
    def configured(self) -> set:
        return set(self.field_selectors)


@dataclass
class ParsedItem:
    index: int
    values: dict
    content_hash: str


@dataclass
class PageResult:
    items: list = field(default_factory=list)
    next_url: str | None = None


def hash_values(values: dict) -> str:
    """SHA-256 del contenido capturado (Tabla 5, fila 4)."""
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = " ".join(map(str, value))
    text = " ".join(str(value).split())
    return text or None


class PageParser:
    def __init__(self, rules: RuleSet):
        self.rules = rules

    def parse(self, html: str, base_url: str) -> PageResult:
        if not html or not html.strip():
            raise ValueError("La respuesta no contiene HTML")
        if self.rules.selector_type == "xpath":
            return self._parse_xpath(html, base_url)
        return self._parse_css(html, base_url)

    # --- CSS -----------------------------------------------------------
    def _parse_css(self, html, base_url) -> PageResult:
        try:
            soup = BeautifulSoup(html, "lxml")
        except FeatureNotFound:
            soup = BeautifulSoup(html, "html.parser")
        nodes = soup.select(self.rules.item_selector) if self.rules.item_selector else [soup]
        result = PageResult()
        for i, node in enumerate(nodes):
            values = {f: self._css_value(node, sel) for f, sel in self.rules.field_selectors.items()}
            result.items.append(ParsedItem(i, values, hash_values(values)))
        result.next_url = self._css_next(soup, base_url)
        return result

    @staticmethod
    def _css_value(node, selector):
        base, attr = split_css_attr(selector)
        target = node if base == "" else node.select_one(base)
        if target is None:
            return None
        return _clean(target.get(attr) if attr else target.get_text(" ", strip=True))

    def _css_next(self, soup, base_url):
        sel = self.rules.next_page_selector
        if not sel:
            return None
        base, attr = split_css_attr(sel)
        target = soup.select_one(base) if base else None
        href = target.get(attr or "href") if target is not None else None
        return _resolve(base_url, href)

    # --- XPath ---------------------------------------------------------
    def _parse_xpath(self, html, base_url) -> PageResult:
        parser = lxml_html.HTMLParser(encoding="utf-8")
        doc = lxml_html.document_fromstring(html.encode("utf-8"), parser=parser)
        if self.rules.item_selector:
            nodes = [n for n in doc.xpath(self.rules.item_selector) if isinstance(n, etree._Element)]
        else:
            nodes = [doc]
        result = PageResult()
        for i, node in enumerate(nodes):
            values = {f: self._xpath_value(node, sel) for f, sel in self.rules.field_selectors.items()}
            result.items.append(ParsedItem(i, values, hash_values(values)))
        sel = self.rules.next_page_selector
        if sel:
            found = doc.xpath(sel)
            first = found[0] if isinstance(found, list) and found else (None if isinstance(found, list) else found)
            href = first.get("href") if isinstance(first, etree._Element) else first
            result.next_url = _resolve(base_url, str(href) if href else None)
        return result

    @staticmethod
    def _xpath_value(node, selector):
        found = node.xpath(selector)
        if isinstance(found, list):
            if not found:
                return None
            found = found[0]
        if isinstance(found, etree._Element):
            try:
                return _clean(found.text_content())
            except Exception:
                return _clean("".join(found.itertext()))
        return _clean(found)


def _resolve(base_url: str, href: str | None) -> str | None:
    if not href or href.strip().lower().startswith(("javascript:", "#", "mailto:")):
        return None
    return urljoin(base_url, href.strip())
