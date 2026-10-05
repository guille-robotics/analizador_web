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

- **Nuevo análisis**: completa el formulario y pulsa *Analizar*. Para un **tiempo completo** pon, por ejemplo,
  minuto de inicio = el del pitazo inicial y duración = 45 (o 50 con adición). Puedes analizar hasta 120 minutos
  (se cambia con `MAX_DURATION` en `.env`).
- **Frames**: el campo vuelve siempre a *automático* (no recuerda el valor anterior). Si lo dejas vacío se analiza 1 frame cada 30 segundos de video: unos 20 para
  10 minutos y unos 90 para **45 minutos**. Si quieres más detalle fija un número (de 3 a 200), por ejemplo
  135 para 45 minutos (1 cada 20 s). La app te avisa si pones muy pocos para la duración.
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
generales, repartidos a lo largo del tramo. Así no se gasta análisis en tomas inútiles y el informe sale mejor.
En la pestaña **Frames** ves cuántas tomas se revisaron y cuántas pasaron el filtro.
Si tu transmisión casi no tiene planos generales, baja `GRASS_MIN` en `.env` (por defecto 0.40).

## Tramos largos (un tiempo completo)

Cuando hay más de 24 frames útiles, el informe no se hace de golpe: primero se **resume cada bloque de unos
12 frames** (por ejemplo, de 10 minutos de juego) y después se unifican los resúmenes en el informe final, que
incluye una sección *Evolución durante el tramo*. Así no se pierde detalle y se ven los cambios a lo largo del
tiempo. Los resúmenes por bloque se pueden ver al final de la pestaña **Informe** y van en el PDF. El chat
conoce el informe, los resúmenes y las observaciones de cada frame.

Para saber:
- La descarga de 45 minutos en 720p ocupa del orden de varios cientos de MB mientras se procesa (se borra al
  terminar) y puede tardar varios minutos según tu conexión. Se puede cancelar en cualquier momento.
- Analizar ~90 frames tarda unos minutos. Si tu cuenta de Anthropic tiene límite de velocidad bajo, la app
  reintenta sola (con espera) y puede tardar un poco más. Si algún frame falla igual, queda registrado en el
  log y el informe se hace con los demás.

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
- El equipo se identifica solo por el color de camiseta: revisa la pestaña **Frames** al terminar para confirmar que miró al equipo correcto.
- Solo se analizan fotogramas sueltos, no el movimiento. Sirve para formación, estructura y tendencias,
  pero un plano de TV no muestra toda la cancha: trata las conclusiones como hipótesis a contrastar.
- Para más calidad cambia `ANALYSIS_MODEL` en `.env` a un modelo más potente (cuesta más por análisis).
- El servidor escucha solo en tu computador (127.0.0.1). No lo expongas a internet tal como está.
