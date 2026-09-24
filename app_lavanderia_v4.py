import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime

import pandas as pd
import streamlit as st

# =========================================================
#  App de avisos de WhatsApp para lavandería (v4)
#  Carga CSV -> SQLite -> buscar/marcar -> enviar (demo o real)
# =========================================================

st.set_page_config(page_title="Avisos de Órdenes", page_icon="🧺", layout="wide")

DB_PATH = "ordenes.db"
LOTE_MAX = 50           # tope de mensajes por tanda
PAUSA_REAL = 1.0        # pausa entre mensajes reales (Twilio)
PAUSA_DEMO = 0.15       # pausa simulada, solo para que se vea la barra avanzar
REQUERIDAS = ["orden", "fecha", "nombre", "telefono", "monto"]
CLAVES_TWILIO = ["ACCOUNT_SID", "AUTH_TOKEN", "CONTENT_SID", "FROM_WHATSAPP"]


# ---------- CONFIGURACIÓN (solo desde .streamlit/secrets.toml) ----------
def leer_config():
    try:
        creds = {k: st.secrets[k] for k in CLAVES_TWILIO}
    except Exception:
        creds = None  # sin secretos -> solo modo demo
    try:
        negocio = st.secrets.get("NOMBRE_NEGOCIO", "Lavandería Modelo")
    except Exception:
        negocio = "Lavandería Modelo"
    return creds, negocio


CREDS, NOMBRE_NEGOCIO = leer_config()


# ---------- BASE DE DATOS ----------
def conectar():
    return sqlite3.connect(DB_PATH)


def ejecutar(sql, params=()):
    with closing(conectar()) as c, c:  # cierra la conexión y hace commit
        return c.execute(sql, params).rowcount


def consultar(sql, params=()):
    with closing(conectar()) as c:
        return pd.read_sql_query(sql, c, params=params)


def init_db():
    with closing(conectar()) as c, c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ordenes (
                orden         TEXT PRIMARY KEY,
                fecha         TEXT,
                nombre        TEXT,
                telefono      TEXT,
                telefono_norm TEXT,
                monto         TEXT,
                valido        INTEGER,
                estado        TEXT DEFAULT 'pendiente',   -- pendiente | enviada
                sid           TEXT DEFAULT ''
            )
        """)
        # Migración desde v3: agrega columnas nuevas si no existen
        existentes = {r[1] for r in c.execute("PRAGMA table_info(ordenes)")}
        for col in ("problema", "enviado_en"):
            if col not in existentes:
                c.execute(f"ALTER TABLE ordenes ADD COLUMN {col} TEXT DEFAULT ''")


# ---------- VALIDACIÓN Y FORMATO ----------
def normalizar_telefono_rd(tel):
    d = re.sub(r"\D", "", str(tel))
    if len(d) == 10:
        d = "1" + d
    return "+" + d if d else ""


def monto_a_numero(monto):
    try:
        return float(re.sub(r"[^\d.]", "", str(monto)))
    except ValueError:
        return None


def formatear_monto(monto):
    n = monto_a_numero(monto)
    return f"{n:,.2f}" if n is not None else str(monto)


def detectar_problema(fila, tel_norm):
    """Devuelve '' si la fila se puede enviar, o el motivo si no."""
    faltan = [c for c in ("orden", "nombre", "telefono", "monto") if not fila[c]]
    if faltan:
        return "Falta: " + ", ".join(faltan)
    if not re.fullmatch(r"\+1(809|829|849)\d{7}", tel_norm):
        return "Teléfono no es de RD"
    if monto_a_numero(fila["monto"]) is None:
        return "Monto no numérico"
    return ""


def texto_mensaje(nombre, orden, monto):
    return (f"¡Hola {nombre}! 👋 Le saludamos de {NOMBRE_NEGOCIO}. "
            f"Su orden #{orden} ya está lista y puede pasar a recogerla cuando guste. "
            f"Monto a pagar al retirar: RD${formatear_monto(monto)}. "
            f"¡Gracias por su preferencia!")


# ---------- CSV ----------
def leer_csv(archivo):
    """Tolera separador ',' o ';', BOM de Excel y encabezados con mayúsculas/espacios."""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            archivo.seek(0)
            df = pd.read_csv(archivo, dtype=str, sep=None, engine="python",
                             encoding=enc, keep_default_na=False)
            break
        except UnicodeDecodeError:
            continue
    df.columns = df.columns.str.strip().str.lower()
    return df


def cargar_csv_a_db(df):
    """Inserta/actualiza órdenes. Nunca toca las que ya fueron enviadas."""
    nuevas, omitidas, con_problema = 0, 0, 0
    with closing(conectar()) as c, c:
        for _, f in df.iterrows():
            f = {k: str(f[k]).strip() for k in REQUERIDAS}
            if not f["orden"]:
                continue
            row = c.execute("SELECT estado FROM ordenes WHERE orden = ?", (f["orden"],)).fetchone()
            if row and row[0] == "enviada":
                omitidas += 1
                continue
            tel_norm = normalizar_telefono_rd(f["telefono"])
            problema = detectar_problema(f, tel_norm)
            con_problema += bool(problema)
            c.execute("""
                INSERT INTO ordenes (orden, fecha, nombre, telefono, telefono_norm, monto,
                                     valido, problema, estado, sid)
                VALUES (?,?,?,?,?,?,?,?, 'pendiente', '')
                ON CONFLICT(orden) DO UPDATE SET
                    fecha=excluded.fecha, nombre=excluded.nombre,
                    telefono=excluded.telefono, telefono_norm=excluded.telefono_norm,
                    monto=excluded.monto, valido=excluded.valido, problema=excluded.problema
            """, (f["orden"], f["fecha"], f["nombre"], f["telefono"], tel_norm,
                  f["monto"], int(not problema), problema))
            nuevas += 1
    return nuevas, omitidas, con_problema


# ---------- ENVÍO ----------
def enviar_uno(client, fila):
    """Envía (o simula) un aviso. Devuelve el SID."""
    if client is None:  # modo demo
        time.sleep(PAUSA_DEMO)
        return f"DEMO-{uuid.uuid4().hex[:10]}"
    msg = client.messages.create(
        from_=CREDS["FROM_WHATSAPP"],
        to=f"whatsapp:{fila['Teléfono']}",
        content_sid=CREDS["CONTENT_SID"],
        content_variables=json.dumps({
            "1": fila["Nombre"],
            "2": NOMBRE_NEGOCIO,
            "3": fila["Orden"],
            "4": formatear_monto(fila["Monto"]),
        }),
    )
    time.sleep(PAUSA_REAL)
    return msg.sid


# ---------- ESTADO DE LA SESIÓN ----------
ss = st.session_state
ss.setdefault("lote", 0)            # al cambiar, se reinician checkboxes y confirmación
ss.setdefault("resultados", None)   # sobrevive al st.rerun()
ss.setdefault("ultimo_archivo", None)
ss.setdefault("msg_carga", None)


def nuevo_lote():
    ss.lote += 1


init_db()

# ===== BARRA LATERAL =====
with st.sidebar:
    st.header("⚙️ Modo")
    modo_real = st.toggle(
        "Envío real por Twilio", value=False, disabled=CREDS is None,
        help="Sin secretos de Twilio configurados solo está disponible el modo demo.",
    )
    if modo_real:
        st.warning("Los mensajes se envían DE VERDAD y tienen costo.")
    else:
        st.info("🧪 Modo demo: los envíos se simulan. No sale ningún mensaje.")

    st.divider()
    st.header("📊 Resumen")
    resumen = consultar("SELECT estado, valido, COUNT(*) AS n FROM ordenes GROUP BY estado, valido")
    pend = int(resumen.query("estado == 'pendiente' and valido == 1")["n"].sum())
    env = int(resumen.query("estado == 'enviada'")["n"].sum())
    prob = int(resumen.query("estado == 'pendiente' and valido == 0")["n"].sum())
    st.metric("Pendientes", pend)
    st.metric("Enviadas", env)
    st.metric("Con problema", prob)

    st.divider()
    if st.button("🔄 Reiniciar demo", help="Vuelve todas las órdenes a 'pendiente'.", width="stretch"):
        ejecutar("UPDATE ordenes SET estado='pendiente', sid='', enviado_en=''")
        ss.resultados = None
        nuevo_lote()
        st.rerun()
    if st.button("🗑️ Vaciar base de datos", width="stretch"):
        ejecutar("DELETE FROM ordenes")
        ss.resultados = ss.ultimo_archivo = ss.msg_carga = None
        nuevo_lote()
        st.rerun()

# ===== ENCABEZADO =====
st.title("🧺 Avisos de Órdenes Listas")
st.caption(f"{NOMBRE_NEGOCIO} · {'📲 Envío real' if modo_real else '🧪 Modo demo'}")

# ===== SECCIÓN 1: CARGA (una sola vez por archivo) =====
hay_ordenes = (pend + env + prob) > 0
with st.expander("📥 Cargar órdenes desde CSV", expanded=not hay_ordenes):
    archivo = st.file_uploader("Archivo CSV (orden, fecha, nombre, telefono, monto)", type=["csv"])
    if archivo is not None and archivo.file_id != ss.ultimo_archivo:
        try:
            df = leer_csv(archivo)
            faltantes = [c for c in REQUERIDAS if c not in df.columns]
            if faltantes:
                ss.msg_carga = ("error", f"Faltan columnas: {', '.join(faltantes)}. "
                                         f"Encontré: {', '.join(df.columns)}")
            else:
                n, o, p = cargar_csv_a_db(df)
                ss.msg_carga = ("success", f"Cargadas/actualizadas: {n} · "
                                           f"Omitidas (ya enviadas): {o} · Con problema: {p}")
                nuevo_lote()
        except Exception as e:
            ss.msg_carga = ("error", f"No pude leer el archivo: {e}")
        ss.ultimo_archivo = archivo.file_id
        st.rerun()

if ss.msg_carga:  # fuera del expander para que siempre se vea
    tipo, texto = ss.msg_carga
    getattr(st, tipo)(texto)

# ===== RESULTADOS DEL ÚLTIMO ENVÍO (persisten tras el rerun) =====
if ss.resultados is not None:
    res = ss.resultados
    ok = int(res["Resultado"].str.startswith("✅").sum())
    (st.success if ok == len(res) else st.warning)(
        f"Último envío: {ok} de {len(res)} {'simulados' if res['SID'].str.startswith('DEMO').all() else 'enviados'}."
    )
    with st.expander("Ver detalle del último envío", expanded=ok < len(res)):
        st.dataframe(res, hide_index=True, width="stretch")

# ===== SECCIÓN 2: BUSCAR Y MARCAR =====
st.subheader("🔎 Buscar y seleccionar órdenes")

col1, col2, col3 = st.columns([3, 1, 1])
with col1:
    filtro = st.text_input("Buscar por número de orden o nombre", "").strip()
with col2:
    solo_pend = st.checkbox("Solo pendientes", value=True)
with col3:
    marcar_todas = st.checkbox("Marcar todas", value=False, key=f"todas_{ss.lote}")

q = """SELECT orden, fecha, nombre, telefono_norm, monto, valido, problema, estado
       FROM ordenes WHERE 1=1"""
params = []
if filtro:
    q += " AND (orden LIKE ? OR nombre LIKE ?)"
    params += [f"%{filtro}%", f"%{filtro}%"]
if solo_pend:
    q += " AND estado = 'pendiente'"
q += " ORDER BY fecha, orden"
df_orden = consultar(q, params)

if df_orden.empty:
    st.info("No hay órdenes que coincidan. Carga un CSV o ajusta la búsqueda.")
    st.stop()

df_orden["valido"] = df_orden["valido"].astype(bool)          # 1/0 -> True/False
pendiente = df_orden["estado"] == "pendiente"
df_orden.insert(0, "Enviar", marcar_todas & df_orden["valido"] & pendiente)
df_orden["monto"] = df_orden["monto"].map(lambda m: f"RD${formatear_monto(m)}")
vista = df_orden.rename(columns={
    "orden": "Orden", "fecha": "Fecha", "nombre": "Nombre", "telefono_norm": "Teléfono",
    "monto": "Monto", "valido": "¿Válido?", "problema": "Problema", "estado": "Estado",
})

# La key cambia con el filtro, las opciones y cada envío/carga:
# así las marcas NUNCA se quedan pegadas a una fila que ahora es otra orden.
editada = st.data_editor(
    vista,
    column_config={
        "Enviar": st.column_config.CheckboxColumn("Enviar", default=False),
        "¿Válido?": st.column_config.CheckboxColumn("¿Válido?"),
    },
    disabled=[c for c in vista.columns if c != "Enviar"],
    hide_index=True,
    width="stretch",
    key=f"editor_{ss.lote}_{filtro}_{solo_pend}_{marcar_todas}",
)

marcadas_todas = editada[editada["Enviar"]]
marcadas = marcadas_todas[marcadas_todas["¿Válido?"] & (marcadas_todas["Estado"] == "pendiente")]
ignoradas = len(marcadas_todas) - len(marcadas)
a_enviar = marcadas.head(LOTE_MAX)

st.write(f"**{len(marcadas)}** órdenes marcadas listas para enviar.")
if ignoradas:
    st.warning(f"{ignoradas} marcadas tienen un problema o ya fueron enviadas y NO se enviarán.")
if len(marcadas) > LOTE_MAX:
    st.warning(f"Por seguridad se envían tandas de {LOTE_MAX}. Esta tanda enviará las primeras "
               f"{LOTE_MAX}; repite para el resto.")

# ===== VISTA PREVIA DEL MENSAJE =====
if not a_enviar.empty:
    ejemplo = a_enviar.iloc[0]
    with st.container(border=True):
        st.caption(f"👀 Así le llegaría a {ejemplo['Nombre']} ({ejemplo['Teléfono']}):")
        st.markdown(texto_mensaje(ejemplo["Nombre"], ejemplo["Orden"], ejemplo["Monto"])
                    .replace("$", r"\$"))

# ===== SECCIÓN 3: ENVIAR =====
verbo = "enviar" if modo_real else "simular el envío de"
confirmar = st.checkbox(f"Confirmo {verbo} el aviso a las {len(a_enviar)} órdenes seleccionadas",
                        key=f"confirmar_{ss.lote}")

if st.button("📤 Enviar avisos" if modo_real else "🧪 Simular envío",
             disabled=(not confirmar or a_enviar.empty), type="primary"):
    client = None
    if modo_real:
        from twilio.rest import Client  # solo se importa si se envía de verdad
        client = Client(CREDS["ACCOUNT_SID"], CREDS["AUTH_TOKEN"])

    barra = st.progress(0, text="Enviando...")
    resultados = []
    total = len(a_enviar)
    for i, (_, fila) in enumerate(a_enviar.iterrows()):
        orden = fila["Orden"]
        # Candado anti doble envío: se verifica en la base justo antes de enviar
        estado = consultar("SELECT estado FROM ordenes WHERE orden=?", (orden,))
        if estado.empty or estado.iloc[0, 0] != "pendiente":
            resultados.append({"Orden": orden, "Nombre": fila["Nombre"],
                               "Teléfono": fila["Teléfono"], "Resultado": "⏭️ Ya enviada", "SID": ""})
        else:
            try:
                sid = enviar_uno(client, fila)
                ejecutar("UPDATE ordenes SET estado='enviada', sid=?, enviado_en=? "
                         "WHERE orden=? AND estado='pendiente'",
                         (sid, datetime.now().isoformat(timespec="seconds"), orden))
                resultados.append({"Orden": orden, "Nombre": fila["Nombre"],
                                   "Teléfono": fila["Teléfono"], "Resultado": "✅ Enviado", "SID": sid})
            except Exception as e:
                resultados.append({"Orden": orden, "Nombre": fila["Nombre"],
                                   "Teléfono": fila["Teléfono"], "Resultado": f"❌ {e}", "SID": ""})
        barra.progress((i + 1) / total, text=f"Enviando... {i + 1}/{total}")

    ss.resultados = pd.DataFrame(resultados)
    nuevo_lote()   # limpia marcas y confirmación
    st.rerun()
