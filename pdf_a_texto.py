"""
pdf_a_texto.py

Lee todos los PDFs de una carpeta de entrada (y subcarpetas) y genera
un archivo .txt por cada PDF en una carpeta de salida, con el mismo
nombre base. Usa extracción de texto nativo y, si el PDF resulta ser
escaneado (poco o ningún texto), hace OCR automáticamente.

------------------------------------------------------------------
INSTALACIÓN (una sola vez)
------------------------------------------------------------------

1) Librerías de Python:
   pip install pymupdf pytesseract pdf2image

2) Herramientas de sistema (necesarias para el OCR y para convertir
   páginas de PDF a imagen):

   Windows:
     - Instala Tesseract OCR:
         https://github.com/UB-Mannheim/tesseract/wiki
       (durante la instalación, marca el idioma "Spanish" si lo pide,
        o descarga spa.traineddata aparte y ponlo en la carpeta tessdata)
     - Instala Poppler para Windows:
         https://github.com/oschwartz10612/poppler-windows/releases
       Descomprime y agrega la carpeta "bin" al PATH, o indica la ruta
       con la variable POPPLER_PATH más abajo en este script.
     - Si Tesseract no queda en el PATH, define TESSERACT_CMD más abajo.

   Linux (Debian/Ubuntu):
     sudo apt-get install tesseract-ocr tesseract-ocr-spa poppler-utils

   Mac:
     brew install tesseract tesseract-lang poppler

------------------------------------------------------------------
USO
------------------------------------------------------------------

   python pdf_a_texto.py "C:\\ruta\\carpeta_pdfs" "C:\\ruta\\carpeta_txt"

   o simplemente edita CARPETA_ENTRADA y CARPETA_SALIDA abajo y corre:

   python pdf_a_texto.py
------------------------------------------------------------------
"""

import sys
import traceback
from pathlib import Path

import fitz  # PyMuPDF
from pdf2image import convert_from_path
import pytesseract

# ================== CONFIGURACIÓN ==================

# Si no pasas argumentos por consola, se usan estas rutas por defecto.
# Ejemplo Windows: r"C:\Datos\pdfs_entrada"
CARPETA_ENTRADA = r"pdfs"
CARPETA_SALIDA = r"pdfs_salida_txt"

# Umbral: si una página nativa trae menos caracteres que esto, se asume
# que el PDF es escaneado y se activa el OCR para todo el documento.
MIN_CHARS_POR_PAGINA = 30

# Idioma para el OCR (usa "spa" para español, "eng" para inglés,
# o "spa+eng" para ambos)
IDIOMA_OCR = "spa"

# --- Solo en Windows, si Tesseract/Poppler no están en el PATH ---
# Descomenta y ajusta las siguientes líneas si es necesario:
# TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
POPPLER_PATH = None  # ej: r"C:\poppler-24.x\Library\bin"

# =====================================================


def extraer_texto_nativo(pdf_path: Path) -> list[str]:
    textos = []
    doc = fitz.open(pdf_path)
    for page in doc:
        textos.append(page.get_text("text"))
    doc.close()
    return textos


def extraer_texto_ocr(pdf_path: Path) -> list[str]:
    kwargs = {"dpi": 300}
    if POPPLER_PATH:
        kwargs["poppler_path"] = POPPLER_PATH
    imagenes = convert_from_path(pdf_path, **kwargs)
    textos = []
    for img in imagenes:
        texto = pytesseract.image_to_string(img, lang=IDIOMA_OCR)
        textos.append(texto)
    return textos


def procesar_pdf(pdf_path: Path) -> tuple[str, str, str | None]:
    """Devuelve (texto_extraido, metodo_usado, error)."""
    try:
        paginas_nativas = extraer_texto_nativo(pdf_path)
        texto_nativo = "".join(paginas_nativas).strip()
        promedio = len(texto_nativo) / max(len(paginas_nativas), 1)

        if promedio >= MIN_CHARS_POR_PAGINA:
            return "\n\n".join(paginas_nativas).strip(), "nativo", None
        else:
            paginas_ocr = extraer_texto_ocr(pdf_path)
            return "\n\n".join(paginas_ocr).strip(), "ocr", None

    except Exception as e:
        return "", "error", f"{e}\n{traceback.format_exc()}"


def main():
    if len(sys.argv) >= 3:
        carpeta_entrada = Path(sys.argv[1])
        carpeta_salida = Path(sys.argv[2])
    else:
        carpeta_entrada = Path(CARPETA_ENTRADA)
        carpeta_salida = Path(CARPETA_SALIDA)

    if not carpeta_entrada.exists():
        print(f"ERROR: no existe la carpeta de entrada: {carpeta_entrada}")
        sys.exit(1)

    carpeta_salida.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(carpeta_entrada.rglob("*.pdf"))
    print(f"Encontrados {len(pdfs)} PDFs en: {carpeta_entrada}")
    print(f"Los .txt se guardarán en: {carpeta_salida}\n")

    ok, ocr_usados, fallidos = 0, 0, 0

    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name} ... ", end="", flush=True)
        texto, metodo, error = procesar_pdf(pdf_path)

        if error:
            print(f"ERROR ({error.splitlines()[0]})")
            fallidos += 1
            # Igual dejamos rastro del error en un .txt para revisión
            nombre_salida = carpeta_salida / f"{pdf_path.stem}__ERROR.txt"
            nombre_salida.write_text(error, encoding="utf-8")
            continue

        nombre_salida = carpeta_salida / f"{pdf_path.stem}.txt"
        nombre_salida.write_text(texto, encoding="utf-8")

        if metodo == "ocr":
            ocr_usados += 1
            print("OK (via OCR)")
        else:
            print("OK (texto nativo)")
        ok += 1

    print(f"\nResumen: {ok} procesados, {fallidos} con error, "
          f"{ocr_usados} requirieron OCR.")


if __name__ == "__main__":
    main()