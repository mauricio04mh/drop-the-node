# Contrato de comportamiento

Este documento fija qué puede hacer quien usa drop-the-node y qué le promete el sistema en cada caso, también cuando hay almacenes caídos o lentos. La sección 7 fija además la interfaz entre el cliente y cada almacén. Describe solo lo que se ve desde fuera: no menciona Ray, actores, procesos ni la forma de replicar. Por eso vale igual para el prototipo y para la versión nativa, y cualquier cambio interno debe seguir cumpliéndolo.

Cada promesa sale de una decisión de [`decisions.md`](decisions.md), que explica el porqué. El contrato no toma decisiones propias: si una promesa necesita una decisión, esta está en `decisions.md`.

Al cerrar la Fase 1 este contrato se congela: solo se cambia con una justificación escrita.

Notación: **S** almacenes, **N** réplicas de cada objeto, **F** almacenes caídos a la vez. Valores actuales: S = 5, N = 3, F = 2 (ver «Cuántas copias guardar de cada objeto y en cuántos almacenes»).

---

## 1. Conceptos visibles

### Pista

Una pista es audio con sus metadatos, subido una vez y nunca editado. Para corregir algo se sube de nuevo, lo que produce otra pista.

### Identificador de pista

- Es el SHA-256 de su manifiesto, escrito como 64 caracteres hexadecimales en minúsculas.
- Lo calcula el sistema al subir y lo devuelve `upload`. Quien usa el sistema no lo elige.
- Dos subidas con el mismo audio y los mismos metadatos producen el mismo identificador (ver § 2.4).
- Un identificador que no tenga esa forma se rechaza con `Invalid` antes de consultar ningún almacén.

### Metadatos

| Campo | Lo da | Reglas |
|---|---|---|
| `title` | quien sube | Texto no vacío, de como mucho 200 caracteres (ver «Cuánto puede durar una operación»). |
| `artist` | quien sube | Texto no vacío, de como mucho 200 caracteres. |
| `license` | quien sube | Obligatoria, de una lista cerrada: `CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`, `CC-BY-NC-4.0`, `public-domain`. |
| `duration` | el sistema | Segundos, calculada a partir de los segmentos. Si quien sube la envía, se rechaza con `Invalid`. |

### Estados de una pista

```text
no existe ──upload──▶ subiendo (invisible) ──confirmación──▶ visible ──withdraw──▶ retirada
```

- **Subiendo:** sus segmentos ya pueden estar guardados, pero ninguna búsqueda la devuelve y no se puede reproducir.
- **Visible:** aparece en las búsquedas y se puede reproducir.
- **Retirada:** es definitivo. No vuelve a estar visible, ni siquiera subiéndola de nuevo (ver § 3.1).

Los cambios de estado solo avanzan hacia la derecha. Ninguna operación devuelve una pista a un estado anterior.

---

## 2. Convenciones comunes

### 2.1 Errores

Se usan las clases de error del principio P5 de [`principles.md`](principles.md). Esto es lo que significa cada una para quien usa el sistema:

| Error | Significado en este contrato | Qué debe hacer quien lo recibe |
|---|---|---|
| `Invalid` | La petición está mal formada: identificador con otra forma, metadatos que no cumplen sus reglas, archivo que no es audio válido. | Corregir la petición. Repetirla igual fallará igual. |
| `NotFound` | La pista no existe o está retirada, y el sistema puede afirmarlo con los almacenes que respondieron. | No reintentar: la respuesta es definitiva. |
| `Unavailable` | No respondieron suficientes almacenes para completar la operación o para dar una respuesta segura. En las escrituras, el resultado es **incierto** (§ 2.2). | Reintentar más tarde. Reintentar es seguro en todas las operaciones (§ 2.4). |
| `Conflict` | La petición contradice el estado actual. Solo lo usa `upload` (§ 3.1). | Revisar el estado: no tiene sentido reintentar igual. |

`Corrupt` nunca llega a quien usa el sistema: cuando una réplica está dañada, el cliente la descarta y usa otra. Si no queda ninguna réplica válida, la operación termina con `Unavailable`.

Todo error lleva un mensaje legible y el identificador de la operación (§ 2.5).

### 2.2 Resultado incierto

En `upload` y `withdraw`, un `Unavailable` **no** significa «no ocurrió». Puede que la operación se haya aplicado en parte o del todo y no se haya podido confirmar (ver «Cuándo se considera completa una subida»). Quien la recibe:

- no debe suponer que la pista no existe o que no se retiró;
- puede repetir **exactamente la misma petición** sin riesgo: no crea duplicados y completa lo que falte.

### 2.3 Plazos

- Toda operación tiene un plazo máximo. Si se agota, termina con `Unavailable`, y en las escrituras su resultado es incierto.
- Los plazos internos de cada petición a un almacén son adaptativos (ver «Cómo se detecta que un almacén falla»). Quien usa el sistema solo ve el plazo total.
- El plazo total de cada operación es configuración: `DTN_UPLOAD_TIMEOUT_SECONDS`, `DTN_SEARCH_TIMEOUT_MS` y `DTN_WITHDRAW_TIMEOUT_SECONDS` (ver «Cuánto puede durar una operación»). La reproducción tiene el suyo: `DTN_GIVE_UP_SECONDS`.

### 2.4 Repetición segura

| Operación | ¿Repetirla es seguro? | Por qué |
|---|---|---|
| `upload` | Sí | Todo se identifica por el hash de su contenido: repetir con el mismo archivo y los mismos metadatos da el mismo identificador y no crea nada nuevo. |
| `search`, `list` | Sí | Solo leen. |
| `play` | Sí | Solo lee. |
| `withdraw` | Sí | Retirar una pista ya retirada no cambia nada y responde éxito. |

**Supuesto:** todos los clientes usan la misma versión de `ffmpeg`, porque con ella recodificar el mismo archivo produce exactamente los mismos bytes (ver «Cómo se garantiza que repetir una subida produzca lo mismo»).

### 2.5 Identificador de operación

Cada llamada recibe un identificador de operación generado por el cliente. Aparece en todos los registros de todos los procesos que participan, y en el error si lo hay (principio P7). Sirve para reconstruir el recorrido de una operación, no para la idempotencia: esta se basa en el contenido (§ 2.4).

### 2.6 Concurrencia

Varios clientes pueden usar el sistema a la vez, y un mismo cliente puede lanzar varias operaciones a la vez.

- Una búsqueda que se ejecuta mientras otra persona sube una pista puede devolverla o no. Una búsqueda que **empieza** después de que `upload` confirmó la devuelve, siempre que se cumplan las condiciones de § 3.2.
- Dos subidas simultáneas de lo mismo terminan en una sola pista, con el mismo identificador para ambas.
- Una retirada simultánea con una reproducción no la interrumpe (§ 3.4).

---

## 3. Operaciones

### 3.1 `upload(file, metadata) → track_id`

Sube una pista y devuelve su identificador cuando ya es visible.

**Entrada**

- `file`: archivo de audio en cualquier formato que el sistema sepa recodificar, de como mucho `DTN_MAX_UPLOAD_SECONDS` de audio (20 minutos) y `DTN_MAX_UPLOAD_BYTES` (200 MB) (ver «Cuánto puede durar una operación»).
- `metadata`: `title`, `artist` y `license`, con las reglas de § 1.

**Qué ocurre**

1. Se validan los metadatos y se comprueba que el archivo es audio decodificable. Si algo falla: `Invalid`, y no se escribe nada.
2. El audio se recodifica y se divide en segmentos (ver «Cómo se divide el audio en fragmentos»).
3. Se guardan los segmentos, el segmento inicial y el original, cada uno con N réplicas.
4. Por último se guarda el manifiesto con N réplicas. **En ese momento la pista pasa a ser visible y `upload` devuelve su identificador.**

**Resultados**

| Resultado | Cuándo | Estado de la pista |
|---|---|---|
| `track_id` | El manifiesto tiene sus N réplicas. | Visible. |
| `Invalid` | Metadatos o archivo no válidos. | No existe. |
| `Conflict` | El identificador resultante corresponde a una pista retirada (ver «Qué pasa al volver a subir una pista retirada»). | Sigue retirada. |
| `Unavailable` | Quedan menos de N almacenes vivos, o se agotó el plazo. | **Incierta:** puede no existir o ser ya visible. |

**Garantías**

| Tipo | Declaración |
|---|---|
| Corrección | Si `upload` devuelve un identificador, la pista es visible y todos sus objetos tienen N réplicas. |
| Corrección | Toda pista visible tiene todos sus segmentos guardados. Una subida interrumpida nunca deja una pista visible a medias. |
| Límite | Con más de S − N almacenes caídos (más de 2), `upload` termina con `Unavailable`. |
| Semántica | Tras un `Unavailable`, repetir la misma subida es seguro y la completa (§ 2.2, § 2.4). |

### 3.2 `search(query) → SearchResult` y `list() → SearchResult`

Devuelven las pistas visibles cuyos metadatos coinciden con la consulta. `list()` equivale a `search` con la consulta vacía.

**Consulta** (ver «Cómo se busca una pista»)

- Es texto libre. Una pista coincide si **cada palabra** de la consulta aparece dentro de `title` o de `artist`.
- No distingue mayúsculas de minúsculas ni letras con tilde de letras sin tilde.
- La consulta vacía coincide con todas las pistas.
- Si tiene más de 200 caracteres, se rechaza con `Invalid`.

**Resultado**

| Campo | Contenido |
|---|---|
| `tracks` | Lista de `{track_id, title, artist, duration, license}`, sin repetidos y sin pistas retiradas, ordenada por `artist`, luego `title`, luego `track_id`, para que el resultado sea reproducible. |
| `complete` | `true` si no respondieron **menos de N** almacenes. |
| `unreachable` | Nombres lógicos de los almacenes que no respondieron dentro del plazo. |

**Qué significa `complete`.** Toda pista visible tiene su manifiesto en N almacenes. Si los que no respondieron son menos de N, al menos una copia de cada manifiesto estaba entre los que sí respondieron, y lo mismo pasa con las lápidas de las pistas retiradas. Por eso el cliente puede afirmar que el resultado está completo sin preguntar nada más (ver «Qué interfaz se ofrece»).

**Comportamiento con almacenes inaccesibles.** `search` **nunca** termina con error por falta de almacenes. Siempre devuelve lo que encontró dentro del plazo y declara si está completo. Si no responde ningún almacén, devuelve una lista vacía con `complete = false`. Así se cumple el requisito del P10 de definir cómo se comportan los metadatos cuando hay fuentes inaccesibles.

**Garantías**

| Tipo | Declaración |
|---|---|
| Corrección | Si `complete = true`, el resultado contiene todas las pistas cuya subida se confirmó antes de que empezara la búsqueda, que coinciden con la consulta y cuya retirada no se confirmó. |
| Corrección | Si `complete = true`, no contiene ninguna pista cuya retirada se confirmó antes de que empezara la búsqueda. |
| Corrección | Toda pista devuelta, completa o no, tiene todos sus segmentos guardados: se confirmaron antes que su manifiesto. |
| Límite | Si `complete = false`, puede faltar alguna pista visible y puede aparecer alguna retirada. El resultado lo indica, y quien lo usa decide si le basta. |
| Supuesto | Cada manifiesto y cada lápida tienen sus N réplicas: las réplicas perdidas se reponen antes de que un objeto acumule N pérdidas (ver «Cómo se recupera la redundancia perdida»). |

### 3.3 `play(track_id, options) → PlaybackSession`

Reproduce una pista desde los almacenes y mantiene la reproducción aunque caiga una fuente mientras quede otra válida.

**Opciones**

| Opción | Efecto |
|---|---|
| `headless` | Mismo reloj y misma medición, pero sin sonido. Es para los experimentos automáticos (ver «Cómo se reproduce una pista»). |

**Inicio.** Antes de sonar, el cliente obtiene el manifiesto, comprueba que su hash coincide con `track_id` y comprueba que la pista no está retirada. Errores al empezar:

| Error | Cuándo |
|---|---|
| `Invalid` | `track_id` no tiene la forma de un SHA-256. |
| `NotFound` | Ningún almacén que respondió tiene el manifiesto y no respondieron menos de N, o la pista está retirada. El error indica cuál de los dos casos es. |
| `Unavailable` | Ningún almacén que respondió tiene el manifiesto, pero no respondieron N o más: el sistema no puede afirmar que no exista. |

La regla que separa `NotFound` de `Unavailable` es la misma que define `complete` en `search`: solo se afirma que algo no existe cuando, si existiera, alguno de los almacenes que respondieron lo tendría (ver «Cuándo se responde «no existe» y cuándo «no disponible»»).

**Durante la reproducción.** La sesión emite estos eventos, cada uno con el instante, medido con un reloj monótono, y el número de segmento:

| Evento | Significado |
|---|---|
| `started` | Hay `DTN_PREROLL_SECONDS` de audio recibidos y empieza a sonar. Incluye el tiempo hasta el inicio. |
| `source_switch` | Un segmento se obtuvo de otro almacén distinto del preferido. Incluye el almacén abandonado, el nuevo y el motivo: plazo agotado, almacén caído o `Corrupt`. |
| `stall_start` / `stall_end` | **Corte**: la salida de audio necesitó muestras y no había. `stall_end` incluye la duración del corte. |
| `finished` | Se entregaron todos los segmentos del manifiesto. |
| `failed` | Pasaron `DTN_GIVE_UP_SECONDS` sin conseguir un segmento. Termina con `Unavailable`. |

Al terminar, la sesión da un resumen: tiempo hasta el inicio, número de cortes, duración total de los cortes, número de cambios de fuente y segmentos obtenidos de cada almacén. Estos datos son la evidencia de los experimentos de la Fase 3.

**Cambios de fuente, indisponibilidad y recuperación**, que el P10 pide definir:

- **Cambio de fuente:** el usuario no lo nota si se cumple la condición `d + tiempo de lectura de otra réplica < audio en el búfer` (ver «Qué hace el reproductor cuando una lectura falla»). Solo queda registrado como `source_switch`.
- **Indisponibilidad temporal:** si ninguna réplica responde, el cliente reintenta mientras quede audio en el búfer. Si el búfer se vacía, la reproducción se **pausa** (`stall_start`), sin saltar audio y sin terminar.
- **Recuperación:** en cuanto llega el segmento que faltaba, la reproducción **se reanuda sola** desde el punto exacto donde se detuvo (`stall_end`). Si la recuperación tarda más de `DTN_GIVE_UP_SECONDS`, termina con `failed`.

**Garantías**

| Tipo | Declaración |
|---|---|
| Corrección | El audio entregado al decodificador es idéntico byte a byte a la versión guardada al subir, en orden, sin repetir y sin saltar ningún segmento; o la reproducción termina con error. |
| Objetivo | Con como mucho 2 almacenes caídos, la reproducción no tiene cortes, siempre que se cumpla la condición del búfer. |
| Límite | Si ninguna réplica de un segmento responde durante `DTN_GIVE_UP_SECONDS`, la reproducción termina con `Unavailable`. |
| Semántica | Retirar la pista durante la reproducción no la interrumpe: termina normalmente. |

**Fuera de alcance en la Fase 1:** pausar a voluntad y saltar a un punto de la pista. Los segmentos se decodifican por separado, así que añadir el salto más adelante no cambia nada de lo anterior.

### 3.4 `withdraw(track_id, reason) → ok`

Retira una pista de forma definitiva, por ejemplo si se descubre que no tenía una licencia adecuada (ver «Cómo se retira una pista»).

**Entrada:** `track_id` y `reason`, un texto no vacío que queda guardado en la lápida.

**Resultados**

| Resultado | Cuándo |
|---|---|
| `ok` | La lápida tiene N réplicas. También si la pista ya estaba retirada. |
| `Invalid` | `track_id` mal formado o `reason` vacío. |
| `NotFound` | La pista no existe, con la misma regla que `play`. |
| `Unavailable` | No se pudo guardar la lápida con N réplicas. **Resultado incierto:** la pista puede haber quedado retirada. Repetir es seguro. |

**Garantías**

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho 2 almacenes caídos, una pista cuya retirada se confirmó no aparece en ninguna búsqueda completa posterior y no se puede empezar a reproducir. |
| Supuesto | Una retirada no se deshace. |
| Límite | Cualquiera que use el sistema puede retirar cualquier pista: no hay autenticación en la Fase 1 (ver «Quién puede retirar una pista»). |

---

## 4. Resumen por número de almacenes caídos

| Almacenes caídos | `upload` | `search` / `list` | `play` | `withdraw` |
|---|---|---|---|---|
| 0, 1 o 2 | Funciona. | Funciona, `complete = true`. | Funciona, sin cortes si se cumple la condición del búfer. | Funciona. |
| 3 o más | `Unavailable`, resultado incierto. | Devuelve lo encontrado con `complete = false`. | Las pistas con alguna réplica viva del manifiesto y de cada segmento suenan. Las demás terminan en `Unavailable`. | `Unavailable`, resultado incierto. |

En ningún caso el sistema entrega audio distinto del subido ni muestra como completa una búsqueda que no lo es.

---

## 5. Lo que este contrato no promete

- **Editar** una pista: se sube de nuevo.
- **Varias calidades** ni adaptación al ancho de banda.
- Tolerancia a **almacenes maliciosos** o con fallos arbitrarios. Los hashes detectan datos incorrectos, pero no que un almacén oculte datos.
- Tolerancia al **fallo de la máquina** donde corren todos los almacenes.
- Comportamiento ante **particiones de red y pérdida de mensajes**: depende de la cuestión abierta «Una máquina o varios nodos» de `decisions.md`.
- **Autenticación y permisos.**

---

## 6. Interfaz de línea de comandos

Es la forma de usar el sistema a mano y desde los guiones de experimentos (ver «Cómo se usa el sistema desde fuera»). Los códigos de salida permiten a un guion distinguir los errores sin leer texto.

```text
dtn upload FILE --title T --artist A --license L     → imprime el track_id
dtn search TEXTO                                     → una línea por pista + estado de completitud
dtn list                                             → igual que search sin texto
dtn play TRACK_ID [--headless]                       → eventos de la sesión, uno por línea
dtn withdraw TRACK_ID --reason MOTIVO
```

| Código de salida | Significado |
|---|---|
| 0 | Éxito. En `search` y `list`, también si el resultado está incompleto, que se indica en la salida. |
| 2 | `Invalid` |
| 3 | `NotFound` |
| 4 | `Unavailable` (en `upload` y `withdraw`, resultado incierto) |
| 5 | `Conflict` |

Toda salida tiene también una versión en JSON (`--json`) para procesarla en los experimentos.

---

## 7. Interfaz de un almacén

Las secciones anteriores describen lo que ve quien usa el sistema. Esta describe la frontera entre el cliente y cada almacén, que tampoco cambia entre fases. En el prototipo son métodos de un actor; en la versión nativa, peticiones por la red. Su significado y sus errores son los mismos en ambos casos. La usan la biblioteca del cliente y el agente de reparación, nunca una persona.

### 7.1 Reglas comunes

- **Identificadores.** Todo `id` o `track_id` debe tener la forma de un SHA-256 en hexadecimal y en minúsculas. Si no la tiene: `Invalid`, antes de tocar el disco.
- **Integridad.** Un almacén nunca entrega un objeto cuyo hash no coincide con su identificador (ver «Cómo se detecta la corrupción»).
- **Saturación.** Con `DTN_STORE_CONCURRENCY` peticiones en curso, la siguiente se rechaza en el acto con `Unavailable`, indicando saturación (ver «Qué hace un almacén cuando está al máximo de peticiones»).
- **Registro.** Toda petición lleva el identificador de la operación que la origina. El almacén registra su llegada antes de procesarla y su respuesta después, con su nombre lógico (principio P7).
- **Repetición segura.** Todas las operaciones de esta interfaz se pueden repetir sin cambiar el resultado.

### 7.2 Operaciones

#### `put(id, data) → {stored: "new" | "existing"}`

Guarda un objeto.

1. Si `sha256(data) ≠ id`: `Invalid`, y no se escribe nada.
2. Si el almacén ya tiene el objeto y su copia es válida, no lo reescribe: renueva su antigüedad y responde `existing` (ver «Qué se hace con los objetos que nadie usa»).
3. Si no lo tiene, o su copia estaba dañada, lo escribe en un archivo temporal, fuerza la escritura, lo renombra y fuerza la carpeta. Solo entonces responde `new` (ver «Cómo guarda un almacén sus objetos»).
4. Si el objeto es un manifiesto o una lápida, lo añade a su índice (ver «Cómo se escriben los manifiestos y las lápidas»).

| Error | Cuándo |
|---|---|
| `Invalid` | `id` mal formado, o `data` no corresponde a `id`. |
| `Unavailable` | Saturación o error de disco. El objeto puede haber quedado guardado; repetir es seguro. |

| Tipo | Declaración |
|---|---|
| Corrección | Si `put` responde, el objeto sobrevive a la caída y el reinicio del almacén. |

#### `get(id) → data`

Entrega un objeto.

| Resultado | Cuándo |
|---|---|
| `data` | El almacén tiene el objeto y su hash coincide. |
| `NotFound` | No lo tiene, o lo tenía en cuarentena. |
| `Corrupt` | Lo tenía, pero al leerlo el hash no coincide. Lo aparta a cuarentena antes de responder, así que la siguiente petición recibe `NotFound`. |
| `Invalid` | `id` mal formado. |
| `Unavailable` | Saturación o error de disco. |

#### `inventory() → [{id, bytes, age_seconds}]`

Lista los objetos que guarda: su tamaño y el tiempo desde su última escritura, medido por el propio almacén. No incluye los objetos en cuarentena. Es una foto: un objeto escrito mientras se responde puede aparecer o no. La usa el agente de reparación (ver «Cómo se recupera la redundancia perdida»).

#### `search(query) → {manifests: [{track_id, manifest}], tombstoned: [track_id]}`

Aplica la regla de búsqueda a sus manifiestos (ver «Cómo se busca una pista») y devuelve los que coinciden, cada uno con su identificador. Devuelve además los `track_id` de **todas** las lápidas que guarda, porque la lápida de una pista puede estar en un almacén distinto de su manifiesto. Comprueba el hash de cada manifiesto antes de devolverlo y aparta los dañados.

| Error | Cuándo |
|---|---|
| `Invalid` | Consulta de más de 200 caracteres. |
| `Unavailable` | Saturación. |

#### `tombstones(track_id) → [tombstone]`

Devuelve las lápidas que guarda para esa pista, normalmente ninguna o una. Una lista vacía es una respuesta válida, no un error: significa «yo no tengo ninguna». El cliente decide con la regla de «Cómo encuentra el cliente la lápida de una pista».

#### `delete_if_older(id, seconds) → {deleted: bool, reason: "deleted" | "recent" | "absent"}`

Borra el objeto solo si su última escritura es más antigua que `seconds`. La comprobación y el borrado se hacen sin que ninguna escritura del mismo objeto pueda colarse entre ambos (ver «Cómo borra un almacén un objeto sin uso»). Solo la usa el agente de reparación.

| Resultado | Significado |
|---|---|
| `deleted` | Se borró. |
| `recent` | Se conservó, porque se escribió hace menos de `seconds`. |
| `absent` | No lo tenía. Repetir un borrado ya hecho da este resultado. |

| Tipo | Declaración |
|---|---|
| Corrección | Un objeto escrito hace menos de `seconds` nunca se borra. |
| Supuesto | El reloj del sistema de cada almacén no retrocede: la antigüedad se calcula con la hora de la última escritura guardada en disco, que tiene que sobrevivir a los reinicios. |

#### `ping() → {store, incarnation, objects}`

Responde con su nombre lógico, un identificador de esta ejecución del proceso, que cambia en cada reinicio (solo para diagnóstico, principio P1), y el número de objetos que guarda. No toca el disco. La usan el detector de fallos para readmitir almacenes sospechosos y los guiones de diagnóstico (ver «Cómo se detecta que un almacén falla»).

---

## 8. Correspondencia con `decisions.md`

| Parte del contrato | Secciones de `decisions.md` |
|---|---|
| § 1 Conceptos | «Qué debe hacer el sistema», «Dónde viven los metadatos de cada pista», «Cuánto puede durar una operación» |
| § 2.2 Resultado incierto, § 3.1 `upload` | «Cuándo se considera completa una subida», «Cómo se escribe durante una subida», «Cómo se divide el audio en fragmentos», «Qué pasa al volver a subir una pista retirada» |
| § 2.3 Plazos | «Cuánto puede durar una operación», «Cómo se detecta que un almacén falla» |
| § 2.4 Repetición segura | «Cuándo se considera completa una subida», «Cómo guarda un almacén sus objetos», «Cómo se garantiza que repetir una subida produzca lo mismo» |
| § 3.2 `search` / `list` | «Dónde viven los metadatos de cada pista», «Cómo se busca una pista», «Qué interfaz se ofrece» |
| § 3.3 `play` | «Cómo se reproduce una pista», «Cuánto audio se pide por adelantado», «De qué réplica se lee», «Cómo se detecta que un almacén falla», «Qué hace el reproductor cuando una lectura falla», «Cuándo se responde «no existe» y cuándo «no disponible»», «Cómo se usa el sistema desde fuera» |
| § 3.4 `withdraw` | «Cómo se retira una pista», «Quién puede retirar una pista», «Cuándo se responde «no existe» y cuándo «no disponible»» |
| § 4 Resumen | «Cuántas copias guardar de cada objeto y en cuántos almacenes», «En qué almacenes se coloca cada réplica» |
| § 6 Línea de comandos | «Cómo se usa el sistema desde fuera» |
| § 7 Interfaz de un almacén | «Qué interfaz se ofrece», «Cómo guarda un almacén sus objetos», «Cómo se detecta la corrupción», «Cómo se escriben los manifiestos y las lápidas», «Cómo encuentra el cliente la lápida de una pista», «Qué hace un almacén cuando está al máximo de peticiones», «Cómo borra un almacén un objeto sin uso», «Cómo se recupera la redundancia perdida» |
