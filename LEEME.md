# Analizador de rivales (versión web local)

Interfaz en el navegador: pegas el link de YouTube, indicas minuto, duración, equipo y color de camiseta,
y al terminar el análisis conversas con un chat sobre el rival.

## Puesta en marcha en Windows (lo más fácil)

1. Descomprime esta carpeta donde quieras.
2. Haz **doble clic en `iniciar.bat`**. La primera vez crea el entorno virtual, instala las dependencias y
   te abre el Bloc de notas para pegar tu API key en el archivo `.env` (la **nueva**, no la que estaba
   escrita en los scripts). Después abre solo http://127.0.0.1:5000
3. Las siguientes veces basta con el doble clic: comprueba dependencias, actualiza `yt-dlp` (YouTube cambia
   seguido y eso evita muchos errores) y arranca.

Necesitas además `ffmpeg` (para cortar el tramo del video): `winget install Gyan.FFmpeg` y reiniciar la terminal.
`iniciar.bat` te avisa si falta ffmpeg o Node.js.

### Si prefieres la terminal
```
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe app.py
```

## Cómo se usa

- **Nuevo análisis**: completa el formulario y pulsa *Analizar*. Antes, el botón **Previsualizar** descarga
  8 segundos del video en ese minuto para que confirmes el color de camiseta.
- **Cancelar**: mientras se descarga o se analiza hay un botón rojo para cancelar. Corta de verdad la
  descarga (no queda yt-dlp corriendo de fondo).
- Cuando termina: pestaña **Chat** (preguntas), **Informe** (resumen + **Descargar PDF**) y
  **Frames** (las imágenes analizadas, para verificar que el modelo miró al equipo correcto).
- **Combinar análisis** (botón en la barra lateral): elige 2 a 8 análisis terminados *del mismo equipo*
  (varios tramos de un partido, o de varios partidos) y genera un informe único que separa lo que se repite
  de lo que cambió. Tiene su propio chat y su PDF.
- Los análisis y los chats quedan guardados en la carpeta `data/`.

## Qué hace el filtro de planos generales

De cada tramo se revisan unas 4 veces más tomas de las que pides y se descartan, **sin llamar a la API**,
las que no muestran la cancha (primeros planos, público, banco, gráficos). Solo se envían a Claude los planos
generales, repartidos a lo largo del tramo. Es más barato y el informe sale mejor. En la pestaña **Frames**
ves cuántas tomas se revisaron y cuántas pasaron el filtro.
Si tu transmisión casi no tiene planos generales, baja `GRASS_MIN` en `.env` (por defecto 0.40).

## PDF

El botón **Descargar PDF** (pestaña Informe) genera el informe con datos del partido, el texto del análisis
y, opcionalmente, los frames y la conversación del chat. Si falla con "Falta la librería reportlab",
ejecuta `iniciar.bat` otra vez (instala lo que falte).

## Si YouTube dice "Sign in to confirm you're not a bot"

YouTube a veces bloquea las descargas automáticas. Se arregla dejando que la app use tu sesión de YouTube:

1. Abre **Firefox**, inicia sesión en YouTube y reproduce cualquier video un momento.
2. En el archivo `.env` agrega la línea `COOKIES_BROWSER=firefox`
3. Cierra la app y vuelve a abrirla.

Alternativas: `COOKIES_BROWSER=chrome` o `edge` (cierra ese navegador por completo antes de analizar;
las versiones recientes de Chrome/Edge en Windows suelen fallar con esto), o exporta un `cookies.txt`
con una extensión como "Get cookies.txt LOCALLY" y pon `COOKIES_FILE=ruta\al\cookies.txt`.
Conviene usar una cuenta de Google secundaria: YouTube puede limitar cuentas que descargan mucho.

## Notas

- El minuto de inicio es el **minuto del video de YouTube** (no del partido).
- El equipo se identifica solo por el color de camiseta: confirma el color con la previsualización.
- Solo se analizan fotogramas sueltos, no el movimiento. Sirve para formación, estructura y tendencias,
  pero un plano de TV no muestra toda la cancha: trata las conclusiones como hipótesis a contrastar.
- Para más calidad cambia `ANALYSIS_MODEL` en `.env` a un modelo más potente (cuesta más por análisis).
- El servidor escucha solo en tu computador (127.0.0.1). No lo expongas a internet tal como está.
