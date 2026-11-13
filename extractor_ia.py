"""
extractor_ia.py

Etapa 2 del pipeline: toma el texto plano de un resultado de citologia
(ya extraido por pdf_a_texto.py) y le pide a un modelo LOCAL, corriendo
en Ollama, que devuelva un JSON estructurado con los codigos del
catalogo DTP_RESULTADO_CITOLOGIA_ESCALA_BETHESDA y del VPH.

La respuesta es DETERMINISTA (temperature 0 y semilla fija): el mismo texto
produce la misma respuesta en cada corrida. Antes, con la temperatura por
defecto de Ollama, el mismo informe podia dar codigos distintos de una
corrida a otra.

Requisitos:
    1) Tener Ollama instalado y corriendo (ollama serve, normalmente ya
       queda corriendo despues de instalarlo).
    2) Tener descargado un modelo, por ejemplo:
           ollama pull llama3.1
    3) pip install requests

Uso:
    from extractor_ia import extraer_campos
    resultado = extraer_campos(texto_del_pdf)
"""

import json
import re
import requests

from motor_reglas import CITOLOGIA, VPH_DETECTADO, VPH_NO_DETECTADO

OLLAMA_HOST = "http://localhost:11434"
MODELO_DEFECTO = "llama3.1"
SEMILLA = 42

# Construimos el catalogo como texto para el prompt, a partir del mismo
# catalogo que usa el motor de reglas (una sola fuente de verdad).
_CATALOGO_TXT = "\n".join(f"{cod}: {nombre}" for cod, nombre in sorted(CITOLOGIA.items()))

SYSTEM_PROMPT = f"""Eres un asistente que SOLO extrae datos de un informe de \
citología cervicouterina (sistema Bethesda). No interpretas, no opinas, no \
das recomendaciones clínicas. Tu única salida es un JSON, sin texto \
adicional, sin explicaciones, sin marcas de código (```).

Catálogo de códigos de citología (usa SOLO estos códigos):
{_CATALOGO_TXT}

Catálogo de VPH:
{VPH_DETECTADO}: Detectado
{VPH_NO_DETECTADO}: No detectado

Devuelve exactamente este JSON (sin texto antes ni después):
{{
  "codigos_citologia": [lista de enteros del catálogo que apliquen, puede ser más de uno],
  "vph": entero 19, 20, o null si no se menciona VPH en el texto,
  "tiene_anexo1": true si el texto menciona una orden médica o Anexo 1 de colposcopia, si no false,
  "evidencia": "la frase EXACTA del texto original que sustenta los códigos elegidos",
  "confianza": número entre 0 y 1, tu certeza sobre esta extracción
}}

Reglas importantes:
- Si el texto dice "Negativo para lesión intraepitelial y malignidad" o \
similar, usa el código 17.
- Si no puedes determinar un campo con certeza, usa null y baja la confianza.
- Nunca inventes un código que no esté en el catálogo.
- La evidencia debe ser una cita literal del texto, no un resumen.
- "A favor de neoplasia" y "sospechosas de neoplasia" significan LO MISMO. \
"AGC-Neoplasia" también. Usa 10 si son células endocervicales, 11 si son \
endometriales y 12 si son glandulares. NO uses 7, 8 ni 9 para esos casos \
(7, 8 y 9 son solo "atípicas sin ningún otro significado").
- ASC-H es el código 2. La frase entre paréntesis que dice "de significado \
indeterminado, sugestivo de LEI de alto grado" es solo la descripción de \
ASC-H: NO agregues por eso los códigos 1 ni 4.
- ASC-US es el código 1. Su descripción entre paréntesis no agrega otros códigos.
- Una muestra "insatisfactoria" o "inadecuada" para evaluación o lectura es el código 18.
- Solo cuenta lo que dice la sección de resultado/interpretación de la citología \
actual; ignora antecedentes, resultados anteriores y recomendaciones.
"""


def _limpiar_json(texto_crudo: str) -> str:
    """Quita cercas de código markdown si el modelo las agrega igual."""
    texto = texto_crudo.strip()
    texto = re.sub(r"^```(json)?", "", texto).strip()
    texto = re.sub(r"```$", "", texto).strip()
    return texto


def _validar(datos: dict) -> dict:
    """Aplica sanity checks contra el catálogo real; nunca confía ciegamente
    en lo que devolvió el modelo."""
    errores = []

    codigos = datos.get("codigos_citologia") or []
    codigos_validos = [c for c in codigos if c in CITOLOGIA]
    if len(codigos_validos) != len(codigos):
        errores.append("Se descartaron códigos fuera del catálogo")

    vph = datos.get("vph")
    if vph not in (VPH_DETECTADO, VPH_NO_DETECTADO, None):
        errores.append("Valor de VPH inválido, se descarta")
        vph = None

    confianza = datos.get("confianza")
    try:
        confianza = float(confianza)
        confianza = max(0.0, min(1.0, confianza))
    except (TypeError, ValueError):
        confianza = 0.0
        errores.append("Confianza no numérica, se fija en 0")

    return {
        "codigos_citologia": codigos_validos,
        "vph": vph,
        "tiene_anexo1": bool(datos.get("tiene_anexo1", False)),
        "evidencia": datos.get("evidencia", ""),
        "confianza": confianza,
        "errores_validacion": errores,
    }


def extraer_campos(texto: str, modelo: str = MODELO_DEFECTO,
                    host: str = OLLAMA_HOST, timeout: int = 60) -> dict:
    """Llama al modelo local en Ollama y devuelve el JSON ya validado.

    Si algo falla (Ollama apagado, JSON mal formado, etc.) devuelve un
    resultado con confianza 0 y el error en 'errores_validacion', para que
    el caso caiga en revisión humana en vez de romper el pipeline.
    """
    payload = {
        "model": modelo,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": texto},
        ],
        "format": "json",   # Ollama fuerza salida JSON válida
        "stream": False,
        # Determinista: sin esto el mismo informe puede dar respuestas distintas entre corridas
        "options": {"temperature": 0, "seed": SEMILLA},
    }

    try:
        resp = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
        contenido = resp.json()["message"]["content"]
        datos = json.loads(_limpiar_json(contenido))
        return _validar(datos)

    except requests.exceptions.ConnectionError:
        return _validar({}) | {
            "errores_validacion": [
                f"No se pudo conectar a Ollama en {host}. "
                f"¿Está corriendo? Prueba: ollama serve"
            ]
        }
    except Exception as e:
        return _validar({}) | {"errores_validacion": [f"Error inesperado: {e}"]}


if __name__ == "__main__":
    # Prueba rápida manual: python extractor_ia.py archivo.txt
    import sys
    if len(sys.argv) < 2:
        print("Uso: python extractor_ia.py <archivo.txt> [modelo]")
        sys.exit(1)

    texto = open(sys.argv[1], encoding="utf-8").read()
    modelo = sys.argv[2] if len(sys.argv) > 2 else MODELO_DEFECTO
    resultado = extraer_campos(texto, modelo=modelo)
    print(json.dumps(resultado, ensure_ascii=False, indent=2))