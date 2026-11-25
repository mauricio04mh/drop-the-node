# Prototipo con Ray

Implementación de drop-the-node sobre Ray Core. Ray actúa como middleware: se encarga de la comunicación entre procesos, de dónde se ejecuta cada actor y de detectar cuándo uno muere.

## Requisitos

- Linux.
- Python 3.12 o superior.
- `ffmpeg` y `ffprobe` con el codificador `libopus` (`sudo apt install ffmpeg`). La versión forma parte de la configuración: otra versión puede producir otros bytes para el mismo archivo.

## Instalación

Desde esta carpeta (`prototype/`):

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Comprobar la instalación:

```bash
python3 -c 'import ray; print(ray.__version__)'   # debe mostrar 2.58.0
```

## Pruebas

```bash
python3 -m pytest           # resumen
python3 -m pytest -v        # una línea por prueba
```

`tests/test_ray_runtime.py` comprueba los comportamientos de Ray de los que depende el diseño. Por ejemplo, que un actor conserva su estado entre llamadas, que llamar a un actor muerto lanza `ActorDiedError` y que un actor muerto no se reinicia por defecto. 

## Uso

```bash
python3 -m dtn upload cancion.flac --title "Canción" --artist "Ana" --license CC-BY-4.0
python3 -m dtn search mar
python3 -m dtn list
python3 -m dtn withdraw TRACK_ID --reason "sin licencia"
```

Todas las órdenes admiten `--json`, y el código de salida indica cómo terminó la operación (ver `docs/contract.md` § 6). Hasta que exista el almacenamiento sobre Ray, las órdenes responden que no está disponible; las pruebas usan `FakeCluster`.

## Estructura

```text
prototype/
├── README.md
├── requirements.txt   versiones fijas de Ray y pytest
├── pytest.ini         configuración de las pruebas
├── dtn/               código del prototipo
│   ├── errors.py      clases de error (P5)
│   ├── ids.py         identificadores SHA-256 (P2)
│   ├── objects.py     manifiestos, lápidas, metadatos y búsqueda
│   ├── media.py       recodificación con ffmpeg
│   ├── cluster.py     interfaz con el almacenamiento replicado
│   ├── fake_cluster.py  almacenamiento en memoria para las pruebas
│   ├── client.py      upload, search, list y withdraw
│   └── cli.py         la orden dtn (python3 -m dtn)
└── tests/             pruebas
    └── conftest.py    arranca una instancia local de Ray para las pruebas
```

## Dónde deja Ray sus logs

Ray guarda los logs de cada ejecución fuera del repositorio, en `/tmp/ray/session_latest/logs/`.
