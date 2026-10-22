from __future__ import annotations

import hashlib
import io
import logging
import threading
from pathlib import Path

from PIL import Image, ImageCms

log = logging.getLogger(__name__)

# Colorimétrico relativo con compensación de punto negro: lo que hacen Photoshop
# e InDesign por defecto al "Convertir en perfil" para imprenta. NOCACHE porque
# LittleCMS guarda el último píxel en la transformación y aquí se comparte entre
# hilos.
_INTENT = ImageCms.Intent.RELATIVE_COLORIMETRIC
_FLAGS = ImageCms.Flags.BLACKPOINTCOMPENSATION | ImageCms.Flags.NOCACHE

_SRGB_KEY = "sRGB"


class PressColorError(RuntimeError):
    pass


class CmykConverter:
    """Separa imágenes al perfil CMYK de la imprenta.

    Las imágenes con perfil incrustado se convierten desde ese perfil; las que
    no lo traen se tratan como sRGB, que es lo que son las de Scryfall y casi
    todas las de los drives.
    """

    def __init__(self, output_profile: Path) -> None:
        try:
            self._output = ImageCms.ImageCmsProfile(str(output_profile))
        except (OSError, ImageCms.PyCMSError) as e:
            raise PressColorError(f"No se pudo cargar el perfil {output_profile.name}: {e}") from e
        if self._output.profile.xcolor_space.strip() != "CMYK":
            raise PressColorError(f"{output_profile.name} no es un perfil CMYK")
        self._transforms: dict[tuple[str, str], ImageCms.ImageCmsTransform] = {}
        self._lock = threading.Lock()

    def to_cmyk(self, img: Image.Image, icc_profile: bytes | None = None) -> Image.Image:
        """`img` debe venir en RGB o CMYK. Devuelve siempre una imagen CMYK."""
        if img.mode not in ("RGB", "CMYK"):
            raise ValueError(f"Modo no soportado para separar: {img.mode}")
        source = self._source_profile(img.mode, icc_profile)
        if source is None:
            # CMYK sin perfil: se asume ya separado para imprenta y se respeta.
            return img
        key, profile = source
        transform = self._transform(key, profile, img.mode)
        converted = ImageCms.applyTransform(img, transform)
        if converted is None:  # solo ocurre con inPlace=True
            raise PressColorError("La conversión a CMYK no devolvió imagen")
        return converted

    def _source_profile(
        self, mode: str, icc_profile: bytes | None
    ) -> tuple[str, ImageCms.ImageCmsProfile | None] | None:
        if icc_profile:
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
            except (OSError, ImageCms.PyCMSError):
                log.debug("Perfil incrustado ilegible; se ignora")
            else:
                space = profile.profile.xcolor_space.strip()
                if space == mode:
                    return hashlib.sha256(icc_profile).hexdigest(), profile
        if mode == "CMYK":
            return None
        return _SRGB_KEY, None

    def _transform(
        self, key: str, profile: ImageCms.ImageCmsProfile | None, mode: str
    ) -> ImageCms.ImageCmsTransform:
        with self._lock:
            cached = self._transforms.get((key, mode))
            if cached is not None:
                return cached
            source = profile if profile is not None else ImageCms.createProfile("sRGB")
            transform = ImageCms.buildTransform(
                source, self._output, mode, "CMYK", renderingIntent=_INTENT, flags=_FLAGS
            )
            self._transforms[(key, mode)] = transform
            return transform

    def describe_source(self, mode: str, icc_profile: bytes | None) -> str:
        source = self._source_profile(mode, icc_profile)
        if source is None:
            return "cmyk_untagged"
        return "srgb" if source[0] == _SRGB_KEY else "embedded"
