"""
pipeline.py  (versión con datos del afiliado, cruce con BD y envío de correo)

  1) Lee los .txt extraídos de los PDFs
  2) Reglas de texto (y IA solo si el caso no es claro) -> códigos de citología y VPH
  3) Extrae los datos de la afiliada y los cruza / complementa con la BD
  4) Consulta en la BD exclusiones y antecedente de ASC-US (regla 3)
  5) motor_reglas.decidir() decide
  6) Si activa y no requiere revisión -> envía correo al prestador (PDF adjunto)

Ya NO se usa el Excel: la decisión sale solo del resultado del PDF.

Uso:
    python pipeline.py <carpeta_txt> <resultados.json> --pdfs <carpeta_pdfs>
        [--db demo.db] [--destino correo@prestador] [--enviar]
        [--modelo qwen2.5:3b] [--sin-ia] [--siempre-ia]

  --enviar   envía por SMTP de verdad. Sin esta bandera se SIMULA: guarda
             los correos como .eml en ./correos_salida (seguro para la demo).
"""

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()          # lee el archivo .env (pip install python-dotenv)
except ImportError:
    pass

from base_datos import BaseDatos
from extractor_afiliado import extraer_afiliado
from extractor_ia import MODELO_DEFECTO, extraer_campos
from extractor_reglas_texto import extraer_por_reglas
from motor_reglas import Caso, decidir
from notificador import construir_correo, enviar

UMBRAL_CONFIANZA_MINIMA = 0.6


def _env_si(nombre: str) -> bool:
    return os.environ.get(nombre, "").strip().lower() in ("1", "true", "si", "sí", "yes")

TIMEOUT_IA = 300
CAMPOS = ("codigos_citologia", "vph", "tiene_anexo1", "evidencia",
          "confianza", "errores_validacion")


def _firma(d) -> tuple:
    return (d.activar, d.regla, d.indicacion, d.revision_humana)


def _resumen(d) -> str:
    if d.revision_humana:
        return "revisión humana"
    return f"activar (regla {d.regla})" if d.activar else "no activar"


def procesar_texto(nombre: str, texto: str, modelo: str, usar_ia: bool,
                   siempre_ia: bool, bd: BaseDatos, pdf_path: Path | None,
                   destino: str, modo_envio: str, remitente: str) -> dict:
    sha = hashlib.sha256(texto.encode("utf-8")).hexdigest()

    # ---- 1) citología / VPH: las reglas mandan; la IA verifica o resuelve lo que las reglas no ----
    reglas = extraer_por_reglas(texto)
    campos_ia, segundos_ia = None, None
    if usar_ia and (siempre_ia or not reglas["claro"]):
        texto_ia = reglas["texto_recortado"] if reglas["seccion_encontrada"] else texto[:8000]
        t0 = time.time()
        campos_ia = extraer_campos(texto_ia, modelo=modelo, timeout=TIMEOUT_IA)
        segundos_ia = round(time.time() - t0, 1)

    if reglas["claro"]:
        campos = {k: reglas[k] for k in CAMPOS}
        fuente = "reglas+ia" if campos_ia is not None else "reglas"
    elif campos_ia is not None:
        campos, fuente = campos_ia, "ia"
    else:
        campos = {k: reglas[k] for k in CAMPOS}
        campos["confianza"] = 0.0
        campos["errores_validacion"] = ["Sin IA: las reglas no resolvieron el caso"]
        fuente = "reglas_sin_ia"

    # ---- 2) afiliada: datos del PDF cruzados con la BD ----
    afiliado, alertas_afiliado, conflicto_bd = bd.completar(extraer_afiliado(texto))
    doc = afiliado.get("documento")
    antecedente = bd.antecedente_asc_us(doc, sha) if doc else None
    exclusiones = bd.exclusiones(doc) if doc else []
    antecedentes = bd.antecedentes(doc, sha) if doc else []

    def caso_de(c):
        return Caso(codigos_citologia=c["codigos_citologia"], vph=c["vph"],
                    antecedente_asc_us=antecedente, tiene_anexo1=c["tiene_anexo1"],
                    exclusiones=exclusiones)

    # ---- 3) decisión ----
    decision = decidir(caso_de(campos))

    # La IA y las reglas pueden nombrar distinto el mismo hallazgo (p. ej. 10 vs 7+10) sin que
    # cambie nada. Solo importa si cambia la DECISIÓN (activar / regla / indicación).
    discrepancia, detalle_disc, ia_fallo = False, None, False
    if campos_ia is not None:
        if campos_ia["errores_validacion"]:
            ia_fallo = fuente == "reglas+ia"   # pedida como verificación y no respondió bien
        else:
            ref = campos if fuente == "reglas+ia" else (reglas if reglas["codigos_citologia"] else None)
            if ref is not None:
                d_ref = decision if fuente == "reglas+ia" else decidir(caso_de(ref))
                d_ia = decidir(caso_de(campos_ia))
                if _firma(d_ref) != _firma(d_ia):
                    discrepancia = True
                    detalle_disc = f"reglas: {_resumen(d_ref)} | IA: {_resumen(d_ia)}"

    if doc and campos["codigos_citologia"]:   # alimenta el histórico para próximas lecturas
        bd.registrar_historico(doc, datetime.now().date().isoformat(),
                               campos["codigos_citologia"], campos["vph"], sha)

    rev_confianza = campos["confianza"] < UMBRAL_CONFIANZA_MINIMA
    rev_errores = bool(campos["errores_validacion"])
    sin_doc = decision.activar and not doc   # no se puede remitir sin identificar a la usuaria
    revision = (decision.revision_humana or rev_confianza or rev_errores or discrepancia
                or ia_fallo or conflicto_bd or sin_doc)

    # ---- 4) correo ----
    correo = {"estado": "no_aplica"}
    if decision.activar and revision:
        correo = {"estado": "pendiente_revision_humana"}
    elif decision.activar:
        if bd.ya_enviado(sha):
            correo = {"estado": "ya_enviado_antes"}
        else:
            msg = construir_correo(
                destino, remitente, afiliado, campos,
                {"regla": decision.regla, "indicacion": decision.indicacion},
                antecedentes, pdf_path)
            estado, detalle = enviar(msg, modo_envio)
            if estado != "error_envio":
                bd.registrar_envio(sha, doc, destino, decision.regla,
                                   decision.indicacion, estado)
            correo = {"estado": estado, "detalle": detalle, "destino": destino}

    return {
        "archivo": nombre,
        "fecha_procesado": datetime.now(timezone.utc).isoformat(),
        "texto_sha256": sha,
        "fuente_extraccion": fuente,
        "segundos_ia": segundos_ia,
        "alertas_reglas": reglas["alertas"],
        "codigos_reglas": reglas["codigos_citologia"],                       # lo que vieron las reglas
        "codigos_ia": campos_ia["codigos_citologia"] if campos_ia else None,  # lo que vio la IA
        "vph_reglas": reglas["vph"],
        "vph_ia": campos_ia["vph"] if campos_ia else None,
        "extraccion": campos,   # lo que se usó para decidir
        "afiliado": {"documento": doc, "alertas": alertas_afiliado,
                     "en_bd": bd.buscar_afiliado(doc) is not None if doc else False},
        "exclusiones": exclusiones,
        "decision": {"activar": decision.activar, "regla": decision.regla,
                     "indicacion": decision.indicacion, "motivos": decision.motivos},
        "correo": correo,
        "revision_humana": revision,
        "motivo_revision": (
            f"reglas e IA deciden distinto ({detalle_disc})" if discrepancia else
            "la IA no respondió bien (verificación pedida)" if ia_fallo else
            "nombre PDF distinto al de la BD" if conflicto_bd else
            "falta documento de la usuaria" if sin_doc else
            "confianza baja" if rev_confianza else
            "errores de validación" if rev_errores else
            decision.motivos[0] if decision.revision_humana else None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("carpeta_txt", type=Path, nargs="?",
                    default=Path(os.environ.get("CARPETA_TXT", "pdfs_salida_txt")),
                    help="carpeta con los .txt (por defecto CARPETA_TXT del .env o pdfs_salida_txt)")
    ap.add_argument("salida", type=Path, nargs="?", default=Path("resultados.json"))
    ap.add_argument("--pdfs", type=Path, default=Path(os.environ.get("CARPETA_PDFS", "pdfs")),
                    help="carpeta con los PDF originales (adjunto del correo)")
    ap.add_argument("--db", default="demo.db", help="SQLite local (histórico, envíos)")
    ap.add_argument("--mysql", action="store_true", default=_env_si("USAR_MYSQL"),
                    help="leer afiliados de MySQL (aseg_afiliados). También: USAR_MYSQL=1 en .env")
    ap.add_argument("--destino", help="correo del prestador (por defecto PRESTADOR_EMAIL_TO)")
    ap.add_argument("--remitente", default=os.environ.get("EMAIL_FROM", "conexiones@aseguradora-demo.test"))
    ap.add_argument("--enviar", action="store_true",
                    help="enviar por SMTP de verdad (a propósito NO se puede activar desde el .env)")
    ap.add_argument("--modelo", default=MODELO_DEFECTO)
    ap.add_argument("--sin-ia", action="store_true")
    ap.add_argument("--siempre-ia", action="store_true", default=_env_si("SIEMPRE_IA"),
                    help="consulta la IA incluso en casos claros. También: SIEMPRE_IA=1 en .env")
    a = ap.parse_args()
    if a.sin_ia:
        a.siempre_ia = False

    if a.mysql:
        from base_datos_mysql import BaseDatosMySQL
        bd = BaseDatosMySQL(a.db)
    else:
        bd = BaseDatos(a.db)
    destino = a.destino or os.environ.get("PRESTADOR_EMAIL_TO") or bd.correo_prestador()
    if not destino:
        raise SystemExit("Falta el correo del prestador: usa --destino o cárgalo en la BD")
    modo_envio = "smtp" if a.enviar else "simulado"

    pdfs = {p.stem: p for p in a.pdfs.rglob("*.pdf")} if a.pdfs and a.pdfs.exists() else {}
    if not pdfs:
        print(f"[aviso] no se encontraron PDF en {a.pdfs}: los correos saldrán SIN adjunto")
    archivos = sorted(p for p in a.carpeta_txt.rglob("*.txt")
                      if not p.name.endswith("__ERROR.txt"))
    print(f"Procesando {len(archivos)} archivos | correo: {modo_envio.upper()} -> {destino}\n")

    resultados, conteo = [], {}
    for i, archivo in enumerate(archivos, 1):
        print(f"[{i}/{len(archivos)}] {archivo.name} ... ", end="", flush=True)
        r = procesar_texto(str(archivo.relative_to(a.carpeta_txt)),
                           archivo.read_text(encoding="utf-8"), a.modelo,
                           not a.sin_ia, a.siempre_ia, bd, pdfs.get(archivo.stem),
                           destino, modo_envio, a.remitente)
        resultados.append(r)
        d, c = r["decision"], r["correo"]["estado"]
        conteo[c] = conteo.get(c, 0) + 1
        estado = ("REVISIÓN HUMANA" if r["revision_humana"] else
                  f"ACTIVAR (regla {d['regla']})" if d["activar"] else "no activa")
        print(f"{estado} | correo: {c}  [{r['fuente_extraccion']}]")

    a.salida.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nCorreos: {conteo}")
    motivos = {}
    for r in resultados:
        if r["revision_humana"]:
            motivos[r["motivo_revision"]] = motivos.get(r["motivo_revision"], 0) + 1
    if motivos:
        print("Revisión humana por motivo:")
        for m, n in sorted(motivos.items(), key=lambda x: -x[1]):
            print(f"  {n:3} x {m}")
    print(f"Resultados guardados en: {a.salida}")
    envios = bd.listar_envios()
    if envios:
        print("\nSEGUIMIENTO (semáforo de días desde el envío)")
        for e in envios:
            print(f"  doc {e['documento']} | regla {e['regla']} | día {e['dias']} | {e['semaforo'].upper()}")


if __name__ == "__main__":
    main()