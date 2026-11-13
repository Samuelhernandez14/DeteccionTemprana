"""
base_datos.py

Capa de acceso a datos. Hoy usa SQLite (archivo demo.db) como BD de pruebas.
Para conectar la BD real de tu sistema (SQL Server, Postgres...) reemplaza
SOLO los métodos de esta clase por consultas a tus tablas; el pipeline no
cambia. Ej. SQL Server:  import pyodbc; self.con = pyodbc.connect(...)

Tablas:
  afiliados            datos maestros (nombre, teléfono, dirección...)
  exclusiones          programa de cáncer, disentimiento, etc.
  historico_citologias citologías anteriores (para la regla 3: ASC-US previo)
  prestadores          correo certificado del prestador de colposcopia
  envios               correos enviados + fecha, para el semáforo de días

Uso:
  python base_datos.py demo.db --demo               # datos de ejemplo
  python base_datos.py demo.db --csv afiliados.csv  # cargar export de tu sistema
    (columnas: documento,tipo_doc,nombres,telefono,correo,direccion,municipio,departamento)
"""

import argparse
import csv
import sqlite3
from datetime import date, datetime, timezone

from extractor_afiliado import nombres_coinciden

ESQUEMA = """
CREATE TABLE IF NOT EXISTS afiliados (
    documento TEXT PRIMARY KEY, tipo_doc TEXT, nombres TEXT, telefono TEXT,
    correo TEXT, direccion TEXT, municipio TEXT, departamento TEXT);
CREATE TABLE IF NOT EXISTS exclusiones (
    documento TEXT, motivo TEXT);
CREATE TABLE IF NOT EXISTS historico_citologias (
    documento TEXT, fecha TEXT, codigos TEXT, vph INTEGER, sha256 TEXT,
    UNIQUE(documento, sha256));
CREATE TABLE IF NOT EXISTS prestadores (
    nombre TEXT, correo TEXT);
CREATE TABLE IF NOT EXISTS envios (
    sha256 TEXT PRIMARY KEY, documento TEXT, fecha_envio TEXT, destino TEXT,
    regla INTEGER, indicacion TEXT, estado TEXT, justificacion TEXT);
"""


def semaforo(fecha_envio_iso: str, hoy: date | None = None) -> tuple[int, str]:
    """Día 0 = día del envío. Verde 0-2, amarillo 3, rojo 4 en adelante."""
    hoy = hoy or date.today()
    dias = (hoy - datetime.fromisoformat(fecha_envio_iso).date()).days
    return dias, ("verde" if dias <= 2 else "amarillo" if dias == 3 else "rojo")


class BaseDatos:
    def __init__(self, ruta: str = "demo.db"):
        self.con = sqlite3.connect(ruta)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(ESQUEMA)

    # ---------------- lectura ----------------
    def buscar_afiliado(self, documento: str) -> dict | None:
        r = self.con.execute("SELECT * FROM afiliados WHERE documento=?",
                             (documento,)).fetchone()
        return dict(r) if r else None

    def exclusiones(self, documento: str) -> list[str]:
        return [r["motivo"] for r in self.con.execute(
            "SELECT motivo FROM exclusiones WHERE documento=?", (documento,))]

    def antecedentes(self, documento: str, sha_actual: str = "") -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT fecha, codigos, vph FROM historico_citologias "
            "WHERE documento=? AND sha256<>? ORDER BY fecha DESC",
            (documento, sha_actual))]

    def antecedente_asc_us(self, documento: str, sha_actual: str = "") -> bool | None:
        """True si hubo ASC-US previo. False si la afiliada existe en la BD y no
        lo tiene (histórico verificable). None si no hay forma de verificar."""
        if any("1" in a["codigos"].split(",") for a in self.antecedentes(documento, sha_actual)):
            return True
        return False if self.buscar_afiliado(documento) else None

    def correo_prestador(self) -> str | None:
        r = self.con.execute("SELECT correo FROM prestadores LIMIT 1").fetchone()
        return r["correo"] if r else None

    # ------------- cruce PDF vs BD -------------
    def completar(self, pdf: dict) -> tuple[dict, list[str], bool]:
        """Compara y complementa los datos del PDF con los de la BD.
        Devuelve (datos_finales, alertas, hay_conflicto).
        Regla: la BD manda en datos de contacto; el PDF rellena lo que falte.
        Si el nombre no coincide, hay conflicto (posible documento mal digitado)."""
        datos, alertas = dict(pdf), []
        if not pdf.get("documento"):
            return datos, ["No se extrajo el documento del PDF"], False

        bd = self.buscar_afiliado(pdf["documento"])
        if not bd:
            return datos, ["Afiliada no encontrada en la BD: se usan solo datos del PDF"], False

        conflicto = False
        if pdf.get("nombres") and bd["nombres"] and \
                not nombres_coinciden(pdf["nombres"], bd["nombres"]):
            alertas.append(f"Nombre distinto: PDF='{pdf['nombres']}' vs BD='{bd['nombres']}'")
            conflicto = True

        for campo in ("nombres", "correo", "direccion", "municipio", "departamento"):
            if bd.get(campo):
                if campo != "nombres" and pdf.get(campo) and pdf[campo] != bd[campo]:
                    alertas.append(f"{campo} difiere (PDF vs BD); se usa la BD")
                if not (campo == "nombres" and conflicto):
                    datos[campo] = bd[campo]
            elif pdf.get(campo):
                alertas.append(f"{campo} completado desde el PDF (faltaba en la BD)")

        telefonos = [t for t in (bd.get("telefono") or "").replace(" ", "").split(",") if t]
        datos["telefonos"] = telefonos + [t for t in pdf.get("telefonos", [])
                                          if t not in telefonos]
        if not datos["telefonos"]:
            alertas.append("Sin teléfono ni en PDF ni en BD")
        datos["tipo_doc"] = pdf.get("tipo_doc") or bd.get("tipo_doc")
        return datos, alertas, conflicto

    # ---------------- escritura ----------------
    def registrar_historico(self, documento, fecha, codigos, vph, sha256):
        self.con.execute("INSERT OR IGNORE INTO historico_citologias VALUES (?,?,?,?,?)",
                         (documento, fecha, ",".join(map(str, codigos)), vph, sha256))
        self.con.commit()

    def ya_enviado(self, sha256: str) -> bool:
        return self.con.execute("SELECT 1 FROM envios WHERE sha256=?",
                                (sha256,)).fetchone() is not None

    def registrar_envio(self, sha256, documento, destino, regla, indicacion, estado):
        self.con.execute(
            "INSERT OR REPLACE INTO envios VALUES (?,?,?,?,?,?,?,NULL)",
            (sha256, documento, datetime.now(timezone.utc).isoformat(),
             destino, regla, indicacion, estado))
        self.con.commit()

    def listar_envios(self) -> list[dict]:
        out = []
        for r in self.con.execute("SELECT * FROM envios ORDER BY fecha_envio"):
            d = dict(r)
            d["dias"], d["semaforo"] = semaforo(d["fecha_envio"])
            out.append(d)
        return out

    def upsert_afiliado(self, a: dict):
        self.con.execute(
            "INSERT OR REPLACE INTO afiliados VALUES (?,?,?,?,?,?,?,?)",
            (a["documento"], a.get("tipo_doc"), a.get("nombres"), a.get("telefono"),
             a.get("correo"), a.get("direccion"), a.get("municipio"),
             a.get("departamento")))
        self.con.commit()

    def cargar_csv(self, ruta: str) -> int:
        n = 0
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                self.upsert_afiliado(fila)
                n += 1
        return n

    def sembrar_demo(self):
        self.upsert_afiliado(dict(
            documento="1000000001", tipo_doc="CC", nombres="MARIA PEREZ GOMEZ",
            telefono="3001234567", correo="", direccion="Cra 10 # 20-30",
            municipio="Medellín", departamento="Antioquia"))
        self.upsert_afiliado(dict(
            documento="1000000002", tipo_doc="CC", nombres="LUZ RESTREPO",
            telefono="3109876543", correo="", direccion="Cl 50 # 40-10",
            municipio="Bello", departamento="Antioquia"))
        self.con.execute("INSERT INTO exclusiones VALUES ('1000000002','disentimiento')")
        self.registrar_historico("1000000001", "2025-03-01", [1], 20, "hist-demo-1")
        if not self.correo_prestador():
            self.con.execute("INSERT INTO prestadores VALUES "
                             "('Prestador Demo','colposcopia@prestador-demo.test')")
        self.con.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--csv")
    a = ap.parse_args()
    bd = BaseDatos(a.db)
    if a.demo:
        bd.sembrar_demo()
        print("Datos de ejemplo cargados.")
    if a.csv:
        print(f"{bd.cargar_csv(a.csv)} afiliados cargados desde {a.csv}")
