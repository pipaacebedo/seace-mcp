"""seace-mcp: servidor MCP para el SEACE (Sistema Electronico de los Contratos Publicos del Estado), Peru.

Busca y lee procesos de seleccion, anuncios de contratacion futura, expresiones de interes, difusion de requerimientos, ordenes de compra y servicio, condiciones de contratacion, entidades, contratos y sus documentos (seace.gob.pe) desde tu harness MCP.

Conexion directa, sin navegador y sin credenciales: todo el contenido consultado es publico. El servidor nunca guarda archivos: devuelve datos limpios y URLs de descarga.

Proyecto NO oficial, sin afiliacion con el OECE ni el Estado peruano.
"""

import asyncio
import html as html_mod
import json
import os
import re
import time
import unicodedata
import uuid
from typing import Any, Optional
from urllib.parse import urlencode, quote

import httpx
from mcp.server.fastmcp import FastMCP

BASE_UI = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/"
PAGE_BUSCADOR = BASE_UI + "buscadorPublico/buscadorPublico.xhtml"
PAGE_FICHA = BASE_UI + "fichaSeleccion/fichaSeleccion.xhtml"
ALFRESCO = "https://alfprod.seace.gob.pe/alfresco"
API4 = os.getenv("SEACE_API", "https://prod4.seace.gob.pe:9000")

TIMEOUT = float(os.getenv("SEACE_TIMEOUT", "150"))
MAX_CONCURRENCY = int(os.getenv("SEACE_MAX_CONCURRENCY", "3"))
INFLIGHT_CAP = int(os.getenv("SEACE_INFLIGHT_CAP", "12"))
SESION_TTL = int(os.getenv("SEACE_SESION_TTL", "900"))
SESION_MAX = int(os.getenv("SEACE_SESION_MAX", "12"))
CACHE_API_TTL = int(os.getenv("SEACE_CACHE_API_TTL", "900"))
CACHE_API_BYTES = int(os.getenv("SEACE_CACHE_API_BYTES", str(96 * 1024 * 1024)))
TAM_PAGINA = 15
MAX_PAGINAS = 8

FORMS = {
    "procesos": "tbBuscador:idFormBuscarProceso",
    "acf": "tbBuscador:idFormbuscarACF",
    "ei": "tbBuscador:idFormbuscarexpresionInteres",
    "difreq": "tbBuscador:idFormbuscarDifusionRequerimientos",
    "ocos": "tbBuscador:idFormbuscarOCOS",
    "cco": "tbBuscador:idFormbuscarCCO",
}
PANELES = {
    "procesos": ["pnlGrdResultadosProcesos", "footerBuscador"],
    "acf": ["pnlGrdResultadosAnuncioContratacionFutura"],
    "ei": ["pnlGrdResultadosExpReconstruccion"],
    "difreq": ["pnlGrdResultadosExpReconstruccion"],
    "ocos": ["pnlGrdResultadosOCOS"],
    "cco": ["pnlGrdResultadosExpReconstruccion"],
}
CAMPOS_TAB = {
    "procesos": {
        "descripcion": "descripcionObjeto",
        "nro_seleccion": "numeroSeleccion",
        "numero_convocatoria": "numeroConvocatoria",
        "snip": "codigoSnip",
        "cui": "CUI",
    },
    "acf": {
        "descripcion": "descripcionObjeto",
        "desde_pub": "dfechaInicioPubACF_input",
        "hasta_pub": "dfechaFinPubACF_input",
        "desde_conv": "dfechaInicioAproxConvACF_input",
        "hasta_conv": "dfechaFinAproxConvACF_input",
    },
    "ei": {
        "descripcion": "idDescExpreInteres",
        "nro_requerimiento": "idNumExpreInteres",
        "desde": "dfechaInicio_input",
        "hasta": "dfechaFin_input",
    },
    "difreq": {
        "descripcion": "idDescExpreInteres",
        "nro_requerimiento": "idNumExpreInteres",
        "desde": "dfechaInicio_input",
        "hasta": "dfechaFin_input",
    },
    "ocos": {},
    "cco": {},
}
FILAS_TAB: dict[str, list] = {
    "procesos": ["nro", "entidad", "fecha_publicacion", "nomenclatura",
                 None, "objeto", "descripcion", None, None, "monto",
                 "moneda", "version", None],
    "acf": ["nro", "entidad", "fecha_publicacion", "tipo", "objeto",
            "descripcion", "condiciones", "nro_items", "plazo_dias",
            "fecha_aprox_conv"],
    "ocos": ["nro", "entidad", "tipo_orden", "numero", "objeto_descripcion",
             "fecha_ini", "fecha_fin", "monto", "ruc", "entidad_rap",
             "situacion", "plazo"],
    "cco": ["nro", "fecha", "entidad", "descripcion", "tipo", "codigo",
            "objeto", None, None],
    "ei": ["nro", "entidad", "numero", "descripcion", None, None, None],
    "difreq": ["nro", "entidad", "numero", "descripcion", None, None, None],
}
OBLIGATORIOS: dict[str, tuple] = {
    "procesos": ("anio",),
    "acf": ("objeto",),
    "ei": ("objeto",),
    "difreq": ("objeto",),
    "ocos": ("anio", "mes", "ruc"),
    "cco": ("anio",),
}
EJEMPLO_OBL = {
    "procesos": "ej anio='2026'",
    "acf": "ej objeto='Bien'",
    "ei": "ej objeto='Bien'",
    "difreq": "ej objeto='Servicio'",
    "ocos": "ej anio='2026' mes='9' ruc='20100152356'",
    "cco": "ej anio='2026'",
}

INSTRUCCIONES = (
    "Servidor SEACE (contrataciones publicas del Estado peruano). Doble fuente: buscador publico de procesos y fichas + API de contratos. Lee en 3 niveles:\n"
    "0 DESCUBRIR - buscar_procesos ('anio' OBLIGATORIO ej '2026'; acota con descripcion/tipo/objeto/entidad), buscar_acf, buscar_requerimientos y buscar_expresiones_interes ('objeto' requerido), buscar_ocos ('anio'+'mes'+'ruc' requeridos; halla el ruc con buscar_entidades), buscar_cco ('anio') y buscar_contratos (API JSON: 'anio' obligatorio; un 'texto' acota server-side). PAC: buscar_pac(entidad, anio) y detalle_pac(entidad, anio) cubren el Plan Anual de Contrataciones publico. Catalogos: catalogo_ubigeo(nivel, id) da departamentos/provincias/distritos y listar_filtros los valores exactos de los combos. Vistas agregadas: resumen_georef(anio, depa, objeto) (sin 'objeto' resumen por tipo; con 'objeto' drill-down de contratos). Cada busqueda devuelve 'ses', filas con 'index' (GLOBAL y continuo entre paginas), 'total' y 'paginas'; siguiente pagina = re-llamada con (ses, pagina) SIN cambiar los filtros (cambiarlos inicia una busqueda nueva).\n"
    "1 DETALLE - ficha_proceso(ses, index): items con CUBSO/cantidad/MYPE + cronograma + LISTA DE DOCUMENTOS con 'uuid'.\n"
    "1b PROFUNDIDAD - detalle_item(ses, index, nro_item): acciones del item; historial_proceso(ses, index): convocatorias del proceso con etapa y estado; codigos_proceso(ses, index): SNIP/CUI publicados.\n"
    "BATCH - fichas_procesos(ses, indices, profundidad='resumen'|'completa') abre VARIAS fichas de la misma busqueda en una llamada (cap 50 resumen / 8 completa) y detalles_contratos(ids, maximo=10) varios detalles de contrato: usa el batch cuando vayas a repetir la misma llamada por varios indices/ids; los fallidos van a 'faltas' con su motivo.\n"
    "2 DOCUMENTOS - url_documento(uuid) -> URL firmada del PDF; url_documento_contrato(id_documento) para contratos; url_perfil_proveedor(ruc) -> perfil OECE del proveedor.\n"
    "3 CONTRATOS (API de contratos) - detalle_contrato(id_contrato): ficha completa con items CUBSO, garantias, acciones con documentos, disputas/arbitrajes y URLs de los 2 documentos del contrato; contratos_expediente(id_expediente): todos los contratos de un proceso. El servidor NUNCA descarga ni guarda archivos.\n"
    "Politica de sesion: GLOBAL y compartida entre tools, con TTL de 15 min (DESLIZANTE: cada uso renueva el contador; los 15 min corren desde la ultima actividad) y eliminacion LRU. Sin 'ses', cada llamada de busqueda abre una sesion nueva: OJO, si varias llamadas tienen EXACTAMENTE los mismos argumentos, el harness cliente puede reutilizar la misma sesion (optimizacion del cliente, no del servidor). Pasa el 'ses' SOLO para paginar, continuar o re-buscar: si envias filtros distintos con un 'ses' valido, la busqueda se re-ejecuta CONSERVANDO el mismo 'ses' (no se emite id nuevo). Los 'ses' vencidos o evictados devuelven error 'sesion expirada o no encontrada': repite la busqueda. Si la respuesta trae 'error', usa la 'pista' y repite.\n"
)

mcp = FastMCP("seace-mcp", instructions=INSTRUCCIONES, stateless_http=True)

_SEM: Optional[asyncio.Semaphore] = None
_INFLIGHT = 0

HDRS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "es-PE,es;q=0.9",
    "Origin": "https://prod2.seace.gob.pe",
    "Referer": PAGE_BUSCADOR,
}


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(MAX_CONCURRENCY)
    return _SEM


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _plain(s: Optional[str]) -> str:
    return _sin_acentos((s or "").strip()).lower()


def _limpia(s: str) -> str:
    t = re.sub(r"(?is)<script[\s\S]*?</script>", " ", s or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_mod.unescape(t)
    t = t.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------- cache

_CACHE_API: dict[str, tuple[float, bytes]] = {}
_CACHE_BYTES = 0


def _cache_get(key: str) -> Optional[str]:
    global _CACHE_BYTES
    hit = _CACHE_API.get(key)
    if hit and time.time() - hit[0] < CACHE_API_TTL:
        return hit[1].decode("utf-8", "replace")
    if hit:
        _CACHE_API.pop(key, None)
        _CACHE_BYTES -= len(hit[1])
    return None


def _cache_put(key: str, body: str) -> None:
    global _CACHE_BYTES
    raw = body.encode("utf-8")
    if len(raw) > CACHE_API_BYTES:
        return
    while _CACHE_API and _CACHE_BYTES + len(raw) > CACHE_API_BYTES:
        k = next(iter(_CACHE_API))
        _, b = _CACHE_API.pop(k)
        _CACHE_BYTES -= len(b)
    if _CACHE_BYTES + len(raw) > CACHE_API_BYTES:
        return
    _CACHE_API[key] = (time.time(), raw)
    _CACHE_BYTES += len(raw)


# -------------------------------------------------------------------- parser

_RE_SUBMIT = re.compile(
    r"PrimeFaces\.addSubmitParam\('([^']+)',\{([^}]*)\}\)\.submit\('([^']*)'\)")


def _growl(x: str) -> str:
    pares = re.findall(r'summary:"([^"]*)",detail:"([^"]*)"', x)
    if not pares:
        pares = [("", d) for d in re.findall(r'detail:"([^"]*)"', x)]
    partes = []
    for s, d in pares:
        t = _limpia(d) or _limpia(s)
        if t:
            partes.append(t)
    return "; ".join(partes)


def filas_raw(x: str) -> list[list[str]]:
    out = []
    for m in re.finditer(r'<tr data-ri="(\d+)"[^>]*>([\s\S]*?)</tr>', x):
        cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", m.group(2))
        out.append([_limpia(c) for c in cells])
    return out


def parse_total(x: str) -> tuple[int, int]:
    pl = _plain(html_mod.unescape(x))
    m = re.search(r"del total ([\d.,]+)\s*-\s*pagina: (\d+)/(\d+)", pl)
    if m:
        tot = int(re.sub(r"[.,]", "", m.group(1)) or 0)
        return tot, int(m.group(3) or 1)
    m = re.search(r"del total ([\d.,]+)", pl)
    return ((int(re.sub(r"[.,]", "", m.group(1)) or 0), 1) if m else (0, 1))


def links_ficha(x: str) -> list[dict]:
    out = []
    for m in re.finditer(
            r'<a[^>]+onclick="(PrimeFaces\.addSubmitParam[^"]+)"[^>]*>'
            r'\s*<img[^>]*grafichaSel', x):
        js = html_mod.unescape(m.group(1))
        mm = _RE_SUBMIT.search(js)
        if not mm:
            continue
        params = dict(re.findall(r"'([^']+)'\s*:\s*'([^']*)'", mm.group(2)))
        out.append({"btn": mm.group(1), "parametros": params})
    return out


def links_historial(x: str) -> list[dict]:
    out = []
    for m in re.finditer(
            r'<a[^>]+onclick="(PrimeFaces\.addSubmitParam[^"]+)"[^>]*>'
            r'\s*<img[^>]*btnHistorial', x):
        js = html_mod.unescape(m.group(1))
        mm = _RE_SUBMIT.search(js)
        if not mm:
            continue
        params = dict(re.findall(r"'([^']+)'\s*:\s*'([^']*)'", mm.group(2)))
        out.append({"btn": mm.group(1), "parametros": params})
    return out


def links_cui(x: str) -> list[dict]:
    out = []
    x = x.replace("\\'", "'")
    for m in re.finditer(
            r"frmListaCodigoCUI\.show\(\);PrimeFaces\.ab\(\{source:'([^']+)", x):
        seg = x[m.start():m.end() + 600]
        params_m = re.search(r"params:\[([^\]]*)\]", seg)
        params = dict(re.findall(r"name:'([^']+)',value:'([^']*)'",
                                 params_m.group(1) if params_m else ""))
        out.append({"btn": m.group(1), "parametros": params})
    return out


def links_requerimiento(x: str) -> list[dict]:
    out = []
    for mm in _RE_SUBMIT.finditer(html_mod.unescape(x)):
        params = dict(re.findall(r"'([^']+)'\s*:\s*'([^']*)'", mm.group(2)))
        if "nidRequerimiento" in params:
            out.append({"params": params})
    return out


def mapear_fila(tab: str, cells: list[str], i: int) -> dict:
    spec = FILAS_TAB.get(tab) or []
    fila: dict[str, Any] = {"index": i}
    for j, clave in enumerate(spec):
        if j >= len(cells) or clave is None:
            continue
        fila[clave] = cells[j]
    fila["campos"] = cells
    return fila


# -------------------------------------------------------------------- sesion

class Sesion:
    """Sesion del buscador publico: cliente + pagina + ultima busqueda."""

    def __init__(self) -> None:
        self.key = uuid.uuid4().hex[:8]
        self.ts = time.time()
        self.client = httpx.AsyncClient(timeout=TIMEOUT, headers=HDRS,
                                        follow_redirects=True)
        self.sid = ""
        self.vs = ""
        self.html = ""
        self.html_ts = 0.0
        self.tabla = ""
        self.links: list = []
        self._lk_extra: list[dict] = []
        self.rows: list[dict] = []
        self.total = 0
        self.tpag = 1
        self.tab: Optional[str] = None
        self.campos: Optional[dict] = None
        self.vista = 0
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass

    def touch(self) -> None:
        self.ts = time.time()

    def _url(self, page: str) -> str:
        return page + (";jsessionid=" + self.sid if self.sid else "")

    async def _get(self, url: str) -> httpx.Response:
        global _INFLIGHT
        async with _sem():
            while _INFLIGHT >= INFLIGHT_CAP:
                await asyncio.sleep(0.2)
            _INFLIGHT += 1
            try:
                return await self.client.get(url)
            finally:
                _INFLIGHT -= 1

    async def post(self, url: str, body: str, *, navegacion: bool = False,
                   referer: str = PAGE_BUSCADOR) -> httpx.Response:
        global _INFLIGHT
        hb = dict(HDRS)
        hb["Referer"] = referer
        hb["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if not navegacion:
            hb["Faces-Request"] = "partial/ajax"
        async with _sem():
            while _INFLIGHT >= INFLIGHT_CAP:
                await asyncio.sleep(0.2)
            _INFLIGHT += 1
            try:
                return await self.client.post(url, content=body.encode("utf-8"),
                                              follow_redirects=not navegacion,
                                              headers=hb)
            finally:
                _INFLIGHT -= 1

    async def abrir(self, forzar: bool = False) -> str:
        if not forzar and self.html and time.time() - self.html_ts < 60:
            return self.html
        r = await self._get(PAGE_BUSCADOR)
        if r.status_code != 200:
            raise RuntimeError(f"buscador no disponible (HTTP {r.status_code})")
        x = r.text
        self.html = x
        self.html_ts = time.time()
        m = re.search(r'action="[^"]*;jsessionid=([^"?]+)', x)
        self.sid = m.group(1) if m else ""
        m = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', x)
        self.vs = m.group(1) if m else ""
        if not self.vs:
            raise RuntimeError("buscador sin ViewState; el sitio puede haber cambiado")
        return x

    def seg_form(self, form: str) -> str:
        i0 = self.html.find('<form id="%s"' % form)
        if i0 < 0:
            return ""
        i1 = self.html.find("</form>", i0)
        return self.html[i0:i1]

    def campos_form(self, form: str) -> list[tuple[str, str]]:
        seg = html_mod.unescape(self.seg_form(form))
        if not seg:
            return []
        out: list[tuple[str, str]] = []
        vistos: set[str] = set()
        for m in re.finditer(r'<(?:input|select|textarea)[^>]*?name="([^"]+)"', seg):
            name = m.group(1)
            if not name.startswith("tbBuscador:") or name in vistos:
                continue
            vistos.add(name)
            fin = name.rsplit(":", 1)[-1]
            if fin.endswith("_focus") or fin.endswith("_s") \
                    or fin.endswith("_collapsed") or fin.startswith("btn"):
                continue
            val = ""
            mm = re.search(r'value="([^"]*)"', m.group(0))
            if mm and 'type="hidden"' in m.group(0):
                val = mm.group(1)
            out.append((name, val))
        return out

    def combos(self, form: str) -> dict[str, tuple[str, dict[str, str]]]:
        seg = html_mod.unescape(self.seg_form(form))
        if not seg:
            return {}
        res: dict[str, tuple[str, dict[str, str]]] = {}
        pat = (r'<select[^>]*name="(' + re.escape(form) +
               r':[^"]+_input)"[^>]*>([\s\S]*?)</select>')
        for m in re.finditer(pat, seg):
            nombre = m.group(1)
            vals = dict(re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>',
                                   m.group(2)))
            labels = [_plain(v) for v in vals.values() if _plain(v)]
            clave = None
            if any(p in ("seace 2", "seace 3") for p in labels):
                clave = "version"
            elif sum(1 for p in labels if re.fullmatch(r"(19|20)\d{2}", p)) >= 2 \
                    and len(labels) >= 2 \
                    and not any(p == "bien" for p in labels):
                clave = "anio"
            elif len(labels) <= 6 and any(p == "bien" for p in labels) \
                    and any(p in ("obra", "servicio", "consultoria de obra")
                            for p in labels):
                clave = "objeto"
            elif any(p.startswith("amazonas") for p in labels):
                clave = "departamento"
            elif any(p.startswith("adjudicacion abreviada")
                     for p in labels) \
                    or any(p.startswith("licitacion publica") for p in labels):
                clave = "tipo_seleccion"
            elif any(p.startswith("acuerdo marco") for p in labels):
                clave = "modalidad"
            elif any(p.startswith("contratos estandarizados")
                     for p in labels) \
                    or any(p.startswith("asistencia tecnica")
                           for p in labels):
                clave = "tipo_proceso"
            elif any("encargo" in p for p in labels):
                clave = "encargo"
            elif any(p.startswith("enero") for p in labels):
                clave = "mes"
            if not clave:
                continue
            if clave in res and len(res[clave][1]) >= len(vals):
                continue
            res[clave] = (nombre, vals)
        return res

    def campo_para(self, form: str, *sufijos: str) -> str:
        for name, _v in self.campos_form(form):
            fin = name.rsplit(":", 1)[-1]
            for suf in sufijos:
                if fin == suf:
                    return name
        return ""

    def _setear(self, data: list, nombre: str, valor: str) -> None:
        for i, (n, _v) in enumerate(data):
            if n == nombre:
                data[i] = (nombre, valor)
                return
        data.append((nombre, valor))

    def aplicar_combo(self, data: list, form: str, clave: str, valor: str) -> None:
        combos = self.combos(form)
        if clave not in combos:
            alias = {"tipo": ["tipo_seleccion", "tipo_proceso"]}
            for cand in alias.get(clave, []):
                if cand in combos:
                    clave = cand
                    break
        nombre, vals = combos.get(clave, (None, None))
        if nombre is None:
            raise RuntimeError(f"el filtro {clave!r} no aplica en este buscador; usa listar_filtros")
        eleccion = None
        if valor in vals:
            eleccion = valor
        else:
            pl = _plain(valor)
            for val, lab in vals.items():
                if _plain(lab) == pl or (pl and pl in _plain(lab)):
                    eleccion = val
                    break
        if eleccion is None:
            opciones = sorted({lab for lab in vals.values() if _plain(lab)})
            raise RuntimeError(f"valor {valor!r} no es valido para {clave!r}; ejemplos: {opciones[:8]}")
        self._setear(data, nombre, eleccion)

    async def ajax(self, data: list) -> str:
        r = await self.post(self._url(PAGE_BUSCADOR), urlencode(data))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} en peticion parcial")
        x = r.text
        m = re.search(r'<update id="j_id1:javax\.faces\.ViewState:[^"]*">'
                      r'<!\[CDATA\[([^\]]+)\]', x)
        if m:
            self.vs = m.group(1)
        if "validationFailed" in x:
            raise RuntimeError(_growl(x) or "validacion rechazada")
        self.touch()
        return x

    def tabla_de_xml(self, x: str) -> str:
        ids = re.findall(r'id="(tbBuscador:[A-Za-z0-9:]+?)_paginator_bottom"', x)
        if ids:
            return ids[-1]
        ids = re.findall(r'id="(tbBuscador:[A-Za-z0-9:]+?)_data"', x)
        return ids[-1] if ids else self.tabla

    async def paginar(self, tabla_id: str, first: int) -> tuple[list[list[str]], str]:
        form = ":".join(tabla_id.split(":")[:2]) if tabla_id else ""
        data = [
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", tabla_id),
            ("javax.faces.partial.event", "page"),
            ("javax.faces.partial.execute", tabla_id),
            ("javax.faces.partial.render", tabla_id),
            (tabla_id + "_pagination", "true"),
            (tabla_id + "_first", str(first)),
            (tabla_id + "_rows", str(TAM_PAGINA)),
            (tabla_id + "_encodeFeature", "true"),
            ("javax.faces.ViewState", self.vs),
        ]
        if form:
            data.append((form, form))
        x = await self.ajax(data)
        return filas_raw(x), x

    def _guardar_links(self, pos0: int, links: list[dict]) -> None:
        while len(self.links) < pos0 + len(links):
            self.links.append(None)
        for j, lk in enumerate(links):
            self.links[pos0 + j] = lk
        self.vista = pos0 // TAM_PAGINA

    async def buscar_tab(self, tab: str, campos: dict, pagina: int,
                         paginas: int) -> tuple[list[dict], list[str]]:
        async with self.lock:
            return await self._buscar_tab_impl(tab, campos, pagina, paginas)

    async def _hacer_busqueda(self, tab: str, form: str, filtros: dict,
                              campos: dict) -> tuple[str, list[dict]]:
        """POST de busqueda (nueva). Devuelve lk_pag desde la respuesta."""
        data = [
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", form + ":btnBuscarSel"),
            ("javax.faces.partial.execute", form),
            ("javax.faces.partial.render",
             " ".join(form + ":" + p for p in PANELES[tab]) +
             " frmMesajes:gPrincipal"),
            ("submit", "S"),
            (form + ":btnBuscarSel", ""),
            (form, form),
            ("javax.faces.ViewState", self.vs),
        ]
        for name, val in self.campos_form(form):
            data.append((name, val))

        faltan = [clave for clave in OBLIGATORIOS.get(tab, ())
                  if not _plain((campos.get(clave) or "").strip())]
        for clave in OBLIGATORIOS.get(tab, ()):
            v = _plain((campos.get(clave) or "").strip())
            if clave not in ("anio", "mes", "objeto", "tipo",
                             "tipo_proceso", "modalidad",
                             "departamento", "version", "encargo"):
                continue
            self.aplicar_combo(data, form, clave, v)
        if faltan:
            falta = " y ".join([f"'{c}'" for c in faltan])
            verbo = "son obligatorios" if len(faltan) > 1 else "es obligatorio"
            raise RuntimeError(f"{falta} {verbo} en {tab} ({EJEMPLO_OBL[tab]})")

        self._aplicar_entidad(data, tab, form, campos)
        self._aplicar_campos(data, tab, form, campos)

        r = await self.post(self._url(PAGE_BUSCADOR), urlencode(data))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} en busqueda {tab}")
        x = r.text
        m = re.search(r'<update id="j_id1:javax\.faces\.ViewState:[^"]*">'
                      r'<!\[CDATA\[([^\]]+)\]', x)
        if m:
            self.vs = m.group(1)
        if "validationFailed" in x:
            raise RuntimeError(_growl(x) or "validacion rechazada")

        self.tabla = self.tabla_de_xml(x)
        self.total, self.tpag = parse_total(x)
        self.campos = dict(filtros)
        self.tab = tab
        self.vista = 0
        if tab == "procesos":
            self.links = links_ficha(x)
        if tab in ("ei", "difreq", "cco"):
            return x, links_requerimiento(x)
        return x, []

    async def _buscar_tab_impl(self, tab: str, campos: dict, pagina: int,
                               paginas: int) -> tuple[list[dict], list[str]]:
        errores: list[str] = []
        form = FORMS[tab]
        filtros = {k: str(v).strip() for k, v in campos.items()
                   if k not in ("pagina", "paginas", "ses")
                   and str(v or "").strip()}
        continuar = (pagina > 1 and self.tab == tab and self.campos is not None
                     and (not filtros
                          or filtros.items() <= self.campos.items()))
        intento_salva = False
        obtenidas: list[list[str]] = []
        lk_pag: list[dict] = []
        while True:
            try:
                await self.abrir()
                if continuar and not filtros:
                    filtros = dict(self.campos or {})
                if continuar:
                    restantes = paginas
                    first = ( pagina - 1) * TAM_PAGINA
                else:
                    x0, lp0 = await self._hacer_busqueda(tab, form, filtros,
                                                         campos)
                    lk_pag = lp0
                    crudos = filas_raw(x0)
                    if crudos and pagina == 1:
                        obtenidas.extend(crudos)
                        restantes = paginas - 1
                        first = TAM_PAGINA
                    else:
                        restantes = paginas
                        first = (pagina - 1) * TAM_PAGINA
                for _k in range(max(0, restantes)):
                    bloque, xpag = await self.paginar(self.tabla, first)
                    if not bloque:
                        break
                    obtenidas.extend(bloque)
                    if tab == "procesos":
                        self._guardar_links(first, links_ficha(xpag))
                    if tab in ("ei", "difreq", "cco"):
                        lk_pag.extend(links_requerimiento(xpag))
                    first += TAM_PAGINA
                break
            except RuntimeError:
                if continuar and not intento_salva and self.campos:
                    # la vista del servidor pudo expirar a mitad de la
                    # cadena: re-ejecutar la busqueda congelada una vez
                    intento_salva = True
                    continuar = False
                    filtros = dict(self.campos or {})
                    obtenidas = []
                    lk_pag = []
                    continue
                raise
        base = (pagina - 1) * TAM_PAGINA
        filas = [mapear_fila(tab, f, base + i) for i, f in enumerate(obtenidas)]
        self.rows = filas
        if tab in ("ei", "difreq", "cco"):
            for i, fila in enumerate(filas):
                if i < len(lk_pag):
                    fila["id_interno"] = lk_pag[i]["params"].get(
                        "nidRequerimiento")
        self._lk_extra = []
        self.touch()
        return filas, errores

    def _aplicar_entidad(self, data: list, tab: str, form: str, campos: dict) -> None:
        ruc = (campos.get("ruc") or "").strip()
        entidad = _limpia((campos.get("entidad") or "").strip())
        if ruc.isdigit():
            n = (self.campo_para(form, "hddNumeroRuc", "rucOCOS")
                 or self.campo_para(form, "txtRucEntidad"))
            if n:
                self._setear(data, n, ruc)
        if entidad:
            n = self.campo_para(form, "nombreEntidad", "txtNombreEntidad")
            if n:
                self._setear(data, n, entidad)
        sigla = _limpia((campos.get("sigla") or "").strip())
        if sigla:
            n = self.campo_para(form, "txtsigla", "siglasEntidad")
            if n:
                self._setear(data, n, sigla)

    def _aplicar_campos(self, data: list, tab: str, form: str, campos: dict) -> None:
        mapa = CAMPOS_TAB[tab]
        for clave, valor in campos.items():
            vv = str(valor or "").strip()
            kk = _plain(clave)
            if not vv or kk in ("anio", "mes", "objeto", "entidad", "ruc",
                                "sigla", "pagina", "paginas", "ses",
                                "sumilla_chars", "tipo", "modalidad",
                                "departamento", "version", "encargo"):
                continue
            nombre = ""
            suf = mapa.get(kk)
            if suf:
                nombre = self.campo_para(form, suf)
            if not nombre:
                cand = [(n, v) for (n, v) in self.campos_form(form)
                        if kk in _plain(n.rsplit(":", 1)[-1]).replace("_input", "")]
                if not cand:
                    continue
                nombre = cand[0][0]
            self._setear(data, nombre, vv)
        for clave in ("tipo", "modalidad", "departamento", "version", "encargo"):
            v = str(campos.get(clave) or "").strip()
            if not v:
                continue
            self.aplicar_combo(data, form, clave, v)

    async def ficha(self, index: int) -> tuple[str, str]:
        async with self.lock:
            return await self._ficha_impl(index)

    async def _ficha_impl(self, index: int) -> tuple[str, str]:
        self._chequeo_procesos()
        if index < 0:
            raise RuntimeError("index no puede ser negativo")
        if self.total and index >= self.total:
            raise RuntimeError(f"index {index} fuera de rango; el total de la busqueda es {self.total}")
        pos0, xpag = await self._resync(index)
        return await self._abrir_ficha(index, pos0, xpag)

    async def _abrir_ficha(self, index: int, pos0: int,
                           xpag: str) -> tuple[str, str]:
        nuevos = links_ficha(xpag)
        j = index - pos0
        if j >= len(nuevos):
            raise RuntimeError(f"index {index} no esta en la pagina {index // TAM_PAGINA + 1} (filas recibidas: {len(nuevos)})")
        lk = nuevos[j]
        form = FORMS["procesos"]
        data = [(form, form), ("javax.faces.ViewState", self.vs),
                (lk["btn"], lk["btn"])]
        for k, v in lk["parametros"].items():
            data.append((k, v))
        ref = self._url(PAGE_BUSCADOR)
        r = await self.post(self._url(PAGE_BUSCADOR), urlencode(data),
                            navegacion=True, referer=ref)
        loc = (r.headers.get("location") or "").replace(
            "http://prod2.seace.gob.pe", "https://prod2.seace.gob.pe")
        if not loc:
            raise RuntimeError("el servidor no navego a la ficha; la sesion pudo expirar: re-corre buscar_procesos")
        url_ficha = loc if loc.startswith("http") else BASE_UI + loc.lstrip("/")
        url_ficha = re.sub(r";jsessionid=[^?]*", "", url_ficha)
        r2 = await self._get(url_ficha)
        if r2.status_code != 200:
            raise RuntimeError(f"la ficha no abrio (HTTP {r2.status_code})")
        if "Nomenclatura" not in r2.text:
            raise RuntimeError("la ficha llego vacia; re-corre buscar_procesos y usa el nuevo 'ses'")
        self.touch()
        return r2.text, url_ficha

    def _chequeo_procesos(self) -> None:
        if self.tab != "procesos" or not self.links:
            if self.tab == "procesos" and self.campos is not None:
                raise RuntimeError("la busqueda de esta sesion devolvio 0 filas; re-correr buscar_procesos con otros filtros")
            raise RuntimeError("no hay filas de procesos en esta sesion; corre buscar_procesos y usa el 'ses' devuelto")

    async def _resync(self, index: int) -> tuple[int, str]:
        """Lleva la tabla a la pagina del index y devuelve (pos0, xpag)."""
        pag = index // TAM_PAGINA
        pos0 = pag * TAM_PAGINA
        bloque, xpag = await self.paginar(self.tabla, pos0)
        if not bloque:
            raise RuntimeError(f"no se pudo cargar la pagina {pag + 1} de la sesion (pudo expirar); re-corre buscar_procesos")
        self.vista = pag
        return pos0, xpag

    async def fichas_batch(self, indices: list[int],
                           profundidad: str) -> tuple[list, list]:
        """Abre varias fichas (o filas) agrupando por pagina: 1 paginar por
        pagina distinta en lugar de 1 por ficha."""
        async with self.lock:
            self._chequeo_procesos()
            total = self.total or 0
            fichas: list[dict] = []
            faltas: list[dict] = []
            por_pag: dict[int, list[int]] = {}
            for i in indices:
                if i < 0 or (total and i >= total):
                    faltas.append({"index": i, "motivo": f"fuera de rango (total {total})"})
                    continue
                por_pag.setdefault(i // TAM_PAGINA, []).append(i)
            for pag in sorted(por_pag):
                pos0 = pag * TAM_PAGINA
                pedidos = sorted(por_pag[pag])
                bloque, xpag = await self.paginar(self.tabla, pos0)
                if not bloque:
                    faltas.extend({"index": i, "motivo": "pagina sin filas (sesion pudo expirar)"} for i in pedidos)
                    continue
                self.vista = pag
                for i in pedidos:
                    j = i - pos0
                    try:
                        if profundidad == "resumen":
                            if j < len(bloque):
                                fichas.append(mapear_fila(self.tab, bloque[j], i))
                            else:
                                faltas.append({"index": i, "motivo": "la pagina devolvio menos filas"})
                        else:
                            x, url = await self._abrir_ficha(i, pos0, xpag)
                            fichas.append({"index": i, "url_ficha": url, **parse_ficha(x)})
                    except RuntimeError as e:
                        faltas.append({"index": i, "motivo": str(e)})
            return fichas, faltas

    async def historial(self, index: int) -> str:
        self._chequeo_procesos()
        async with self.lock:
            pos0, xpag = await self._resync(int(index))
            lk = links_historial(xpag)
            j = int(index) - pos0
            if j >= len(lk):
                raise RuntimeError("esta fila no tiene historial de contrataciones en el buscador")
            lk0 = lk[j]
            form = FORMS["procesos"]
            data = [(form, form), ("javax.faces.ViewState", self.vs),
                    (lk0["btn"], lk0["btn"])]
            for k, v in lk0["parametros"].items():
                data.append((k, v))
            r = await self.post(self._url(PAGE_BUSCADOR), urlencode(data),
                                navegacion=True)
            loc = r.headers.get("location", "")
            if not loc:
                raise RuntimeError("el servidor no navego al historial; re-corre buscar_procesos")
            url = loc if loc.startswith("http") else BASE_UI + loc.lstrip("/")
            url = re.sub(r";jsessionid=[^?]*", "", url)
            r2 = await self._get(url)
            if r2.status_code != 200 or "Historial" not in r2.text:
                raise RuntimeError("la pagina de historial no abrio; re-corre buscar_procesos")
            self.touch()
            return r2.text, url

    async def codigos(self, index: int) -> list[list[str]]:
        self._chequeo_procesos()
        async with self.lock:
            pos0, xpag = await self._resync(int(index))
            lk = links_cui(xpag)
            j = int(index) - pos0
            if j >= len(lk):
                raise RuntimeError("esta fila no expone codigos SNIP/CUI")
            lk0 = lk[j]
            form = FORMS["procesos"]
            data = [
                ("javax.faces.partial.ajax", "true"),
                ("javax.faces.source", lk0["btn"]),
                ("javax.faces.partial.execute", lk0["btn"]),
                ("javax.faces.partial.render",
                 form + ":dataTableCodCUI"),
                ("frmListaCodigoCUI", ""),
            ]
            for k, v in lk0["parametros"].items():
                data.append((k, v))
            data.append((form, form))
            data.append(("javax.faces.ViewState", self.vs))
            x = await self.ajax(data)
            self.touch()
            return filas_raw(x)

    async def detalle_item(self, index: int, nro_item: int) -> str:
        async with self.lock:
            x, url = await self._ficha_impl(int(index))
            plain = html_mod.unescape(x)
            vsf = re.search(
                r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', plain)
            if not vsf:
                raise RuntimeError("la ficha llego sin ViewState; re-corre buscar_procesos")
            i0 = plain.find('id="tbFicha:idGridLstItems"')
            if i0 < 0:
                raise RuntimeError("esta ficha no tiene grilla de items")
            i_end = plain.find("tabItemDet", i0)
            seg = plain[i0:i_end if i_end > i0 else len(plain)]
            titulos = [m for m in re.finditer(
                r'<span[^>]*font-weight:\s*bold[^>]*>\s*(\d{1,3})\s*-\s*',
                seg)]
            k = int(nro_item) - 1
            if k < 0 or k >= len(titulos):
                raise RuntimeError(f"nro_item {nro_item} fuera de rango (la ficha muestra {len(titulos)} items)")
            inicio = titulos[k].start()
            fin = (titulos[k + 1].start() if k + 1 < len(titulos)
                   else len(seg))
            trozo = seg[inicio:fin]
            mbtn = re.search(r'id="(tbFicha:idGridLstItems:\d+:btnAcciones)"',
                             trozo)
            mp = re.search(
                r"addSubmitParam\('([^']+)',\{([\s\S]*?idItem[\s\S]*?)\}\)",
                trozo)
            if not mbtn or not mp:
                raise RuntimeError("esta ficha no expone acciones de item")
            params = dict(re.findall(r"'([^']+)'\s*:\s*'([^']*)'",
                                     mp.group(2)))
            data = [("tbFicha:idFormFichaSeleccion",
                     "tbFicha:idFormFichaSeleccion"),
                    ("javax.faces.ViewState", vsf.group(1)),
                    (mbtn.group(1), mbtn.group(1))]
            for kk, v in params.items():
                data.append((kk, v))
            r = await self.post(url, urlencode(data), navegacion=True,
                                referer=url)
            loc = r.headers.get("location", "")
            rx = r.text
            url_fin = url
            if loc:
                url_fin = (loc if loc.startswith("http")
                           else BASE_UI + loc.lstrip("/"))
                r2 = await self._get(url_fin)
                rx = r2.text
            if "acciones realizadas" not in _plain(rx):
                raise RuntimeError("el detalle del item no llego; reintentar")
            self.touch()
            return rx, url_fin


def parse_item_detalle(x: str) -> dict:
    plain = html_mod.unescape(x)

    def pares(seg: str) -> list:
        out = []
        for m in re.finditer(
                r'<td[^>]*>(?:<span[^>]*>)?([^<]{2,64}?)(?:</span>)?\s*'
                r'</td>\s*<td[^>]*>([\s\S]*?)</td>', seg):
            lab = _limpia(m.group(1))
            if lab:
                out.append((lab, _limpia(m.group(2))))
        return out

    i_d = plain.find("Datos del item")
    if i_d < 0:
        i_d = plain.find("Datos del \u00edtem")
    fin_d = plain.find("Listado de acciones", i_d) if i_d > 0 else -1
    cab: dict[str, Any] = {}
    for lab, val in pares(plain[:i_d if i_d > 0 else len(plain)]):
        cl = _clave_ficha(lab)
        if cl and val and cl not in cab:
            cab[cl] = val
    item: dict[str, Any] = {}
    if i_d > 0 and fin_d > i_d:
        for lab, val in pares(plain[i_d:fin_d]):
            cl = _clave_ficha(lab)
            if cl and val and cl not in item:
                item[cl] = val
    acciones: list[dict] = []
    i_a = plain.find("frmListaAccionItem")
    if i_a > 0:
        for m in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>", plain[i_a:i_a + 40000]):
            cells = [_limpia(c) for c in re.findall(
                r"<td[^>]*>([\s\S]*?)</td>", m.group(1))]
            if len(cells) >= 4 and re.match(r"^\d+$", cells[0] or ""):
                acciones.append({"nro": cells[0], "situacion": cells[1],
                                 "fecha_publicacion": cells[2],
                                 "motivo": cells[3]})
    return {"cabecera": cab, "item": item, "acciones": acciones}


# --------------------------------------------------------------------- PAC

PAC_PAGE = "https://prod2.seace.gob.pe/pac3-publico/pages/buscadorPACpublico.xhtml"

HDRS_PAC = {
    "User-Agent": HDRS["User-Agent"],
    "Accept-Language": "es-PE,es;q=0.9",
    "Origin": "https://prod2.seace.gob.pe",
    "Referer": PAC_PAGE,
}


class PacCliente:
    """Sesion del buscador publico del PAC."""

    def __init__(self) -> None:
        self.key = uuid.uuid4().hex[:8]
        self.client = httpx.AsyncClient(timeout=TIMEOUT, headers=HDRS_PAC,
                                        follow_redirects=True)
        self.sid = ""
        self.vs = ""
        self.entidad = ""
        self.anio = ""
        self.filas: list[dict] = []
        self.ts = time.time()
        self.rk = "0"
        self.lock = asyncio.Lock()

    def touch(self) -> None:
        self.ts = time.time()

    async def close(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass

    def _url(self) -> str:
        return PAC_PAGE + (";jsessionid=" + self.sid if self.sid else "")

    async def _ab(self, data: list) -> str:
        hb = dict(HDRS_PAC)
        hb["Faces-Request"] = "partial/ajax"
        hb["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        r = await self.client.post(self._url(), content=urlencode(data).encode(),
                                   headers=hb)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} en PAC")
        m = re.search(r'<update id="j_id1:javax\.faces\.ViewState:[^"]*">'
                      r'<!\[CDATA\[([^\]]+)\]', r.text)
        if m:
            self.vs = m.group(1)
        return r.text

    async def abrir(self) -> None:
        r = await self.client.get(PAC_PAGE)
        x = r.text
        m = re.search(r'action="[^"]*;jsessionid=([^"?]+)', x)
        self.sid = m.group(1) if m else ""
        m = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', x)
        self.vs = m.group(1) if m else ""
        if not self.vs:
            raise RuntimeError("PAC sin ViewState; el sitio puede haber cambiado")

    async def _elegir_entidad(self, entidad: str) -> None:
        x = await self._ab([
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", "frmBusqEntidad:j_idt128"),
            ("javax.faces.partial.execute", "frmBusqEntidad"),
            ("javax.faces.partial.render", "frmBusqEntidad:tblInstituciones"),
            ("frmBusqEntidad:j_idt128", ""),
            ("frmBusqEntidad", "frmBusqEntidad"),
            ("javax.faces.ViewState", self.vs),
            ("frmBusqEntidad:txt_noment", _limpia(entidad)),
        ])
        rows = filas_raw(x)
        if not rows:
            raise RuntimeError(f"la entidad {entidad!r} no aparece en el catalogo del PAC (probablemente no tiene PAC publicado); prueba otra entidad")
        mbtn = re.search(r'id="(frmBusqEntidad:tblInstituciones:\d+:j_idt\d+)"', x)
        if not mbtn:
            raise RuntimeError("el dialogo de entidades no expone el boton de seleccion")
        await self._ab([
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", mbtn.group(1)),
            ("javax.faces.partial.execute",
             "frmBusqEntidad:tblInstituciones"),
            ("javax.faces.partial.render", "frmBusqPlanesAnuales:panelgrid"),
            (mbtn.group(1), mbtn.group(1)),
            ("frmBusqEntidad:tblInstituciones_instantSelectedRowKey", "0"),
            ("javax.faces.ViewState", self.vs),
        ])

    async def buscar(self, entidad: str, anio: str) -> dict:
        async with self.lock:
            await self.abrir()
            await self._elegir_entidad(entidad)
            x = await self._ab([
                ("javax.faces.partial.ajax", "true"),
                ("javax.faces.source", "frmBusqPlanesAnuales:j_idt55"),
                ("javax.faces.partial.execute", "@all"),
                ("javax.faces.partial.render", "formTable:tbl_pac"),
                ("frmBusqPlanesAnuales:j_idt55", ""),
                ("frmBusqPlanesAnuales", "frmBusqPlanesAnuales"),
                ("javax.faces.ViewState", self.vs),
                ("frmBusqPlanesAnuales:cb_search_input", str(anio).strip()),
                ("frmBusqPlanesAnuales:customRadio", "3"),
            ])
            self.entidad, self.anio = _limpia(entidad), str(anio).strip()
            filas = self._parsear_filas_pac(x)
            self.filas = filas
            return {"total": len(filas), "filas": filas}

    def _parsear_filas_pac(self, x: str) -> list[dict]:
        out = []
        for m in re.finditer(r'<tr data-ri="\d+"[^>]*>([\s\S]*?)</tr>',
                             html_mod.unescape(x)):
            cells = [_limpia(c) for c in re.findall(
                r"<td[^>]*>([\s\S]*?)</td>", m.group(1))]
            if len(cells) < 6:
                continue
            out.append({"item": cells[0], "entidad": cells[1],
                        "ubigeo": cells[2], "ultima_version": cells[3],
                        "cantidad_procesos": cells[4],
                        "valor_proceso_soles": cells[5]})
        return out

    async def paginar(self, first: int) -> list[dict]:
        async with self.lock:
            x = await self._ab([
                ("javax.faces.partial.ajax", "true"),
                ("javax.faces.source", "formTable:tbl_pac"),
                ("javax.faces.partial.event", "page"),
                ("javax.faces.partial.execute", "formTable:tbl_pac"),
                ("javax.faces.partial.render", "formTable:tbl_pac"),
                ("formTable:tbl_pac_pagination", "true"),
                ("formTable:tbl_pac_first", str(first)),
                ("formTable:tbl_pac_rows", str(TAM_PAGINA)),
                ("formTable:tbl_pac_encodeFeature", "true"),
                ("formTable", "formTable"),
                ("javax.faces.ViewState", self.vs),
            ])
            return self._parsear_filas_pac(x)

    async def detalle(self, index: int = 0) -> dict:
        async with self.lock:
            if not self.filas:
                raise RuntimeError("corre buscar_pac primero")
            if index < 0 or index >= len(self.filas):
                raise RuntimeError(f"index {index} fuera de rango (hay {len(self.filas)} filas)")
            btn = re.search(r'id="(formTable:tbl_pac:%d:j_idt\d+)"' % index, html_mod.unescape(await self._vista_tbl()))
            if not btn:
                raise RuntimeError("la fila no expone el boton de procesos programados")
            hb = dict(HDRS_PAC)
            hb["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            r = await self.client.post(
                self._url(),
                content=urlencode([
                    ("formTable", "formTable"),
                    (btn.group(1), btn.group(1)),
                    ("javax.faces.ViewState", self.vs),
                ]).encode(), headers=hb, follow_redirects=False)
            loc = r.headers.get("location", "")
            if not loc:
                raise RuntimeError("el servidor no navego al listado de procesos programados")
            u = loc.replace("http://", "https://")
            r2 = await self.client.get(u)
            self.touch()
            return self._parsear_detalle(r2.text), u

    async def _vista_tbl(self) -> str:
        """re-render del tbl_pac para localizar el boton de fila (utiles
        ids inestables entre renders)"""
        return await self._ab([
            ("javax.faces.partial.ajax", "true"),
            ("javax.faces.source", "formTable:tbl_pac"),
            ("javax.faces.partial.event", "page"),
            ("javax.faces.partial.execute", "formTable:tbl_pac"),
            ("javax.faces.partial.render", "formTable:tbl_pac"),
            ("formTable:tbl_pac_pagination", "true"),
            ("formTable:tbl_pac_first", "0"),
            ("formTable:tbl_pac_rows", str(TAM_PAGINA)),
            ("formTable:tbl_pac_encodeFeature", "true"),
            ("formTable", "formTable"),
            ("javax.faces.ViewState", self.vs),
        ])

    def _parsear_detalle(self, x: str) -> dict:
        plain = html_mod.unescape(x)
        out: list[dict] = []
        for m in re.finditer(r'<tr data-ri="\d+"[^>]*>([\s\S]*?)</tr>', plain):
            cells = [_limpia(c) for c in re.findall(
                r"<td[^>]*>([\s\S]*?)</td>", m.group(1))]
            if len(cells) < 7:
                continue
            out.append({"nro": cells[0], "entidad": cells[1],
                        "objeto_descripcion": cells[2],
                        "tipo_compra": cells[3],
                        "nro_convocatoria": cells[4],
                        "mes_programado": cells[5],
                        "fuente_financiamiento": cells[6]})
        return {"procesos": out}


_PACS: dict[tuple[str, str], PacCliente] = {}


async def _pac(entidad: str, anio: str) -> PacCliente:
    now = time.time()
    for k in list(_PACS):
        p = _PACS[k]
        if now - getattr(p, "ts", now) > SESION_TTL:
            vieja = _PACS.pop(k)
            asyncio.ensure_future(vieja.close())
    key = (_plain(entidad), _plain(anio))
    p = _PACS.get(key)
    if p is not None:
        p.ts = time.time()
        return p
    p = PacCliente()
    p.ts = time.time()
    _PACS[key] = p
    while len(_PACS) > 6:
        vieja = _PACS.pop(next(iter(_PACS)))
        asyncio.ensure_future(vieja.close())
    return p


# ------------------------------------------------------------------ sesiones

_SESIONES: dict[str, Sesion] = {}


async def _cerrar(s: Sesion) -> None:
    await s.close()


def _purgar() -> None:
    ahora = time.time()
    for k in list(_SESIONES):
        if ahora - _SESIONES[k].ts > SESION_TTL:
            s = _SESIONES.pop(k)
            asyncio.ensure_future(_cerrar(s))


def _registrar(s: Sesion) -> None:
    _SESIONES.pop(s.key, None)
    _SESIONES[s.key] = s
    while len(_SESIONES) > SESION_MAX:
        vieja = _SESIONES.pop(next(iter(_SESIONES)))
        asyncio.ensure_future(_cerrar(vieja))


def _het(ses: Optional[str]) -> str:
    return (ses or "").strip()


def _ses_viva(ses: Optional[str], tab: Optional[str] = None) -> bool:
    key = _het(ses)
    return bool(key) and key in _SESIONES


async def obtener_sesion(ses: Optional[str]) -> Sesion:
    _purgar()
    key = _het(ses)
    s = _SESIONES.get(key) if key else None
    if s is not None:
        s.touch()
        _SESIONES.pop(key, None)
        _SESIONES[key] = s
        return s
    s = Sesion()
    try:
        await s.abrir()
    except Exception:
        await s.close()
        raise
    _registrar(s)
    return s


# ------------------------------------------------------------- api4 / alfresco

async def api4_json(path: str, params: Optional[dict] = None) -> Any:
    key = "API:" + path + ":" + json.dumps(params or {}, sort_keys=True)
    cached = _cache_get(key)
    if cached is not None:
        return json.loads(cached)
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(API4 + path, params=params,
                        headers={"User-Agent": HDRS["User-Agent"],
                                 "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"API JSON HTTP {r.status_code}: {r.text[:140]}")
    _cache_put(key, r.text)
    return r.json() if r.text else None


_RE_ALF = re.compile(r"\(([\s\S]*)\)\s*;")


async def url_alfresco(uid: str) -> dict:
    url = (f"{ALFRESCO}/service/osce/downloadDoc"
           f"?id={quote(uid)}&doc=c{int(time.time() * 60) % 9999999}"
           f"&guest=true")
    async with httpx.AsyncClient(timeout=20.0) as c:
        try:
            r = await c.get(url)
        except httpx.TimeoutException:
            raise RuntimeError("el servicio de documentos no respondio a tiempo; el uuid puede no existir o el servicio estar saturado")
    m = _RE_ALF.search(r.text.strip())
    if not m:
        raise RuntimeError(f"el servicio de documentos no respondio correctamente ({r.text[:80]!r})")
    try:
        j = json.loads(m.group(1))
    except Exception:
        raise RuntimeError("el servicio de documentos devolvio una respuesta invalida")
    resultado = str(j.get("result", ""))
    dl = j.get("downloadUrl") or ""
    if resultado == "200":
        full = ALFRESCO + dl
    elif resultado == "201":
        full = "https://prodcont2.seace.gob.pe/alfresco" + dl
    elif resultado == "204":
        raise RuntimeError("el documento solicitado no existe en el repositorio (result 204)")
    else:
        raise RuntimeError(f"el servicio de documentos no puede servir el documento (result={resultado or 'n/d'})")
    nombre = dl.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "documento.pdf"
    return {"url": full, "archivo": nombre}


# ---------------------------------------------------------------- ficha parse



def _clave_ficha(l: str) -> str:
    p = _plain(l).rstrip(":").strip()
    p = re.sub(r"[^a-z0-9]+", "_", p).strip("_")
    mapa = {
        "nomenclatura": "nomenclatura",
        "n_convocatoria": "nro_convocatoria",
        "no_convocatoria": "nro_convocatoria",
        "nro_convocatoria": "nro_convocatoria",
        "tipo_compra_o_seleccion": "tipo_compra",
        "normativa_aplicable": "normativa",
        "version_seace": "version_seace",
        "entidad_convocante": "entidad",
        "direccion_legal": "direccion_legal",
        "pagina_web": "pagina_web",
        "telefono_de_la_entidad": "telefono",
        "objeto_de_contratacion": "objeto",
        "vr_ve_cuantia_de_la_contratacion": "monto_contratacion",
        "monto_del_derecho_de_participacion": "monto_participacion",
        "fecha_y_hora_publicacion": "fecha_publicacion",
        "fecha_y_hora_de_publicacion": "fecha_publicacion",
        "codigo_cui": "cui",
        "codigo_unico_de_inversion": "cui",
        "descripcion_del_objeto": "descripcion_objeto",
        "nro_item": "nro_item",
        "codigo_cubso": "cubso",
        "descripcion_del_cubso": "descripcion_cubso",
        "cantidad": "cantidad",
        "reserva_para_mype": "reserva_mype",
        "paquete": "paquete",
        "denominacion_del_bien_o_servicio_comun": "denominacion_comun",
        "unidad_medida": "unidad_medida",
        "estado": "estado",
        "causal": "causal",
    }
    return mapa.get(p) or p[:36]


_CLAVES_ITEM = {
    "codigo_cubso": "cubso",
    "descripcion_del_cubso": "descripcion_cubso",
    "cantidad": "cantidad",
    "reserva_para_mype": "reserva_mype",
    "paquete": "paquete",
    "denominacion_del_bien_o_servicio_comun": "denominacion_comun",
    "vr_ve_cuantia_de_la_contratacion": "monto_contratacion",
    "moneda": "moneda",
    "estado": "estado",
    "postor": "postor",
    "mype": "mype",
    "ley_de_promocion_de_la_selva": "selva",
    "cantidad_adjudicada": "cantidad_adjudicada",
    "monto_adjudicado": "monto_adjudicado",
}


def parse_items_grid(plain: str) -> list[dict]:
    """Items del DataGrid idGridLstItems (vinen en el HTML inicial)."""
    i0 = plain.find('id="tbFicha:idGridLstItems"')
    if i0 < 0:
        return []
    i_end = plain.find("tabItemDet", i0)
    if i_end < 0:
        i_end = plain.find("dtCronograma_data", i0)
    if i_end < 0:
        i_end = plain.find("dtDocumentos_data", i0)
    if i_end < 0:
        i_end = min(len(plain), i0 + 100000)
    seg = plain[i0:i_end]
    titulos = [m for m in re.finditer(
        r'<span[^>]*font-weight:\s*bold[^>]*>\s*(\d{1,3})\s*-\s*([^<]{2,200})',
        seg)]
    if not titulos:
        return []
    items: list[dict] = []
    for k, m in enumerate(titulos):
        fin = titulos[k + 1].start() if k + 1 < len(titulos) else len(seg)
        trozo = seg[m.start():fin]
        it: dict[str, Any] = {"nro_item": m.group(1),
                              "item": _limpia(m.group(2))}
        for lm in re.finditer(
                r'<td>\s*<span[^>]*>([^<]{2,60}?)</span>\s*</td>\s*'
                r'<td[^>]*>([\s\S]*?)</td>', trozo):
            lab = _limpia(lm.group(1)).rstrip(":")
            val = _limpia(lm.group(2))
            if not lab or not val:
                continue
            cl = re.sub(r"[^a-z0-9]+", "_", _plain(lab)).strip("_")
            cl = _CLAVES_ITEM.get(cl) or cl[:32]
            if cl not in it:
                it[cl] = val
        # participantes (adjudicacion) de la tabla dtParticipantes del item
        ip = trozo.find("dtParticipantes")
        if ip > 0:
            finp = trozo.find("</table>", trozo.find("</table>", ip) + 8)
            pseg = trozo[ip:finp if finp > ip else len(trozo)]
            parts = []
            for rw in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>", pseg):
                cells = [_limpia(c) for c in re.findall(
                    r"<td[^>]*>([\s\S]*?)</td>", rw.group(1))]
                cells = [c for c in cells if c]
                if len(cells) >= 6:
                    parts.append({"postor": cells[0], "mype": cells[1],
                                  "selva": cells[2], "bonificacion": cells[3],
                                  "cantidad_adjudicada": cells[4],
                                  "monto_adjudicado": cells[5]})
            if parts:
                it["participantes"] = parts
        if it.get("item"):
            items.append(it)
    return items


def parse_ficha(x: str) -> dict:
    plain = html_mod.unescape(x)
    i_it = plain.find("tabItemDet")
    i_cr = plain.find("dtCronograma_data")
    i_do = plain.find("dtDocumentos_data")
    fin_cab = min([q for q in (i_it, i_cr, i_do) if q > 0]
                  or [len(plain)])

    def pares(seg: str) -> list:
        out = []
        for m in re.finditer(
                r'<td[^>]*>(?:<span[^>]*>)?([^<]{2,64}?)</span>\s*</td>\s*'
                r'<td[^>]*>([\s\S]*?)</td>', seg):
            lab = _limpia(m.group(1))
            if lab:
                out.append((lab, _limpia(m.group(2))))
        return out

    base: dict[str, Any] = {}
    for lab, val in pares(plain[:fin_cab]):
        cl = _clave_ficha(lab)
        if cl and val and cl not in base:
            base[cl] = val

    items = parse_items_grid(plain)
    if not items and i_it > 0:
        fin_i = min([q for q in (i_cr if i_cr > i_it else 0,
                                 i_do if i_do > i_it else 0,
                                 len(plain))])
        it = None
        for lab, val in pares(plain[i_it:fin_i]):
            cl = _clave_ficha(lab)
            if cl == "nro_item":
                it = {"nro_item": val}
                items.append(it)
                continue
            if it is not None and cl and cl not in it:
                it[cl] = val
        items = [it for it in items
                 if any(v for k2, v in it.items() if k2 != "nro_item")]

    cron: list[dict] = []
    if i_cr > 0:
        fin_c = i_do if (i_do > i_cr) else min(i_cr + 12000, len(plain))
        for m in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>", plain[i_cr:fin_c]):
            cells = [_limpia(c) for c in
                     re.findall(r"<td[^>]*>([\s\S]*?)</td>", m.group(1))]
            if len(cells) >= 3 and (
                    re.fullmatch(r"\d{2}/\d{2}/\d{4}( .+)?", cells[1] or "")
                    or re.fullmatch(r"\d{2}/\d{2}/\d{4}( .+)?", cells[2] or "")):
                cron.append({"actividad": cells[0], "inicio": cells[1],
                             "fin": cells[2]})

    docs: list[dict] = []
    if i_do > 0:
        for m in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>", plain[i_do:i_do + 40000]):
            celdas = re.findall(r"<td[^>]*>([\s\S]*?)</td>", m.group(1))
            vals = [_limpia(c) for c in celdas]
            if len(vals) >= 5 and re.match(r"^\d+$", vals[0] or ""):
                doc: dict[str, Any] = {"nro": vals[0], "etapa": vals[1],
                                       "documento": vals[2]}
                c3 = celdas[3] if len(celdas) > 3 else ""
                mx = re.search(r"descargaDocGeneral\('([^']+)',\s*'[^']*',\s*'([^']*)'",
                               html_mod.unescape(c3))
                if mx:
                    doc["uuid"] = mx.group(1)
                    doc["archivo"] = mx.group(2)
                kb = re.search(r"\((\d+) KB\)", c3)
                if kb:
                    doc["tamano_kb"] = int(kb.group(1))
                doc["fecha"] = vals[4] if len(vals) > 4 else ""
                docs.append(doc)
    return {"cabecera": base, "items": items, "cronograma": cron,
            "documentos": docs}


def _cfg(s: Sesion, tab: str, pagina: int, paginas: int, nfilas: int) -> dict:
    out = {
        "ok": True,
        "ses": s.key,
        "tab": tab,
        "total": s.total,
        "paginas": s.tpag,
        "pagina": pagina,
        "filas": nfilas,
        "tamano_pagina": TAM_PAGINA,
    }
    if nfilas and (pagina - 1) * TAM_PAGINA + nfilas < s.total:
        out["siguiente_pagina"] = pagina + paginas
    return out


# ---------------------------------------------------------------------- tools

@mcp.tool()
async def listar_filtros(categoria: str = "todos",
                         ses: Optional[str] = None) -> dict:
    """    Valores exactos de los combos del buscador SEACE y catalogos de la API JSON de contratos.

    Args:
        categoria: 'todos' (resumen por buscador), 'anio', 'mes', 'objeto', 'tipo_seleccion', 'modalidad', 'departamento', 'version', 'encargo' (combos del buscador JSF) o 'tipos_contrato' (catalogo de la API con 'codSeleccion' para buscar_contratos).
        ses: sesion opcional de una busqueda previa (reutiliza la pagina cacheada; si se omite se abre una temporal). La sesion es GLOBAL y compartida entre tools.

    Devuelve:
        {'ok': True, 'combos': {categoria: {buscador: {total, ejemplos (primeros 12): [...]}}}} o, para 'tipos_contrato', {'ok': True, 'codigos': [{codSeleccion, descSeleccion}]}. Nota: el buscador CCO solo ofrece anios recientes (2024-2026, dato del sitio).
"""
    try:
        cat = _plain(categoria) or "todos"
        s = await obtener_sesion(ses)
        if cat == "tipos_contrato":
            j = await api4_json("/api/bus/tiposeleccion/2026")
            if isinstance(j, list):
                j = j
            else:
                j = (j or {}).get("data") or []
            return {"ok": True, "categoria": cat, "codigos": j}
        if cat == "todos":
            res: dict[str, Any] = {}
            for tab, form in FORMS.items():
                combos = s.combos(form)
                for k, (_n, vals) in combos.items():
                    nombres = sorted({v for v in vals.values()
                                      if _plain(v)
                                      and "seleccione" not in _plain(v)})
                    res.setdefault(k, {})[tab] = {
                        "total": len(nombres),
                        "ejemplos": nombres[:12],
                    }
            return {"ok": True, "categoria": "todos", "combos": res,
                    "nota": "usa listar_filtros(categoria=X) para la lista completa de una categoria"}
        if cat in ("anio", "mes", "objeto", "tipo_seleccion", "modalidad",
                   "departamento", "version", "encargo"):
            res = {}
            for tab, form in FORMS.items():
                combos = s.combos(form).get(cat)
                if not combos:
                    continue
                _n, vals = combos
                res[tab] = sorted({v for v in vals.values()
                                   if _plain(v)
                                   and "seleccione" not in _plain(v)})
            return {"ok": True, "categoria": cat, "valores": res}
        return {"ok": False, "error": f"categoria {categoria!r} desconocida",
                "pista": "'todos', 'anio', 'mes', 'objeto', 'tipo_seleccion', 'modalidad', 'departamento', 'version', 'encargo', 'tipos_contrato'"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


ERR_PISTA = "revisa los filtros con listar_filtros; 'objeto' acepta Bien/Consultoria de Obra/Obra/Servicio; 'anio' es un numero de 4 digitos"


def _pista_valido(msg: str) -> str:
    m = re.search(r"no es valido para '(\w+)'", msg)
    clave = m.group(1) if m else "filtros"
    return f"usa listar_filtros(categoria='{clave}') para el catalogo completo de '{clave}'"


def _pista_obligatorio(msg: str) -> str:
    m = re.match(r"([\w' y]+?) (?:es|son) obligatorios? en (\w+)", msg)
    if not m:
        return ERR_PISTA
    campos, tab = m.groups()
    campos = campos.replace("'", "")
    ejemplos = []
    for c in campos.split(" y "):
        ej = EJEMPLO_OBL.get(tab, "")
        mm = re.search(re.escape(c) + r"='[^']*'", ej)
        ejemplos.append(mm.group(0) if mm else f"{c}=...")
    ej = " ".join(ejemplos)
    return f"indica {campos} ({ej}); valores validos con listar_filtros"


ERR_SESION = "sesion expirada o no encontrada; repite la busqueda para obtener un nuevo 'ses'"


def _defensa_sesion(ses: Optional[str]) -> Optional[dict]:
    if _het(ses) and not _ses_viva(ses):
        return {"ok": False, "error": ERR_SESION,
                "pista": "repite la busqueda para obtener un nuevo 'ses'; las sesiones viven 15 min desde su ultima actividad (cada uso renueva el contador)"}
    return None


async def _tool_buscar(tab: str, ses: Optional[str], pagina: int, paginas: int,
                       campos: dict) -> dict:
    guardia = _defensa_sesion(ses)
    if guardia and max(1, pagina) > 1:
        return guardia
    try:
        s = await obtener_sesion(ses)
        filas, _errs = await s.buscar_tab(tab, campos, max(1, pagina),
                                          max(1, paginas))
        if not filas and s.total == 0:
            return {"ok": True, **_cfg(s, tab, pagina, paginas, 0),
                    "nota": "sin resultados; revisa anio/objeto/ruc o usa listar_filtros",
                    "filas": []}
        return {"ok": True, **_cfg(s, tab, pagina, paginas, len(filas)),
                "filas": filas}
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        if "no es valido" in msg:
            out["pista"] = _pista_valido(msg)
        elif "obligatorio" in msg:
            out["pista"] = _pista_obligatorio(msg)
        return out


@mcp.tool()
async def buscar_procesos(anio: Optional[str] = None,
                          descripcion: Optional[str] = None,
                          entidad: Optional[str] = None,
                          tipo: Optional[str] = None,
                          objeto: Optional[str] = None,
                          modalidad: Optional[str] = None,
                          departamento: Optional[str] = None,
                          version: Optional[str] = None,
                          nro_seleccion: Optional[str] = None,
                          numero_convocatoria: Optional[str] = None,
                          snip: Optional[str] = None,
                          cui: Optional[str] = None,
                          sigla: Optional[str] = None,
                          pagina: int = 1, paginas: int = 1,
                          ses: Optional[str] = None) -> dict:
    """    Busca PROCESOS DE SELECCION en el Buscador Publico del SEACE (licitaciones, adjudicaciones abreviadas, comparaciones de precios, etc). POLITICA DE SESION: para continuar una busqueda en la pagina siguiente, re-llama con el MISMO 'ses' y la 'pagina' pedida sin repetir los filtros (o repitiendolos identicos); si envias filtros distintos inicia una busqueda nueva.

    Args:
        anio: anio de la convocatoria (OBLIGATORIO en la primera llamada; ej '2026'); sin otros filtros devuelve los procesos mas recientes de ese anio.
        descripcion: texto libre del objeto de contratacion (ej 'material electrico'); el buscador filtra server-side.
        entidad: nombre o parte del nombre de la entidad convocante.
        tipo: tipo de seleccion exacto (ej 'Licitacion Publica'); valores en listar_filtros.
        objeto: Bien / Consultoria de Obra / Obra / Servicio.
        modalidad: modalidad de seleccion (ej 'Acuerdo Marco').
        departamento: departamento de la entidad (ej 'LIMA').
        version: 'Seace 3' (se envia '3' al filtro).
        nro_seleccion: numero de seleccion (sin nomenclatura).
        numero_convocatoria: numero de convocatoria (ej '1').
        snip: codigo SNIP.
        cui: codigo unico de inversion (CUI).
        sigla: sigla de la entidad.
        pagina: numero de pagina (15 filas por pagina; 'index' es GLOBAL y continuo entre paginas; SIN 'ses' sirve la pagina N de una busqueda NUEVA).
        paginas: paginas consecutivas a traer en una llamada (1..8).
        ses: sesion de una busqueda previa para pedir 'siguiente_pagina' o re-buscar; con filtros distintos la busqueda se re-ejecuta conservando el mismo 'ses' (no se emite id nuevo).

    Devuelve:
        {'ok', 'ses', 'tab', 'tamano_pagina', 'total', 'paginas', 'pagina', 'siguiente_pagina', 'filas': [{index (global 0-based), nro, entidad, fecha_publicacion, nomenclatura, objeto, descripcion, monto ('Reservado' o '---': ambos indican monto no publicado), moneda, version, campos (fila cruda sin JS)}]}. Los 'index' se usan con ficha_proceso.
"""
    campos = _limpiar_campos(locals())
    return await _tool_buscar("procesos", ses, pagina, paginas, campos)


def _limpiar_campos(locals_d: dict) -> dict:
    excl = {"ses", "pagina", "paginas"}
    return {k: v for k, v in locals_d.items()
            if k not in excl and v not in (None, "")}


@mcp.tool()
async def buscar_acf(objeto: Optional[str] = None,
                     descripcion: Optional[str] = None,
                     tipo: Optional[str] = None,
                     entidad: Optional[str] = None,
                     desde_pub: Optional[str] = None,
                     hasta_pub: Optional[str] = None,
                     desde_conv: Optional[str] = None,
                     hasta_conv: Optional[str] = None,
                     sigla: Optional[str] = None,
                     pagina: int = 1, paginas: int = 1,
                     ses: Optional[str] = None) -> dict:
    """    Busca ANUNCIOS DE CONTRATACION FUTURA (ACF) publicados por las entidades en el SEACE.

    Args:
        objeto: tipo de objeto (OBLIGATORIO): Bien / Consultoria de Obra / Obra / Servicio.
        descripcion: texto del anuncio de contratacion futura.
        tipo: tipo de compra futura (ej 'Licitacion Publica').
        entidad: entidad convocante.
        desde_pub: fecha inicial de publicacion dd/mm/aaaa.
        hasta_pub: fecha final de publicacion dd/mm/aaaa.
        desde_conv: fecha inicial de convocatoria aproximada dd/mm/aaaa.
        hasta_conv: fecha final de convocatoria aproximada dd/mm/aaaa.
        sigla: sigla de la entidad.
        pagina: numero de pagina (15 filas por pagina; 'index' global y continuo).
        paginas: paginas consecutivas a traer en una llamada (1..8).
        ses: sesion de una busqueda previa para pedir 'siguiente_pagina' o re-buscar; con filtros distintos la busqueda se re-ejecuta conservando el mismo 'ses' (no se emite id nuevo).

    Devuelve:
        Igual que buscar_procesos; cada fila trae entidad, fecha_publicacion, tipo, objeto, descripcion, condiciones, nro_items, plazo_dias y fecha_aprox_conv.
"""
    campos = _limpiar_campos(locals())
    return await _tool_buscar("acf", ses, pagina, paginas, campos)


@mcp.tool()
async def buscar_expresiones_interes(objeto: Optional[str] = None,
                                     descripcion: Optional[str] = None,
                                     nro_requerimiento: Optional[str] = None,
                                     desde: Optional[str] = None,
                                     hasta: Optional[str] = None,
                                     entidad: Optional[str] = None,
                                     sigla: Optional[str] = None,
                                     pagina: int = 1, paginas: int = 1,
                                     ses: Optional[str] = None) -> dict:
    """    Busca EXPRESIONES DE INTERES (EI) publicadas en el SEACE.

    Args:
        objeto: tipo de objeto (OBLIGATORIO): Bien / Consultoria de Obra / Obra / Servicio.
        descripcion: texto de la expresion de interes (acota).
        nro_requerimiento: numero del requerimiento.
        desde: fecha inicial dd/mm/aaaa.
        hasta: fecha final dd/mm/aaaa.
        entidad: entidad convocante.
        sigla: sigla de la entidad.
        pagina: numero de pagina (15 filas por pagina; 'index' global y continuo).
        paginas: paginas consecutivas a traer en una llamada (1..8).
        ses: sesion de una busqueda previa para pedir 'siguiente_pagina' o re-buscar; con filtros distintos la busqueda se re-ejecuta conservando el mismo 'ses' (no se emite id nuevo).

    Devuelve:
        Filas con {index (global), nro, entidad, numero, descripcion} + 'id_interno' (nidRequerimiento). Nota: el buscador de EI no expone fecha de publicacion ni monto por fila.
"""
    campos = _limpiar_campos(locals())
    return await _tool_buscar("ei", ses, pagina, paginas, campos)


@mcp.tool()
async def buscar_requerimientos(objeto: Optional[str] = None,
                                descripcion: Optional[str] = None,
                                nro_requerimiento: Optional[str] = None,
                                desde: Optional[str] = None,
                                hasta: Optional[str] = None,
                                entidad: Optional[str] = None,
                                sigla: Optional[str] = None,
                                pagina: int = 1, paginas: int = 1,
                                ses: Optional[str] = None) -> dict:
    """    Busca en la DIFUSION DE REQUERIMIENTOS del SEACE (bienes y servicios que las entidades requeriran).

    Args:
        objeto: tipo de objeto (OBLIGATORIO): Bien / Consultoria de Obra / Obra / Servicio.
        descripcion: texto del bien o servicio requerido (acota).
        nro_requerimiento: numero del requerimiento.
        desde: fecha inicial dd/mm/aaaa.
        hasta: fecha final dd/mm/aaaa.
        entidad: entidad convocante.
        sigla: sigla de la entidad.
        pagina: numero de pagina (15 filas por pagina; 'index' global y continuo).
        paginas: paginas consecutivas a traer en una llamada (1..8).
        ses: sesion de una busqueda previa para pedir 'siguiente_pagina' o re-buscar; con filtros distintos la busqueda se re-ejecuta conservando el mismo 'ses' (no se emite id nuevo).

    Devuelve:
        Filas con {index (global), nro, entidad, numero, descripcion} + 'id_interno' (nidRequerimiento). Nota: el buscador de difusion no expone fecha de publicacion ni monto por fila.
"""
    campos = _limpiar_campos(locals())
    return await _tool_buscar("difreq", ses, pagina, paginas, campos)


@mcp.tool()
async def buscar_ocos(anio: Optional[str] = None, mes: Optional[str] = None,
                      ruc: Optional[str] = None,
                      entidad: Optional[str] = None,
                      pagina: int = 1, paginas: int = 1,
                      ses: Optional[str] = None) -> dict:
    """    Busca ORDENES DE COMPRA Y ORDENES DE SERVICIO (OC/OS) registradas en el SEACE.

    Args:
        anio: anio de la orden (OBLIGATORIO; ej '2026').
        mes: mes de la orden (OBLIGATORIO; '9' o 'setiembre').
        ruc: RUC de la entidad o del contratista (OBLIGATORIO); hallalo con buscar_entidades.
        entidad: nombre exacto de la entidad (opcional).
        pagina: numero de pagina (15 filas por pagina).
        paginas: paginas consecutivas a traer en una llamada (1..8).
        ses: sesion de una busqueda previa para pedir 'siguiente_pagina' o re-buscar; con filtros distintos la busqueda se re-ejecuta conservando el mismo 'ses' (no se emite id nuevo).

    Devuelve:
        Filas con nro, entidad, tipo_orden ('O/C' u 'O/S'), numero, objeto_descripcion, fecha_ini, fecha_fin, monto, ruc, entidad_rap, situacion y plazo.
"""
    campos = _limpiar_campos(locals())
    return await _tool_buscar("ocos", ses, pagina, paginas, campos)


@mcp.tool()
async def buscar_cco(anio: Optional[str] = None, objeto: Optional[str] = None,
                     tipo: Optional[str] = None,
                     pagina: int = 1, paginas: int = 1,
                     ses: Optional[str] = None) -> dict:
    """    Busca CONDICIONES DE CONTRATACION del SEACE (contratos estandarizados de encargos y compras corporativas).

    Args:
        anio: anio de la condicion (OBLIGATORIO; ej '2026'; valores en listar_filtros).
        objeto: tipo de objeto: Bien / Consultoria de Obra / Obra / Servicio.
        tipo: tipo de procedimiento CCO (ej 'Contratos Estandarizados').
        pagina: numero de pagina (15 filas por pagina).
        paginas: paginas consecutivas a traer en una llamada (1..8).
        ses: sesion de una busqueda previa para pedir 'siguiente_pagina' o re-buscar; con filtros distintos la busqueda se re-ejecuta conservando el mismo 'ses' (no se emite id nuevo).

    Devuelve:
        Filas con nro, fecha, entidad, descripcion, tipo, codigo, objeto e 'id_interno' (nidRequerimiento).
"""
    campos = _limpiar_campos(locals())
    return await _tool_buscar("cco", ses, pagina, paginas, campos)


@mcp.tool()
async def buscar_entidades(nombre: str = "", ruc: str = "",
                           sigla: str = "", ses: Optional[str] = None) -> dict:
    """    Busca ENTIDADES del Estado peruano en el buscador SEACE por nombre, RUC o sigla.

    Args:
        nombre: texto del nombre de la entidad (ej 'SEDAPAL').
        ruc: RUC exacto de 11 digitos.
        sigla: sigla de la entidad.
        ses: sesion previa opcional (GLOBAL y compartida entre tools; si se omite se abre una temporal).

    Devuelve:
        {'ok', 'ses', 'total', 'filas': [{index, ruc, tipo_documento, nombre}]}. Usa el 'ruc' para buscar_ocos y el nombre exacto para buscar_procesos(entidad=...).
"""
    if not (nombre or ruc or sigla):
        return {"ok": False, "error": "indica 'nombre', 'ruc' o 'sigla'",
                "pista": "ej nombre='SEDAPAL', ruc='20100152356' o sigla='SE'; el resultado te da el 'ruc' para buscar_ocos y el nombre exacto para la entidad de buscar_procesos"}
    guardia = _defensa_sesion(ses)
    if guardia:
        return guardia
    try:
        s = await obtener_sesion(ses)
        async with s.lock:
            form = FORMS["procesos"]
            await s.abrir()
            data = [
                ("javax.faces.partial.ajax", "true"),
                ("javax.faces.source", form + ":btnBuscarEntidad"),
                ("javax.faces.partial.execute",
                 " ".join([form + ":btnBuscarEntidad",
                           form + ":txtNombreEntidad",
                           form + ":txtRucEntidad", form + ":txtsigla"])),
                ("javax.faces.partial.render", form + ":pnlTblEntidades"),
                (form + ":btnBuscarEntidad", form + ":btnBuscarEntidad"),
                (form, form),
                ("javax.faces.ViewState", s.vs),
            ]
            for n, v in s.campos_form(form):
                if n == form or n.endswith("btnBuscarEntidad"):
                    continue
                fin = n.rsplit(":", 1)[-1]
                val = ""
                if fin == "txtNombreEntidad" and nombre:
                    val = nombre
                elif fin == "txtRucEntidad" and ruc:
                    val = ruc
                elif fin == "txtsigla" and sigla:
                    val = sigla
                data.append((n, val))
            x = await s.ajax(data)
        filas = []
        for i, ce in enumerate(filas_raw(x)):
            if len(ce) >= 3:
                filas.append({"index": i, "ruc": ce[1],
                              "tipo_documento": ce[2], "nombre": ce[3:] and
                              ", ".join(ce[3:]) or ""})
        return {"ok": True, "ses": s.key, "total": len(filas), "filas": filas,
                "nota": "usa el ruc y el nombre EXACTO en las demas tools"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------- fichas / docs

@mcp.tool()
async def ficha_proceso(ses: str, index: int) -> dict:
    """    FICHA DE SELECCION de un proceso del SEACE: cabecera completa, items, cronograma y documentos.

    Args:
        ses: sesion devuelta por buscar_procesos.
        index: 'index' de la fila de esa busqueda (0-based).

    Devuelve:
        {'ok', 'url_ficha', 'cabecera': mapa de pares etiqueta/valor normalizados con campos OPCIONALES (pagina_web, telefono, cui, causal, monto_del_costo... aparecen solo cuando el proceso los publica), 'items': [{nro_item, item (denominacion), cubso, cantidad, reserva_mype, paquete, monto_contratacion, estado, postor, monto_adjudicado (si adjudicado)}], 'cronograma': [{actividad, inicio, fin}], 'documentos': [{nro, etapa, documento, uuid, archivo, tamano_kb, fecha}]}. Usa url_documento(uuid) para la URL firmada del PDF. Nota: los procesos de emergencia o sin items poblados pueden traer 'items' vacio.
"""
    guardia = _defensa_sesion(ses)
    if guardia:
        return guardia
    try:
        s = await obtener_sesion(ses)
        x, url = await s.ficha(int(index))
        parse = parse_ficha(x)
        return {"ok": True, "ses": s.key, "index": int(index),
                "url_ficha": url, **parse}
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        if any(k in msg for k in ("no navego", "llego vacia",
                                  "no se pudo cargar", "no abrio")):
            out["pista"] = "la sesion pudo expirar (15 min) o hubo una operacion concurrente: re-corre buscar_procesos y usa el nuevo 'ses'"
        elif any(k in msg for k in ("fuera de rango", "no disponible",
                                    "no puede ser negativo",
                                    "no hay filas")):
            out["pista"] = "el 'index' es GLOBAL (0-based): consulta 'total' y 'paginas' en la ultima buscar_procesos y pide mas paginas con 'pagina'"
        else:
            out["pista"] = "re-corre buscar_procesos y usa el 'ses' devuelto"
        return out


@mcp.tool()
async def fichas_procesos(ses: str, indices: list[int],
                          profundidad: str = "resumen") -> dict:
    """    Lote de fichas o resumenes de procesos de la MISMA busqueda, en UNA llamada (evita repetir ficha_proceso por cada index).

    Args:
        ses: sesion devuelta por buscar_procesos.
        indices: 'index' globales (0-based) de esa busqueda (ej [0, 1, 2]; duplicados se procesan una sola vez; cap: 50 con 'resumen', 8 con 'completa').
        profundidad: 'resumen' trae la fila del buscador (entidad, nomenclatura, objeto, monto, fechas, version); 'completa' trae la ficha entera (items, cronograma, documentos) como ficha_proceso.

    Devuelve:
        {'ok', 'ses', 'profundidad', 'fichas': [...], 'faltas': [{index, motivo}], 'nota'}: los indices fallidos van a 'faltas' con su motivo y el resto se entrega; usa ficha_proceso individual si solo necesitas una.
"""
    profundidad = (profundidad or "").strip().lower()
    if profundidad not in ("resumen", "completa"):
        return {"ok": False, "error": f"profundidad {profundidad!r} invalida",
                "pista": "usa 'resumen' (filas rapidas, cap 50) o 'completa' (fichas enteras, cap 8)"}
    if not indices:
        return {"ok": False, "error": "indices vacio",
                "pista": "lista de 'index' de la busqueda (ej [0, 1, 2]); revisa 'total' en buscar_procesos"}
    indices = sorted(set(int(i) for i in indices))
    cap = 50 if profundidad == "resumen" else 8
    recortados = len(indices) - cap
    indices = indices[:cap]
    guardia = _defensa_sesion(ses)
    if guardia:
        return guardia
    try:
        s = await obtener_sesion(ses)
        fichas, faltas = await s.fichas_batch(indices, profundidad)
        out = {"ok": True, "ses": s.key, "profundidad": profundidad,
               "fichas": fichas, "faltas": faltas,
               "total": s.total, "paginas": s.tpag}
        if recortados > 0:
            out["nota"] = f"se recortaron {recortados} indices por el cap de {cap} ({profundidad}); pide el resto en otra llamada"
        return out
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        if "no hay filas" in msg or "0 filas" in msg:
            out["pista"] = "la sesion no tiene una busqueda con filas: corre buscar_procesos primero"
        else:
            out["pista"] = "re-corre buscar_procesos y usa el 'ses' devuelto"
        return out


@mcp.tool()
async def detalles_contratos(ids: list[str], maximo: int = 10) -> dict:
    """    Lote de DETALLES de contratos publicados en UNA llamada (evita repetir detalle_contrato por cada id).

    Args:
        ids: lista de 'id_contrato' numericos (ej ['2377490']; duplicados se procesan una sola vez; cap: 10 por llamada).
        maximo: tope de detalles por llamada (1..10).

    Devuelve:
        {'ok', 'detalles': [{ok, cabecera, items, acciones, documentos...}], 'faltas': [{id_contrato, error}], 'nota'}: cada elemento trae los mismos campos de detalle_contrato con su 'ok' individual; los ids no numericos van a 'faltas' y el resto se procesa; usa detalle_contrato si solo necesitas uno.
"""
    if not ids:
        return {"ok": False, "error": "ids vacio",
                "pista": "lista de 'id_contrato' (viene de buscar_contratos, contratos_expediente o resumen_georef con objeto)"}
    ids = sorted(set(str(i).strip() for i in ids if str(i).strip()))
    maximo = max(1, min(int(maximo or 10), 10))
    validos: list[str] = []
    faltas: list[dict] = []
    for i in ids:
        if i.isdigit():
            validos.append(i)
        else:
            faltas.append({"id_contrato": i,
                           "error": "id_contrato no numerico"})
    recortados = len(validos) - maximo
    validos = validos[:maximo]
    if not validos:
        out = {"ok": False, "detalles": [], "faltas": faltas}
        out["error"] = "ningun id_contrato valido en la lista"
        out["pista"] = "los 'id_contrato' son numericos (ej '2377490'); viene de buscar_contratos o detalle_contrato"
        return out
    coros = [detalle_contrato(id_contrato=i) for i in validos]
    resu = await asyncio.gather(*coros)
    detalles: list[dict] = []
    for i, r in zip(validos, resu):
        if r.get("ok"):
            detalles.append(r)
        else:
            faltas.append({"id_contrato": i, "error": r.get("error")})
    out: dict[str, Any] = {"ok": True, "detalles": detalles,
                           "faltas": faltas}
    if recortados > 0:
        out["nota"] = f"se recortaron {recortados} ids por el cap de {maximo}; pide el resto en otra llamada"
    return out


@mcp.tool()
async def url_documento(uuid: str) -> dict:
    """    Devuelve la URL firmada para descargar un DOCUMENTO de proceso del SEACE (bases integradas, integraciones, etc).

    Args:
        uuid: 'uuid' del documento listado en ficha_proceso (ej 'ac1aedfc-230d-4e50-9756-bb2fe1f1a4f7').

    Devuelve:
        {'ok', 'url', 'archivo'}: 'url' es el enlace directo del PDF, firmado al vuelo y con vigencia corta; el servidor NO descarga el archivo. Nota: ante un uuid inexistente el servicio puede no responder y el error se reporta como timeout; distinguelo del uuid malformado, que se rechaza al instante.
"""
    uid = (uuid or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", uid):
        return {"ok": False, "error": f"uuid malformado: {uuid!r}",
                "pista": "usa el 'uuid' exacto (formato 8-4-4-4-12 hexadecimal) de ficha_proceso -> documentos"}
    try:
        out = await url_alfresco(uid)
        return {"ok": True, **out}
    except RuntimeError as e:
        return {"ok": False, "error": str(e),
                "pista": "un uuid bien formado puede no existir o el servicio estar lento: verifica con ficha_proceso o reintenta"}


@mcp.tool()
async def historial_proceso(ses: str, index: int) -> dict:
    """    HISTORIAL DE CONTRATACIONES del proceso (ses, index de buscar_procesos): cada convocatoria de la contratacion con su etapa y estado.

    Args:
        ses: sesion devuelta por buscar_procesos.
        index: 'index' global de la fila de esa busqueda (0-based).

    Devuelve:
        {'ok', 'url', 'cabecera': mapa de pares (entidad, nomenclatura, nro_convocatoria, objeto, ...), 'historial': [{nro, nomenclatura, etapa, estado}]}.
"""
    guardia = _defensa_sesion(ses)
    if guardia:
        return guardia
    try:
        s = await obtener_sesion(ses)
        x, url = await s.historial(int(index))
        plain = html_mod.unescape(x)
        i_t = plain.find("dtHistorialContrata_data")
        hist: list[dict] = []
        if i_t > 0:
            fin = plain.find("frmHistorialContratacion_paginator", i_t)
            if fin < 0:
                fin = i_t + 40000
            for m in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>",
                                 plain[i_t:fin]):
                cells = [_limpia(c) for c in re.findall(
                    r"<td[^>]*>([\s\S]*?)</td>", m.group(1))]
                if len(cells) >= 5 and re.match(r"^\d+$", cells[1] or ""):
                    hist.append({"nro": cells[1], "nomenclatura": cells[2],
                                 "etapa": cells[3], "estado": cells[4]})
        cab = {}
        for m in re.finditer(
                r'<td[^>]*>(?:<span[^>]*>)?([^<]{2,64}?)</span>\s*</td>\s*'
                r'<td[^>]*>([\s\S]*?)</td>', plain[:i_t if i_t > 0 else len(plain)]):
            lab = _limpia(m.group(1))
            val = _limpia(m.group(2))
            cl = _clave_ficha(lab)
            if cl and val and cl not in cab:
                cab[cl] = val
        return {"ok": True, "ses": s.key, "index": int(index),
                "url": url, "cabecera": cab, "historial": hist}
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        if "no navego" in msg or "no abrio" in msg:
            out["pista"] = "la sesion pudo expirar (15 min): re-corre buscar_procesos y usa el nuevo 'ses'"
        else:
            out["pista"] = "verifica 'index' con buscar_procesos; el historial solo existe para procesos con convocatorias registradas"
        return out


@mcp.tool()
async def codigos_proceso(ses: str, index: int) -> dict:
    """    CODIGOS SNIP/CUI asociados a un proceso (ses, index de buscar_procesos) desde el popup 'Codigos' del buscador.

    Args:
        ses: sesion devuelta por buscar_procesos.
        index: 'index' global de la fila de esa busqueda (0-based).

    Devuelve:
        {'ok', 'ses', 'index', 'filas': [codigos...]}; 'filas' vacio cuando el proceso no publica CUI (el sitio muestra 'Sin informacion').
"""
    guardia = _defensa_sesion(ses)
    if guardia:
        return guardia
    try:
        s = await obtener_sesion(ses)
        filas = await s.codigos(int(index))
        codigos = [f[0] for f in filas if f]
        return {"ok": True, "ses": s.key, "index": int(index),
                "filas": codigos,
                "nota": "el proceso no publica CUI ('Sin informacion')" if not codigos else ""}
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        out["pista"] = "verifica 'index' con buscar_procesos; la sesion pudo expirar (15 min): re-corre buscar_procesos"
        return out


@mcp.tool()
async def detalle_item(ses: str, index: int, nro_item: int) -> dict:
    """    ACCIONES REALIZADAS POR ITEM de un proceso (ses, index de buscar_procesos, nro_item 1-based de ficha_proceso -> items).

    Args:
        ses: sesion devuelta por buscar_procesos.
        index: 'index' global de la fila de esa busqueda (0-based).
        nro_item: numero del item (1-based; el orden de 'items' en ficha_proceso).

    Devuelve:
        {'ok', 'url', 'cabecera' (mapa de pares opcionales), 'item': {nro_item, descripcion_item}, 'acciones': [{nro, situacion, fecha_publicacion, motivo}]}: el ciclo de vida completo del item (publicacion de convocatoria, adjudicado, contrato, etc).
"""
    guardia = _defensa_sesion(ses)
    if guardia:
        return guardia
    try:
        s = await obtener_sesion(ses)
        x, url = await s.detalle_item(int(index), int(nro_item))
        parse = parse_item_detalle(x)
        return {"ok": True, "ses": s.key, "index": int(index),
                "nro_item": int(nro_item), "url": url, **parse}
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        out["pista"] = "verifica 'nro_item' contra 'items' de ficha_proceso; la sesion pudo expirar: re-corre buscar_procesos"
        return out


@mcp.tool()
async def buscar_pac(entidad: str, anio: str = "2026") -> dict:
    """    Busca en los PLANES ANUALES DE CONTRATACIONES (PAC) publicos del SEACE por ENTIDAD y anio. Cada fila trae la entidad, su ubigeo, ultima version del PAC, cantidad de procesos programados y el valor agregado en Soles.

    Args:
        entidad: nombre o parte del nombre de la entidad (ej 'SEDAPAL'). Requerida: el buscador del PAC obliga a elegir institucion o ubigeo.
        anio: anio del PAC (ej '2026'; el catalogo del sitio ofrece 2019..2026).

    Devuelve:
        {'ok', 'entidad', 'anio', 'total', 'filas': [{item, entidad, ubigeo, ultima_version, cantidad_procesos, valor_proceso_soles}]}. Con detalle_pac(...) bajas el listado de procesos programados.
"""
    try:
        p = await _pac(entidad, anio)
        r = await p.buscar(entidad, anio)
        return {"ok": True, "ses": p.key, "entidad": p.entidad,
                "anio": p.anio, **r,
                "nota": "sin procesos en el PAC de esa entidad/anio" if not r["total"] else ""}
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        out["pista"] = "prueba parte del nombre de la entidad tal como se registra en el SEACE (ej 'SEDAPAL', 'MINSA', 'MUNICIPALIDAD'); si no tiene PAC publicado no aparece"
        return out


@mcp.tool()
async def detalle_pac(entidad: str, anio: str = "2026", index: int = 0) -> dict:
    """    LISTADO DE PROCESOS PROGRAMADOS del PAC de la entidad (busca en el PAC por entidad/anio y baja el detalle): cada fila trae el proceso programado con su mes y fuente de financiamiento.

    Args:
        entidad: nombre de la entidad (igual que buscar_pac).
        anio: anio del PAC.
        index: fila del resultado de buscar_pac (0-based; normalmente 0).

    Devuelve:
        {'ok', 'ses', 'procesos': [{nro, entidad, objeto_descripcion, tipo_compra, nro_convocatoria, mes_programado, fuente_financiamiento}]}: el plan anual completo de la entidad.
"""
    try:
        p = await _pac(entidad, anio)
        if not p.filas:
            await p.buscar(entidad, anio)
        parse, url = await p.detalle(index)
        return {"ok": True, "ses": p.key, "url": url, **parse}
    except RuntimeError as e:
        msg = str(e)
        out: dict[str, Any] = {"ok": False, "error": msg}
        out["pista"] = "corre buscar_pac(entidad, anio) primero; el listado solo existe si la entidad tiene PAC publicado"
        return out


# ------------------------------------------------------- API json de contratos

def _parse_contrato(d: dict) -> dict:
    return {
        "id_contrato": (d.get("idContrato") or "").strip(),
        "numero_contrato": (d.get("numeroContrato") or "").strip(),
        "descripcion": (d.get("desContrato") or "").strip(),
        "nomenclatura": (d.get("nomenclaturaProceso") or "").strip(),
        "id_expediente": (d.get("idExpediente") or "").strip(),
        "entidad": (d.get("entidad") or "").strip(),
        "ruc_entidad": (d.get("documentoEntidad") or "").strip(),
        "contratista": (d.get("contratista") or d.get("rucContratista")
                        or "").strip(),
        "ruc_contratista": (d.get("rucContra") or "").strip(),
        "consorcio": (d.get("nIndConsorcio") or "").strip(),
        "monto": (d.get("monto") or "").strip(),
        "monto_presupuesto": (d.get("montoPresupuesto") or "").strip(),
        "moneda": (d.get("moneda") or "").strip(),
        "anio": (d.get("anioContrato") or d.get("anio") or "").strip(),
        "fecha_suscripcion": (d.get("fechaSuscriContrato") or "").strip(),
        "fecha_inicio": (d.get("fechaIniContrato") or "").strip(),
        "fecha_fin": (d.get("fechaFinContrato") or "").strip(),
        "objeto": (d.get("desObjeto") or "").strip(),
        "objeto_codigo": (d.get("objeto") or "").strip(),
        "estado": (d.get("estado") or "").strip(),
        "id_documento": (d.get("idDocumentoContrato") or "").strip(),
    }


def _ruc_split(v: str) -> tuple[str, str]:
    v = (v or "").strip()
    if "-" in v:
        a, b = v.split("-", 1)
        return a.strip(), b.strip()
    return "", v


def _doc_api4(iid, tam, nom, fec) -> dict:
    iid = str(iid or "").strip()
    out: dict[str, Any] = {"id_documento": iid}
    if iid:
        out["url"] = API4 + "/api/con/documentos/descargar/" + iid
    if nom:
        out["archivo"] = str(nom).strip()
    if tam and str(tam).strip().isdigit():
        out["tamano_kb"] = round(int(str(tam).strip()) / 1024, 1)
    if fec:
        out["fecha"] = str(fec).strip()
    return out


def _parse_detalle_contrato(d: dict) -> dict:
    ent_ruc, ent_nom = _ruc_split(d.get("entidadContratante"))
    pro_ruc, pro_nom = _ruc_split(d.get("contratista"))
    pag_ruc, pag_nom = _ruc_split(d.get("destinatarioPago"))
    cab = {
        "id_contrato": str(d.get("idContrato") or "").strip(),
        "numero_contrato": (d.get("numeroContrato") or "").strip(),
        "nomenclatura_proceso": (d.get("nomenclaturaProceso") or "").strip(),
        "descripcion": (d.get("descripcionContrato") or "").strip(),
        "entidad_ruc": ent_ruc, "entidad": ent_nom,
        "entidad_convocante": (d.get("entidadConvocante") or "").strip(),
        "contratista_ruc": pro_ruc, "contratista": pro_nom,
        "destinatario_pago_ruc": pag_ruc, "destinatario_pago": pag_nom,
        "moneda": (d.get("tipoMoneda") or "").strip(),
        "monto": (d.get("montoContrato") or "").strip(),
        "monto_presupuesto": (d.get("montoPresupuesto") or "").strip(),
        "monto_ccp": (d.get("montoCCP") or "").strip(),
        "monto_prevision": (d.get("montoPrevision") or "").strip(),
        "fecha_suscripcion": (d.get("fechaSuscripcionContrato") or "").strip(),
        "fecha_inicio": (d.get("fechaInicioVigenciaContrato") or "").strip(),
        "fecha_fin": (d.get("fechaFinVigenciaContrato") or "").strip(),
        "fecha_publicacion": (d.get("fechaPublicacionContrato") or "").strip(),
        "lugar": (d.get("lugar") or "").strip(),
        "estado": (d.get("estadoSituacional") or "").strip(),
        "version": (d.get("version") or "").strip(),
        "norma": (d.get("norma") or "").strip(),
        "consorcio": (d.get("nIndConsorcio") or "").strip(),
        "contratos_menores": (d.get("indContratosMenores") or "").strip(),
    }
    items = []
    for it in d.get("listaItemsContrato") or []:
        cub, cubd = _ruc_split(it.get("cubso"))
        items.append({
            "nro_item": str(it.get("nroItem") or "").strip(),
            "cubso_codigo": cub, "cubso": cubd,
            "descripcion": (it.get("descripcionItem") or "").strip(),
            "lugar": (it.get("lugar") or "").strip(),
            "monto": str(it.get("monto") or "").strip(),
            "unidad_medida": (it.get("unidadMedida") or "").strip(),
            "cantidad": str(it.get("cantidadContratada") or "").strip(),
        })
    garantias = []
    for g in d.get("listaGarantiaContrato") or []:
        garantias.append({k: _limpia(str(v)) for k, v in g.items()
                          if v is not None})
    acciones = []
    for a in d.get("listaAccionContrato") or []:
        acc = {
            "tipo": _limpia(str(a.get("tipoAccion") or "")),
            "descripcion": _limpia(str(a.get("descripcion") or "")),
            "fecha_publicacion": _limpia(str(a.get("fechaPublicacion") or "")),
            "monto_total": _limpia(str(a.get("montoTotal") or "")),
            "moneda": _limpia(str(a.get("codMoneda") or "")),
            "tipo_incremento": _limpia(str(a.get("tipoIncremento") or "")),
        }
        did = str(a.get("idDocumento") or "").strip()
        if did:
            acc["documento"] = _doc_api4(did, None,
                                         a.get("nombreDocumento"), None)
        acciones.append(acc)
    docs = []
    for did, tam, nom, fec in (
            (d.get("idDocumento"), d.get("tamanioDocumento"),
             d.get("archivoAdjunto"), d.get("fechaDocumento")),
            (d.get("idDocumento2"), d.get("tamanioDocumento2"),
             d.get("archivoAdjunto2"), d.get("fechaDocumento2"))):
        if str(did or "").strip():
            docs.append(_doc_api4(did, tam, nom, fec))
    extras = {}
    for campo, clave in (("proyectos", "listaProyectoContrato"),
                         ("disputas", "listaResolucionDisputas"),
                         ("conciliaciones", "listaConciliaciones"),
                         ("arbitrajes", "listaArbitrajes")):
        lst = d.get(clave) or []
        if lst:
            extras[campo] = [{k: _limpia(str(v)) for k, v in e.items()
                              if v is not None} for e in lst]
    return {"cabecera": cab, "items": items, "garantias": garantias,
            "acciones": acciones, "documentos": docs, **extras}


@mcp.tool()
async def buscar_contratos(anio: str, texto: Optional[str] = None,
                           entidad: Optional[str] = None,
                           descripcion: Optional[str] = None,
                           proveedor: Optional[str] = None,
                           contrato: Optional[str] = None,
                           proyecto: Optional[str] = None,
                           expediente: Optional[str] = None,
                           nomenclatura: Optional[str] = None,
                           depa: Optional[str] = None,
                           objeto: Optional[str] = None,
                           cod_seleccion: Optional[str] = None,
                           desde: Optional[str] = None,
                           hasta: Optional[str] = None,
                           maximo: int = 100) -> dict:
    """    Busca CONTRATOS PUBLICADOS en la API de contratos del SEACE. IMPORTANTE: la API no pagina y sin filtros trae el anio completo (~50k registros); pasa SIEMPRE un filtro acotador (texto, descripcion, proveedor, contrato, nomenclatura, expediente o entidad).

    Args:
        anio: anio del contrato (OBLIGATORIO; ej '2026').
        texto: palabra libre filtrada server-side (la mas efectiva; ej 'tuberia').
        entidad: nombre o RUC de la entidad contratante.
        descripcion: texto dentro de la descripcion del contrato.
        proveedor: nombre o RUC del proveedor o consorcio.
        contrato: numero del contrato.
        proyecto: texto del proyecto de inversion.
        expediente: id del expediente (idExpediente de otro resultado).
        nomenclatura: nomenclatura del proceso (ej 'LP-SM-13-2025-SEDAPAL-1').
        depa: departamento ('0' = todos).
        objeto: '0' todos, '62' Bien, '63' Consultoria, '64' Obra, '65' Servicio.
        cod_seleccion: codigo del tipo de seleccion (listar_filtros(categoria='tipos_contrato')).
        desde: fecha inicial de suscripcion dd/mm/aaaa.
        hasta: fecha final de suscripcion dd/mm/aaaa.
        maximo: filas a devolver (1..300).

    Devuelve:
        {'ok', 'total_recibidos', 'mostrados', 'contratos': [{id_contrato, numero_contrato, descripcion, nomenclatura, id_expediente, entidad, ruc_entidad, contratista, ruc_contratista, consorcio, monto, monto_presupuesto, moneda, anio, fecha_suscripcion, fecha_inicio, fecha_fin, objeto, objeto_codigo, estado, id_documento}]}. Con 'id_documento' usa url_documento_contrato.
"""
    try:
        anio = str(anio).strip()
        if not anio.isdigit():
            return {"ok": False, "error": f"anio {anio!r} invalido",
                    "pista": "ej anio='2026'"}
        params: dict[str, str] = {
            "depa": (depa or "0").strip() or "0",
            "objeto": (objeto or "0").strip() or "0",
            "codSeleccion": (cod_seleccion or "0").strip() or "0",
            "palabra": (texto or "").strip(),
            "entidad": (entidad or "").strip(),
            "descripcion": (descripcion or "").strip(),
            "proveedor": (proveedor or "").strip(),
            "contrato": (contrato or "").strip(),
            "proyecto": (proyecto or "").strip(),
            "desde": (desde or "").strip(),
            "hasta": (hasta or "").strip(),
            "expediente": (expediente or "").strip(),
            "nomenclatura": (nomenclatura or "").strip(),
        }
        acotador = any((texto, entidad, descripcion, proveedor, contrato,
                        proyecto, expediente, nomenclatura, cod_seleccion))
        if not acotador and int(maximo) >= 1000:
            maximo = 200
        j = await api4_json(f"/api/bus/contrato/query/{anio}", params)
        if not isinstance(j, list):
            j = (j or {}).get("data") or []
        maximo = max(1, min(int(maximo or 100), 300))
        docs = [_parse_contrato(d) for d in j[:maximo]]
        out: dict[str, Any] = {
            "ok": True, "total_recibidos": len(j), "contratos": docs,
            "mostrados": len(docs),
        }
        if len(j) > maximo:
            out["nota"] = f"la API devolvio {len(j)} contratos; mostrando los primeros {maximo}. Acota con 'texto', 'proveedor', 'nomenclatura' o 'entidad'"
        if not out.get("nota"):
            out["nota"] = "sin resultados con esos filtros; revisa 'texto' y el 'anio'" if not len(j) else ""
        if not out.get("nota"):
            out.pop("nota", None)
        if not acotador and len(j) > 3000:
            out["nota"] = out.get("nota", "") + " sin filtros un anio trae ~50k registros (lento); siempre manda un filtro acotador"
        return out
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def tipos_seleccion_contratos(anio: str = "2026") -> dict:
    """    Catalogo EXACTO de tipos de seleccion de la API JSON de contratos.

    Args:
        anio: anio del catalogo (ej '2026').

    Devuelve:
        {'ok', 'tipos': [{codSeleccion, descSeleccion}]}: 'codSeleccion' es el valor de 'cod_seleccion' en buscar_contratos.
"""
    try:
        anio = str(anio).strip()
        if not anio.isdigit():
            return {"ok": False, "error": f"anio {anio!r} invalido",
                    "pista": "el catalogo de tipos de seleccion existe por anio (ej '2026'); el 'codSeleccion' devuelto alimenta 'cod_seleccion' de buscar_contratos"}
        j = await api4_json(f"/api/bus/tiposeleccion/{anio}")
        tipos = j if isinstance(j, list) else (j or {})
        out = {"ok": True, "tipos": tipos}
        if not tipos:
            out["nota"] = "sin tipos publicados para ese anio; verifica el anio con listar_filtros(categoria='anio')"
        return out
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def catalogo_ubigeo(nivel: str = "depas",
                          id: Optional[str] = None) -> dict:
    """    Catalogo geografico de la API de contratos del SEACE: departamentos, provincias y distritos (llena 'depa' en buscar_contratos y resumen_georef).

    Args:
        nivel: 'depas' (lista completa), 'provi' (provincias de un departamento) o 'distri' (distritos de una provincia).
        id: codigo del nivel padre; OBLIGATORIO con 'provi' (codigo de departamento ej '19' Pasco) y con 'distri' (codigo de provincia ej '2503').

    Devuelve:
        {'ok', 'nivel', 'catalogo': [{codigo, nombre}], 'nota'}: los codigos 'codigo' se usan como 'depa'/'id' en las otras tools.
"""
    nivel = (nivel or "").strip().lower()
    id_ = (id or "").strip()
    if nivel == "depas":
        try:
            j = await api4_json("/api/bus/contrato/depas")
            cat = [{"codigo": (e.get("codRefrencia") or "").strip(),
                    "nombre": _limpia(e.get("desUbica") or "")}
                   for e in (j or [])]
            return {"ok": True, "nivel": "depas", "catalogo": cat,
                    "nota": "'codigo' es el 'depa' de buscar_contratos y el 'id' de resumen_georef; con nivel='provi' pide las provincias de un departamento"}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
    if nivel not in ("provi", "distri"):
        return {"ok": False, "error": f"nivel {nivel!r} invalido",
                "pista": "usa 'depas', 'provi' o 'distri'"}
    if not id_.isdigit():
        return {"ok": False, "error": "id invalido",
                "pista": "'provi' necesita el codigo del departamento y 'distri' el de la provincia; obtienelos con nivel='depas'"}
    try:
        j = await api4_json(f"/api/bus/contrato/{nivel}/{id_}")
        cat = [{"codigo": (e.get("codRefrencia") or "").strip(),
                "nombre": _limpia(e.get("desUbica") or "")}
               for e in (j or [])]
        nota = ("'provi' devuelve las provincias del departamento " f"'{id_}'; con nivel='distri' pide los distritos de una provincia"
                if nivel == "provi" else
                f"'distri' devuelve los distritos de la provincia '{id_}'")
        return {"ok": True, "nivel": nivel, "catalogo": cat, "nota": nota}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def resumen_georef(anio: str, depa: str,
                         objeto: Optional[str] = None) -> dict:
    """    Resumen GEOreferenciado de contratos publicados de un departamento y anio. Sin 'objeto': cuantos contratos y monto acumulado por tipo de objeto (Bien/Consultoria/Obra/Servicio). Con 'objeto': la lista de contratos de ese departamento, anio y objeto (drill-down, mismos campos que buscar_contratos).

    Args:
        anio: anio del resumen (ej '2026').
        depa: codigo del departamento del catalogo_ubigeo (ej '19' Pasco).
        objeto: codigo del tipo de objeto del drill-down ('62' Bien, '63' Consultoria, '64' Obra, '65' Servicio).

    Devuelve:
        {'ok', 'anio', 'depa', 'resumen': [{tipo_objeto, cantidad, total_soles}], 'nota'} o, con 'objeto', {'ok', 'contratos': [...], 'total_recibidos', 'mostrados', 'nota'}: para la ficha del contrato usa detalle_contrato. El drill-down con 'objeto' no pagina y se recorta a los primeros 100 contratos ('mostrados'); para el resto usa buscar_contratos con depa+objeto.
"""
    anio = str(anio).strip()
    depa = str(depa).strip()
    if not anio.isdigit() or not depa.isdigit():
        return {"ok": False, "error": "anio y depa deben ser numericos",
                "pista": "depa sale de catalogo_ubigeo(nivel='depas')"}
    obj = (objeto or "").strip()
    try:
        if obj:
            if obj not in ("62", "63", "64", "65"):
                return {"ok": False, "error": f"objeto {obj!r} invalido",
                        "pista": "'62' Bien, '63' Consultoria, '64' Obra, '65' Servicio"}
            j = await api4_json(f"/api/bus/contrato/codDepartamento/anio/codObjeto/{depa}/{anio}/{obj}")
            lst = j if isinstance(j, list) else []
            maximo = max(1, min(len(lst), 100))
            out: dict[str, Any] = {"ok": True, "anio": anio, "depa": depa,
                                   "total_recibidos": len(lst),
                                   "contratos": [_parse_contrato(e)
                                                 for e in lst[:maximo]],
                                   "mostrados": maximo}
            if len(lst) > maximo:
                out["nota"] = f"la API devolvio {len(lst)} contratos; mostrando los primeros {maximo}"
            return out
        j = await api4_json(f"/api/bus/contrato/codDepartamento/anio/{depa}/{anio}")
        nom = {"62": "Bien", "63": "Consultoria", "64": "Obra",
               "65": "Servicio"}
        res = [{"tipo_objeto": nom.get(str(e.get("tipoObjeto") or ""),
                                       _limpia(str(e.get("tipoObjeto") or ""))),
                "cantidad": str(e.get("cantidad") or ""),
                "total_soles": str(e.get("semiTotal") or "")}
               for e in (j or [])]
        return {"ok": True, "anio": anio, "depa": depa, "resumen": res,
                "nota": "montos en soles; con 'objeto' obtienes el drill-down de contratos de ese tipo"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def detalle_contrato(id_contrato: str) -> dict:
    """    Detalle COMPLETO de un contrato publicado de la API JSON del SEACE: cabecera, items con CUBSO, garantias, acciones (penalidades/adicionalidades con documento), proyectos de inversion, disputas, conciliaciones y arbitrajes, y los documentos con su URL de descarga.

    Args:
        id_contrato: 'id_contrato' de buscar_contratos o contratos_expediente (ej '2377490').

    Devuelve:
        {'ok', 'cabecera', 'items', 'garantias', 'acciones', 'documentos': [{id_documento, url, archivo, tamano_kb, fecha}], 'proyectos'|'disputas'|'conciliaciones'|'arbitrajes' (si hay)}: las URLs son descarga directa del PDF (sin token).
"""
    idc = (id_contrato or "").strip()
    if not idc.isdigit():
        return {"ok": False, "error": "id_contrato debe ser numerico",
                "pista": "el 'id_contrato' viene de buscar_contratos o de contratos_expediente"}
    try:
        j = await api4_json("/api/bus/contrato/idContrato/" + idc)
        if not isinstance(j, dict):
            return {"ok": False, "error": "la API no devolvio el contrato " + idc}
        parse = _parse_detalle_contrato(j)
        if not (parse.get("cabecera") or {}).get("numero_contrato"):
            return {"ok": False, "error": f"el contrato {idc} no existe o no esta publicado",
                    "pista": "verifica el id con buscar_contratos"}
        parse["ok"] = True
        return parse
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def contratos_expediente(id_expediente: str) -> dict:
    """    Todos los CONTRATOS publicados de un expediente de seleccion (un proceso puede tener varios contratos).

    Args:
        id_expediente: id del expediente de seleccion ('id_expediente' de buscar_contratos; ej '1171246').

    Devuelve:
        {'ok', 'id_expediente', 'total', 'contratos': [{id_contrato, numero_contrato, ...}]}: con 'id_contrato' usa detalle_contrato.
"""
    idx = (id_expediente or "").strip()
    if not idx.isdigit():
        return {"ok": False, "error": "id_expediente debe ser numerico",
                "pista": "el 'id_expediente' viene de buscar_contratos o de ficha_proceso"}
    try:
        j = await api4_json("/api/bus/contrato/idexpediente/" + idx)
        lst = j if isinstance(j, list) else []
        out = {"ok": True, "id_expediente": idx, "total": len(lst),
               "contratos": [_parse_contrato(e) for e in lst]}
        if not lst:
            out["nota"] = "sin contratos publicados para ese expediente; verifica el id con buscar_contratos"
        return out
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def url_documento_contrato(id_documento: str) -> dict:
    """    URL de descarga del documento de un CONTRATO de la API JSON del SEACE.

    Args:
        id_documento: 'id_documento' del contrato (idDocumentoContrato; ej '153203160').

    Devuelve:
        {'ok', 'url', 'nota'}: GET directo del PDF; el servidor no lo guarda.
"""
    iid = (id_documento or "").strip()
    if not iid.isdigit():
        return {"ok": False, "error": "id_documento es obligatorio",
                "pista": "el 'id_documento' numerico del contrato (ej '153203160') viene de buscar_contratos o de los 'documentos' de detalle_contrato"}
    url = (API4 + "/api/con/documentos/descargar/" + iid)
    return {"ok": True, "url": url,
            "nota": "descarga el PDF directo con la url (sin token)"}


@mcp.tool()
async def url_perfil_proveedor(ruc: str) -> dict:
    """    URL del PERFIL del proveedor en el portal del OECE (perfilprov).

    Args:
        ruc: RUC del proveedor o consorcio.

    Devuelve:
        {'ok', 'url'}: pagina publica del perfil del proveedor.
"""
    ruc = (ruc or "").strip()
    if not ruc.isdigit():
        return {"ok": False, "error": "el ruc debe ser numerico",
                "pista": "RUC de 11 digitos del proveedor o consorcio (ej ruc='20600138899'); el 'contratista_ruc' o el 'ruc_contratista' de buscar_contratos y detalle_contrato son validos"}
    return {"ok": True,
            "url": "https://apps.oece.gob.pe/perfilprov-ui/ficha/" + ruc}


# ---------------------------------------------------------------------- main

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

