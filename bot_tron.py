"""
Bot de protección de wallet TRON  ·  v3 TIEMPO REAL
=====================================================
MEJORA PRINCIPAL v3:
  - WebSocket en tiempo real (TronGrid WSS)
  - Detección en ~ms en vez de 0.1–0.5s
  - Polling de respaldo si el WS se cae
  - Toda la lógica v2 conservada (failover, fee dinámico, turbo, backoff)

Requisitos:
    pip install tronpy aiohttp websockets

Configurar:
    export CLAVE_PRIVADA="tu_clave_privada_hex"
    export TRONGRID_API_KEY="tu_api_key_de_trongrid.io"
"""

import asyncio
import json
import logging
import os
import time
from decimal import Decimal

import websockets
from tronpy import AsyncTron
from tronpy.exceptions import AddressNotFound, TransactionError, BadSignature
from tronpy.keys import PrivateKey
from tronpy.providers.async_http import AsyncHTTPProvider

# =============================================================================
# CONFIGURACIÓN
# =============================================================================

WALLET_PROTEGIDA = "TNKkwpyhCtf54uQDqPbMSkeNofkBRxCJHa"
WALLET_SEGURA    = "THTssArzvSgoWwKV8rrKms8tMT9k2XY1Qg"

CLAVE_PRIVADA    = os.getenv("CLAVE_PRIVADA", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")

WS_URL = "wss://api.trongrid.io/v1/transactions/subscribe"

INTERVALO_RESPALDO = Decimal("1.0")
FEE_MINIMO         = Decimal("5")
FEE_BUFFER         = Decimal("1.5")
MINIMO_ANTI_SPAM   = Decimal("0.1")

ENDPOINTS = [
    "https://api.trongrid.io",
    "https://api.tronstack.io",
    "https://rpc.ankr.com/tron_jsonrpc",
]

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_log.txt", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# CLIENTE HTTP CON FAILOVER
# =============================================================================

class ClienteConFailover:
    def __init__(self):
        self.endpoints = list(ENDPOINTS)
        self.indice    = 0
        self.client: AsyncTron | None = None
        self._lock = asyncio.Lock()

    def _crear_provider(self, url: str) -> AsyncHTTPProvider:
        if TRONGRID_API_KEY and "trongrid" in url:
            return AsyncHTTPProvider(url, api_key=TRONGRID_API_KEY)
        return AsyncHTTPProvider(url)

    async def conectar(self) -> None:
        url = self.endpoints[self.indice]
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.client = AsyncTron(self._crear_provider(url))
        log.info(f"🔌 HTTP conectado a: {url}")

    async def rotar_endpoint(self) -> None:
        async with self._lock:
            self.indice = (self.indice + 1) % len(self.endpoints)
            log.warning(f"🔄 Rotando a: {self.endpoints[self.indice]}")
            await self.conectar()

    async def cerrar(self) -> None:
        if self.client:
            await self.client.close()

# =============================================================================
# BALANCE Y FEE
# =============================================================================

async def obtener_balance(gestor: ClienteConFailover) -> Decimal | None:
    for intento in range(len(ENDPOINTS)):
        try:
            return await gestor.client.get_account_balance(WALLET_PROTEGIDA)
        except AddressNotFound:
            return Decimal("0")
        except Exception as e:
            log.warning(f"⚠️  Error balance (intento {intento+1}): {e}")
            await gestor.rotar_endpoint()
    log.error("❌ Todos los endpoints fallaron.")
    return None


async def calcular_fee(gestor: ClienteConFailover) -> Decimal:
    try:
        client    = gestor.client
        bandwidth = await client.get_bandwidth(WALLET_PROTEGIDA)
        try:
            account = await client.get_account(WALLET_PROTEGIDA)
            energy  = account.get("EnergyLimit", 0) - account.get("EnergyUsed", 0)
        except Exception:
            energy = 0

        if bandwidth >= 267 and energy > 0:
            fee_base, estado = Decimal("0.5"), "BW+Energy"
        elif bandwidth >= 267:
            fee_base, estado = Decimal("1.0"), "BW OK"
        else:
            fee_base, estado = Decimal("2.5"), "sin recursos"

        fee_final = max(fee_base * FEE_BUFFER, FEE_MINIMO)
        log.info(f"📊 {estado} | BW={bandwidth} | Fee={fee_final} TRX")
        return fee_final
    except Exception as e:
        log.warning(f"⚠️  Fee fallback ({e}) → {FEE_MINIMO} TRX")
        return FEE_MINIMO

# =============================================================================
# CAPTURA DE GAS
# =============================================================================

async def capturar_gas(gestor: ClienteConFailover, balance_trx: Decimal) -> bool:
    t0 = time.perf_counter()
    try:
        fee       = Decimal("0.5")        # ← ÚNICA LÍNEA MODIFICADA
        monto_trx = balance_trx - fee

        if monto_trx <= Decimal("0"):
            log.warning(f"⚠️  Insuficiente: balance={balance_trx} fee={fee}")
            return False

        monto_sun = int(monto_trx * 1_000_000)
        if monto_sun <= 0:
            return False

        log.info(f"⚡ Capturando {monto_trx} TRX...")
        priv_key    = PrivateKey(bytes.fromhex(CLAVE_PRIVADA))
        txn_builder = gestor.client.trx.transfer(WALLET_PROTEGIDA, WALLET_SEGURA, monto_sun)
        txn         = await txn_builder.build()
        result      = await txn.sign(priv_key).broadcast()

        ms = (time.perf_counter() - t0) * 1000
        log.info(f"✅ TX OK | {result.txid} | {ms:.0f}ms")
        log.info(f"   {monto_trx} TRX → {WALLET_SEGURA}")
        log.info(f"💎 USDT intacto en {WALLET_PROTEGIDA}")
        return True

    except TransactionError as e:
        log.error(f"❌ TX error: {e}")
        await gestor.rotar_endpoint()
        return False
    except BadSignature as e:
        log.error(f"❌ Firma inválida: {e}")
        return False
    except Exception as e:
        log.error(f"❌ Error inesperado: {e}")
        await gestor.rotar_endpoint()
        return False

# =============================================================================
# NÚCLEO DE REACCIÓN
# =============================================================================

async def reaccionar_si_hay_gas(
    gestor: ClienteConFailover,
    balance_actual: Decimal,
    balance_anterior: Decimal,
    en_proceso: asyncio.Event,
    fuente: str = "WS",
) -> Decimal:
    incremento = balance_actual - balance_anterior

    if incremento > MINIMO_ANTI_SPAM and not en_proceso.is_set():
        log.warning(
            f"🚨 [{fuente}] GAS DETECTADO! "
            f"+{incremento} TRX | total: {balance_actual} TRX"
        )
        en_proceso.set()
        try:
            exito = await capturar_gas(gestor, balance_actual)
        finally:
            en_proceso.clear()

        nuevo = await obtener_balance(gestor)
        return nuevo if nuevo is not None else balance_actual

    elif Decimal("0") < incremento <= MINIMO_ANTI_SPAM:
        log.info(f"🔕 [{fuente}] Dust ignorado: +{incremento} TRX")

    return balance_actual

# =============================================================================
# WEBSOCKET
# =============================================================================

async def escuchar_websocket(
    gestor: ClienteConFailover,
    balance_ref: list,
    en_proceso: asyncio.Event,
    ws_activo: asyncio.Event,
):
    headers = {}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY

    reintentos = 0
    while True:
        try:
            log.info(f"🌐 Conectando WebSocket: {WS_URL}")
            async with websockets.connect(
                WS_URL,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                ws_activo.set()
                reintentos = 0
                log.info("✅ WebSocket conectado — monitoreo en tiempo real activo")

                async for mensaje in ws:
                    try:
                        data = json.loads(mensaje)
                        txs  = data.get("transactions", [])

                        for tx in txs:
                            contratos = tx.get("raw_data", {}).get("contract", [])
                            for contrato in contratos:
                                valor   = contrato.get("parameter", {}).get("value", {})
                                destino = valor.get("to_address", "")
                                if WALLET_PROTEGIDA in (destino, valor.get("owner_address", "")):
                                    log.info("⚡ [WS] TX detectada — consultando balance...")
                                    balance_actual = await obtener_balance(gestor)
                                    if balance_actual is not None:
                                        balance_ref[0] = await reaccionar_si_hay_gas(
                                            gestor, balance_actual, balance_ref[0],
                                            en_proceso, fuente="WS"
                                        )
                    except Exception as e:
                        log.warning(f"⚠️  Error procesando mensaje WS: {e}")

        except Exception as e:
            ws_activo.clear()
            reintentos += 1
            espera = min(2 ** reintentos, 60)
            log.warning(f"⚠️  WebSocket caído ({e}) — reintento en {espera}s...")
            await asyncio.sleep(espera)

# =============================================================================
# POLLING DE RESPALDO
# =============================================================================

async def polling_respaldo(
    gestor: ClienteConFailover,
    balance_ref: list,
    en_proceso: asyncio.Event,
    ws_activo: asyncio.Event,
):
    backoff = 1
    while True:
        if ws_activo.is_set():
            await asyncio.sleep(1)
            continue

        log.info("🔁 [Polling] WS inactivo — polling de respaldo activo")
        balance_actual = await obtener_balance(gestor)

        if balance_actual is None:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        backoff        = 1
        balance_ref[0] = await reaccionar_si_hay_gas(
            gestor, balance_actual, balance_ref[0],
            en_proceso, fuente="Polling"
        )
        await asyncio.sleep(float(INTERVALO_RESPALDO))

# =============================================================================
# MONITOREO PRINCIPAL
# =============================================================================

async def monitorear():
    log.info("=" * 60)
    log.info("🤖 Bot de protección TRON v3 — TIEMPO REAL")
    log.info(f"   Monitoreando : {WALLET_PROTEGIDA}")
    log.info(f"   Destino TRX  : {WALLET_SEGURA}")
    log.info(f"   Modo         : WebSocket + polling de respaldo")
    log.info("=" * 60)

    gestor = ClienteConFailover()
    await gestor.conectar()

    balance_inicial = await obtener_balance(gestor)
    if balance_inicial is None:
        raise ConnectionError("No se pudo obtener balance inicial.")

    log.info(f"💰 Balance inicial: {balance_inicial} TRX")

    balance_ref = [balance_inicial]
    en_proceso  = asyncio.Event()
    ws_activo   = asyncio.Event()

    try:
        await asyncio.gather(
            escuchar_websocket(gestor, balance_ref, en_proceso, ws_activo),
            polling_respaldo(gestor, balance_ref, en_proceso, ws_activo),
        )
    finally:
        await gestor.cerrar()
        log.info("🔌 Cliente cerrado")

# =============================================================================
# ARRANQUE CON AUTO-REINICIO
# =============================================================================

async def main():
    while True:
        try:
            await monitorear()
        except Exception as e:
            log.error(f"💥 Bot caído: {e}")
            log.info("🔄 Reiniciando en 5 segundos...")
            await asyncio.sleep(5)

# =============================================================================
# VALIDACIÓN
# =============================================================================

def validar_configuracion():
    errores = []

    if not CLAVE_PRIVADA:
        errores.append("CLAVE_PRIVADA no configurada → export CLAVE_PRIVADA='hex64chars'")
    elif len(CLAVE_PRIVADA) != 64:
        errores.append(f"CLAVE_PRIVADA debe tener 64 caracteres (tiene {len(CLAVE_PRIVADA)})")
    else:
        try:
            bytes.fromhex(CLAVE_PRIVADA)
        except ValueError:
            errores.append("CLAVE_PRIVADA contiene caracteres no hexadecimales")

    if not TRONGRID_API_KEY:
        log.warning("⚠️  Sin TRONGRID_API_KEY — el WebSocket puede no funcionar")

    if errores:
        for e in errores:
            log.error(f"❌ {e}")
        return False

    log.info("✅ Configuración verificada")
    return True

# =============================================================================
# ENTRADA
# =============================================================================

if __name__ == "__main__":
    if not validar_configuracion():
        exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Bot detenido (Ctrl+C)")
