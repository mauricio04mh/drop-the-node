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

## Arrancar los almacenes

Los almacenes y el agente de reparación son actores de Ray con nombre que siguen vivos entre una orden y otra, así que primero hace falta un cluster de Ray en marcha en esta máquina:

```bash
ray start --head
python3 -m dtn.admin start           # arranca los almacenes y el agente (si ya están, no hace nada)
python3 -m dtn.admin status          # pregunta a cada almacén y muestra la última ronda de reparación
python3 -m dtn.admin repair          # ejecuta ahora una ronda de reparación
python3 -m dtn.admin crash store-2   # mata con SIGKILL el proceso de un almacén (o del agente: repair)
python3 -m dtn.admin stop            # elimina los almacenes y el agente; los datos quedan en disco
ray stop
```

Cada almacén guarda sus objetos en `DTN_DATA_DIR/<nombre>` (por defecto `./data`). La primera vez, `start` guarda S y N en `DTN_DATA_DIR/cluster.json`, y después se niega a arrancar con otros valores. Un cliente que se conecta con otro `DTN_NUM_STORES` u otro `DTN_REPLICAS` que los almacenes también se detiene con un error. Tras un `crash`, Ray lo vuelve a arrancar solo y el almacén reconstruye su índice desde disco; `status` lo muestra con una encarnación nueva.

El agente de reparación repone las réplicas perdidas y borra los objetos que ninguna pista usa. Hace una ronda cada `DTN_REPAIR_INTERVAL_SECONDS`, otra en cuanto un almacén deja de responder o vuelve con una encarnación nueva, y otra cuando un almacén lleva `DTN_STORE_LOST_SECONDS` sin responder: solo entonces lo da por perdido y repone lo que guardaba.

## Uso

```bash
python3 -m dtn upload cancion.flac --title "Canción" --artist "Ana" --license CC-BY-4.0
python3 -m dtn search mar
python3 -m dtn list
python3 -m dtn withdraw TRACK_ID --reason "sin licencia"
```

Todas las órdenes admiten `--json`, y el código de salida indica cómo terminó la operación (ver `docs/contract.md` § 6). Hasta que la orden `dtn` se conecte a los almacenes, responde que el almacenamiento no está disponible; las pruebas de las operaciones usan `FakeCluster`.

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
│   ├── cli.py         la orden dtn (python3 -m dtn)
│   ├── config.py      configuración del almacenamiento (P6)
│   ├── store.py       un almacén: disco, integridad y antigüedad, sin Ray
│   ├── store_actor.py el almacén como actor de Ray
│   ├── placement.py   orden de colocación de cada objeto
│   ├── detector.py    detector de fallos adaptativo
│   ├── ray_cluster.py almacenamiento replicado sobre los actores
│   ├── repair.py      agente de reparación y limpieza
│   └── admin.py       arrancar, consultar y matar almacenes
└── tests/             pruebas
    └── conftest.py    arranca una instancia local de Ray para las pruebas
```

## Dónde deja Ray sus logs

Ray guarda los logs de cada ejecución fuera del repositorio, en `/tmp/ray/session_latest/logs/`.
