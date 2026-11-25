# Bot Quasimodo (QM · QML · BOS · barrida) para BingX + Telegram + Railway

Port a Python del Pine **"Quasimodo QM Pattern, QML, BOS & Liquidity Sweep [LunqFX]"** más una capa de entrada propia.
Escanea todos los perpetuos USDT de BingX en cada cierre de vela, envía señales detalladas a Telegram y, si `DRY_RUN=false`,
opera en BingX con SL, TP1, TP2 y break-even.

## Archivos (todos en la raíz, sin carpetas)
| Archivo | Función |
|---|---|
| main.py | Un escáner por temporalidad (hilos), señales, avisos de patrón, radar, aviso si la API falla |
| strategy.py | Máquina de estados del QM (igual que el Pine) + señal de entrada + filtros R:R y coste |
| backtest.py | Compara temporalidades con datos reales de BingX (sin claves) |
| indicators.py · bingx.py · trader.py · telegram_bot.py · config.py | Soporte |
| requirements.txt · Procfile · railway.json | Despliegue Railway |

## Qué hace el Pine y qué añade el bot
El Pine solo **detecta y sigue** el patrón: hombro izquierdo → cabeza que barre su liquidez → BOS; marca el QML (el hombro) y registra
si el precio vuelve al QML, llega a T1 (extremo del BOS) / T2 (proyección de la pierna) o rompe la cabeza. No da entrada.
El bot replica esa máquina de estados (mismos parámetros por defecto) y le añade:
- **Entrada**: vela de confirmación en la zona QML (`SIG_MODE=confirm`) o al tocar (`touch`).
- **Confluencias (0-5)**: Supertrend, giro RSI, divergencia en la cabeza, CVD estimado, volumen relativo. Avisa con ≥ `MIN_SCORE` (2); opera con ≥ `TRADE_MIN_SCORE` (3).
- **Filtros**: R:R a TP1 ≥ `MIN_RR`; coste (comisión+slippage) ≤ `MAX_COST_R`.
- Sin OI ni liquidaciones (eran datos de otro indicador); CVD estimado con velas.

## Mensajes de Telegram
- **Señal** (LONG/SHORT con entrada, SL, TP1, TP2, calidad A/B/C, coste en R).
- **Radar** cada `RADAR_EVERY_MIN` min por temporalidad: nº de QM esperando el QML, en juego, y los 5 más cercanos al QML.
- **Avisos de patrón** opcionales (`NOTIFY_SETUPS=bos,tap`).
- **Resultado** (T1 / T2 / cabeza rota) de las señales ya enviadas, en modo papel.
- **Alerta de fallo**: si >50 % de las monedas fallan al analizarse (API bloqueada, región, límites) lo dice en Telegram.

## Elegir temporalidad
`TIMEFRAMES=1h,4h` (por defecto) lanza un escáner por cada una. Primer backtest real (40 monedas, 60 días, fee 0.05 % + slip 0.03 %/lado): 5m y 15m salen negativos tras costes, 30m ≈ 0, 1h ≈ +0.2R y 4h ≈ +0.3R pero con muy pocas señales (no distinguibles de cero aún). Para decidir con más datos:
```
python backtest.py --tfs 1h,4h --symbols 150 --days 180 --scores 1,2,3
```
Usa los mismos parámetros que el bot, incluye comisiones y slippage (`FEE_PCT`, `SLIP_PCT`) y guarda las operaciones en `backtest_trades.csv`.
Mira **R neto ± 2·SE** (el margen no debe cruzar 0), que las dos mitades del periodo coincidan y ≥100 señales.
Coste por operación en R ≈ 2·(fee+slip) / distancia al SL (%): con 0.08 %/lado, SL de 0.5 % → 0.32R; de 1 % → 0.16R; de 2 % → 0.08R.

## Despliegue
1. Sube todos los archivos a un repo de GitHub (raíz). 2. Railway → Deploy from GitHub repo.
3. *Variables*: las de `.env.example` (mínimo `BINGX_API_KEY`, `BINGX_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
   **Las variables definidas en Railway pisan los valores por defecto del código**: borra las antiguas (`TIMEFRAME`, `MIN_LEG`, `MIN_SCORE`, `MIN_SWEEP`, `ZONE_ATR`, `MAX_WAIT`, `MAX_RUN`) o actualízalas.
4. Empieza con `DRY_RUN=true`. Demo: `BINGX_BASE_URL=https://open-api-vst.bingx.com` con claves de demo.
5. Opcional: Volume en `/data` y `STATE_FILE=/data/state.json` para no repetir avisos tras un reinicio.

API de BingX: permisos de **lectura y trading futuros** (sin retiros), y restringe por IP si puedes.

## Gestión de la operación
Riesgo = `RISK_PCT`% del equity (entrada–SL), limitado por margen × apalancamiento. Entrada a mercado; SL (STOP_MARKET) por toda la posición;
TP1 (`TP1_PCT`%) y TP2 (resto) como TAKE_PROFIT_MARKET; tras TP1 el SL pasa a break-even. Si falla el SL, se cierra al instante.

## Aviso
No es consejo financiero. El cripto con apalancamiento puede liquidarte. Prueba en papel/demo y valida los endpoints en tu cuenta.
