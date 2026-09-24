import streamlit as st
import pandas as pd
import sqlite3
import re
import time
import json
from twilio.rest import Client

# =========================================================
#  App de avisos de WhatsApp para lavandería (v3)
#  Carga CSV -> SQLite -> buscar/marcar -> enviar por lotes
# =========================================================

st.set_page_config(page_title="Avisos de Órdenes", page_icon="🧺", layout="wide")

DB_PATH = "ordenes.db"
LOTE_MAX = 50          # tope de mensajes por tanda de envío
PAUSA_SEG = 1.0        # pausa entre mensajes (anti-spam / observable)

# ---------- CREDENCIALES ----------
try:
    ACCOUNT_SID   = st.secrets["ACCOUNT_SID"]
    AUTH_TOKEN    = st.secrets["AUTH_TOKEN"]
    CONTENT_SID   = st.secrets["CONTENT_SID"]
    FROM_WHATSAPP = st.secrets["FROM_WHATSAPP"]
except Exception:
    ACCOUNT_SID   = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    AUTH_TOKEN    = "tu_auth_token_aqui"
    CONTENT_SID   = "HXd7c7172da384a1776b3e69901d46f6c4"
    FROM_WHATSAPP = "whatsapp:+14155238886"

NOMBRE_NEGOCIO = "Lavandería Modelo"

# ---------- BASE DE DATOS ----------
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ordenes (
                orden     TEXT PRIMARY KEY,
                fecha     TEXT,
                nombre    TEXT,
                telefono  TEXT,
                telefono_norm TEXT,
                monto     TEXT,
                valido    INTEGER,
                estado    TEXT DEFAULT 'pendiente',   -- pendiente | enviada
                sid       TEXT DEFAULT ''
            )
        """)

def normalizar_telefono_rd(tel):
    d = re.sub(r"\D", "", str(tel))
    if len(d) == 10:
        d = "1" + d
    return "+" + d

def formatear_monto(monto):
    """Convierte '1655.00' o '1655' en '1,655.00' (coma de miles, 2 decimales).
    Si el valor no es numérico, lo devuelve tal cual sin romper el envío."""
    try:
        limpio = re.sub(r"[^\d.]", "", str(monto))   # quita comas/símbolos previos
        return f"{float(limpio):,.2f}"
    except (ValueError, TypeError):
        return str(monto)

def telefono_valido(t):
    return bool(re.fullmatch(r"\+1\d{10}", t))

def cargar_csv_a_db(df):
    """Inserta/actualiza órdenes. No pisa el estado de las ya enviadas."""
    insertadas, omitidas = 0, 0
    with get_conn() as c:
        for _, f in df.iterrows():
            orden = str(f["orden"]).strip()
            # ¿ya existe y fue enviada? -> no tocar
            row = c.execute("SELECT estado FROM ordenes WHERE orden = ?", (orden,)).fetchone()
            if row and row[0] == "enviada":
                omitidas += 1
                continue
            tel_norm = normalizar_telefono_rd(f["telefono"])
            c.execute("""
                INSERT INTO ordenes (orden, fecha, nombre, telefono, telefono_norm, monto, valido, estado, sid)
                VALUES (?,?,?,?,?,?,?, 'pendiente', '')
                ON CONFLICT(orden) DO UPDATE SET
                    fecha=excluded.fecha, nombre=excluded.nombre,
                    telefono=excluded.telefono, telefono_norm=excluded.telefono_norm,
                    monto=excluded.monto, valido=excluded.valido
            """, (orden, str(f["fecha"]), str(f["nombre"]), str(f["telefono"]),
                  tel_norm, str(f["monto"]), int(telefono_valido(tel_norm))))
            insertadas += 1
    return insertadas, omitidas

def query_ordenes(filtro_orden="", solo_pendientes=True):
    q = "SELECT orden, fecha, nombre, telefono_norm, monto, valido, estado, sid FROM ordenes WHERE 1=1"
    params = []
    if filtro_orden:
        q += " AND orden LIKE ?"
        params.append(f"%{filtro_orden}%")
    if solo_pendientes:
        q += " AND estado = 'pendiente'"
    q += " ORDER BY fecha, orden"
    with get_conn() as c:
        return pd.read_sql_query(q, c, params=params)

def marcar_enviada(orden, sid):
    with get_conn() as c:
        c.execute("UPDATE ordenes SET estado='enviada', sid=? WHERE orden=?", (sid, orden))

# ---------- INICIALIZAR ----------
init_db()

st.title("🧺 Avisos de Órdenes Listas")

# ===== SECCIÓN 1: CARGA =====
with st.expander("📥 Cargar órdenes desde CSV", expanded=False):
    archivo = st.file_uploader("Archivo CSV (orden, fecha, nombre, telefono, monto)", type=["csv"])
    if archivo is not None:
        try:
            df = pd.read_csv(archivo, dtype=str).fillna("")
            requeridas = {"orden", "fecha", "nombre", "telefono", "monto"}
            faltantes = requeridas - set(df.columns)
            if faltantes:
                st.error(f"Faltan columnas: {', '.join(faltantes)}")
            else:
                ins, omi = cargar_csv_a_db(df)
                st.success(f"Cargadas/actualizadas: {ins}. Omitidas (ya enviadas): {omi}.")
        except Exception as e:
            st.error(f"Error al leer: {e}")

# ===== SECCIÓN 2: BUSCAR Y MARCAR =====
st.subheader("🔎 Buscar y seleccionar órdenes")

col1, col2 = st.columns([3, 1])
with col1:
    filtro = st.text_input("Buscar por número de orden (parcial o completo)", "")
with col2:
    solo_pend = st.checkbox("Solo pendientes", value=True)

df_orden = query_ordenes(filtro, solo_pend)

if df_orden.empty:
    st.info("No hay órdenes que coincidan. Carga un CSV o ajusta la búsqueda.")
else:
    df_orden["Enviar"] = False
    # columnas legibles
    vista = df_orden.rename(columns={
        "orden": "Orden", "fecha": "Fecha", "nombre": "Nombre",
        "telefono_norm": "Teléfono", "monto": "Monto", "estado": "Estado"
    })
    # el usuario marca con checkboxes en la tabla
    editada = st.data_editor(
        vista[["Enviar", "Orden", "Fecha", "Nombre", "Teléfono", "Monto", "valido", "Estado"]],
        column_config={
            "Enviar": st.column_config.CheckboxColumn("Enviar", default=False),
            "valido": st.column_config.CheckboxColumn("¿Tel. válido?", disabled=True),
        },
        disabled=["Orden", "Fecha", "Nombre", "Teléfono", "Monto", "Estado"],
        hide_index=True,
        use_container_width=True,
        key="editor",
    )

    marcadas = editada[editada["Enviar"] & editada["valido"]]
    invalidas_marcadas = editada[editada["Enviar"] & ~editada["valido"]]

    n_marcadas = len(marcadas)
    st.write(f"**{n_marcadas}** órdenes marcadas con teléfono válido.")
    if len(invalidas_marcadas) > 0:
        st.warning(f"{len(invalidas_marcadas)} marcadas tienen teléfono inválido y NO se enviarán.")

    if n_marcadas > LOTE_MAX:
        st.warning(f"Marcaste {n_marcadas}. Por seguridad se enviarán en tandas de {LOTE_MAX}. "
                   f"Esta tanda enviará las primeras {LOTE_MAX}; repite para el resto.")

    # ===== SECCIÓN 3: ENVIAR =====
    confirmar = st.checkbox(f"Confirmo enviar el aviso a las {min(n_marcadas, LOTE_MAX)} órdenes seleccionadas")
    if st.button("📤 Enviar avisos", disabled=(not confirmar or n_marcadas == 0), type="primary"):
        client = Client(ACCOUNT_SID, AUTH_TOKEN)
        a_enviar = marcadas.head(LOTE_MAX)
        barra = st.progress(0, text="Enviando...")
        resultados = []
        total = len(a_enviar)

        for i, (_, fila) in enumerate(a_enviar.iterrows()):
            tel = fila["Teléfono"]
            try:
                msg = client.messages.create(
                    from_=FROM_WHATSAPP,
                    to=f"whatsapp:{tel}",
                    content_sid=CONTENT_SID,
                    content_variables=json.dumps({
                        "1": fila["Nombre"],
                        "2": NOMBRE_NEGOCIO,
                        "3": fila["Orden"],
                        "4": formatear_monto(fila["Monto"]),
                    })
                )
                marcar_enviada(fila["Orden"], msg.sid)
                resultados.append({"Orden": fila["Orden"], "Nombre": fila["Nombre"], "Estado": "✅ Enviado"})
            except Exception as e:
                resultados.append({"Orden": fila["Orden"], "Nombre": fila["Nombre"], "Estado": f"❌ {e}"})
            barra.progress((i + 1) / total, text=f"Enviando... {i+1}/{total}")
            time.sleep(PAUSA_SEG)

        barra.empty()
        res = pd.DataFrame(resultados)
        ok = (res["Estado"] == "✅ Enviado").sum()
        st.success(f"Terminado: {ok} de {total} enviados. Las enviadas quedaron marcadas y salen de la lista de pendientes.")
        st.dataframe(res, use_container_width=True)
        st.rerun()
