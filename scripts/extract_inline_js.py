"""Extrae el JavaScript embebido en los templates a módulos ES.

Situación de partida: `deck.html` tenía 4.286 líneas, de las cuales 2.423 eran
JavaScript dentro de un `<script>`. Sin resaltado de sintaxis fiable, sin
linter, sin posibilidad de reutilizar nada entre vistas y sin forma de
comprobar el JS en CI.

Este script hace la extracción de forma mecánica y verificable, en vez de a
mano. Es idempotente: si el módulo ya existe y coincide, no hace nada.

Requisito previo comprobado: ninguno de los bloques contiene interpolación
Jinja (`{{ }}`, `{% %}`), así que el JS es literal y se puede mover tal cual
sin cambiar una sola línea de lógica. El script aborta si encuentra alguna.

Uso:
    python scripts/extract_inline_js.py [--check]

Con `--check` no escribe nada: solo informa de si la extracción está al día.
Es lo que ejecuta CI.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
JS_DIR = ROOT / "static" / "js"

# Template → nombre del módulo destino. `base.html` queda fuera: sus bloques
# son configuración de arranque muy pequeña, y moverlos añadiría una petición
# extra en la ruta crítica de cada página para ahorrar 90 líneas.
TARGETS = {
    "deck.html": "deck-editor.js",
    "pdf_studio.html": "pdf-studio.js",
    "settings.html": "settings.js",
    "index.html": "home.js",
    "history.html": "history.js",
    "collection.html": "collection.js",
    "print_planner.html": "print-planner.js",
    "art_library.html": "art-library.js",
    "calibration.html": "calibration.js",
}

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)</script>")
JINJA_RE = re.compile(r"\{\{[^}]*\}\}|\{%[^%]*%\}")

HEADER = """/**
 * {title}
 *
 * Extraído de `templates/{template}`, donde vivía como un bloque `<script>`
 * de {lines} líneas. La lógica es idéntica: solo ha cambiado de fichero.
 *
 * Las funciones que Alpine necesita resolver desde los atributos `x-data` del
 * HTML se publican en `window` al final del módulo. Es deliberado: Alpine
 * evalúa `x-data` como una expresión en el ámbito global, así que un `export`
 * por sí solo no basta.
 *
 * Regenerar con:  python scripts/extract_inline_js.py
 */
"""

# Símbolos que el HTML referencia desde atributos de Alpine y que, por tanto,
# tienen que seguir siendo globales tras la extracción.
GLOBAL_DECL_RE = re.compile(
    r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE
)


def collect_globals(js: str) -> list[str]:
    """Nombres de funciones declaradas en el nivel superior del bloque."""
    names = []
    for match in GLOBAL_DECL_RE.finditer(js):
        # Solo las de nivel superior: las anidadas van indentadas.
        line_start = js.rfind("\n", 0, match.start()) + 1
        if js[line_start:match.start()].strip() == "":
            names.append(match.group(1))
    return names


def build_module(template: str, js: str) -> str:
    title = {
        "deck.html": "Editor de mazos",
        "pdf_studio.html": "PDF Studio",
        "settings.html": "Vista de Ajustes",
        "index.html": "Pantalla de inicio",
        "history.html": "Historial y línea de tiempo",
        "collection.html": "Colección por expansión",
    }.get(template, template)

    header = HEADER.format(
        title=title, template=template, lines=len(js.splitlines())
    )

    names = collect_globals(js)
    if names:
        exports = "\n".join(f"window.{n} = {n}" for n in sorted(names))
        footer = (
            "\n\n// --- Puente con Alpine -------------------------------------\n"
            "// Alpine resuelve las expresiones de `x-data` contra el ámbito\n"
            "// global, así que estas funciones tienen que estar en `window`.\n"
            f"{exports}\n"
        )
    else:
        footer = ""

    return header + js.strip() + "\n" + footer


def process(check_only: bool) -> int:
    problems = 0
    changed = 0

    for template, module_name in TARGETS.items():
        path = TEMPLATES / template
        if not path.exists():
            print(f"  aviso  {template} no existe; se omite")
            continue

        html = path.read_text(encoding="utf-8")
        matches = list(SCRIPT_RE.finditer(html))
        inline = [m for m in matches if m.group(2).strip()]

        if not inline:
            print(f"  ok     {template} ya no tiene JS embebido")
            continue

        if len(inline) > 1:
            print(f"  ERROR  {template} tiene {len(inline)} bloques; "
                  f"se esperaba 1")
            problems += 1
            continue

        block = inline[0]
        js = block.group(2)

        jinja = JINJA_RE.findall(js)
        if jinja:
            print(f"  ERROR  {template} interpola Jinja dentro del JS "
                  f"({jinja[:3]}). No se puede extraer sin refactor previo: "
                  f"pasa esos valores por un atributo data-* del HTML.")
            problems += 1
            continue

        module_path = JS_DIR / module_name
        content = build_module(template, js)

        if check_only:
            current = module_path.read_text(encoding="utf-8") if module_path.exists() else ""
            if current != content:
                print(f"  DESFASADO  {module_name}")
                problems += 1
            else:
                print(f"  ok     {module_name} al día")
            continue

        JS_DIR.mkdir(parents=True, exist_ok=True)
        module_path.write_text(content, encoding="utf-8")

        # Sustituir el bloque por la etiqueta que carga el módulo.
        # El bloque embebido se ELIMINA de su sitio y el módulo se declara en
        # `{% block view_module %}`, que base.html renderiza en el <head>
        # ANTES de Alpine.
        #
        # No es un detalle cosmético: la build CDN de Alpine arranca con
        # `queueMicrotask(() => Alpine.start())`, es decir en cuanto termina su
        # propio script diferido. Los `defer` y los `type="module"` comparten
        # el mismo orden de ejecución (orden de documento), así que un módulo
        # declarado más abajo en la página se ejecuta DESPUÉS de que Alpine
        # haya empezado a recorrer el DOM, y las funciones de `x-data` aún no
        # están en `window`. Resultado: "xxx is not defined" y vista en blanco.
        #
        # OJO también: el comentario NO puede contener la palabra "script"
        # entre ángulos. Si la contiene, la expresión regular de este mismo
        # script la detecta como bloque embebido en la siguiente pasada y
        # `--check` reporta un desfase permanente.
        #
        # `asset_v()` añade el mtime como query para invalidar la caché del
        # navegador al actualizar la app: los módulos se sirven con
        # max-age=1 día, así que sin esto un usuario que actualiza seguiría
        # ejecutando el JavaScript viejo durante 24 horas.
        block_declaration = (
            f'{{% block view_module %}}\n'
            f'  {{# La lógica de esta vista vive en /static/js/{module_name}.\n'
            f'     Se extrajo del bloque embebido que había en el cuerpo de\n'
            f'     esta plantilla ({len(js.splitlines())} líneas) para poder '
            f'lintarlo, depurarlo\n'
            f'     con nombres de fichero reales y reutilizar código entre '
            f'vistas.\n'
            f'\n'
            f'     Se declara en este bloque, y no donde estaba, porque '
            f'base.html lo\n'
            f'     renderiza en el <head> antes de Alpine. Ver la nota en '
            f'base.html.\n'
            f'\n'
            f'     Regenerar con scripts/extract_inline_js.py #}}\n'
            f'  <script defer src="/static/js/{module_name}'
            f'?v={{{{ asset_v(\'js/{module_name}\') }}}}"></script>\n'
            f'{{% endblock %}}\n'
        )
        replacement = ""

        html = html[:block.start()] + replacement + html[block.end():]

        # El bloque se inserta justo después de `{% extends %}`, que siempre
        # es la primera línea de una plantilla hija.
        marker = "{% extends"
        if marker not in html:
            print(f"  ERROR  {template} no extiende de base.html")
            problems += 1
            continue
        end_of_extends = html.index("%}", html.index(marker)) + 2
        html = (
            html[:end_of_extends]
            + "\n\n" + block_declaration
            + html[end_of_extends:].lstrip("\n")
        )
        path.write_text(html, encoding="utf-8")

        print(f"  extraído  {template} → {module_name} "
              f"({len(js.splitlines())} líneas)")
        changed += 1

    if check_only:
        if problems:
            print(f"\n{problems} módulos desfasados. "
                  f"Ejecuta: python scripts/extract_inline_js.py")
            return 1
        print("\nTodos los módulos están al día.")
        return 0

    print(f"\n{changed} templates procesados, {problems} problemas.")
    return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="No escribe; solo comprueba que está al día")
    args = parser.parse_args()
    sys.exit(process(args.check))
