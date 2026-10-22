from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mpc_forge.paths import resources_dir

# Medidas de una carta de Magic real. Las imágenes de Scryfall y de los drives de
# MPC Autofill parten de este tamaño (con o sin 1/8" de sangrado alrededor).
MTG_CARD_W_MM = 63.5
MTG_CARD_H_MM = 88.9


@dataclass(frozen=True)
class PrintShopSpec:
    """Lo que una imprenta exige a cada página del PDF que recibe.

    Una página es una carta: el tamaño final (trim) más el sangrado por cada
    lado. Las cotas salen de la plantilla oficial de la imprenta, no de su web.
    """

    key: str
    shop_name: str
    product_name: str
    trim_w_mm: float
    trim_h_mm: float
    bleed_mm: float
    safe_margin_mm: float
    corner_radius_mm: float
    ppi: int
    max_bytes_per_page: int
    icc_filename: str
    # Condición de impresión registrada en el ICC (https://www.color.org): con
    # ella el OutputIntent identifica FOGRA39 sin incrustar 8 MB de perfil.
    output_condition_identifier: str
    output_condition: str
    output_registry: str
    package_folder: str
    package_pdf_name: str
    delivery_email: str
    delivery_url: str
    templates_url: str
    # Va a la imprenta, así que está en su idioma y no en el de la interfaz.
    delivery_message_template: str

    @property
    def page_w_mm(self) -> float:
        return self.trim_w_mm + 2 * self.bleed_mm

    @property
    def page_h_mm(self) -> float:
        return self.trim_h_mm + 2 * self.bleed_mm

    @property
    def page_px(self) -> tuple[int, int]:
        return (
            round(self.page_w_mm / 25.4 * self.ppi),
            round(self.page_h_mm / 25.4 * self.ppi),
        )

    @property
    def icc_path(self) -> Path:
        return resources_dir() / "icc" / self.icc_filename

    @property
    def package_entry(self) -> str:
        return f"{self.package_folder}/{self.package_pdf_name}"

    def delivery_message(self, deck_name: str, total_cards: int, total_pages: int) -> str:
        return self.delivery_message_template.format(
            deck=deck_name,
            cards=total_cards,
            pages=total_pages,
            product=self.product_name,
            entry=self.package_entry,
            bleed=f"{self.bleed_mm:g}",
            profile=self.output_condition,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "shop_name": self.shop_name,
            "product_name": self.product_name,
            "trim_w_mm": self.trim_w_mm,
            "trim_h_mm": self.trim_h_mm,
            "bleed_mm": self.bleed_mm,
            "safe_margin_mm": self.safe_margin_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "page_w_mm": self.page_w_mm,
            "page_h_mm": self.page_h_mm,
            "ppi": self.ppi,
            "max_bytes_per_page": self.max_bytes_per_page,
            "color_profile": self.output_condition,
            "package_entry": self.package_entry,
            "delivery_email": self.delivery_email,
            "delivery_url": self.delivery_url,
            "templates_url": self.templates_url,
        }


# Pinbro Games (https://www.pinbrogames.com/plantillas/), plantilla MOD20_63x88:
# página 69×94 mm, corte 63×88 mm con esquinas de 3 mm y zona segura 57×82 mm.
# La guía pide CMYK Coated FOGRA39 a 300 ppp, un PDF por tamaño con la medida en
# el nombre, cada anverso seguido de su reverso y como mucho 3 MB por página.
PINBRO_POKER = PrintShopSpec(
    key="pinbro_poker",
    shop_name="Pinbro Games",
    product_name="Póquer 63×88 mm",
    trim_w_mm=63.0,
    trim_h_mm=88.0,
    bleed_mm=3.0,
    safe_margin_mm=3.0,
    corner_radius_mm=3.0,
    ppi=300,
    # "3Mb" en la guía: se toma el megabyte decimal, el más estricto de los dos.
    max_bytes_per_page=3_000_000,
    icc_filename="Coated_Fogra39L_VIGC_300.icc",
    output_condition_identifier="FOGRA39",
    output_condition="Coated FOGRA39 (ISO 12647-2:2004)",
    output_registry="http://www.color.org",
    package_folder="Cartas",
    package_pdf_name="Póquer(63x88).pdf",
    delivery_email="info@pinbrogames.com",
    delivery_url="https://wetransfer.com/",
    templates_url="https://www.pinbrogames.com/plantillas/",
    delivery_message_template=(
        "Hola:\n\n"
        "Os envío por WeTransfer los archivos del pedido «{deck}»: {cards} cartas "
        "tamaño {product}, impresas a doble cara.\n\n"
        "El archivo {entry} tiene {pages} páginas, cada anverso seguido de su "
        "reverso y en el orden del mazo, maquetadas sobre vuestra plantilla "
        "(sangrado de {bleed} mm por lado, CMYK {profile}, sin marcas de corte).\n\n"
        "Muchas gracias. Un saludo."
    ),
)

PRINT_SHOPS: dict[str, PrintShopSpec] = {PINBRO_POKER.key: PINBRO_POKER}


def get_print_shop(key: str) -> PrintShopSpec | None:
    return PRINT_SHOPS.get(key)
