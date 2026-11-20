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
