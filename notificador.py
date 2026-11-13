"""
notificador.py

Arma y envía el correo al prestador de colposcopia con los datos de la
usuaria, el resultado de la citología, los antecedentes y el PDF adjunto.

Dos modos:
  - "simulado" (por defecto): NO envía nada; guarda el correo como .eml en
    una carpeta para que lo abras y lo muestres. Ideal para la demo.
  - "smtp": envía de verdad. Configura estas variables de entorno:
        SMTP_HOST, SMTP_PORT (465 o 587), SMTP_USER, SMTP_PASSWORD
"""

import os
import re
import smtplib
import ssl
import time
from html import escape
from email.message import EmailMessage
from pathlib import Path

from motor_reglas import CITOLOGIA, VPH_DETECTADO, VPH_NO_DETECTADO

PLAZO_DIAS = 5
INDICACION_TXT = {
    "tamizaje": "Tamizaje alterado",
    "clinica": "Indicación clínica (orden médica de colposcopia / Anexo 1)",
}


def _nombre_codigos(codigos) -> str:
    return "; ".join(CITOLOGIA.get(int(c), f"código {c}") for c in codigos) or "—"


def _texto_vph(vph) -> str:
    return {VPH_DETECTADO: "Detectado", VPH_NO_DETECTADO: "No detectado"}.get(vph, "No informado")


def _tabla(pares: list[tuple[str, str]]) -> str:
    filas = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#6b7280;font-size:14px;width:120px;'
        f'vertical-align:top">{escape(k)}</td>'
        f'<td style="padding:6px 0;font-size:15px;color:#111827;font-weight:500">{escape(v)}</td></tr>'
        for k, v in pares)
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{filas}</table>'


def _seccion(titulo: str, contenido: str) -> str:
    return (f'<tr><td style="padding:20px 28px 4px 28px">'
            f'<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;'
            f'color:#0f5c7a;font-weight:700;border-bottom:2px solid #e5e7eb;'
            f'padding-bottom:6px;margin-bottom:8px">{escape(titulo)}</div>{contenido}</td></tr>')


def _pdf_adjunto(pdf_path: Path | None) -> tuple[bytes, str] | None:
    """Devuelve (bytes, nombre) del PDF a adjuntar, o None si no se debe adjuntar.
    PRIVACIDAD: algunos informes traen otros exámenes (VIH, serología...) en las demás
    hojas. Al prestador de colposcopia solo se le envían las páginas de la citología.
    Si el PDF tiene varias hojas y no se puede identificar cuáles son, NO se adjunta."""
    if not pdf_path or not Path(pdf_path).exists():
        return None
    pdf_path = Path(pdf_path)
    try:
        try:
            import pymupdf as fitz   # nombre actual de PyMuPDF
        except ImportError:
            import fitz
        doc = fitz.open(pdf_path)
        if doc.page_count == 1:
            return pdf_path.read_bytes(), pdf_path.name
        paginas = [i for i, pg in enumerate(doc)
                   if re.search(r"citolog|bethesda", pg.get_text().lower())]
        if not paginas:
            return None
        if len(paginas) == doc.page_count:
            return pdf_path.read_bytes(), pdf_path.name
        solo = fitz.open()
        for i in paginas:
            solo.insert_pdf(doc, from_page=i, to_page=i)
        return solo.tobytes(), f"{pdf_path.stem}_citologia.pdf"
    except Exception:
        return None


def construir_correo(destino: str, remitente: str, afiliado: dict, extraccion: dict,
                     decision: dict, antecedentes: list[dict],
                     pdf_path: Path | None) -> EmailMessage:
    nombre = afiliado.get("nombres") or "No disponible"
    doc = f"{afiliado.get('tipo_doc') or ''} {afiliado.get('documento') or ''}".strip() or "No disponible"
    tel = ", ".join(afiliado.get("telefonos") or []) or "No disponible"
    direccion = afiliado.get("direccion") or "No disponible"
    municipio = afiliado.get("municipio") or "No disponible"
    indicacion = INDICACION_TXT.get(decision["indicacion"], decision["indicacion"])
    hallazgo = _nombre_codigos(extraccion["codigos_citologia"])
    vph = _texto_vph(extraccion["vph"]) if extraccion["vph"] in (VPH_DETECTADO, VPH_NO_DETECTADO) else None
    adjunto = _pdf_adjunto(pdf_path)
    ants = [(a["fecha"], _nombre_codigos(a["codigos"].split(","))) for a in antecedentes]

    # ---------- texto plano (respaldo) ----------
    plano = [
        "Buen día,", "",
        "Se remite a la siguiente usuaria para COLPOSCOPIA.",
        f"Plazo de atención: {PLAZO_DIAS} días desde la recepción de este correo.", "",
        "DATOS DE LA USUARIA",
        f"  Nombre:     {nombre}", f"  Documento:  {doc}", f"  Teléfonos:  {tel}",
        f"  Dirección:  {direccion}", f"  Municipio:  {municipio}", "",
        "INDICACIÓN", f"  {indicacion}", "",
        "RESULTADO DE LA CITOLOGÍA", f"  Hallazgo: {hallazgo}",
    ]
    if vph:
        plano.append(f"  VPH:      {vph}")
    if adjunto:
        plano.append("  (Se adjunta el informe de la citología en PDF)")
    plano += ["", "ANTECEDENTES"]
    plano += [f"  - {f}: {r}" for f, r in ants] or ["  Sin antecedentes registrados"]
    plano += ["", "Si la usuaria no pudo ser contactada, no asistió, está hospitalizada o enferma,",
              "registre la justificación en el sistema.", "",
              "Mensaje automático con información confidencial de salud. "
              "Si lo recibió por error, elimínelo y avise al remitente."]

    # ---------- HTML ----------
    res = [("Hallazgo", hallazgo)] + ([("VPH", vph)] if vph else [])
    ant_html = ("".join(
        f'<div style="padding:4px 0;font-size:14px;color:#111827">'
        f'<span style="color:#6b7280">{escape(f)}</span> &nbsp;·&nbsp; {escape(r)}</div>'
        for f, r in ants) or '<div style="font-size:14px;color:#6b7280">Sin antecedentes registrados</div>')

    html = f"""<!DOCTYPE html><html lang="es"><body style="margin:0;padding:0;background:#f3f5f8">
<div style="background:#f3f5f8;padding:24px 12px;font-family:'Segoe UI',Arial,sans-serif;color:#1f2937">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"
 style="max-width:620px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
 <tr><td style="background:#0f5c7a;padding:24px 28px">
   <div style="color:#bfe3f0;font-size:12px;letter-spacing:1px;text-transform:uppercase">Remisión</div>
   <div style="color:#ffffff;font-size:22px;font-weight:700;margin-top:2px">Solicitud de colposcopia</div>
   <div style="color:#d9eef6;font-size:14px;margin-top:6px">{escape(indicacion)}</div>
 </td></tr>
 <tr><td style="padding:18px 28px 0 28px">
   <div style="background:#fff7e6;border-left:4px solid #f59e0b;border-radius:4px;padding:12px 14px;font-size:14px;color:#92400e">
     <strong>Plazo de atención: {PLAZO_DIAS} días</strong> desde la recepción de este correo.
   </div>
 </td></tr>
 {_seccion("Datos de la usuaria", _tabla([("Nombre", nombre), ("Documento", doc), ("Teléfonos", tel), ("Dirección", direccion), ("Municipio", municipio)]))}
 {_seccion("Resultado de la citología", _tabla(res) + ('<div style="font-size:13px;color:#6b7280;padding-top:6px">El informe de la citología está adjunto en PDF.</div>' if adjunto else ''))}
 {_seccion("Antecedentes", ant_html)}
 <tr><td style="padding:20px 28px 8px 28px;font-size:14px;color:#374151">
   Si la usuaria no pudo ser contactada, no asistió, está hospitalizada o enferma,
   registre la justificación en el sistema.
 </td></tr>
 <tr><td style="padding:16px 28px 22px 28px;font-size:12px;color:#9ca3af;border-top:1px solid #f0f1f3">
   Mensaje automático con información confidencial de salud. Si lo recibió por error,
   elimínelo y avise al remitente.
 </td></tr>
</table></div></body></html>"""

    msg = EmailMessage()
    # Asunto SIN datos personales (los correos se indexan y reenvían)
    msg["Subject"] = f"Remisión a colposcopia - {indicacion.split(' (')[0]}"
    msg["From"] = remitente
    msg["To"] = destino
    msg.set_content("\n".join(plano))
    msg.add_alternative(html, subtype="html")

    if adjunto:
        msg.add_attachment(adjunto[0], maintype="application", subtype="pdf", filename=adjunto[1])
    return msg


def _enviar_smtp(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", 465))
    user = os.environ["SMTP_USER"]
    clave = os.environ.get("SMTP_PASSWORD") or os.environ["SMTP_PASS"]
    ctx = ssl.create_default_context()
    if port == 465:   # SSL directo (Gmail)
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, clave)
            s.send_message(msg)
    else:             # 587: STARTTLS
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, clave)
            s.send_message(msg)


def enviar(msg: EmailMessage, modo: str = "simulado",
           carpeta_eml: Path = Path("correos_salida")) -> tuple[str, str]:
    """Devuelve (estado, detalle). Nunca lanza excepción: un fallo de envío
    no debe tumbar el procesamiento del resto de PDFs."""
    try:
        if modo == "smtp":
            ultimo = None
            for intento in range(3):   # Gmail a veces corta la conexión: se reintenta
                try:
                    _enviar_smtp(msg)
                    return "enviado", msg["To"]
                except (smtplib.SMTPException, OSError) as e:
                    ultimo = e
                    time.sleep(3 * (intento + 1))
            raise ultimo

        carpeta_eml.mkdir(parents=True, exist_ok=True)
        ruta = carpeta_eml / f"correo_{abs(hash(msg.as_bytes())) % 10**8}.eml"
        ruta.write_bytes(bytes(msg))
        return "simulado", str(ruta)

    except Exception as e:
        return "error_envio", str(e)