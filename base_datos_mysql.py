"""
base_datos_mysql.py

Lee los datos de la afiliada desde la tabla aseg_afiliados de MySQL
(SOLO LECTURA). Credenciales desde variables de entorno / .env:
    DATABASE_HOST, DATABASE_PORT, DATABASE_NAME, DATABASE_USER, DATABASE_PASSWORD

Requisito:  pip install pymysql

El histórico de citologías, las exclusiones y los envíos siguen guardándose en
el SQLite local (demo.db) hasta que me pases las tablas reales del módulo de
muestras. Así no se escribe nada en la BD de tu sistema.
"""

import os
import re

from base_datos import BaseDatos

_SQL = """
SELECT aa.numero_documento, aa.mae_tipo_documento_codigo,
       aa.primer_nombre, aa.segundo_nombre, aa.primer_apellido, aa.segundo_apellido,
       aa.telefono_movil, aa.telefono_fijo, aa.telefono_contacto_emergencia,
       aa.email, aa.direccion_residencia, aa.barrio,
       gu.nombre AS municipio
FROM aseg_afiliados aa
LEFT JOIN gn_ubicaciones gu ON gu.id = aa.afiliacion_ubicaciones_id
WHERE aa.numero_documento = %s
ORDER BY (aa.duplicado IS NULL OR aa.duplicado = 0) DESC,
         aa.fecha_hora_modifica DESC, aa.id DESC
LIMIT 1
"""


def _telefonos(*valores) -> str:
    """'300 123 4567 / 604-555-1234' -> '3001234567,6045551234' (sin repetidos)."""
    out = []
    for v in valores:
        limpio = re.sub(r"(?<=\d)[\s\-\.](?=\d)", "", v or "")
        for t in re.findall(r"\d{7,10}", limpio):
            if t not in out:
                out.append(t)
    return ",".join(out)


def fila_a_afiliado(f: dict) -> dict:
    """Convierte una fila de aseg_afiliados al formato que usa el pipeline."""
    nombres = " ".join(
        (f.get(k) or "").strip() for k in
        ("primer_nombre", "segundo_nombre", "primer_apellido", "segundo_apellido")
    ).split()
    return {
        "documento": str(f["numero_documento"]).strip(),
        "tipo_doc": f.get("mae_tipo_documento_codigo"),
        "nombres": " ".join(nombres).upper(),
        # móvil primero; el de emergencia solo como último recurso
        "telefono": _telefonos(f.get("telefono_movil"), f.get("telefono_fijo"),
                               f.get("telefono_contacto_emergencia")),
        "correo": (f.get("email") or "").strip().lower() or None,
        "direccion": " ".join(filter(None, [(f.get("direccion_residencia") or "").strip(),
                                            (f.get("barrio") or "").strip()])) or None,
        "municipio": (f.get("municipio") or "").strip() or None,   # gn_ubicaciones.nombre
        "departamento": None,
    }


class BaseDatosMySQL(BaseDatos):
    def __init__(self, ruta_local: str = "demo.db"):
        super().__init__(ruta_local)      # SQLite local: histórico, exclusiones, envíos
        self._my = None

    def _conexion(self):
        import pymysql
        if self._my is None or not self._my.open:
            self._my = pymysql.connect(
                host=os.environ["DATABASE_HOST"],
                port=int(os.environ.get("DATABASE_PORT", 3306)),
                user=os.environ["DATABASE_USER"],
                password=os.environ["DATABASE_PASSWORD"],
                database=os.environ["DATABASE_NAME"],
                charset="utf8mb4", connect_timeout=10,
                cursorclass=pymysql.cursors.DictCursor,
                init_command="SET SESSION TRANSACTION READ ONLY",
            )
        return self._my

    def buscar_afiliado(self, documento: str) -> dict | None:
        try:
            with self._conexion().cursor() as cur:
                cur.execute(_SQL, (documento,))
                fila = cur.fetchone()
            return fila_a_afiliado(fila) if fila else None
        except Exception as e:   # BD caída: el caso sigue con datos del PDF
            print(f"  [aviso] no se pudo consultar MySQL: {e}")
            return None

    def antecedente_asc_us(self, documento: str, sha_actual: str = "") -> bool | None:
        """Con el histórico local vacío NO se puede afirmar 'sin antecedente'.
        Devuelve True si el SQLite local lo registra; si no, None (sin verificar),
        y el motor manda el ASC-US sin VPH a revisión humana. Cuando se conecte
        la tabla real de citologías, aquí se consulta y se devuelve False."""
        if any("1" in a["codigos"].split(",")
               for a in self.antecedentes(documento, sha_actual)):
            return True
        return None


if __name__ == "__main__":
    # Prueba sin conexión del mapeo de columnas
    f = dict(numero_documento="1000000001", mae_tipo_documento_codigo="CC",
             primer_nombre="María", segundo_nombre=None, primer_apellido="Pérez",
             segundo_apellido="Gómez", telefono_movil="300 123 4567",
             telefono_fijo="604-555-1234", telefono_contacto_emergencia=None,
             email="Maria@Correo.com", direccion_residencia="Cra 10 # 20-30",
             barrio="Laureles", municipio="Medellín")
    a = fila_a_afiliado(f)
    assert a["nombres"] == "MARÍA PÉREZ GÓMEZ"
    assert a["telefono"] == "3001234567,6045551234"
    assert a["correo"] == "maria@correo.com"
    assert a["direccion"] == "Cra 10 # 20-30 Laureles"
    assert a["municipio"] == "Medellín"
    print("OK", a)