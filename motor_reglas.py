"""
motor_reglas.py

Motor de reglas (sin IA) para decidir si una usuaria se activa hacia el
prestador de colposcopia. Es deterministico y auditable: cada decision
devuelve que regla se cumplio y por que.

Ejecutar pruebas:  python motor_reglas.py
"""

from dataclasses import dataclass, field

# Catalogo de la tabla de maestros (DTP_RESULTADO_CITOLOGIA_ESCALA_BETHESDA)
CITOLOGIA = {
    1: "ASC-US (células escamosas atípicas de significado indeterminado)",
    2: "ASC-H (células escamosas atípicas, sugestivo de LEI de alto grado)",
    3: "LEI de bajo grado - HPV (NIC I)",
    4: "LEI de alto grado (NIC II-III CA in situ)",
    5: "LEI de alto grado sospechosa de infiltración",
    6: "Carcinoma de células escamosas",
    7: "Células endocervicales atípicas sin ningún otro significado",
    8: "Células endometriales atípicas sin ningún otro significado",
    9: "Células glandulares atípicas sin ningún otro significado",
    10: "Células endocervicales atípicas sospechosas de neoplasia",
    11: "Células endometriales atípicas sospechosas de neoplasia",
    12: "Células glandulares atípicas sospechosas de neoplasia",
    13: "Adenocarcinoma endocervical in situ",
    14: "Adenocarcinoma endocervical",
    15: "Adenocarcinoma endometrial",
    16: "Otras neoplasias",
    17: "Negativa para lesión intraepitelial o neoplasia",
    18: "Inadecuada para lectura",
}
VPH_DETECTADO = 19
VPH_NO_DETECTADO = 20

ANORMALES_REGLA_1 = set(range(2, 17))  # 2..16


@dataclass
class Caso:
    codigos_citologia: list          # codigos 1..18 encontrados en la citologia
    vph: int | None = None           # 19 detectado, 20 no detectado, None desconocido
    antecedente_asc_us: bool | None = None  # None = historico no verificado
    tiene_anexo1: bool = False       # orden medica de colposcopia
    exclusiones: list = field(default_factory=list)


@dataclass
class Decision:
    activar: bool
    regla: int | None
    indicacion: str | None           # "tamizaje" | "clinica"
    revision_humana: bool
    motivos: list = field(default_factory=list)


def coinciden(codigos_pdf, codigos_excel) -> bool:
    """Validacion PDF vs Excel: mismos codigos, sin importar el orden."""
    return set(codigos_pdf) == set(codigos_excel)


def decidir(caso: Caso) -> Decision:
    codigos = set(caso.codigos_citologia)
    motivos = []

    invalidos = codigos - set(CITOLOGIA)
    if invalidos:
        return Decision(False, None, None, True,
                        [f"Códigos fuera del catálogo: {sorted(invalidos)}"])

    if caso.exclusiones:
        return Decision(False, None, None, False,
                        [f"Excluida: {', '.join(caso.exclusiones)}"])

    anormales = codigos & ANORMALES_REGLA_1
    if 17 in codigos and (anormales or 1 in codigos):
        motivos.append("El informe trae 'negativa' y un hallazgo a la vez; "
                       "prevalece el hallazgo")

    # Regla 1: cualquier categoria 2..16
    if anormales:
        return Decision(True, 1, "tamizaje", False,
                        motivos + [f"Regla 1: códigos {sorted(anormales)}"])

    # Reglas 2 y 3: ASC-US
    if 1 in codigos:
        if caso.vph == VPH_DETECTADO:
            return Decision(True, 2, "tamizaje", False,
                            motivos + ["Regla 2: ASC-US con VPH detectado"])
        if caso.antecedente_asc_us is True:
            return Decision(True, 3, "tamizaje", False,
                            motivos + ["Regla 3: ASC-US con ASC-US previo"])
        if not caso.tiene_anexo1:
            # Faltan datos para descartar las reglas 2 y 3: no se cierra sola
            if caso.vph is None or caso.antecedente_asc_us is None:
                return Decision(False, None, None, True,
                                motivos + ["ASC-US sin VPH o sin histórico "
                                           "verificado: no se puede descartar"])
            return Decision(False, None, None, False,
                            motivos + ["ASC-US sin VPH detectado ni antecedente"])

    # Regla 4: orden medica (Anexo 1), aplica si no cumplio 1-3
    if caso.tiene_anexo1:
        return Decision(True, 4, "clinica", False,
                        motivos + ["Regla 4: Anexo 1 con orden de colposcopia"])

    if 18 in codigos:
        return Decision(False, None, None, True,
                        motivos + ["Muestra inadecuada para lectura: sin regla "
                                   "definida"])

    if not codigos:
        return Decision(False, None, None, True,
                        ["No se identificó resultado de citología"])

    # Solo queda 17 (negativa) sin Anexo 1
    return Decision(False, None, None, False,
                    motivos + ["Citología negativa sin criterio de activación"])


def _pruebas():
    casos = [
        ("Alma Máter: negativa", Caso([17], vph=None, antecedente_asc_us=None),
         dict(activar=False, revision_humana=False)),
        ("Citofemme: ASC-US, sin VPH ni histórico",
         Caso([17, 1]), dict(activar=False, revision_humana=True)),
        ("ASC-US + VPH detectado", Caso([1], vph=19, antecedente_asc_us=False),
         dict(activar=True, regla=2)),
        ("ASC-US + antecedente ASC-US",
         Caso([1], vph=20, antecedente_asc_us=True), dict(activar=True, regla=3)),
        ("ASC-US, VPH no detectado, sin antecedente",
         Caso([1], vph=20, antecedente_asc_us=False),
         dict(activar=False, revision_humana=False)),
        ("LEI de alto grado", Caso([4]), dict(activar=True, regla=1)),
        ("Inadecuada", Caso([18]), dict(activar=False, revision_humana=True)),
        ("Sin citología + Anexo 1", Caso([], tiene_anexo1=True),
         dict(activar=True, regla=4)),
        ("Negativa + Anexo 1", Caso([17], tiene_anexo1=True),
         dict(activar=True, regla=4)),
        ("Código inválido", Caso([99]), dict(activar=False, revision_humana=True)),
        ("Excluida", Caso([4], exclusiones=["disentimiento"]),
         dict(activar=False)),
    ]
    fallos = 0
    for nombre, caso, esperado in casos:
        d = decidir(caso)
        ok = all(getattr(d, k) == v for k, v in esperado.items())
        fallos += not ok
        print(f"[{'OK' if ok else 'FALLA'}] {nombre} -> regla={d.regla} "
              f"activar={d.activar} revision={d.revision_humana} | {d.motivos}")
    assert coinciden([1, 17], [17, 1]) and not coinciden([17], [1])
    print(f"\n{len(casos) - fallos}/{len(casos)} pruebas correctas")


if __name__ == "__main__":
    _pruebas()