# Analizador de rivales (versión web local)

Interfaz en el navegador: pegas el link de YouTube, indicas minuto, duración, equipo y color de camiseta,
y al terminar el análisis conversas con un chat sobre el rival.

## Puesta en marcha (Windows, en la terminal de VS Code con tu entorno activado)

1. Descomprime esta carpeta donde quieras (por ejemplo `Analisador de Rivales\web`).
2. Instala las dependencias:
   ```
   pip install -r requirements.txt
   ```
3. Crea tu archivo de configuración: copia `.env.example` como `.env` y pega tu API key
   (la **nueva**, no la que estaba escrita en los scripts).
4. Necesitas `ffmpeg` (para cortar el tramo del video). Si no lo tienes:
   ```
   winget install Gyan.FFmpeg
   ```
   y reinicia la terminal.
5. Arranca:
   ```
   python app.py
   ```
   Se abre solo http://127.0.0.1:5000

## Cómo se usa

- **Nuevo análisis**: completa el formulario y pulsa *Analizar*. Verás el progreso en vivo.
- Cuando termina: pestaña **Chat** (preguntas), **Informe** (resumen consolidado) y
  **Frames** (las imágenes analizadas, para verificar que el modelo miró al equipo correcto).
- Los análisis quedan guardados en la carpeta `data/` y en la lista de la izquierda; el chat también.

## Si YouTube dice "Sign in to confirm you're not a bot"

YouTube a veces bloquea las descargas automaticas. Se arregla dejando que la app use tu sesion de YouTube:

1. Abre **Firefox**, inicia sesion en YouTube y reproduce cualquier video un momento.
2. En el archivo `.env` agrega la linea `COOKIES_BROWSER=firefox`
3. Cierra la app (Ctrl+C) y vuelve a ejecutar `python app.py`.

Alternativas: `COOKIES_BROWSER=chrome` o `edge` (cierra ese navegador por completo antes de analizar;
las versiones recientes de Chrome/Edge en Windows suelen fallar con esto), o exporta un `cookies.txt`
con una extension como "Get cookies.txt LOCALLY" y pon `COOKIES_FILE=ruta\al\cookies.txt`.
Conviene usar una cuenta de Google secundaria: YouTube puede limitar cuentas que descargan mucho.

## Notas

- El minuto de inicio es el **minuto del video de YouTube** (no del partido).
- El equipo se identifica solo por el color de camiseta: confirma el color mirando el video.
- Solo se analizan fotogramas sueltos, no el movimiento. Sirve para formación, estructura y tendencias,
  pero un plano de TV no muestra toda la cancha: trata las conclusiones como hipótesis a contrastar.
- Para más calidad cambia `ANALYSIS_MODEL` en `.env` a un modelo más potente (cuesta más por análisis).
- El servidor escucha solo en tu computador (127.0.0.1). No lo expongas a internet tal como está.
