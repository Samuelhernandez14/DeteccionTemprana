"""
extractor_reglas_texto.py

Paso previo al LLM: resuelve con expresiones regulares los informes que usan
frases estándar del sistema Bethesda. Es instantáneo, determinista y auditable.
Solo los casos que NO resuelve con claridad pasan al modelo de IA.

Devuelve el mismo formato que extractor_ia.extraer_campos(), más:
  - claro: True si el caso se puede cerrar sin IA
  - alertas: por qué NO es claro (negaciones, plantillas, sin sección, etc.)
  - texto_recortado: solo la sección del resultado (sin nombre, cédula, dirección),
    que es lo que conviene enviar al LLM si hace falta

Pruebas:  python extractor_reglas_texto.py
"""

import re

VPH_DETECTADO = 19      # mismos códigos que motor_reglas.py
VPH_NO_DETECTADO = 20

# Quita acentos sin cambiar la longitud, para poder mapear posiciones al texto original
_ACENTOS = str.maketrans("áéíóúüñ", "aeiouun")


def _norm(texto: str) -> str:
    minus = "".join(c.lower() if len(c.lower()) == 1 else c for c in texto)
    return minus.translate(_ACENTOS)


# Catálogo Bethesda (códigos de motor_reglas.CITOLOGIA).
# EL ORDEN IMPORTA: de lo más específico a lo más general. Cada coincidencia se
# "consume", así "LEI de alto grado sospechosa de infiltración" cuenta como 5 y no como 4,
# y "adenocarcinoma endocervical in situ" como 13 y no como 14.
# LEI / LIE, con o sin "(LEI)" entre paréntesis: "Lesión intraepitelial escamosa (LEI) de bajo grado"
_LEI = r"(?:\blei\b|\blie\b|lesion\s+intraepitelial(?:\s+escamosa)?(?:\s*\(\s*lei\s*\))?)"
_PAREN = r"(?:\s*\([^)]{0,200}\))?"                      # "(células escamosas atípicas ... )"
_AGC_NEO = r"(?:\s*\(\s*agc-?\s*neoplasia\s*\))?"       # "(AGC-Neoplasia)" al final
_SOSP = r"(?:sospech|favor\s+de\s+neoplasia)"             # "sospechosas de" == "a favor de" neoplasia
_HUECO = r"(?:(?!celulas)[^.\n]){0,40}"                   # hueco corto que no cruza a otra "células"
# ASC-H y ASC-US van PRIMERO: su descripción ("no excluye LEI de alto grado", "significado
# indeterminado") no debe contarse como otro hallazgo.
_PATRONES = [
    (2,  r"\basc-?\s?h\b" + _PAREN
         + r"|(?:celulas\s+escamosas\s+atipicas[^.\n]{0,40}?)?(?:que\s+)?no\s+(?:se\s+puede\s+)?exclu\w+\s+"
           r"(?:la\s+|una\s+)?(?:" + _LEI + r"|lesion)\s+de\s+alto\s+grado(?:\s*\(?\s*asc-?\s?h\s*\)?)?"),
    (1,  r"\basc-?\s?us\b" + _PAREN + r"|significado\s+indeterminado"),
    (13, r"adenocarcinoma\s+endocervical\s+in\s+situ"),
    (14, r"adenocarcinoma\s+endocervical"),
    (15, r"adenocarcinoma\s+endometrial"),
    (6,  r"carcinoma\s+(?:de\s+celulas\s+)?escamos[ao]s?|carcinoma\s+escamocelular"),
    (5,  _LEI + r"\s+de\s+alto\s+grado[^.\n]{0,40}(?:sospech|infiltra)"
              r"|sospech\w+\s+de\s+(?:infiltracion|invasion)"),
    (4,  _LEI + r"\s+de\s+alto\s+grado|\bh-?sil\b|\bnic\s*(?:ii|iii|2|3)\b|\bcarcinoma\s+in\s+situ|\blie-?\s?ag\b"),
    (3,  _LEI + r"\s+de\s+bajo\s+grado|\bl-?sil\b|\bnic\s*(?:i|1)\b|\blie-?\s?bg\b"),
    (10, r"celulas\s+endocervicales(?:\s*,)?(?:\s+atipicas)?" + _HUECO + _SOSP + _AGC_NEO),
    (11, r"celulas\s+endometriales(?:\s*,)?(?:\s+atipicas)?" + _HUECO + _SOSP + _AGC_NEO),
    (12, r"(?:agc-?\s*neoplasia\s*:?\s*)?celulas\s+glandulares(?:\s*,)?(?:\s+atipicas)?" + _HUECO + _SOSP
         + _AGC_NEO + r"|\bagc-?\s*neoplasia\b"),
    (7,  r"celulas\s+endocervicales\s+atipicas"),
    (8,  r"celulas\s+endometriales\s+atipicas"),
    (9,  r"celulas\s+glandulares\s+atipicas|\bagc\b"),
    # "Adenocarcinoma no especificado (NOS)" se asigna a "Otras neoplasias" (16): VALIDAR con el catálogo
    (16, r"otras\s+neoplasias|adenocarcinoma\s+(?:no\s+especificado|nos\b)(?:\s*\(\s*nos\s*\))?"),
    (17, r"negativ[oa]\s+para\s+(?:la\s+)?(?:lesion\s+intraepitelial|malignidad)"),
    (18, r"inadecuad[ao]\s+para\s+(?:la\s+)?(?:lectura|evaluacion)|\binsatisfactori[ao]"
         r"|muestra\s+rechazada"),
]
_PATRONES = [(c, re.compile(p)) for c, p in _PATRONES]

# Negación justo antes del hallazgo: "no se observan ...", "sin evidencia de ..."
_NEGACION = re.compile(
    r"\b(?:no\s+se\s+(?:observan?|identifican?|evidencian?|encontraron|hallaron)"
    r"|sin\s+evidencia\s+de|ausencia\s+de|no\s+hay|se\s+descarta)\b[^.\n]{0,30}$"
)

# Delimitan la sección del resultado. Lo de antes (encabezado, "Resul. Cito. Ant.",
# antecedentes) NO se analiza: un ASC-US previo no debe confundirse con el actual.
_INICIO = ("calidad de la muestra", "calidad muestra", "clasificacion general", "interpretacion")
_FIN_ORIGINAL = ("recomendaciones", "firma", "el resultado emitido")
# Más marcadores: así un informe de varias hojas (otros exámenes después de la citología)
# no mete en la sección resultados que no son de la citología.
_FIN_AMPLIO = _FIN_ORIGINAL + ("microorganismos", "hallazgos microbiologicos",
                               "sugerencias", "revisado por")

_ANEXO1 = re.compile(
    r"anexo\s*(?:no\.?\s*)?1\b|orden\s+medica\s+de\s+colposcopia"
    r"|remision\s+a\s+colposcopia|solicitud\s+de\s+colposcopia"
)


def recortar_seccion(texto: str, marcadores_fin=_FIN_AMPLIO) -> tuple[str, bool]:
    n = _norm(texto)
    inicios = [i for i in (n.find(m) for m in _INICIO) if i != -1]
    if not inicios:
        return texto, False
    ini = min(inicios)
    fines = [i for i in (n.find(m, ini) for m in marcadores_fin) if i != -1]
    fin = min(fines) if fines else len(texto)
    return texto[ini:fin], True


_VPH_ETIQUETA = re.compile(r"\b(?:vph|hpv|pvh|virus\s+del\s+papiloma(?:\s+humano)?)\b")
_VPH_NO = re.compile(
    r"\bno\s+(?:se\s+)?detect\w*|\bnegativ\w*\b(?!\s+para\s+(?:la\s+)?lesion)|\bausente")
_VPH_SI = re.compile(r"\bdetectad\w*|\bse\s+detecta\w*|\bpositiv\w*")
# Palabras de la citología: una línea con estas NO es una etiqueta de test de VPH
_NO_ETIQUETA = re.compile(r"\blei\b|lesion|\bnic\b|bajo\s+grado|alto\s+grado|asc-?\s?us|efecto")


def _valor_vph(segmento: str):
    if _VPH_NO.search(segmento):
        return VPH_NO_DETECTADO
    if _VPH_SI.search(segmento):
        return VPH_DETECTADO
    return None


def _detectar_vph(n: str):
    """Lee el VPH en: 'VPH: Detectado', 'VPH detectado', 'Detectado VPH 16' y
    'VPH' / 'Resultado VPH:' con el valor en la línea siguiente."""
    valores = set()
    for m in _VPH_ETIQUETA.finditer(n):
        ini = n.rfind("\n", 0, m.start()) + 1
        fin = n.find("\n", m.end())
        fin = len(n) if fin == -1 else fin
        v = _valor_vph(n[m.end():fin][:80])             # 1) resto de la misma línea
        if v is None:
            v = _valor_vph(n[max(ini, m.start() - 30):m.start()])   # 2) antes de la etiqueta
        if v is None:                                    # 3) línea siguiente, solo si esta
            linea = n[ini:fin]                           #    línea parece una etiqueta corta
            if len(linea.strip()) <= 40 and not _NO_ETIQUETA.search(linea):
                sig = next((l for l in n[fin + 1:].split("\n", 3)[:3] if l.strip()), "")
                v = _valor_vph(sig[:60])
        if v is not None:
            valores.add(v)
    return valores.pop() if len(valores) == 1 else None   # None si falta o se contradice


def _analizar(recorte: str):
    trabajo = list(_norm(recorte))
    codigos, evidencias, alertas = set(), [], []
    for codigo, patron in _PATRONES:
        s = "".join(trabajo)
        for m in patron.finditer(s):
            ventana = s[max(0, m.start() - 60):m.start()]
            if codigo != 17 and _NEGACION.search(ventana):
                alertas.append(f"Hallazgo negado en el texto (código {codigo})")
            else:
                codigos.add(codigo)
                evidencias.append(" ".join(recorte[m.start():m.end()].split()))
            for k in range(m.start(), m.end()):   # consumir la coincidencia
                trabajo[k] = " "
    return codigos, evidencias, alertas


def extraer_por_reglas(texto: str) -> dict:
    for marcadores in (_FIN_AMPLIO, _FIN_ORIGINAL):
        recorte, encontrada = recortar_seccion(texto, marcadores)
        codigos, evidencias, alertas = _analizar(recorte)
        if codigos or not encontrada:
            break   # si con el corte amplio no apareció nada, se reintenta con el original

    if len(codigos - {17, 18}) >= 3:
        alertas.append("Tres o más hallazgos distintos: posible plantilla con todas las opciones")
    if {17, 18} <= codigos:
        alertas.append("Aparecen 'negativa' e 'inadecuada' a la vez")
    if not encontrada:
        alertas.append("No se ubicó la sección de resultado")

    n_total = _norm(texto)
    claro = encontrada and bool(codigos) and not alertas
    return {
        "codigos_citologia": sorted(codigos),
        "vph": _detectar_vph(n_total),
        "tiene_anexo1": bool(_ANEXO1.search(n_total)),
        "evidencia": " | ".join(evidencias),
        "confianza": 0.95 if claro else (0.5 if codigos else 0.0),
        "errores_validacion": [],
        "claro": claro,
        "alertas": alertas,
        "seccion_encontrada": encontrada,
        "texto_recortado": recorte,
    }


# ------------------------- pruebas (textos inventados) -------------------------
def _pruebas():
    hdr = "Nombres y Apellidos: PACIENTE DE PRUEBA\nIdentificación: CC 000\n"
    casos = [
        ("Negativa (orden desordenado)",
         hdr + "Calidad de la Muestra:\nSatisfactoria\nInterpretación/Resultado:\n"
               "Negativo para lesión intraepitelial y malignidad\nRecomendaciones/ Control: X",
         dict(codigos_citologia=[17], claro=True)),
        ("ASC-US + negativa; ASC-US previo en el encabezado se ignora",
         hdr + "Resul. Cito.\nAnt.:\nASC-US\nINFORME DE CITOLOGÍA\nCALIDAD DE LA MUESTRA\n"
               "Satisfactoria\nCLASIFICACIÓN GENERAL\nAnormalidades en células epiteliales.\n"
               "ESTUDIO ONCOLÓGICO\nNegativo para lesión intraepitelial y malignidad\n"
               "Atipias en células epiteliales escamosas de significado indeterminado ASC-US.\n"
               "RECOMENDACIONES\nSugiere consultar al ginecólogo",
         dict(codigos_citologia=[1, 17], claro=True)),
        ("LEI bajo grado - HPV (NIC I) no es un test de VPH",
         "Interpretación: LEI de bajo grado - HPV (NIC I)\nRecomendaciones: X",
         dict(codigos_citologia=[3], vph=None, claro=True)),
        ("Alto grado sospechosa de infiltración = 5, no 4",
         "Interpretación: LEI de alto grado sospechosa de infiltración\nFirma: X",
         dict(codigos_citologia=[5], claro=True)),
        ("Alto grado NIC II-III = 4",
         "Interpretación: Lesión intraepitelial de alto grado (NIC II-III)\nFirma: X",
         dict(codigos_citologia=[4], claro=True)),
        ("Adenocarcinoma endocervical in situ = 13, no 14",
         "Interpretación: Adenocarcinoma endocervical in situ\nFirma: X",
         dict(codigos_citologia=[13], claro=True)),
        ("Negación: no es claro, va a la IA",
         "Interpretación: No se observan ASC-US\nFirma: X",
         dict(codigos_citologia=[], claro=False)),
        ("Sin sección de resultado: no es claro",
         "Texto sin estructura conocida con ASC-US",
         dict(claro=False)),
        ("VPH detectado",
         "Interpretación: ASC-US\nVPH de alto riesgo: Detectado\nFirma: X",
         dict(codigos_citologia=[1], vph=19, claro=True)),
        ("VPH no detectado",
         "Interpretación: ASC-US\nVPH: No detectado\nFirma: X",
         dict(codigos_citologia=[1], vph=20, claro=True)),
        ("Muestra insatisfactoria",
         "Calidad de la muestra: Insatisfactoria para evaluación\nFirma: X",
         dict(codigos_citologia=[18], claro=True)),
        ("Plantilla con todas las opciones: no es claro",
         "Interpretación: ASC-US ASC-H LEI de bajo grado LEI de alto grado\nFirma: X",
         dict(claro=False)),
        ("VPH con el valor en la línea siguiente",
         "Interpretación: ASC-US\nResultado VPH:\nDETECTADO\nFirma: X",
         dict(codigos_citologia=[1], vph=19)),
        ("VPH etiqueta sin dos puntos, valor abajo",
         "Interpretación: ASC-US\nPrueba VPH\nNo detectado\nFirma: X",
         dict(codigos_citologia=[1], vph=20)),
        ("Se detecta VPH de alto riesgo",
         "Interpretación: ASC-US\nSe detecta VPH de alto riesgo\nFirma: X",
         dict(codigos_citologia=[1], vph=19)),
        ("VPH positivo",
         "Interpretación: ASC-US\nADN VPH: POSITIVO\nFirma: X",
         dict(codigos_citologia=[1], vph=19)),
        ("HPV de la categoría citológica no arrastra la línea siguiente",
         "Interpretación: LEI de bajo grado - HPV (NIC I)\nPositivo para algo\nFirma: X",
         dict(codigos_citologia=[3], vph=None)),
        ("VPH contradictorio => None",
         "Interpretación: ASC-US\nVPH: Detectado\nVPH: No detectado\nFirma: X",
         dict(vph=None)),
        ("Formato real: 'VPH de alto riesgo:' y el valor abajo",
         "Calidad de la Muestra:\nSatisfactoria\nInterpretación/Resultado:\n"
         "Atipias en células epiteliales escamosas de significado indeterminado ASC-US.\n"
         "VPH de alto riesgo:\nDetectado\nRecomendaciones/ Control Sugerido:\nX",
         dict(codigos_citologia=[1], vph=19, claro=True)),
        ("Anexo 1 detectado",
         "Interpretación: Negativo para lesión intraepitelial\nAnexo 1 orden médica\nFirma: X",
         dict(codigos_citologia=[17], tiene_anexo1=True)),
        # ---- casos nuevos (formatos de otros prestadores) ----
        ("ASC-H con descripción: no suma ASC-US ni LEI alto grado",
         "Interpretación del resultado\nASC-H (células escamosas atípicas, de significado "
         "indeterminado sugestivo de LEI de alto\ngrado).\nMicroorganismos: X",
         dict(codigos_citologia=[2], claro=True)),
        ("ASC-US con descripción entre paréntesis = solo 1",
         "Interpretación: ASC-US (células escamosas atípicas de significado indeterminado)\nFirma: X",
         dict(codigos_citologia=[1], claro=True)),
        ("AGC-Neoplasia glandular (a favor de neoplasia) = 12, no 9",
         "Interpretación / Resultado\nAGC-NEOPLASIA: Celulas glandulares atípicas a favor de neoplasia\n"
         "MICROORGANISMOS\nX",
         dict(codigos_citologia=[12], claro=True)),
        ("Endocervicales a favor de neoplasia (AGC-Neoplasia) = 10, no 7 ni 9",
         "Interpretación/Resultado:\nCélulas endocervicales  atípicas a favor de neoplasia (AGC-Neoplasia)\n"
         "Hallazgos Microbiológicos:\nFlora ausente",
         dict(codigos_citologia=[10], claro=True)),
        ("Endocervicales sospechosas de neoplasia = 10",
         "Interpretación: Células endocervicales atípicas sospechosas de neoplasia\nFirma: X",
         dict(codigos_citologia=[10], claro=True)),
        ("Informe de varias hojas: otros exámenes después no alteran el resultado",
         "Calidad de la muestra\nSatisfactoria\nInterpretación / Resultado\nLesión Intraepitelial Escamosa "
         "de Bajo Grado\nMICROORGANISMOS\nBacilos\nSUGERENCIAS\nX\n\nSANGRE OCULTA\nNegativo\nSEROLOGÍA\nNo Reactiva\n",
         dict(codigos_citologia=[3], claro=True)),
        ("'Calidad muestra' (sin 'de la') con muestra insatisfactoria",
         "CALIDAD MUESTRA\nInsatisfactoria para evaluacion por defectos de fijacion.\nMICROORGANISMOS\nX",
         dict(codigos_citologia=[18], claro=True)),
        # ---- casos de las 60 pruebas ----
        ("ASC-H: 'no excluye lesión intraepitelial de alto grado' = solo 2",
         "Interpretación/Resultado\nASC-H: Células escamosas atípicas (no excluye lesión intraepitelial de alto grado)\nMicroorganismos: X",
         dict(codigos_citologia=[2], claro=True)),
        ("'Células escamosas atípicas, no excluye LEI de alto grado (ASC-H)' = 2, no 2+4",
         "Interpretación: Células escamosas atípicas, no excluye LEI de alto grado (ASC-H)\nFirma: X",
         dict(codigos_citologia=[2], claro=True)),
        ("'que no excluyen lesion de alto grado ASC-H' = 2",
         "Interpretación: Atipias de celulas escamosas que no excluyen lesion de alto grado ASC-H\nFirma: X",
         dict(codigos_citologia=[2], claro=True)),
        ("'(LEI)' entre paréntesis, bajo grado = 3",
         "Interpretación: Lesión intraepitelial escamosa (LEI) de bajo grado\nFirma: X",
         dict(codigos_citologia=[3], claro=True)),
        ("'Persiste: (LEI) de bajo grado' = 3",
         "Interpretación: Persiste: Lesión intraepitelial escamosa (LEI) de bajo grado\nFirma: X",
         dict(codigos_citologia=[3], claro=True)),
        ("Endocervicales a favor de neoplasia + (LEI) de alto grado = 4 y 10",
         "Interpretación/Resultado:\nCélulas endocervicales  atípicas a favor de neoplasia (AGC-Neoplasia)\n"
         "Lesión intraepitelial escamosa (LEI) de alto grado. Descartar invasión.\nFirma: X",
         dict(codigos_citologia=[4, 10], claro=True)),
        ("ASC-H + 'Células Glandulares, a favor de neoplasia' (sin 'atípicas') = 2 y 12",
         "Interpretación: Células escamosas atípicas, no excluye LEI de alto grado (ASC-H)\n"
         "Células Glandulares, a favor de neoplasia.\nFirma: X",
         dict(codigos_citologia=[2, 12], claro=True)),
        ("ASC-H + adenocarcinoma NOS = 2 y 16",
         "Interpretación: Células escamosas atípicas, no excluye LEI de alto grado (ASC-H), "
         "Adenocarcinoma no especificado (NOS)\nFirma: X",
         dict(codigos_citologia=[2, 16], claro=True)),
        ("LEI bajo + endometriales atípicas sin especificar = 3 y 8",
         "Interpretación: Células endometriales en mujeres mayores de 45 años\n"
         "Lesión intraepitelial escamosa (LEI) de bajo grado\nCélulas endometriales  atípicas sin especificar\nFirma: X",
         dict(codigos_citologia=[3, 8], claro=True)),
        ("Notas (endometriales >45, glandulares post-histerectomía) no suman hallazgos",
         "Interpretación: Negativo para lesión intraepitelial y malignidad.\n"
         "Células endometriales en mujeres mayores de 45 años\n"
         "Células glandulares en pacientes post-histerectomizadas\nFirma: X",
         dict(codigos_citologia=[17], claro=True)),
        ("'Por PVH' (causa de la lesión) no es un test de VPH",
         "Interpretación: Lesion intraepitelial escamosa de bajo grado por PVH LIE-BG\nFirma: X",
         dict(codigos_citologia=[3], vph=None, claro=True)),
        ("Nota 'endometriales en mujeres >45' + endocervicales a favor de neoplasia = solo 10",
         "Interpretación: Células endometriales en mujeres mayores de 45 años_ "
         "Células endocervicales  atípicas a favor de neoplasia (AGC-Neoplasia)\nFirma: X",
         dict(codigos_citologia=[10], claro=True)),
    ]
    fallos = 0
    for nombre, texto, esperado in casos:
        r = extraer_por_reglas(texto)
        ok = all(r[k] == v for k, v in esperado.items())
        fallos += not ok
        print(f"[{'OK' if ok else 'FALLA'}] {nombre} -> {r['codigos_citologia']} "
              f"vph={r['vph']} claro={r['claro']} {r['alertas'] or ''}")
    print(f"\n{len(casos) - fallos}/{len(casos)} pruebas correctas")
    return fallos


if __name__ == "__main__":
    raise SystemExit(1 if _pruebas() else 0)