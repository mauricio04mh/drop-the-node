# Prototipo con Ray

Implementación de drop-the-node sobre Ray Core. Ray actúa como middleware: se encarga de la comunicación entre procesos, de dónde se ejecuta cada actor y de detectar cuándo uno muere.

## Requisitos

- Linux.
- Python 3.12 o superior.

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

## Estructura

```text
prototype/
├── README.md
├── requirements.txt   versiones fijas de Ray y pytest
├── pytest.ini         configuración de las pruebas
├── dtn/               código del prototipo
└── tests/             pruebas
    └── conftest.py    arranca una instancia local de Ray para las pruebas
```

## Dónde deja Ray sus logs

Ray guarda los logs de cada ejecución fuera del repositorio, en `/tmp/ray/session_latest/logs/`.
