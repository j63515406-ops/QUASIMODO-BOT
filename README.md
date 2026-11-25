# Bot Quasimodo (QM · QML · BOS · barrida) para BingX + Telegram + Railway

Port a Python del indicador "Quasimodo Cripto · QML · BOS · Barrida". Escanea **todos los perpetuos USDT de BingX**
en cada cierre de vela, envía señales detalladas a Telegram y (si DRY_RUN=false) opera en BingX con SL, TP1, TP2 y break-even.

## Archivos (todos en la raíz, sin carpetas)
| Archivo | Función |
|---|---|
| main.py | Bucle principal: espera el cierre de vela, escanea, envía señales, ejecuta |
| strategy.py | Máquina de estados del QM (idéntica al Pine) |
| indicators.py | ATR, RSI, Supertrend, pivotes |
| bingx.py | Cliente REST BingX (swap v2) firmado |
| trader.py | Tamaño de posición, órdenes SL/TP1/TP2, break-even, seguimiento |
| telegram_bot.py | Envío a Telegram |
| config.py | Variables de entorno |
| requirements.txt · Procfile · railway.json | Despliegue Railway |
| .env.example · .gitignore | Plantilla de variables |

## Despliegue
1. Sube todos los archivos a un repo de GitHub (raíz).
2. Railway → New Project → Deploy from GitHub repo.
3. En *Variables* añade las de `.env.example` (mínimo: `BINGX_API_KEY`, `BINGX_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
4. Empieza con `DRY_RUN=true` (solo avisos). Para probar con dinero virtual: `BINGX_BASE_URL=https://open-api-vst.bingx.com` con claves de demo.
5. Cuando estés conforme, `DRY_RUN=false`.
6. Opcional: añade un Volume en Railway montado en `/data` y pon `STATE_FILE=/data/state.json` para conservar el estado entre reinicios.

API de BingX: crea la clave con permisos de **lectura y trading futuros** (sin retiros) y, si puedes, restringe por IP.

## Telegram
Crea el bot con @BotFather (token) y obtén tu chat id con @userinfobot.

## Diferencias respecto al Pine
- **Sin OI ni liquidaciones**: son datos de AlertaChart Pro. La confluencia máxima es 5 (Supertrend, giro RSI, divergencia RSI/CVD, flujo CVD, volumen relativo).
- **CVD estimado** con las velas (BingX no entrega CVD real por API pública).
- Sin repintado: solo velas cerradas; cada ciclo reevalúa las últimas 500 velas de cada moneda (sin estado propio, robusto a reinicios).
- Filtro HTF opcional (`USE_HTF=true`).

## Gestión de la operación
- Riesgo por trade = `RISK_PCT`% del equity (distancia entrada–SL), limitado por margen disponible × apalancamiento.
- Entrada a mercado; SL (STOP_MARKET) por toda la posición; TP1 (`TP1_PCT`%) y TP2 (resto) como TAKE_PROFIT_MARKET.
- Tras TP1, el SL pasa a break-even (`MOVE_BE`).
- Si falla el SL, la posición se cierra al instante.
- `HEDGE_MODE=true` por defecto (modo cobertura de BingX). Si tu cuenta es one-way, pon `false`.

## Aviso
No es consejo financiero. El cripto con apalancamiento puede liquidarte. Haz pruebas en papel/demo, revisa el rendimiento
y empieza con riesgo bajo. Valida los endpoints en tu cuenta (BingX modifica su API con frecuencia).
