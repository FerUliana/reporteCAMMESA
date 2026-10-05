"""
Extrae producción y factor de planta de los parques Arauco (PEA, VAR, AR)
desde el Parte Post Operativo público de CAMMESA.

No requiere usuario/contraseña (es un endpoint público).

Uso:
    python extraer_produccion_arauco.py 2026-09-29
    (si no se pasa fecha, usa el día de ayer)

Salida:
    produccion_arauco_<fecha>.json
"""
import sys
import re
import json
import zipfile
import tempfile
import datetime
import collections
from pathlib import Path

import requests  # pip install requests

BASE = "https://api.cammesa.com/pub-svc/public"
NEMO = "PARTE_POST_OPERATIVO_UNIF"

# Potencia instalada de cada parque (MW)
POTENCIA_INSTALADA = {
    "PEA": 50.4,   # Arauco I  (ARAUEO + ARA2EO)
    "VAR": 99.75,  # Arauco II Etapa 1 y 2  (AR21EO)
    "AR":  99.4,   # Arauco II Etapa 3 y 4  (AR22EO)
}

# Qué máquina(s) de CAMMESA componen cada parque
MAQUINAS_POR_PARQUE = {
    "PEA": ["ARAUEO", "ARA2EO"],
    "VAR": ["AR21EO"],
    "AR":  ["AR22EO"],
}


def obtener_doc_id(fecha: datetime.date) -> tuple[str, str]:
    """Busca el documento del Post Operativo de una fecha. Devuelve (docId, nemo_real)."""
    desde = f"{fecha.isoformat()}T00:00:00.000-03:00"
    hasta = f"{fecha.isoformat()}T23:59:59.000-03:00"
    r = requests.get(
        f"{BASE}/findDocumentosByNemoRango",
        params={"fechadesde": desde, "fechahasta": hasta, "nemo": NEMO},
        timeout=30,
    )
    r.raise_for_status()
    docs = r.json()
    if not docs:
        raise RuntimeError(f"No se encontró Post Operativo para {fecha}")
    doc = docs[0]
    return doc["id"], doc["nemo"]


def descargar_zip(doc_id: str, nemo_real: str, destino: Path) -> Path:
    r = requests.get(
        f"{BASE}/findAllAttachmentZipByNemoId",
        params={"docId": doc_id, "nemo": nemo_real},
        timeout=60,
    )
    r.raise_for_status()
    out = destino / "post_operativo.zip"
    out.write_bytes(r.content)
    return out


def parsear_enerrenov(zip_path: Path) -> list[tuple]:
    """Descomprime (el zip viene anidado) y parsea la tabla de energía renovable."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        # el primer zip contiene otro zip adentro (ej: PO260929.zip)
        inner_zips = list(tmp.glob("*.zip"))
        if inner_zips:
            with zipfile.ZipFile(inner_zips[0]) as z:
                z.extractall(tmp)

        archivo = next(tmp.rglob("enerrenov.html"))
        html = archivo.read_text(encoding="utf-8", errors="ignore")

    patron = re.compile(
        r"<tr><td>(AR21EO|AR22EO|ARA2EO|ARAUEO)</td><td>(\d+)</td>"
        r"<td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td>"
        r"<td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td></tr>"
    )
    return patron.findall(html)


def procesar(fecha: datetime.date) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        doc_id, nemo_real = obtener_doc_id(fecha)
        zip_path = descargar_zip(doc_id, nemo_real, tmp)
        filas = parsear_enerrenov(zip_path)

    generada_por_maquina = collections.defaultdict(float)
    for grupo, hora, restric, generada, pron, posible, vertida, top, acred in filas:
        generada_por_maquina[grupo] += float(generada.replace(",", "."))

    resultado = {"fecha": fecha.isoformat(), "parques": {}}
    for parque, maquinas in MAQUINAS_POR_PARQUE.items():
        total_mwh = sum(generada_por_maquina.get(m, 0.0) for m in maquinas)
        potencia = POTENCIA_INSTALADA[parque]
        factor_planta = total_mwh / (potencia * 24) if potencia else None
        resultado["parques"][parque] = {
            "maquinas": maquinas,
            "produccion_mwh": round(total_mwh, 2),
            "potencia_instalada_mw": potencia,
            "factor_planta": round(factor_planta, 4) if factor_planta is not None else None,
        }
    return resultado


def procesar_mes(anio: int, mes: int) -> dict:
    """Recorre todos los días disponibles del mes (hasta ayer, si el mes está en curso)
    y arma el acumulado mensual de producción y factor de planta por parque."""
    primer_dia = datetime.date(anio, mes, 1)
    if mes == 12:
        siguiente_mes = datetime.date(anio + 1, 1, 1)
    else:
        siguiente_mes = datetime.date(anio, mes + 1, 1)
    ayer = datetime.date.today() - datetime.timedelta(days=1)
    ultimo_dia = min(siguiente_mes - datetime.timedelta(days=1), ayer)

    diario_por_parque = {p: [] for p in MAQUINAS_POR_PARQUE}
    dias_procesados = []

    dia = primer_dia
    while dia <= ultimo_dia:
        try:
            r = procesar(dia)
            dias_procesados.append(dia.isoformat())
            for parque, info in r["parques"].items():
                diario_por_parque[parque].append(
                    {"fecha": dia.isoformat(), "produccion_mwh": info["produccion_mwh"]}
                )
        except Exception as e:
            print(f"  aviso: no se pudo procesar {dia} ({e})", file=sys.stderr)
        dia += datetime.timedelta(days=1)

    horas_cubiertas = len(dias_procesados) * 24

    resultado = {
        "mes": f"{anio:04d}-{mes:02d}",
        "dias_procesados": dias_procesados,
        "parques": {},
    }
    for parque, maquinas in MAQUINAS_POR_PARQUE.items():
        serie = diario_por_parque[parque]
        total_mwh = sum(d["produccion_mwh"] for d in serie)
        potencia = POTENCIA_INSTALADA[parque]
        factor_planta = total_mwh / (potencia * horas_cubiertas) if horas_cubiertas else None
        resultado["parques"][parque] = {
            "maquinas": maquinas,
            "produccion_mensual_mwh": round(total_mwh, 2),
            "potencia_instalada_mw": potencia,
            "factor_planta_mensual": round(factor_planta, 4) if factor_planta is not None else None,
            "diario": serie,
        }
    return resultado


if __name__ == "__main__":
    # Un solo día:   python extraer_produccion_arauco.py 2026-09-29
    # Un mes entero: python extraer_produccion_arauco.py 2026-09 --mes
    if len(sys.argv) > 1 and "--mes" in sys.argv:
        anio, mes = (int(x) for x in sys.argv[1].split("-"))
        data = procesar_mes(anio, mes)
        out_path = Path(f"produccion_arauco_{data['mes']}.json")
    elif len(sys.argv) > 1:
        fecha = datetime.date.fromisoformat(sys.argv[1])
        data = procesar(fecha)
        out_path = Path(f"produccion_arauco_{fecha.isoformat()}.json")
    else:
        fecha = datetime.date.today() - datetime.timedelta(days=1)
        data = procesar(fecha)
        out_path = Path(f"produccion_arauco_{fecha.isoformat()}.json")

    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK -> {out_path}")
    print(json.dumps(data, indent=2, ensure_ascii=False))
