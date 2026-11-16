# Principios de arquitectura

Este documento recoge las reglas que debe cumplir el código de drop-the-node, sea cual sea la funcionalidad que se implemente.

Cada principio tiene tres partes:

- **Regla:** lo que se debe cumplir.
- **Por qué:** el problema que evita.
- **Cómo se comprueba:** la forma de verificar que se cumple.

---

## P1 · Identidad lógica separada del proceso y de la dirección

**Regla.** Cada nodo tiene un identificador lógico estable que se indica al crearlo (por ejemplo `s1`). Ese identificador es distinto del PID del proceso, que cambia en cada reinicio, y de su dirección de red, que puede cambiar. Los datos persistentes (por ejemplo, la lista de nodos que tienen un fragmento) guardan identificadores lógicos, nunca PID ni direcciones. La dirección se resuelve en el momento de usarla.

**Por qué.** Si un nodo se reinicia en otro lugar, todo lo que guardara su PID o su dirección quedaría desactualizado.

**Cómo se comprueba.** Ningún dato guardado ni ningún mensaje entre nodos contiene un PID. Los logs incluyen el identificador lógico en cada evento; el PID solo aparece como información de diagnóstico.

## P2 · El contenido se identifica por su hash

**Regla.** El contenido multimedia se divide en fragmentos. Cada fragmento se identifica por el SHA-256 de sus bytes. El identificador de un fragmento nunca depende de su ubicación ni del orden en que se subió.

**Por qué.** Da tres propiedades a la vez:

- **Integridad:** se puede verificar que unos bytes son los correctos recalculando el hash.
- **Idempotencia:** guardar dos veces el mismo fragmento no crea nada nuevo.
- **Réplicas intercambiables:** dos nodos con el mismo identificador tienen exactamente el mismo contenido, así que el cliente puede pedirlo a cualquiera.

**Cómo se comprueba.** Toda escritura y toda lectura de un fragmento verifican el hash. Un identificador que no tenga la forma de un SHA-256 se rechaza antes de usarlo, en especial si se usa como nombre de archivo.

## P3 · No se confirma nada fuera de la durabilidad declarada

**Regla.** Cada forma de almacenamiento declara por escrito qué sobrevive y qué no: un reinicio del proceso, una caída de la máquina o nada. Una operación solo responde "guardado" cuando el dato cumple esa declaración. Si el almacenamiento vive solo en memoria, se declara explícitamente que los datos se pierden al reiniciar. Si se usa disco, se escribe en un archivo temporal, se fuerza la escritura (`fsync`), se renombra al nombre definitivo y se fuerza la escritura de la carpeta. Cuando haya réplicas, la confirmación espera a que hayan respondido las réplicas que exija la política declarada.

**Por qué.** Si el sistema dice "guardado" y luego pierde el dato, el cliente no tiene forma de enterarse. Y si nadie declaró qué sobrevive, no se puede saber si una pérdida es un error o el comportamiento esperado.

**Cómo se comprueba.** Con una prueba que guarda un dato, mata el proceso, lo reinicia y comprueba que el resultado coincide con lo declarado: el dato sigue ahí, o se perdió y así estaba documentado.

**Límite conocido.** Matar solo el proceso no cubre un corte de luz ni una caída del sistema operativo, porque el sistema operativo sigue vivo y termina de escribir lo que tenía en memoria. La protección ante esos casos se apoya en `fsync` y se declara por escrito, pero no se prueba automáticamente.

## P4 · Toda escritura que se pueda reintentar es idempotente

**Regla.** Cualquier operación que el cliente pueda repetir tras un fallo debe producir el mismo resultado si se ejecuta una o varias veces. Las escrituras de fragmentos lo son por P2. Las operaciones que crean algo nuevo (por ejemplo, registrar una subida) llevan un identificador generado por el cliente, y quien las recibe recuerda el resultado asociado a ese identificador.

**Por qué.** En un sistema distribuido, cuando no llega respuesta, el cliente no sabe si su petición se ejecutó: puede que no llegara, que llegara y no terminara, o que terminara y se perdiera la respuesta. Si reintentar pudiera duplicar datos, tendría que elegir entre arriesgarse a duplicar o a perder.

**Cómo se comprueba.** Con pruebas que envían la misma petición dos veces y verifican que el estado final es el mismo que con una sola.

## P5 · Los errores tienen significado

**Regla.** Los errores se clasifican según lo que debe hacer quien los recibe, y cada clase tiene su propio tipo en el código:

| Error | Significado | Qué hace quien lo recibe |
|---|---|---|
| `NotFound` | Este nodo no lo tiene | Probar otra fuente; reintentar aquí no sirve |
| `Unavailable` | Falla temporal | Reintentar más tarde o en otro nodo |
| `Corrupt` | Los bytes no coinciden con su hash | Descartar esa copia y pedir otra |
| `Invalid` | La petición está mal formada | No reintentar: fallará siempre igual |
| `Conflict` | Contradice el estado actual | Revisar el estado antes de reintentar |
| `NotImplemented` | La operación aún no existe | No reintentar |

**Por qué.** Si todo fuera simplemente "error", el cliente no podría decidir entre reintentar, cambiar de nodo o rendirse, y esas decisiones son la base de la tolerancia a fallos.

**Cómo se comprueba.** Hay pruebas que provocan cada clase de error y verifican que quien llama recibe el tipo correcto.

## P6 · La configuración es explícita

**Regla.** Todo lo que cambia entre ejecuciones o experimentos (identificadores de nodos, cantidad de nodos, carpetas de datos, plazos, tamaño de fragmento y fallos inyectados) se pasa como parámetro al arrancar. Nada de eso se escribe fijo en el código.

**Por qué.** Un experimento solo se puede repetir si se sabe exactamente con qué configuración se ejecutó.

**Cómo se comprueba.** Cada proceso escribe su configuración completa en el log al arrancar. En una revisión, cualquier valor de configuración escrito directamente en el código se señala como problema.

## P7 · La evidencia se produce por defecto

**Regla.** Cada proceso registra sus eventos en formato estructurado, con su identificador lógico. Cada operación lleva un identificador que genera quien la inicia y que registran todos los que participan en ella. Quien recibe una petición registra su llegada antes de procesarla y su respuesta después.

**Por qué.** Cuando algo falla ya es tarde para añadir logs. Registrar llegada y respuesta por separado permite distinguir "nunca llegó" de "llegó pero no respondió".

**Cómo se comprueba.** Buscando el identificador de una operación en los logs de todos los procesos se reconstruye su recorrido completo.

## P8 · Los fallos se pueden provocar de forma controlada

**Regla.** El sistema permite inyectar fallos desde la configuración (retardos, errores cada cierto número de peticiones) y desde scripts (matar un proceso). Los fallos inyectados son deterministas siempre que sea posible, y cada inyección queda registrada en el log.

**Por qué.** Para demostrar qué hace el sistema ante un fallo hay que poder provocarlo cuando se quiere y repetirlo igual.

**Cómo se comprueba.** Cada escenario de fallo que se estudie tiene un comando o un parámetro concreto para provocarlo.

## P9 · Ray solo en el prototipo; lo distribuido de la versión nativa lo construimos nosotros

**Regla.** Ray se usa únicamente en el prototipo, como middleware temporal. La versión nativa no usa Ray ni ningún otro software que automatice o simplifique decisiones de diseño distribuido: coordinación, elección de líder, replicación, membresía, colas de mensajes o almacenamiento replicado. Ante la duda sobre una tecnología, se pregunta a los profesores; si no responden, se asume que no está permitida.

**Por qué.** Es una regla del curso. El prototipo sirve para entender qué hace un middleware por nosotros; la versión nativa debe construir explícitamente esos mecanismos, que son justamente lo que se evalúa.

**Cómo se comprueba.** El código de la versión nativa no importa Ray ni tiene dependencias externas, salvo las aprobadas previamente por los profesores.

## P10 · Cada garantía se declara como corrección o como objetivo

**Regla.** Toda garantía del sistema se escribe explícitamente y se clasifica en una de dos categorías:

- **Corrección (seguridad):** algo que nunca debe ocurrir, pase lo que pase. Por ejemplo: "nunca se entrega al cliente un fragmento cuyo hash no coincide" o "nunca aparece en las búsquedas una pista a la que le faltan fragmentos".
- **Objetivo (disponibilidad o rendimiento):** algo que el sistema intenta lograr mientras se cumplan ciertas condiciones. Por ejemplo: "si cae un nodo y queda otra copia, la reproducción no se corta".

**Por qué.** Una garantía de corrección rota es un error del sistema; un objetivo no alcanzado bajo condiciones extremas puede ser aceptable si se explica.

**Cómo se comprueba.** Cada garantía tiene asociado al menos un experimento o una prueba que la pone a prueba, y su resultado se documenta.
