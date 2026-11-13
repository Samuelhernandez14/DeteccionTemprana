#!/usr/bin/env python3
"""
ejecutar.py  -  Punto de entrada ÚNICO para correr todo el flujo en el servidor.

    PDFs -> texto (pdf_a_texto) -> pipeline (reglas / IA / BD / motor / correo)

Solo procesa los PDF NUEVOS (lleva un registro por hash del archivo), así que
se puede programar en cron cada N minutos sin reprocesar ni reenviar nada.

Uso típico (cron):
    ./ejecutar.sh --enviar

Prueba segura (no envía correos, los guarda como .eml):
    ./ejecutar.sh

Toda la configuración sale de variables de entorno / archivo .env
(ver .env.example). Cualquier opción de línea de comandos las sobreescribe.

Códigos de salida:  0 ok | 1 hubo errores | 2 configuración inválida |
                    3 ya hay otra ejecución en curso
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)   # los módulos importan por nombre; cron no garantiza el cwd
sys.path.insert(0, str(BASE))

try:
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("ENV_FILE") or BASE / ".env")
except ImportError:
    pass


# ------------------------------ utilidades ------------------------------
def _env(nombre: str, defecto: str | None = None) -> str | None:
    v = os.environ.get(nombre, "").strip()
    return v or defecto


def _env_si(nombre: str) -> bool:
    return (_env(nombre, "") or "").lower() in ("1", "true", "si", "sí", "yes")


def _ruta(valor: str | Path) -> Path:
    """Las rutas relativas se resuelven contra la carpeta del script, no contra el cwd."""
    p = Path(str(valor)).expanduser()
    return p if p.is_absolute() else BASE / p


def _bloquear(archivo) -> bool:
    """Bloqueo exclusivo no bloqueante. Linux/Mac: fcntl. Windows: msvcrt."""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(archivo.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(archivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:   # BlockingIOError es subclase de OSError
        return False


class _Tee:
    """Duplica stdout/stderr hacia el archivo de log (los print del pipeline quedan registrados)."""
    def __init__(self, *destinos):
        self.destinos = destinos

    def write(self, t):
        for d in self.destinos:
            d.write(t)
        return len(t)

    def flush(self):
        for d in self.destinos:
            d.flush()


def log(msg: str):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def _sha_archivo(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _cargar_estado(ruta: Path) -> dict:
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _guardar_estado(ruta: Path, estado: dict):
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ruta)   # escritura atómica


# ------------------------------ argumentos ------------------------------
def _args():
    ap = argparse.ArgumentParser(description="Ejecuta el flujo completo de citologías.")
    ap.add_argument("--pdfs", default=_env("PDFS_DIR", "pdfs"),
                    help="carpeta de entrada con los PDF (env PDFS_DIR)")
    ap.add_argument("--datos", default=_env("DATA_DIR", "datos"),
                    help="carpeta base de trabajo (env DATA_DIR)")
    ap.add_argument("--txt-dir", default=_env("TXT_DIR"),
                    help="dónde guardar los .txt extraídos (env TXT_DIR; defecto DATOS/txt)")
    ap.add_argument("--resultados-dir", default=_env("RESULTADOS_DIR"),
                    help="dónde guardar los resultados_*.json (env RESULTADOS_DIR; defecto DATOS/resultados)")
    ap.add_argument("--log-dir", default=_env("LOG_DIR"),
                    help="carpeta de logs (env LOG_DIR; defecto DATOS/logs)")
    ap.add_argument("--db", default=_env("DB_PATH"),
                    help="SQLite local (env DB_PATH; defecto DATOS/demo.db)")
    ap.add_argument("--procesados-dir", default=_env("PDFS_PROCESADOS_DIR"),
                    help="si se define, los PDF ya procesados se MUEVEN aquí (env PDFS_PROCESADOS_DIR)")
    ap.add_argument("--destino", default=_env("PRESTADOR_EMAIL_TO"),
                    help="correo del prestador (env PRESTADOR_EMAIL_TO)")
    ap.add_argument("--modelo", default=_env("MODELO_IA"), help="modelo de Ollama (env MODELO_IA)")
    ap.add_argument("--mysql", action="store_true", default=_env_si("USAR_MYSQL"),
                    help="leer afiliados de MySQL (env USAR_MYSQL=1)")
    ap.add_argument("--sin-ia", action="store_true", default=_env_si("SIN_IA"))
    ap.add_argument("--siempre-ia", action="store_true", default=_env_si("SIEMPRE_IA"))
    ap.add_argument("--enviar", action="store_true",
                    help="enviar correos por SMTP de verdad. Sin esta bandera se SIMULA "
                         "(a propósito no se activa desde el .env)")
    ap.add_argument("--reprocesar", action="store_true",
                    help="ignora el registro de PDF ya procesados")
    return ap.parse_args()


# ------------------------------ flujo ------------------------------
def main() -> int:
    a = _args()

    datos = _ruta(a.datos)
    pdfs_dir = _ruta(a.pdfs)
    txt_base = _ruta(a.txt_dir) if a.txt_dir else datos / "txt"
    res_dir = _ruta(a.resultados_dir) if a.resultados_dir else datos / "resultados"
    log_dir = _ruta(a.log_dir) if a.log_dir else datos / "logs"
    db_path = _ruta(a.db) if a.db else datos / "demo.db"
    proc_dir = _ruta(a.procesados_dir) if a.procesados_dir else None
    estado_path = datos / "estado_procesados.json"

    for d in (datos, txt_base, res_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    # --- un solo proceso a la vez (cron puede solapar ejecuciones) ---
    lock = open(datos / "ejecutar.lock", "w")
    if not _bloquear(lock):
        print("Ya hay otra ejecución en curso; se omite esta.")
        return 3

    ahora = datetime.now()
    for flujo in (sys.stdout, sys.stderr):   # consola de Windows (cp1252): evita errores con tildes
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    archivo_log = open(log_dir / f"ejecutar_{ahora:%Y%m%d}.log", "a", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, archivo_log)
    sys.stderr = _Tee(sys.__stderr__, archivo_log)

    log("=" * 60)
    log(f"Inicio | PDFs: {pdfs_dir} | datos: {datos} | BD local: {db_path}")

    # --- validaciones de configuración ---
    if not pdfs_dir.is_dir():
        log(f"ERROR: no existe la carpeta de PDFs: {pdfs_dir}")
        return 2
    if a.enviar:
        falta = [v for v in ("SMTP_HOST", "SMTP_USER") if not _env(v)]
        if not (_env("SMTP_PASSWORD") or _env("SMTP_PASS")):
            falta.append("SMTP_PASSWORD")
        if falta:
            log(f"ERROR: con --enviar faltan variables SMTP: {', '.join(falta)}")
            return 2
    if a.mysql:
        falta = [v for v in ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
                 if not _env(v)]
        if falta:
            log(f"ERROR: con MySQL faltan variables: {', '.join(falta)}")
            return 2

    from pdf_a_texto import procesar_pdf
    from extractor_ia import MODELO_DEFECTO, OLLAMA_HOST
    modelo = a.modelo or MODELO_DEFECTO

    if not a.sin_ia:
        try:
            import requests
            requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5).raise_for_status()
        except Exception as e:
            log(f"AVISO: Ollama no responde en {OLLAMA_HOST} ({e}). Los casos que las reglas no "
                f"resuelvan quedarán en revisión humana y se reintentarán en la próxima ejecución.")

    # --- PDFs nuevos ---
    todos = sorted(p for p in pdfs_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    conteo_stem: dict[str, int] = {}
    for p in todos:
        conteo_stem[p.stem] = conteo_stem.get(p.stem, 0) + 1
    duplicados = {s for s, n in conteo_stem.items() if n > 1}
    for s in sorted(duplicados):
        log(f"ERROR: hay varios PDF con el nombre '{s}'. Se omiten (el adjunto del correo "
            f"podría corresponder a otra usuaria). Renómbralos.")

    estado = _cargar_estado(estado_path)
    nuevos: list[tuple[Path, str]] = []
    for p in todos:
        if p.stem in duplicados:
            continue
        sha = _sha_archivo(p)
        if a.reprocesar or sha not in estado:
            nuevos.append((p, sha))

    log(f"{len(todos)} PDF en la carpeta | {len(nuevos)} nuevos por procesar")
    if not nuevos:
        log("Nada que procesar.")
        return 1 if duplicados else 0

    # --- 1) PDF -> texto, en una carpeta de lote (queda como evidencia de auditoría) ---
    lote = txt_base / f"lote_{ahora:%Y%m%d_%H%M%S}"
    lote.mkdir(parents=True, exist_ok=True)
    mapa: dict[str, tuple[Path, str]] = {}
    errores = len(duplicados)
    for i, (p, sha) in enumerate(nuevos, 1):
        texto, metodo, error = procesar_pdf(p)
        if error or not texto.strip():
            motivo = error.splitlines()[0] if error else "sin texto"
            log(f"[{i}/{len(nuevos)}] {p.name}: ERROR al leer el PDF ({motivo})")
            errores += 1
            continue
        (lote / f"{p.stem}.txt").write_text(texto, encoding="utf-8")
        mapa[f"{p.stem}.txt"] = (p, sha)
    log(f"Texto extraído de {len(mapa)}/{len(nuevos)} PDF -> {lote}")
    if not mapa:
        return 1

    # --- 2) pipeline (reglas / IA / BD / decisión / correo) ---
    salida = res_dir / f"resultados_{ahora:%Y%m%d_%H%M%S}.json"
    argv = ["pipeline.py", str(lote), str(salida), "--pdfs", str(pdfs_dir),
            "--db", str(db_path), "--modelo", modelo]
    if a.destino:
        argv += ["--destino", a.destino]
    if a.enviar:
        argv.append("--enviar")
    if a.mysql:
        argv.append("--mysql")
    if a.sin_ia:
        argv.append("--sin-ia")
    if a.siempre_ia:
        argv.append("--siempre-ia")

    import pipeline
    sys.argv = argv
    try:
        pipeline.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            log(f"ERROR: el pipeline terminó con: {e.code}")
            return 1
    except Exception as e:
        log(f"ERROR inesperado en el pipeline: {type(e).__name__}: {e}")
        return 1

    # --- 3) registrar como procesados (los fallidos se reintentan en la próxima corrida) ---
    try:
        resultados = json.loads(salida.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"ERROR: no se pudo leer {salida}: {e}")
        return 1

    reintentar = 0
    for r in resultados:
        origen = mapa.get(r["archivo"])
        if not origen:
            continue
        p, sha = origen
        fallo_envio = r["correo"]["estado"] == "error_envio"
        ia_caida = (not a.sin_ia) and r["fuente_extraccion"] == "reglas_sin_ia"
        if fallo_envio or ia_caida:
            reintentar += 1
            log(f"{p.name}: se reintentará en la próxima ejecución "
                f"({'fallo de envío' if fallo_envio else 'IA no disponible'})")
            continue
        estado[sha] = {"archivo": p.name, "fecha": datetime.now().isoformat(timespec="seconds"),
                       "resultados": salida.name}
        if proc_dir:
            destino_pdf = proc_dir / p.relative_to(pdfs_dir)
            destino_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(destino_pdf))
    _guardar_estado(estado_path, estado)

    log(f"Fin | resultados: {salida} | por reintentar: {reintentar} | errores de lectura: {errores}")
    return 1 if (errores or reintentar) else 0


if __name__ == "__main__":
    sys.exit(main())