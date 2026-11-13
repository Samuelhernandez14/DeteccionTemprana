"""
extractor_afiliado.py

Extrae del texto del informe los datos de la afiliada (nombre, documento,
teléfonos, dirección, municipio...) con expresiones regulares. Sin IA:
es rápido, determinista y auditable.

Cada laboratorio ordena el PDF distinto (a veces la etiqueta queda lejos de su
valor), por eso se combinan varias estrategias, de la más fiable a la menos:
  1) "Etiqueta: valor" (o el valor en la línea siguiente), validando el valor
  2) documento por su forma: tipo + número ("CC 1.234.567")
  3) nombre pegado al documento, o "Paciente NOMBRE" sin dos puntos
  4) último recurso: línea en mayúsculas que parezca un nombre / celular suelto

Pruebas:  python extractor_afiliado.py
"""

import re
import unicodedata


def _plano(s: str) -> str:
    """minúsculas, sin acentos, sin espacios sobrantes."""
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


# Etiquetas ya normalizadas (sin acentos, minúsculas)
_CAMPOS = {
    "nombres": r"nombres?(?:\s+y\s+apellidos)?|nombre\s+completo|"
               r"nombre\s+(?:del|de\s+la)\s+(?:paciente|usuari[oa]|afiliad[oa])|"
               r"paciente|usuari[oa]|afiliad[oa]",
    "documento": r"identificacion|doc\.?\s*id\.?|documento(?:\s+de\s+identidad)?|"
                 r"cedula(?:\s+de\s+ciudadania)?|"
                 r"(?:no|numero)\.?\s+(?:de\s+)?(?:documento|identificacion|cedula)",
    "telefono": r"telefonos?(?:\s+(?:movil|celular|fijo))?|celular|tel\.?|movil|contacto",
    "direccion": r"direccion(?:\s+de\s+residencia)?|residencia",
    "municipio": r"municipio|ciudad",
    "fecha_nacimiento": r"fecha\s+de\s+nacimiento|f\.?\s*nacimiento",
    "eps": r"eps|aseguradora|entidad(?:\s+responsable)?",
}
_RX = {k: re.compile(rf"^(?:{p})$") for k, p in _CAMPOS.items()}

_TIPOS = r"(?:CC|TI|CE|RC|PA|PE|PT|CN|MS|AS|NU)"
_TIPO_DOC = re.compile(rf"\b({_TIPOS})\b")
_DOC_TIPADO = re.compile(rf"\b{_TIPOS}\s*[:\-]?\s*\d[\d\.]{{4,}}")
_L = "A-Za-zÁÉÍÓÚÜÑáéíóúüñ"
_NOMBRE_SUELTO = re.compile(rf"^[{_L}]{{2,}}(?: [{_L}]{{2,}}){{1,4}}$")
_NOMBRE_MAYUS = re.compile(r"^[A-ZÁÉÍÓÚÜÑ]{2,}(?: [A-ZÁÉÍÓÚÜÑ]{2,}){1,6}$")
_NOMBRE_ETIQ = re.compile(rf"^(?:paciente|nombres?\s+y\s+apellidos|nombre\s+completo)\s+([{_L} ]{{5,70}})$", re.I)
# Palabras de encabezados/instituciones: una línea con ellas NO es el nombre de la paciente
_NO_NOMBRE = re.compile(
    r"\b(?:hospital|laboratorio|lab|eps|ips|salud|sede|clinica|citologia|resultado|informe|"
    r"escuela|universidad|servicio|empresa|medico|patologo|citotecnologa|examen|analisis|"
    r"bethesda|ese|nit|solicitud|convenio|ambulatorio|femenino|masculino|vaginal|referencia|unidad|"
    r"informacion|paciente|datos|ginecologicos?|reporte|copia|orden|placa|sistema|calidad|muestra|"
    r"clasificacion|estudio|oncologico|microorganismos|observaciones|recomendaciones|responsable|registro)\b", re.I)

# Teléfonos colombianos: celular (3xx xxx xxxx), fijo con indicativo (60x xxx xxxx) o local de 7 dígitos
_TEL = re.compile(r"(?<!\d)(?:3\d{9}|60\d{8}|[2-8]\d{6})(?!\d)")
_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")
_CORREO = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _telefonos_en(valor: str) -> list[str]:
    """'300 123 4567 / 604-555-1234' -> ['3001234567', '6045551234']. Ignora fechas."""
    out = []
    for parte in re.split(r"[,;/]|\s{2,}|\s-\s|\sy\s", _FECHA.sub(" ", valor)):
        limpio = re.sub(r"(?<=\d)[\s\-\.](?=\d)", "", parte)
        for t in _TEL.findall(limpio):
            if t not in out:
                out.append(t)
    return out


def _valido(campo: str, valor: str) -> bool:
    if campo == "nombres":
        return not re.search(r"\d", valor) and len(valor.split()) >= 2
    if campo == "documento":
        return bool(re.search(r"\d", valor))
    if campo == "telefono":
        return bool(_telefonos_en(valor))
    return True


def extraer_afiliado(texto: str) -> dict:
    lineas = [l.rstrip() for l in texto.splitlines()]
    crudo = {}

    # 1) "Etiqueta: valor" o "Etiqueta:" con el valor en la línea siguiente
    for i, linea in enumerate(lineas):
        if ":" not in linea:
            continue
        etiqueta, _, valor = linea.partition(":")
        etiqueta = _plano(etiqueta)
        if len(etiqueta) > 40:
            continue
        for campo, rx in _RX.items():
            if campo in crudo or not rx.match(etiqueta):
                continue
            valor = valor.strip()
            if not valor:
                sig = next((l.strip() for l in lineas[i + 1:i + 3] if l.strip()), "")
                if ":" not in sig:
                    valor = sig
            if valor and _valido(campo, valor):
                crudo[campo] = valor
            break

    # 2) documento por su forma (tipo + número). Gana sobre la etiqueta: en algunos PDFs
    #    "Identificación:" queda pegada al número de placa/orden y no al documento.
    doc_idx = None
    for i, l in enumerate(lineas[:80]):
        m = _DOC_TIPADO.search(l)
        if m:
            doc_idx, crudo["documento"] = i, m.group(0)
            break
    if "documento" not in crudo and re.search(r"identificacion|documento|cedula|doc\.?\s*id\b", _plano(texto)):
        for l in lineas[:60]:   # sin tipo (ej. Citofemme): número suelto que no empieza en 0 ni es celular
            s = l.strip()
            if re.fullmatch(r"[1-9]\d{5,9}", s) and not re.fullmatch(r"3\d{9}", s):
                crudo["documento"] = s
                break

    # 3) nombre sin etiqueta con dos puntos: primero la línea pegada al documento
    if "nombres" not in crudo and doc_idx is not None:  # línea pegada al documento
        for j in (doc_idx + 1, doc_idx - 1, doc_idx + 2, doc_idx - 2):
            if not (0 <= j < len(lineas)) or ":" in lineas[j]:
                continue
            cand = lineas[j].strip()
            m = _NOMBRE_ETIQ.match(cand)               # "Paciente NOMBRE" -> quitar la etiqueta
            cand = m.group(1).strip() if m else cand
            if _NOMBRE_SUELTO.match(cand) and not _NO_NOMBRE.search(_plano(cand)):
                crudo["nombres"] = cand
                break
    if "nombres" not in crudo:
        for l in lineas[:80]:
            m = _NOMBRE_ETIQ.match(l.strip())          # "Paciente FRANCI MARELBA ..."
            if m and _valido("nombres", m.group(1)) and not _NO_NOMBRE.search(_plano(m.group(1))):
                crudo["nombres"] = m.group(1).strip()
                break
    if "nombres" not in crudo:                          # último recurso: primera línea en MAYÚSCULAS
        for l in lineas[:60]:
            s = " ".join(l.split())
            if _NOMBRE_MAYUS.match(s) and not _NO_NOMBRE.search(_plano(s)):
                crudo["nombres"] = s
                break

    # documento: "CC 1.234.567" -> tipo CC, número 1234567
    tipo_doc = documento = None
    if "documento" in crudo:
        v = crudo["documento"]
        m = _TIPO_DOC.search(v.upper())
        tipo_doc = m.group(1) if m else None
        n = re.search(r"\d[\d\.,]*", v)
        documento = re.sub(r"\D", "", n.group(0)) if n else None

    # teléfonos: por etiqueta; si no, celulares sueltos (3xx xxx xxxx) en el encabezado
    telefonos = _telefonos_en(crudo["telefono"]) if "telefono" in crudo else []
    if not telefonos:
        for l in lineas[:80]:
            m = re.fullmatch(r"\s*(3\d{9})\s*", l)
            if m and m.group(1) not in telefonos:
                telefonos.append(m.group(1))
        telefonos = telefonos[:2]

    correo = _CORREO.search(texto)
    return {
        "nombres": " ".join(crudo["nombres"].split()).upper() if "nombres" in crudo else None,
        "tipo_doc": tipo_doc,
        "documento": documento,
        "telefonos": telefonos,
        "correo": correo.group(0).lower() if correo else None,
        "direccion": crudo.get("direccion"),
        "municipio": crudo.get("municipio"),
        "fecha_nacimiento": crudo.get("fecha_nacimiento"),
        "eps": crudo.get("eps"),
    }


def nombres_coinciden(a: str, b: str, umbral: float = 0.6) -> bool:
    """Compara nombres sin importar orden (nombre/apellido), acentos ni mayúsculas."""
    ta, tb = set(_plano(a).split()), set(_plano(b).split())
    if not ta or not tb:
        return True   # nada que comparar
    return len(ta & tb) / max(len(ta), len(tb)) >= umbral


def _pruebas():
    casos = [
        ("etiqueta: valor",
         "Nombres y Apellidos: maría PÉREZ gómez\nIdentificación: CC 1.234.567\n"
         "Teléfono: 300 123 4567 / 604 555 1234\nDirección: Cra 10 # 20-30\n"
         "Municipio:\nMedellín\nCorreo: Maria@Correo.com\n",
         dict(nombres="MARÍA PÉREZ GÓMEZ", tipo_doc="CC", documento="1234567",
              telefonos=["3001234567", "6045551234"], municipio="Medellín", correo="maria@correo.com")),
        ("Echavarría (etiquetas y valores desordenados)",
         "Paciente:\nCC 1128417008\nGLORIA ELCY RAMIREZ ARISTIZABAL\n39 Años\nIdentificación:\nEdad:\n"
         "Teléfono Móvil:\nEmpresa:METROSALUD\nTeléfono:\nE-mail:\nDirección:\nAutorización:\n",
         dict(nombres="GLORIA ELCY RAMIREZ ARISTIZABAL", tipo_doc="CC", documento="1128417008", telefonos=[])),
        ("San Vicente: 'Teléfono: SIN DATO' + 'Teléfono Móvil:' con valor abajo",
         "Paciente:\nCC 39382733\nCARMONA RAMIREZ LUZ MERY\nTeléfono: SIN DATO\nTeléfono Móvil:\n3136048977\nSede: X\n",
         dict(nombres="CARMONA RAMIREZ LUZ MERY", documento="39382733", telefonos=["3136048977"])),
        ("Alma Máter: 'Identificación:' pegada a la placa; teléfono es una fecha",
         "Nombres y Apellidos:\nLEIDY VIVIANA JIMENEZ MORALES\nPlaca N°\nTeléfonos:\n2024-10-15\n"
         "Identificación: 2400031265\nCC 42942053\n3043578580\n",
         dict(nombres="LEIDY VIVIANA JIMENEZ MORALES", tipo_doc="CC", documento="42942053", telefonos=["3043578580"])),
        ("Manuel Uribe: 'Paciente' sin dos puntos",
         "Paciente FRANCI MARELBA FRANCO CASTAÑO\nDocumento: CC 43865985 Servicio:\nTelefono\n3215304822\n2501230241\n",
         dict(nombres="FRANCI MARELBA FRANCO CASTAÑO", tipo_doc="CC", documento="43865985", telefonos=["3215304822"])),
        ("Citofemme: documento sin tipo, nombre suelto en mayúsculas",
         "Solicitud\nIdentificación\nPaciente\n:\nNOELIA DEL SOCORRO MURIEL PANIAGUA\n01873943\n43321164\n"
         "SAVIA SALUD EPS SALUD PUBLICA\nTelefono\n.\n",
         dict(nombres="NOELIA DEL SOCORRO MURIEL PANIAGUA", tipo_doc=None, documento="43321164", telefonos=[])),
        ("POCTLAB/Citofemme: 'Doc. Id.:' con el valor lejos, nombre con doble espacio",
         "INFORMACIÓN DEL PACIENTE\nCOPIA\nTeléfono:\nPaciente:\nDoc. Id.:\nORDEN\nNo.: 513493\nPLACA\n"
         "No.: 26-057079\nGLADYS ENEIDA PATIÑO  VILLA \n51\nAños\nPOCTLAB S.A.S  \n31/07/2026\n22117741\nFEMENINO\n"
         "DATOS GINECOLÓGICOS\n",
         dict(nombres="GLADYS ENEIDA PATIÑO VILLA", tipo_doc=None, documento="22117741", telefonos=[])),
        ("Echavarría: nombre con doble espacio no debe ser reemplazado por un encabezado con tilde",
         "Paciente:\nCC 43277040\nBRICEIDA  DAVID USUGA\n47 Años\nIdentificación:\nEdad:\n"
         " PREANALITICA DE LA CITOLOGÍA\n CITOLOGÍA (SISTEMA BETHESDA 2014)\n",
         dict(nombres="BRICEIDA DAVID USUGA", tipo_doc="CC", documento="43277040")),
        ("Sin nombre real: un encabezado con tilde NUNCA se toma como nombre",
         "Identificación: CC 43277040\n PREANALITICA DE LA CITOLOGÍA\n INTERPRETACIÓN DEL RESULTADO\n",
         dict(nombres=None, documento="43277040")),
    ]
    fallos = 0
    for nombre, texto, esperado in casos:
        r = extraer_afiliado(texto)
        malos = {k: (r[k], v) for k, v in esperado.items() if r[k] != v}
        fallos += bool(malos)
        print(f"[{'OK' if not malos else 'FALLA'}] {nombre} {malos or ''}")
    assert nombres_coinciden("MARIA PEREZ GOMEZ", "Gómez Pérez María")
    assert not nombres_coinciden("MARIA PEREZ", "LUZ RESTREPO")
    print(f"\n{len(casos) - fallos}/{len(casos)} pruebas correctas")
    return fallos


if __name__ == "__main__":
    raise SystemExit(1 if _pruebas() else 0)