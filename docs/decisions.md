# Decisiones de diseño

Este documento recorre los problemas que el sistema tiene que resolver. Para cada uno explica en qué consiste, qué soluciones existen y con qué costes, cuál se eligió y por qué, y qué garantías, límites y supuestos se declaran.

Las decisiones no son definitivas: si aparece una opción mejor, se revisan.

Notación usada en todo el documento:

- **S:** número de almacenes.
- **N:** número de réplicas de cada objeto.
- **F:** número de almacenes caídos a la vez que el sistema debe soportar.

---

## Qué debe hacer el sistema

### El problema

Antes de decidir cómo distribuir nada hay que fijar qué operaciones se ofrecen, porque cada una impone sus propias exigencias: una operación que modifica datos ya guardados obliga a coordinar las réplicas, y una que solo añade datos nuevos no.

### Decisión

El sistema ofrece cuatro operaciones:

- **Subir** una pista con licencia adecuada.
- **Buscar y listar** pistas por sus metadatos.
- **Reproducir** una pista desde almacenamiento distribuido, y seguir dando servicio útil si cae una fuente y queda otra válida.
- **Retirar** una pista, por ejemplo si se descubre que no tenía una licencia adecuada. La retirada es definitiva.

No se ofrece **editar** una pista ya subida: para corregir algo, se sube de nuevo. La razón está en «Dónde viven los metadatos de cada pista»: sin edición, ningún dato guardado cambia nunca, y eso evita tener que ordenar escrituras entre réplicas.

Metadatos mínimos de una pista:

| Campo | Contenido |
|---|---|
| `title` | Título. |
| `artist` | Autor o intérprete. |
| `duration` | Duración en segundos, calculada al subir a partir de los segmentos de audio. |
| `license` | Licencia, obligatoria y de una lista cerrada: `CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`, `CC-BY-NC-4.0`, `public-domain`. Una subida sin licencia válida se rechaza con `Invalid`. |

La lista se puede ampliar sin afectar a ninguna otra decisión.

### Modelo de fallos

«Tolerante a fallos» solo significa algo si se dice a qué fallos. Se toleran:

- **Caídas de proceso con recuperación** (*crash-recovery*): un almacén puede detenerse de golpe y volver más tarde con lo que tenía guardado en disco.
- **Lentitud:** un almacén vivo puede tardar más de lo esperado en responder.
- **Corrupción de datos en disco**, que se detecta y se repara.
- **Almacenes inaccesibles** por un fallo de red o por pérdida de mensajes. Para el cliente, un almacén que no responde es indistinguible de uno caído, así que cuenta como caído: todas las garantías valen contando juntos los caídos y los inaccesibles. Dos clientes que alcanzan almacenes distintos nunca ven datos contradictorios, porque los objetos no cambian.

No se toleran comportamientos maliciosos (fallos bizantinos). Todos los almacenes corren en la misma máquina, así que un fallo de la máquina los afecta a todos a la vez (fallo correlacionado).

---

## Cuántas copias guardar de cada objeto y en cuántos almacenes

### El problema

Un almacén puede caer en cualquier momento, y lo que guarda deja de estar accesible. Replicar responde a dos preguntas distintas: si el sistema puede seguir **leyendo y escribiendo** con almacenes caídos (disponibilidad) y si un dato confirmado **sigue existiendo** después de esas caídas (durabilidad).

Hay que fijar S y N para un F dado. Además, las réplicas solo protegen si no fallan a la vez: dos copias en el mismo dominio de fallo cuentan como una ante un fallo correlacionado.

### Posibles soluciones

**Una réplica en cada almacén (S = N).** Es lo más simple y no hay que decidir dónde va cada copia. Pero si toda escritura debe llegar a todas las réplicas (lectura de una, escritura en todas), un solo almacén caído bloquea las escrituras. Y tras una caída no queda ningún almacén donde reponer la copia perdida.

**Quórums sobre un conjunto fijo de réplicas.** Una escritura espera a W réplicas y una lectura consulta R, con R + W > N para que toda lectura vea la última escritura. Es la solución estándar para datos que cambian. Con N = 3 y W = R = 2 tolera una caída pero no dos; tolerar dos exige N = 5 réplicas de cada objeto.

**Menos réplicas que almacenes, colocando cada réplica en almacenes vivos (S > N).** Cada objeto se escribe en N de los S almacenes, y si uno está caído se usa otro. Funciona sin quórums **solo si los objetos no cambian**: sin versiones, cualquier copia es correcta (se comprueba con su hash) y no hay lecturas desactualizadas que evitar, así que basta leer de una réplica. Para seguir funcionando con F caídas:

- leer exige N ≥ F + 1, para que quede viva al menos una réplica;
- escribir exige S − F ≥ N, para que queden almacenes vivos suficientes donde colocar las N réplicas.

Combinaciones de esta familia (caídas toleradas al leer / al escribir):

- **3 almacenes, 2 réplicas (1 / 1):** mientras se repone una réplica perdida, el objeto vive en un solo almacén, y una segunda caída en ese intervalo lo pierde.
- **4 almacenes, 3 réplicas (2 / 1):** con dos caídos ya no se puede escribir con 3 réplicas.
- **5 almacenes, 3 réplicas (2 / 2):** tolera dos caídas en todas las operaciones.
- **7 o más almacenes, 3 réplicas (2 / 4 o más):** más margen para escribir y reponer, pero la tolerancia al leer depende de N y no mejora.
- **5 almacenes, 4 o más réplicas (3 / 1):** más tolerancia al leer, a costa de espacio y de la capacidad de escribir con dos caídos.

### Decisión

**5 almacenes y 3 réplicas por objeto. Se lee de cualquier réplica válida y se escribe en 3 almacenes vivos.** Ambos números son configuración (`DTN_NUM_STORES`, `DTN_REPLICAS`).

Es la combinación más pequeña que tolera dos caídas tanto al leer como al escribir. Con dos réplicas, una segunda caída durante la reposición pierde datos, y por eso 3 réplicas es el valor habitual en almacenamiento distribuido. Los quórums no hacen falta, porque su objetivo (que las lecturas vean la última versión) no existe con objetos inmutables. Usarlos exigiría 5 réplicas de cada objeto para la misma tolerancia, un 67 % más de espacio sin ninguna garantía adicional.

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho 2 almacenes caídos, todo objeto confirmado sigue legible y toda escritura puede guardar sus 3 réplicas. |
| Límite | Con 3 o más caídos, algunas pistas no se pueden reproducir. El sistema responde `Unavailable` y nunca entrega datos incorrectos. |
| Supuesto | Todos los almacenes comparten máquina: se toleran caídas de proceso, no fallos correlacionados de la máquina. |

---

## Cómo sabe cada cliente qué almacenes existen

### El problema

Para leer o escribir, un cliente necesita saber qué almacenes forman el sistema y cómo llegar a cada uno. Conviene separar tres cosas: el **nombre lógico** de un almacén (`store-3`), el **proceso** que lo ejecuta en cada momento y la **dirección** donde escucha. Un almacén que se reinicia es el mismo almacén lógico en un proceso nuevo.

### Posibles soluciones

**Configuración estática.** La lista de nombres es configuración que todos los clientes comparten. No hay ningún componente extra que pueda fallar, pero cambiar el conjunto de almacenes exige cambiar la configuración, y una lista desactualizada puede durar indefinidamente.

**Registro de servicios.** Los almacenes se registran en un servicio central, con renovación periódica (*leases*) para eliminar a los que dejan de renovarse. Se adapta a cambios, pero el registro es en sí mismo un sistema distribuido: si es único es un punto único de fallo, y si se replica necesita coordinación entre sus réplicas.

**Pertenencia por gossip.** Cada almacén difunde lo que sabe de los demás y la información converge de forma probabilística. No tiene punto central, pero añade sospechas falsas, información desactualizada en circulación y números de encarnación para distinguir rumores viejos de estado actual.

### Decisión

**Configuración estática de los nombres lógicos.** Con un conjunto pequeño y controlado de almacenes, una lista fija es más simple y más correcta que un registro o un protocolo de gossip, y no introduce ningún componente nuevo que pueda fallar. Los clientes trabajan siempre con el nombre lógico, nunca con el proceso o la dirección, de modo que un almacén reiniciado sigue siendo el mismo para todos.

| Tipo | Declaración |
|---|---|
| Supuesto | El conjunto de almacenes no cambia mientras el sistema funciona, y todos los clientes tienen la misma lista. |

---

## Dónde viven los metadatos de cada pista

### El problema

Además de los bytes de sus fragmentos, cada pista necesita dos tipos de información:

- **Metadatos:** lo que se busca y se lee (título, artista, licencia, duración).
- **Manifiesto:** la receta para reconstruir la pista, es decir, la lista ordenada de sus fragmentos, cada uno identificado por su SHA-256.

Sin el manifiesto, los fragmentos son bytes sueltos. La pregunta de fondo es dónde vive la autoridad sobre este estado y qué pasa con él cuando cae el proceso que lo guarda: un proceso guarda su estado en memoria, y si muere sin haberlo replicado, ese estado desaparece.

Si toda la información de las pistas viviera en un solo nodo y este cayera, los fragmentos quedarían huérfanos y no se podría buscar, listar, subir ni empezar a reproducir, aunque todos los datos siguieran en los almacenes. Una reproducción ya empezada sí terminaría, porque el cliente ya tiene el manifiesto.

### Por qué importa que los datos no cambien

Replicar estado que **cambia** obliga a que todas las réplicas apliquen los cambios en el mismo orden. Si dos clientes editan el título casi a la vez y cada réplica recibe las ediciones en distinto orden, las réplicas terminan con versiones distintas. Evitarlo requiere un orden autoritativo de escrituras: un líder que lo fije y, si el líder puede caer, consenso para elegir otro sin que dos nodos se crean líderes a la vez.

Replicar estado que **nunca cambia** no tiene ese problema: una réplica tiene el dato o no lo tiene, pero nunca una versión distinta. Los fragmentos son inmutables por construcción, porque su identificador es el hash de su contenido. Los manifiestos y los metadatos también lo son si no se permite editar.

### Posibles soluciones

**Un catálogo central.** Un nodo guarda metadatos y manifiestos. Es lo más simple, y la búsqueda se hace en una sola consulta. Pero es un punto único de fallo: su caída deja el servicio inutilizable.

**Un catálogo replicado, escribiendo en todas sus réplicas.** Dos o más nodos catálogo idénticos: se escribe en todos y se lee de cualquiera. Tolera la caída de un catálogo y funciona sin conflictos porque lo que guarda no cambia. A cambio, añade otro tipo de nodo, un segundo mecanismo de replicación distinto del de los fragmentos y una decisión más: qué hacer con una subida mientras un catálogo está caído.

**Un catálogo con primario y respaldo.** Un primario recibe todas las escrituras y fija su orden, y las reenvía a un respaldo. Es lo necesario si los metadatos se pueden editar. Pero cuando el primario cae hay que decidir que el respaldo lo sustituya sin que ambos se crean primarios a la vez (*split brain*). Hacerlo bien es un problema de consenso.

**Metadatos y manifiestos guardados en los propios almacenes.** El manifiesto, con los metadatos dentro, es un objeto inmutable más, identificado por su hash y replicado igual que los fragmentos. Para buscar, el cliente consulta a todos los almacenes a la vez y reúne las respuestas (*scatter/gather*). No añade ningún nodo ni ningún mecanismo nuevo de replicación. A cambio, buscar y listar implica consultar a todos los almacenes, y hay que fijar un plazo y saber qué hacer si alguno no responde.

### Decisión

**Metadatos y manifiestos se guardan en los almacenes, replicados como un objeto más.**

Cumple el requisito de seguir dando servicio si cae una fuente en todas las operaciones (subir, buscar, listar, reproducir y retirar), con el mismo mecanismo de replicación que ya hacía falta para los fragmentos. Responde a la pregunta de quién posee el estado sin introducir ninguna autoridad central, y evita el consenso porque el modelo de datos elimina la necesidad de ordenar escrituras: la forma más barata de coordinar réplicas es no necesitar coordinarlas.

Funcionamiento:

1. El identificador de una pista es el SHA-256 de su manifiesto.
2. **Buscar y listar:** el cliente consulta a todos los almacenes a la vez, con plazo. Cada almacén filtra sus manifiestos, y el cliente une las respuestas, elimina los repetidos y descarta las pistas retiradas.
3. **Reproducir:** el cliente obtiene el manifiesto de cualquier almacén que lo tenga.

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho 2 almacenes caídos, la búsqueda devuelve todas las pistas confirmadas y no retiradas. |
| Supuesto | Los metadatos no se editan. |

Si en el futuro hiciera falta editar metadatos, esta decisión dejaría de ser correcta: las réplicas podrían divergir, y la opción adecuada pasaría a ser un catálogo con orden único de escrituras (primario y respaldo con consenso).

---

## Cómo se escriben los manifiestos y las lápidas

### El problema

El identificador de una pista es el hash de su manifiesto, y el de una lápida, el hash de su contenido. Si dos clientes escriben el mismo manifiesto con bytes distintos (otro orden de los campos, otros espacios, otra forma de escribir un número), obtienen identificadores distintos para la misma pista, y la repetición segura de una subida deja de funcionar. Además, cada almacén tiene que distinguir manifiestos y lápidas de los segmentos para poder buscar entre ellos.

### Posibles soluciones

**JSON sin más reglas.** Fácil de leer y de escribir, pero el mismo contenido admite muchas escrituras distintas.

**Un formato binario propio.** Una sola forma de escribir cada contenido, pero hay que programar y documentar el formato, y deja de poder leerse a simple vista.

**JSON canónico:** claves ordenadas, sin espacios, en UTF-8, con números enteros o decimales escritos siempre igual. Se lee a simple vista y el mismo contenido produce siempre los mismos bytes.

### Decisión

**JSON canónico, con un campo `type` que distingue manifiestos y lápidas.**

- Claves en orden alfabético, sin espacios, codificado en UTF-8. Las duraciones se guardan en milisegundos enteros, para no depender de cómo se escriben los decimales.
- **Manifiesto:** `{"artist", "duration_ms", "init", "license", "original", "segments": [{"duration_ms", "id"}, …], "title", "type": "manifest", "version": 1}`.
- **Lápida:** `{"original", "reason", "track_id", "type": "tombstone", "version": 1}`, donde `original` es el hash del archivo original de la pista.
- Al guardar un objeto, el almacén intenta leerlo como JSON con un `type` conocido. Si lo consigue, lo añade a su índice de manifiestos o de lápidas; si no, es un segmento o un original, y lo trata como bytes opacos. Las lápidas se indexan por `track_id` y por `original`.

| Tipo | Declaración |
|---|---|
| Corrección | Con una misma implementación, el mismo contenido de un manifiesto o de una lápida produce siempre el mismo identificador, lo escriba el cliente que lo escriba. |
| Límite | Dos implementaciones distintas pueden escribir el mismo contenido con bytes distintos y obtener identificadores distintos. |

---

## Cómo se normalizan los textos de una pista

### El problema

El identificador de una pista depende de los bytes de su título y su artista. Un mismo texto puede llegar con bytes distintos: «Café» puede escribirse con la «é» como un solo carácter o como «e» seguida de un acento combinado, según el sistema o el teclado, y a veces con espacios de más al principio o al final. Sin tratarlo, la misma subida hecha desde dos máquinas produce dos pistas distintas y la repetición segura deja de funcionar entre clientes.

### Posibles soluciones

**Guardar el texto tal como llega.** Respeta exactamente lo escrito, pero el mismo título puede dar dos identificadores.

**Normalizar a NFC y quitar los espacios de los extremos.** NFC es la forma estándar de Unicode que escribe cada letra acentuada como un solo carácter. El texto que se ve es el mismo, y los bytes, también.

**Normalizar además mayúsculas y tildes.** También unificaría «Cafe» y «CAFÉ», pero cambia lo que el usuario escribió y lo que se muestra.

### Decisión

**NFC y sin espacios en los extremos.** Se aplica al validar los metadatos y al motivo de una retirada, antes de construir el manifiesto o la lápida. Los límites de longitud se miden después de normalizar. `decode_object` solo reconoce manifiestos y lápidas con los textos ya normalizados, para que un mismo contenido no tenga dos formas válidas. Las mayúsculas y las tildes solo se ignoran al buscar (ver «Cómo se busca una pista»), no al guardar.

| Tipo | Declaración |
|---|---|
| Corrección | El mismo título, artista o motivo escrito con distinta composición Unicode o con espacios en los extremos produce el mismo identificador. |

---

## Cómo se busca una pista

### El problema

Buscar es filtrar los manifiestos de cada almacén y unir las respuestas (ver «Dónde viven los metadatos de cada pista»). Falta fijar qué significa que una pista «coincide» con una consulta y en qué orden se devuelven los resultados. Como cada almacén filtra los suyos y el cliente une las respuestas, la regla tiene que dar lo mismo se aplique donde se aplique, y el resultado tiene que ser el mismo sea cual sea el orden en que respondan los almacenes.

### Posibles soluciones

**Coincidencia exacta por campo** (`artist = "Ana"`). Simple y sin ambigüedad, pero obliga a escribir el nombre exacto, con sus mayúsculas y tildes.

**El texto de la consulta aparece dentro del título o del artista.** Más cómodo, pero «ana mar» no encuentra «Mar de Ana».

**Cada palabra de la consulta aparece dentro del título o del artista, sin distinguir mayúsculas ni tildes.** Encuentra la pista aunque las palabras estén en otro orden o en otro campo, y sigue siendo una función pura, igual en todos los almacenes.

**Índice de texto completo con relevancia.** Ordena por parecido, pero cada almacén puntuaría con sus propias estadísticas, así que unir respuestas de varios almacenes daría órdenes distintos según cuáles respondan. Es mucha maquinaria para un catálogo pequeño.

### Decisión

**Cada palabra de la consulta debe aparecer dentro de `title` o de `artist`, sin distinguir mayúsculas ni tildes. La consulta vacía coincide con todo. Los resultados se ordenan por `artist`, luego `title`, luego identificador.**

- La regla es la función pura `matches(metadata, query)` que ya prevé «Qué interfaz se ofrece».
- El orden lo aplica el cliente después de unir y quitar repetidos. Con un orden total, el mismo conjunto de pistas se devuelve siempre igual, respondan los almacenes en el orden que respondan.
- Una consulta de más de 200 caracteres se rechaza con `Invalid`.

| Tipo | Declaración |
|---|---|
| Corrección | El resultado no depende del orden en que respondan los almacenes. |

---

## Cuándo se considera completa una subida

### El problema

Una subida escribe muchos objetos (segmentos de audio, segmento inicial, original y manifiesto) en varios almacenes. Durante ese proceso pueden caer el cliente o los almacenes. Hay que decidir en qué momento la pista pasa a existir para los demás y qué significa la respuesta que recibe quien sube.

Además, cuando una escritura remota no responde, el cliente no sabe qué ocurrió: puede que la petición no llegara, que fallara antes de aplicarse, o que se aplicara y se perdiera la respuesta. Un plazo acota la espera, pero no demuestra que el efecto no ocurriera. El momento en que algo es durable y el momento en que el cliente recibe la confirmación no tienen por qué coincidir.

### Posibles soluciones

**Escribir el manifiesto primero.** La pista se vuelve visible antes de que existan sus fragmentos: una subida interrumpida deja una pista rota a la vista.

**Escribir el manifiesto al final, como punto de confirmación.** Los fragmentos se escriben antes, así que una pista solo puede verse cuando ya están todos guardados. Una subida interrumpida deja fragmentos sueltos que nadie ve.

**Commit atómico del manifiesto entre réplicas (dos fases).** El manifiesto aparece en todas sus réplicas o en ninguna. Pero exige un coordinador y un estado «preparado» durable en cada almacén, y si el coordinador cae en el momento equivocado los almacenes quedan bloqueados esperando su decisión.

**Repetición idempotente.** La subida se identifica por su contenido: subir lo mismo produce siempre los mismos identificadores. Un error significa «resultado incierto», y repetir la subida es seguro, porque no crea duplicados y completa las réplicas que falten.

### Decisión

**El manifiesto se escribe al final, como punto de confirmación, y la incertidumbre se resuelve con repetición idempotente, sin commit atómico.**

1. Primero se escriben, con todas sus réplicas, los segmentos de audio, el segmento inicial y el original.
2. Solo entonces se escribe el manifiesto, con todas sus réplicas.
3. Se distinguen dos momentos. La pista es **visible** desde que un almacén guarda su manifiesto, y **confirmada** cuando el manifiesto tiene todas sus réplicas. La subida responde con el identificador solo al confirmar.
4. Si la escritura del manifiesto falla a mitad, algún almacén puede tenerlo ya y la pista aparecer en las búsquedas aunque quien sube haya recibido un error. Por eso un error en la subida significa **resultado incierto**, no «no ocurrió». Impedir que una pista sea visible antes de confirmarse exigiría un commit atómico o un punto único que decida qué es visible: cualquier marca de confirmación escrita en varios almacenes puede quedarse a medias igual que el manifiesto.
5. El identificador de cada objeto, y por tanto de la pista, es el hash de su contenido. Funciona como clave de idempotencia: repetir una subida con resultado incierto no crea duplicados y completa las réplicas que falten. La reparación de réplicas también las completa.

El commit atómico en dos fases se descarta: puede bloquear y añadiría un coordinador, cuando la repetición idempotente llega al mismo resultado final sin coordinación. Ningún invariante de esta operación cruza fronteras de autoridad que obliguen a una transacción distribuida.

| Tipo | Declaración |
|---|---|
| Corrección | Toda pista visible tiene todos sus fragmentos guardados con todas sus réplicas. |
| Límite | Una pista puede ser visible sin estar confirmada: mientras se escribe su manifiesto, o tras una subida fallida, hasta que se repite o la reparación completa las réplicas del manifiesto. |
| Semántica | Un error al subir significa resultado incierto: la pista puede haber quedado visible. Repetir la subida es seguro y la completa. |

---

## Cómo se retira una pista

### El problema

La exigencia de licencia adecuada implica poder retirar una pista publicada sin derechos. Retirar es un cambio sobre datos ya guardados en varias réplicas, justo lo que el resto del diseño evita.

### Posibles soluciones

**Borrar el manifiesto de todas sus réplicas.** Si una réplica está caída durante el borrado y vuelve después, o la reparación de réplicas la vuelve a copiar, la pista reaparece.

**Un estado «retirada» modificable en un catálogo con orden de escrituras.** Funciona, pero devuelve el problema de ordenar escrituras entre réplicas.

**Lápidas.** Retirar consiste en guardar un objeto inmutable más, «la pista X está retirada», replicado como cualquier otro. La retirada es **monótona**: una vez retirada, una pista no vuelve a estar disponible. Por eso no necesita orden: dos réplicas pueden ir con retraso (una tiene la lápida y otra todavía no), pero nunca contradecirse.

### Decisión

**Retirada con lápidas.**

1. La lápida es un objeto inmutable, identificado por su hash. Lleva el identificador de la pista y el hash de su archivo original.
2. **Se retira la grabación, no solo la pista.** El problema de una licencia es del audio, no del título: la lápida retira también cualquier otra pista con el mismo archivo original, ya subida o que se suba después.
3. **Se replica en todos los almacenes, no solo en N.** El cliente la escribe en todos los que responden y confirma cuando la tienen N; la reparación la copia después a los que faltan. Una lápida ocupa unos cientos de bytes, y así, pasada la reparación, cualquier almacén vivo conoce la retirada, aunque haya más de 2 caídos.
4. La búsqueda devuelve también las lápidas, y el cliente descarta las pistas retiradas y las que tienen el mismo original.
5. Antes de empezar a reproducir, el cliente comprueba que no haya lápida (ver «Cómo encuentra el cliente la lápida de una pista»). Una reproducción ya empezada termina normalmente.
6. Antes de escribir una lápida, el cliente comprueba si ya hay alguna para la pista o su original. Si la hay, no escribe otra: retirar una pista ya retirada no cambia nada, aunque el motivo sea distinto.

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho 2 almacenes caídos, una pista cuya retirada se confirmó no aparece en ninguna búsqueda completa posterior, y tampoco ninguna otra con el mismo archivo original. |
| Objetivo | Tras la reparación, cada lápida está en todos los almacenes, y basta con que responda uno para ver la retirada. |
| Límite | El original se reconoce por su hash: recodificar o modificar el archivo esquiva la retirada. |
| Supuesto | Una retirada no se deshace. Deshacerla sería un cambio no monótono y volvería a exigir orden entre réplicas. |

---

## Qué pasa al volver a subir una pista retirada

### El problema

El identificador de una pista es el hash de su manifiesto. Si alguien vuelve a subir exactamente el mismo audio con los mismos metadatos que una pista retirada, obtiene el mismo identificador, y ese identificador ya tiene lápida. Si cambia los metadatos, obtiene otro identificador, pero el archivo original es el mismo, y la retirada es de la grabación (ver «Cómo se retira una pista»). Hay que decidir qué responde la subida y cuándo lo comprueba.

### Posibles soluciones

**Aceptar la subida en silencio.** Se escriben objetos que ya existen y se responde con el identificador. La pista sigue retirada, porque la lápida no se borra, pero quien sube cree que su pista es visible.

**Reactivar la pista.** La subida anula la lápida. Rompe la regla de que una retirada es definitiva y monótona, y con ello vuelve a exigir orden entre réplicas (ver «Cómo se retira una pista»).

**Rechazar la subida con `Conflict`.** La subida comprueba si existe lápida para ese identificador o ese original. Quien sube sabe que su pista no será visible y por qué.

### Decisión

**Rechazar con `Conflict`.**

1. **Antes de escribir nada**, la subida busca lápidas del identificador o del archivo original, con `tombstones(track_id, original)` enviada a todos los almacenes con plazo. Es posible porque el manifiesto, y por tanto el identificador, se construye en cuanto termina la recodificación: solo contiene hashes y metadatos.
2. Si encuentra alguna, responde `Conflict` sin haber escrito nada. Comprobarlo al final dejaría hasta 200 MB por réplica como basura hasta que pasara la limpieza.
3. Si no responden N o más almacenes, no puede afirmar que no haya lápida y responde `Unavailable`, también sin escribir nada.
4. Una retirada que llegue mientras la subida escribe no se pierde: su lápida retira la pista igualmente cuando se confirma.

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho 2 almacenes caídos, una subida nunca confirma una pista cuya retirada, o la de otra con el mismo original, se confirmó antes de empezar la subida. |

---

## Quién puede retirar una pista

### El problema

Retirar es definitivo. Si cualquiera puede hacerlo, cualquiera puede borrar del catálogo la pista de otro.

### Posibles soluciones

**Cualquiera.** No hay nada que construir, pero no protege contra el abuso.

**Solo quien la subió.** Exige identificar a quien sube y guardar esa identidad con la pista, por ejemplo con una clave pública dentro del manifiesto y una firma en la lápida.

**Solo un administrador.** Exige credenciales y decidir dónde viven y cómo se comprueban en cada almacén.

### Decisión

**En la Fase 1, cualquiera.** La autenticación y los permisos forman parte del modelo de seguridad de la Fase 2. La opción «solo quien la subió» encaja con los objetos inmutables: la clave pública iría en el manifiesto y la firma en la lápida, sin estado que cambie.

| Tipo | Declaración |
|---|---|
| Límite | Cualquiera que use el sistema puede retirar cualquier pista. |

---

## Cómo encuentra el cliente la lápida de una pista

### El problema

Antes de reproducir, antes de empezar a escribir una subida y antes de retirar, el cliente tiene que saber si una pista, o su archivo original, está retirada. Pero el identificador de la lápida es el hash de su contenido, que incluye el motivo, así que el cliente no lo conoce: solo conoce el identificador de la pista.

### Posibles soluciones

**Buscar en todo el catálogo y filtrar.** Reutiliza `search`, pero trae todos los manifiestos de todos los almacenes para responder una pregunta sobre una sola pista.

**Calcular el identificador de la lápida.** Solo es posible si la lápida no lleva motivo ni ningún otro dato variable, y entonces se pierde el motivo de la retirada.

**Una consulta por identificador de pista.** Cada almacén indexa sus lápidas por el `track_id` y el `original` que contienen (ver «Cómo se escriben los manifiestos y las lápidas») y responde si tiene alguna para esa pista o ese original. El cliente pregunta a todos los almacenes a la vez, con plazo.

### Decisión

**Una consulta por identificador de pista y por archivo original, `tombstones(track_id, original)`, enviada a todos los almacenes.**

- Basta con que un almacén responda con una lápida válida (su hash coincide y su `track_id` o su `original` es el pedido) para que la pista esté retirada.
- Si ninguno la tiene, la pista solo se considera no retirada si no respondieron menos de N almacenes, con la misma regla que en «Cuándo se responde «no existe» y cuándo «no disponible»».
- Si respondieron menos, la comprobación no es concluyente. `upload` responde `Unavailable` sin escribir nada. `play`, en cambio, empieza a reproducir y lo indica en la sesión: es el mismo riesgo que ya acepta una búsqueda con `complete = false`, y el requisito de seguir sirviendo mientras quede una fuente válida pesa más. Como las lápidas acaban en todos los almacenes (ver «Cómo se retira una pista»), solo suena una pista retirada si la retirada es tan reciente que la reparación aún no la ha copiado y, además, faltan N almacenes o más.

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho 2 almacenes caídos, el cliente nunca toma por vigente una pista cuya retirada se confirmó. |
| Límite | Con 3 o más almacenes caídos, `play` puede reproducir una pista retirada, y lo indica. |

---

## Cómo se divide el audio en fragmentos

### El problema

Una pista se guarda y se transmite por fragmentos. La forma de dividirla decide si cada fragmento se puede decodificar por sí solo (y por tanto si se puede empezar en cualquier punto), cuánto audio contiene cada fragmento, si el búfer del reproductor se puede medir en segundos exactos y cuántos bytes de más añade el contenedor, que se multiplican por el número de réplicas.

Con fragmentos cortos, la reproducción empieza antes y un cambio de fuente repite poco. Con fragmentos largos hay menos peticiones y un manifiesto más corto.

### Posibles soluciones

**Por bytes, con tamaño fijo.** Independiente del formato y simple. Pero los cortes no coinciden con las tramas del audio: ningún fragmento se decodifica por sí solo, no se puede empezar a mitad de la pista y la duración de cada fragmento solo se puede estimar.

**Por tiempo, conservando el formato original.** Sin pérdida de calidad, pero cada formato se corta de forma distinta, algunos no se pueden cortar sin recodificar, y el cliente tendría que admitirlos todos.

**Por tiempo, recodificando a un formato único.** Cada segmento se decodifica por sí solo y tiene una duración conocida, y el cliente solo necesita un formato. La recodificación tiene pérdida. Dentro de esta familia, el códec y el contenedor importan. Medición con 60 s de audio, tasa objetivo de 128 kbit/s y segmentos de 2 s:

| Códec y contenedor | Bytes de más del contenedor | Segmentos decodificables | Reproducción continua |
|---|---|---|---|
| AAC en MPEG-TS | ≈ 24 % | 31/31 | sin huecos |
| AAC en ADTS | ≈ 0 % | 30/31: el último no contiene audio | sin huecos |
| Opus en Ogg | ≈ 0 % | 31/31 | **con huecos**: cada segmento descarta muestras al empezar |
| Opus en MPEG-TS | ≈ 48 % | 31/31 | sin huecos |
| AAC en fMP4 (CMAF) | ≈ 0 % | 31/31, con su segmento inicial | sin huecos |
| Opus en fMP4 (CMAF) | ≈ 0 % | 31/31, con su segmento inicial | sin huecos |

**Varias calidades por pista** (tasa de bits adaptativa): el cliente baja de calidad si la red empeora. Solo aporta algo cuando el ancho de banda varía.

**Según el contenido** (hash rodante, para deduplicar trozos repetidos): el audio casi nunca comparte trozos, y los cortes tampoco coinciden con las tramas.

### Decisión

**Opus en segmentos fMP4 según CMAF, el formato de segmentos común a HLS y DASH, de `DTN_SEGMENT_SECONDS` (2 s por defecto) y a una tasa objetivo de `DTN_AUDIO_BITRATE` (128 kbit/s por defecto). Cada segmento es un fragmento.**

1. Al subir, `ffmpeg` recodifica la pista y produce un **segmento inicial** (unos 730 bytes, con la descripción del códec) y los **segmentos de audio**. Cualquier segmento de audio se decodifica anteponiéndole el inicial.
2. El manifiesto guarda el identificador del segmento inicial y, en orden, el identificador y la duración de cada segmento de audio. Las duraciones salen de la lista de reproducción del empaquetador HLS de `ffmpeg`.
3. Al subir se comprueba que cada segmento se decodifica junto con el inicial. Si alguno falla, la subida se rechaza con `Invalid`.
4. **El archivo original se conserva** como un objeto más, sin usarlo para reproducir. Permite volver a recodificar en el futuro, por ejemplo para añadir varias calidades, sin pedir de nuevo el archivo.

Por qué este formato:

- fMP4 casi no añade bytes, frente al 24 % o 48 % de MPEG-TS, y ese ahorro se multiplica por las 3 réplicas.
- Opus da mejor calidad por bit, está libre de patentes y en fMP4 se reproduce sin huecos.
- Las varias calidades no se adoptan porque, con todos los procesos en una máquina, el ancho de banda no varía. Conservar el original permite añadirlas si ese supuesto cambia.

| Tipo | Declaración |
|---|---|
| Corrección | Todo segmento de audio confirmado se puede decodificar junto con el segmento inicial de su pista. |
| Supuesto | Lo que se reproduce es la versión recodificada, no el archivo subido byte a byte. El original se conserva aparte. |

---

## Cómo se garantiza que repetir una subida produzca lo mismo

### El problema

Repetir una subida con resultado incierto es seguro porque produce los mismos identificadores (ver «Cuándo se considera completa una subida»). Pero los segmentos no son el archivo subido: son el resultado de recodificarlo. Si recodificar dos veces el mismo archivo diera bytes distintos, repetir la subida crearía segmentos nuevos, un manifiesto nuevo y otra pista con otro identificador.

### Posibles soluciones

**Confiar en que la recodificación es determinista y comprobarlo.** No cambia nada del diseño, pero depende del comportamiento de `ffmpeg` y de su codificador.

**Identificar la pista por el hash del original y los metadatos, no del manifiesto.** El identificador no dependería de la recodificación, pero dos intentos podrían dejar dos juegos de segmentos distintos para la misma pista, y habría que decidir cuál es el bueno.

**Recodificar una sola vez y reutilizar el resultado.** El cliente guarda localmente los segmentos del primer intento y los reutiliza al repetir. No protege si el cliente que repite es otro o perdió su copia local.

### Decisión

**Recodificación determinista, comprobada y con la versión de `ffmpeg` fijada.**

- Se comprobó codificando dos veces el mismo audio con `ffmpeg` 6.1.1 (Opus a 128 kbit/s, segmentos fMP4 de 2 s): el segmento inicial, los 7 segmentos y la lista de reproducción coinciden byte a byte.
- Otra versión de `ffmpeg` o del codificador Opus puede producir otros bytes. La versión forma parte de la configuración y se registra al arrancar (principio P6).

| Tipo | Declaración |
|---|---|
| Supuesto | Todos los clientes usan la misma implementación y la misma versión de `ffmpeg`. Con versiones o implementaciones distintas, la misma subida puede producir pistas distintas, aunque nunca una pista incorrecta. |

---

## En qué almacenes se coloca cada réplica

### El problema

Cada objeto debe quedar en N de los S almacenes. Hay que responder a tres preguntas: en qué almacenes se escribe (y qué pasa si uno está caído), cómo sabe el lector dónde buscar, y dónde se crea una réplica nueva cuando hay que reponer una perdida. La colocación y la forma de encontrar los datos están relacionadas, pero son mecanismos distintos.

### Posibles soluciones

**Módulo del hash.** El objeto va al almacén `hash mod S` y a los siguientes. El lector calcula la ubicación sin información extra. Pero si uno de esos almacenes está caído al escribir, hay que rechazar la escritura o escribir donde la regla no dice. Y si cambia S, casi todos los objetos cambian de ubicación.

**Ubicación apuntada en el manifiesto.** Quien sube elige almacenes vivos y los apunta. El lector sabe exactamente dónde leer. Pero el manifiesto es inmutable: reponer una réplica en otro almacén lo deja desactualizado, así que en la práctica impide reponer.

**Al azar, sin apuntarlo.** Escribir es trivial, pero el lector tiene que preguntar a todos los almacenes por cada objeto.

**Directorio de ubicaciones.** Un servicio guarda dónde está cada réplica y se actualiza al reponer. Es flexible, pero el directorio es estado que cambia y debe estar replicado: los metadatos de encaminamiento se convierten en estado distribuido, con el problema de orden que el resto del diseño evita.

**Hashing consistente.** Almacenes y objetos se colocan en un anillo de identificadores, y cada objeto va a su sucesor y a los siguientes. Al añadir o quitar un almacén solo se mueven los objetos de su tramo. Con pocos almacenes el reparto es desigual si no se usan nodos virtuales.

**Rendezvous hashing (*highest random weight*).** Cada objeto ordena **todos** los almacenes según una puntuación `hash(almacén, objeto)`, y las réplicas van a los primeros vivos del orden. Cualquiera calcula el mismo orden, así que no hay ubicaciones que guardar. Al añadir o quitar un almacén solo se mueven los objetos que lo tenían entre sus primeros. El reparto es uniforme sin nodos virtuales, y reponer es natural: la réplica nueva va al siguiente del orden, que es donde la buscará el lector. Si al escribir se saltó un almacén caído, la réplica queda más abajo en el orden, y el lector tiene que seguir bajando.

### Decisión

**Rendezvous hashing.**

1. La puntuación de cada almacén para un objeto es el SHA-256 de su nombre lógico concatenado con el identificador del objeto. Los almacenes se ordenan de mayor a menor puntuación. Como se usa el nombre lógico, el orden no cambia si un almacén se reinicia.
2. **Escritura:** se intenta en ese orden, con plazo, hasta tener N réplicas confirmadas. Un almacén caído o sospechoso se salta; uno saturado se reintenta (ver «Qué hace un almacén cuando está al máximo de peticiones»).
3. **Lectura:** se sigue el mismo orden (ver «De qué réplica se lee»). Si fallan los primeros, se sigue bajando, y así se encuentran también las réplicas escritas tras un salto.
4. **Reposición:** la réplica que falte se crea en el primer almacén vivo del orden que no la tenga.
5. Los manifiestos, segmentos iniciales y originales usan su propio identificador. Las lápidas no siguen este orden: van a todos los almacenes (ver «Cómo se retira una pista»).

Frente al hashing consistente, ofrece las mismas propiedades ante cambios en el conjunto de almacenes y un reparto uniforme con pocos almacenes sin necesidad de nodos virtuales. Su coste, un hash por almacén y objeto, es irrelevante con 5 almacenes. Si el número de almacenes llegara a cientos, convendría pasar a un anillo con nodos virtuales, cuyas búsquedas son logarítmicas.

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho S − N almacenes caídos (2), toda escritura guarda sus N réplicas. |
| Corrección | Todos los clientes calculan el mismo orden, y como incluye todos los almacenes, quien lo recorre entero encuentra cualquier réplica confirmada que siga viva. |
| Límite | Con más de S − N almacenes caídos, las subidas fallan con `Unavailable`. |

---

## Cómo se reproduce una pista

### El problema

El requisito central es mantener un servicio útil durante la reproducción aunque caiga una fuente. Para demostrarlo hay que reproducir de verdad y, a la vez, **medir** cada corte: cuándo se queda el reproductor sin audio y durante cuánto tiempo.

### Posibles soluciones

**Consumo simulado a ritmo fijo.** Medible y reproducible, pero no suena: no demuestra que el audio entregado se pueda reproducir.

**Sonido real con un reproductor externo.** Suena con poco código, pero el reproductor tiene su propio búfer interno y no se puede saber con precisión cuándo se queda sin datos.

**Sonido real con decodificación propia y reloj propio.** El cliente controla cuándo entrega las muestras al dispositivo de audio, así que suena y cada corte se mide con exactitud. Requiere gestionar el proceso decodificador y la salida de audio.

**Descargar la pista completa y luego reproducir.** No es streaming: no existe el concepto de corte.

### Decisión

**Sonido real con decodificación propia y reloj propio.**

1. El cliente lanza `ffmpeg` como proceso hijo y le escribe el segmento inicial y, después, los segmentos de audio en orden. `ffmpeg` devuelve el audio decodificado (PCM).
2. El cliente entrega el PCM a la salida de audio del sistema, a la frecuencia de muestreo de la pista.
3. Un **corte** es el momento en que la salida de audio pide muestras y no hay. Se registra cuándo empieza y cuánto dura.
4. **Modo sin altavoces:** mismo reloj y misma medición, pero las muestras se descartan. Sirve para experimentos automáticos.
5. **Verificación:** cada segmento entregado ha pasado la comprobación de su hash, y al terminar se comprueba que se entregaron todos los del manifiesto, en orden y sin repetir.

---

## Cuánto audio se pide por adelantado

### El problema

Pedir un fragmento justo cuando se necesita detiene la reproducción en cada petición. Hay que pedir por adelantado, pero de forma acotada, porque pedir sin límite satura a los almacenes.

### Posibles soluciones

**Un fragmento cada vez:** cada lectura detiene la reproducción. Es el error de esperar el resultado de cada petición remota justo después de enviarla.

**Ventana deslizante de K fragmentos:** mientras suena uno, los siguientes ya vienen en camino. Pero el riesgo de corte depende del tiempo de audio en el búfer, no del número de fragmentos.

**Ventana deslizante medida en segundos de audio:** protege siempre el mismo tiempo, sea cual sea la duración de los fragmentos. Requiere conocer la duración de cada uno.

**Descargar todo antes de empezar:** sin cortes, pero no es streaming.

### Decisión

**Ventana deslizante medida en segundos de audio.**

- El cliente mantiene pedidos o recibidos los fragmentos necesarios para cubrir `DTN_BUFFER_SECONDS` de audio por delante del punto de reproducción. Ese límite es también su forma de contrapresión: nunca hay más peticiones en vuelo que las que cubren la ventana.
- Empieza a sonar cuando tiene `DTN_PREROLL_SECONDS` de audio recibido.
- La duración de cada fragmento se lee del manifiesto.
- Los fragmentos llegan en cualquier orden y se recogen según terminan. El decodificador solo recibe el siguiente en orden.

---

## De qué réplica se lee

### El problema

Cada fragmento tiene N réplicas. Elegir mal significa esperar a un almacén lento o caído mientras el búfer se vacía. La latencia de cola, la de las peticiones más lentas, decide si hay cortes.

### Posibles soluciones

**La primera del orden, evitando las que fallaron:** simple, pero ante un almacén lento se tarda un plazo completo en reaccionar.

**Una al azar:** reparte la carga, pero no mejora la anterior ante fallos.

**Pedir siempre a dos réplicas y usar la primera respuesta:** un fallo no cuesta nada, pero duplica todas las lecturas.

**Peticiones duplicadas diferidas, con cancelación** (*hedged requests*): se pide a una réplica y, si no responde tras un retraso corto, también a otra. Se usa la primera respuesta y se cancela la otra. Obtiene casi todo el beneficio de duplicar con muy poca carga extra, pero requiere estimar la latencia normal.

**La réplica que respondió más rápido últimamente:** se adapta a almacenes lentos, pero reacciona tarde a un fallo repentino.

### Decisión

**Peticiones duplicadas diferidas, con cancelación.**

1. Se pide el fragmento al primer almacén del orden de colocación que no esté marcado como sospechoso.
2. Si no responde tras un retraso **d**, se pide también al siguiente almacén no sospechoso.
3. Se usa la primera respuesta válida y se cancela la otra petición. Cancelar no deshace nada, y no hace falta: leer no tiene efectos.
4. **d** se calcula a partir de la latencia observada (ver «Cómo se detecta que un almacén falla»). Fijar `DTN_HEDGE_DELAY_MS=0` equivale a pedir siempre a dos réplicas.

En la medición original de esta técnica sobre BigTable, duplicar tras 10 ms de espera redujo el percentil 99,9 de latencia de 1800 ms a 74 ms, con solo un 2 % más de peticiones.

---

## Cómo se detecta que un almacén falla

### El problema

Una lectura puede fallar porque el almacén murió o porque está vivo pero no responde a tiempo. Desde fuera no siempre se pueden distinguir: un plazo vencido es una decisión local, no la prueba de un hecho remoto. Detectar fallos es, en el fondo, sospechar.

### Posibles soluciones

**Solo los avisos de muerte del entorno de ejecución:** sin configuración, pero con un almacén lento la reproducción espera indefinidamente.

**Avisos de muerte y plazo fijo:** cubre los dos casos, pero un valor fijo es demasiado largo cuando todo va bien y puede ser demasiado corto bajo carga.

**Avisos de muerte y detector adaptativo, con sospecha y readmisión:** los plazos se ajustan a la latencia real de cada almacén, y un almacén lento que se recupera vuelve a usarse. Requiere mantener estimaciones por almacén.

### Decisión

**Avisos de muerte y detector adaptativo, con sospecha y readmisión.**

- **Estimación por almacén** con el método de Jacobson y Karels, el de las retransmisiones de TCP: una media suavizada (SRTT) y una desviación suavizada (RTTVAR) de los tiempos de respuesta.
- **Retraso de duplicación:** d = SRTT + 2·RTTVAR, aproximadamente el percentil 95 de la latencia normal.
- **Plazo de sospecha:** SRTT + 4·RTTVAR, acotado entre `DTN_TIMEOUT_MIN_MS` y `DTN_TIMEOUT_MAX_MS`. Un almacén que lo supera, o cuya petición falla porque murió, pasa a **sospechoso** y deja de ser la réplica preferida.
- **Readmisión:** un almacén sospechoso recibe periódicamente una petición de prueba y vuelve a la lista normal si responde dentro del plazo.
- **Reloj:** todas las duraciones se miden con un reloj monótono, que no salta cuando se corrige la hora del sistema.

Los valores se ajustarán con experimentos, no por elección a priori.

---

## Qué hace el reproductor cuando una lectura falla

### El problema

Cuando una réplica falla hay que decidir cuánto insistir y cómo, sin entregar nunca datos incorrectos (seguridad) y sin rendirse antes de tiempo (vivacidad). Reintentar sin control puede convertir un fallo parcial en uno total: muchos clientes reintentando a la vez saturan a un almacén que intenta recuperarse.

### Posibles soluciones

**Otra réplica y, si también falla, detenerse:** nunca entrega datos incorrectos, pero se rinde aunque el problema sea pasajero y quede audio en el búfer.

**Saltarse el fragmento:** la reproducción no se para, pero queda un hueco y se entrega una pista distinta de la original.

**Reintentar el mismo almacén sin espera:** sirve para fallos muy breves, pero si está caído multiplica la carga sin resultado.

**Otra réplica, luego reintentos con espera exponencial y variación aleatoria dentro del margen del búfer, pausa si se vacía y un límite total:** aprovecha todo el margen antes de cortar, se reanuda sola y solo se rinde tras un límite declarado.

### Decisión

**Otra réplica, reintentos con espera exponencial y variación aleatoria (*jitter*) dentro del margen del búfer, pausa si se vacía y límite total.**

1. **Integridad:** el cliente comprueba el hash de cada fragmento recibido. Si no coincide, lo trata como `Corrupt`, marca el almacén como sospechoso y usa otra réplica.
2. **Otras réplicas:** si fallan las dos primeras, el cliente sigue bajando por el orden de colocación.
3. **Reintentos:** si todas fallan, reintenta con espera exponencial y variación aleatoria mientras quede audio en el búfer.
4. **Corte:** si el búfer se vacía, la reproducción se pausa, el corte se registra y se reanuda en cuanto llega el fragmento.
5. **Límite total:** si pasan `DTN_GIVE_UP_SECONDS` sin conseguir el fragmento, la reproducción termina con `Unavailable`.
6. **Nunca se salta un fragmento.**

Reintentar una lectura es seguro: leer es idempotente, así que una petición repetida o una respuesta tardía no duplica ningún efecto. Las respuestas tardías se descartan.

### Condición para que una caída no produzca cortes

```text
retraso de duplicación (d) + tiempo de lectura de otra réplica  <  audio en el búfer
```

Con peticiones duplicadas, el cambio de fuente cuesta del orden de d (decenas de milisegundos), muy por debajo de los segundos de búfer. Por tanto, la predicción es que **matar el almacén que está sirviendo una pista no produce ningún corte**.

| Tipo | Declaración |
|---|---|
| Corrección | El audio entregado al decodificador es idéntico byte a byte a la versión guardada al subir, o la reproducción termina con un error. Nunca se entrega un fragmento corrupto ni se salta uno. |
| Objetivo | Con como mucho 2 almacenes caídos, la reproducción no tiene cortes, siempre que se cumpla la condición anterior. |
| Límite | Si ninguna réplica de un fragmento responde durante `DTN_GIVE_UP_SECONDS`, la reproducción termina con `Unavailable`. |

---

## Cómo guarda un almacén sus objetos

### El problema

Un almacén solo debe confirmar una escritura cuando el dato sobreviva a los fallos que el sistema declara tolerar. Si confirma antes, la réplica confirmada puede no existir tras una caída. El punto de durabilidad tiene que ir antes del punto de confirmación.

### Posibles soluciones

**En memoria:** rápido, pero toda caída pierde de forma permanente las réplicas de ese almacén.

**En disco, sin forzar la escritura:** sobrevive a la mayoría de reinicios, pero puede confirmar datos que el sistema operativo aún no ha escrito.

**En disco, un archivo por objeto, escrito de forma atómica y forzado antes de confirmar:** «confirmado» significa «sobrevive a un reinicio», y un archivo nunca queda escrito a medias. Cada escritura espera al disco.

**Base de datos embebida:** útil para datos con estructura, pero no aporta nada para objetos inmutables identificados por su hash.

### Decisión

**En disco, un archivo por objeto, escrito de forma atómica y forzado antes de confirmar.**

1. El objeto se escribe en un archivo temporal del mismo directorio y se fuerza con `fsync`.
2. Se renombra al nombre definitivo (su SHA-256) y se fuerza el directorio con `fsync`.
3. Solo entonces el almacén confirma.
4. Cada almacén usa su propio subdirectorio de `DTN_DATA_DIR`.

El renombrado es atómico, así que un objeto existe completo o no existe. Dos escrituras simultáneas del mismo objeto tienen el mismo contenido y dejan un único archivo.

| Tipo | Declaración |
|---|---|
| Corrección | Toda escritura confirmada por un almacén sobrevive a la caída y el reinicio de su proceso. |
| Supuesto | El disco respeta `fsync`. Todos los almacenes comparten el mismo disco físico. |

---

## Qué pasa cuando un almacén cae y vuelve

### El problema

Cada escenario de fallo necesita una historia de recuperación. Un almacén caído puede volver con lo que tenía en disco (*crash-recovery*), pero no con lo que se escribió mientras estaba caído.

### Posibles soluciones

**Lo que cae no vuelve** (*crash-stop*): nada que gestionar, pero el sistema pierde capacidad con cada caída.

**Reinicio automático con el mismo nombre lógico, reconstruyendo su estado desde el disco:** el almacén vuelve solo y con sus datos.

**Un supervisor propio que relanza almacenes:** totalmente explícito, pero duplica lo que el entorno de ejecución ya ofrece.

### Decisión

**Reinicio automático con el mismo nombre lógico, reconstruyendo su índice desde el disco.**

- El almacén reiniciado recorre su directorio y reconstruye la lista de objetos que guarda.
- Lo que se escribió mientras estuvo caído lo completa la reparación de réplicas.
- Un almacén reiniciado es una nueva encarnación del mismo almacén lógico. Como sus datos son inmutables, nada de lo que guardaba puede estar desactualizado: como mucho, le falta algo.

---

## Cómo se recupera la redundancia perdida

### El problema

Tras una caída, los objetos de ese almacén quedan con menos réplicas de las debidas. Si no se reponen, cada caída acerca el sistema al límite en que se pierden datos. La recuperación forma parte de la corrección de la replicación.

### Posibles soluciones

**Sin reposición:** nada que construir, pero la redundancia se va perdiendo.

**Reparación al leer:** el lector repone una réplica cuando la echa en falta. Solo repara lo que alguien lee y añade escrituras al camino de la reproducción.

**Traspaso con aviso** (*hinted handoff*): el almacén sustituto guarda la réplica y la devuelve al original cuando vuelve. Con rendezvous hashing no hace falta, porque el lector sigue el orden y encuentra la réplica en el sustituto.

**Anti-entropía periódica comparando inventarios:** un agente compara qué guarda cada almacén y repone lo que falte. Repara todo, se lea o no, sin tocar el camino de la reproducción.

**Anti-entropía con árboles de Merkle:** detecta diferencias intercambiando muy pocos datos. Solo compensa con volúmenes enormes; con listas de identificadores basta.

### Decisión

**Anti-entropía periódica comparando inventarios.**

1. Cada `DTN_REPAIR_INTERVAL_SECONDS`, y también cuando detecta que un almacén cayó o volvió, el agente de reparación pide su inventario a cada almacén vivo.
2. Para cada objeto calcula el orden de colocación.
3. Si tiene menos de N réplicas, lo copia desde una réplica sana, comprobando su hash, en el primer almacén vivo del orden que no lo tenga. Las lápidas son la excepción: se copian a todos los almacenes vivos que no las tengan (ver «Cómo se retira una pista»).
4. Escribir un objeto inmutable es idempotente, así que pueden trabajar varios agentes a la vez, o un agente y una subida, sin coordinarse.

El agente no está en el camino de las lecturas: si cae, el servicio sigue y solo se retrasa la reparación. Se reinicia igual que los almacenes.

| Tipo | Declaración |
|---|---|
| Objetivo | Tras una caída, cada objeto vuelve a tener N réplicas en como mucho dos periodos de reparación, si quedan almacenes vivos suficientes. |

---

## Cómo se detecta la corrupción

### El problema

Un objeto puede corromperse en el disco después de guardado. Si nadie lo comprueba, el almacén lo sigue ofreciendo y la réplica cuenta como sana sin serlo.

### Posibles soluciones

**Solo comprueba el cliente:** la corrupción se descubre cuando alguien lee, y el almacén sigue ofreciendo el objeto corrupto.

**Comprobar al guardar:** nada corrupto entra, pero no se detecta la corrupción posterior.

**Comprobar al guardar, al leer y periódicamente en segundo plano** (*scrubbing*): la corrupción se detecta antes de que alguien la lea, y el objeto se repone. Leer todo periódicamente consume disco y procesador.

### Decisión

**Comprobar el hash al guardar, al leer y periódicamente.**

- **Al guardar:** si no coincide con el identificador, se rechaza con `Invalid`.
- **Al leer:** si no coincide, se responde `Corrupt`.
- **En segundo plano:** cada `DTN_SCRUB_INTERVAL_SECONDS`, el almacén recorre todo lo que guarda.
- Un objeto corrupto se aparta a cuarentena y deja de aparecer en el inventario, así que la reparación lo repone.

| Tipo | Declaración |
|---|---|
| Corrección | Un almacén nunca entrega un objeto cuyo hash no coincida con su identificador. |

---

## Cuántas peticiones atiende a la vez un almacén

### El problema

Un almacén sigue siendo un programa concurrente normal. Si atiende una petición cada vez, una lectura lenta bloquea a todas las demás. Si atiende sin límite, se satura. El límite de concurrencia forma parte del contrato del servicio.

### Posibles soluciones

**Una petición cada vez:** sin problemas de concurrencia, pero una petición lenta bloquea las demás.

**Varios hilos con un máximo fijo:** varias peticiones avanzan a la vez y el límite actúa como contrapresión. El estado compartido (el índice) necesita protección con un cerrojo.

**Una sola tarea con entrada y salida asíncrona:** muchas peticiones con poco coste cada una, pero exige operaciones de disco asíncronas, menos directas que los hilos para archivos en Python.

### Decisión

**Varios hilos, con un máximo de `DTN_STORE_CONCURRENCY` peticiones simultáneas.**

- Leer objetos inmutables es seguro desde varios hilos, y la entrada y salida de disco libera el bloqueo global de Python.
- El índice en memoria se protege con un cerrojo, que nunca se mantiene durante una llamada remota.

---

## Qué hace un almacén cuando está al máximo de peticiones

### El problema

Un almacén atiende como mucho `DTN_STORE_CONCURRENCY` peticiones a la vez. Hay que decidir qué pasa con la siguiente.

### Posibles soluciones

**Ponerla en cola.** No se rechaza nada, pero la cola puede crecer sin límite, y el cliente no distingue un almacén saturado de uno lento: espera hasta que vence su plazo.

**Cola acotada, y rechazar cuando se llena.** Absorbe ráfagas cortas, pero añade un parámetro más y sigue haciendo esperar.

**Rechazar en el acto con `Unavailable`.** El cliente se entera enseguida y pide a otra réplica, que es lo que ya hace ante cualquier fallo. La carga se reparte sola entre réplicas.

### Decisión

**Rechazar en el acto con `Unavailable`, indicando que el motivo es saturación.** Un rechazo por saturación no cuenta como fallo para el detector: el almacén está vivo y responde rápido. Qué hace el cliente depende de la operación:

- **Al leer**, pide a otra réplica y, si reintenta en el mismo almacén, lo hace con espera exponencial (ver «Qué hace el reproductor cuando una lectura falla»).
- **Al escribir**, reintenta el mismo almacén con espera exponencial, y solo pasa al siguiente del orden si el almacén está caído o es sospechoso. Si escribiera en otro, la réplica quedaría fuera de su sitio: cada lectura posterior preguntaría primero a un almacén que no la tiene, y la reparación no lo corregiría, porque el objeto ya tiene N réplicas.

| Tipo | Declaración |
|---|---|
| Corrección | Un almacén nunca tiene más de `DTN_STORE_CONCURRENCY` peticiones en curso. |

---

## Qué interfaz se ofrece

### El problema

Quien use el sistema no debe depender del entorno de ejecución ni de cómo se replican los objetos. La interfaz tiene que conservar las distinciones útiles entre errores (no es lo mismo «no existe» que «no está disponible ahora»), y el comportamiento ante fallos forma parte del contrato.

### Posibles soluciones

**Exponer directamente los nodos:** nada que construir, pero quien los usa depende del entorno de ejecución y de los detalles de replicación.

**Una biblioteca del cliente que oculte la distribución, y una interfaz interna mínima en cada almacén:** el contrato del sistema no depende de la implementación y se mantiene al cambiarla. Son dos niveles de interfaz que mantener.

### Decisión

**Una biblioteca del cliente que oculta la distribución, y una interfaz interna mínima en cada almacén.**

Biblioteca del cliente:

| Operación | Devuelve | Errores |
|---|---|---|
| `upload(file, metadata)` | Identificador de la pista | `Invalid`; `Conflict` si la pista está retirada; `Unavailable`, con resultado incierto |
| `search(query)` | Pistas encontradas, si el resultado es completo y qué almacenes no respondieron | — |
| `list()` | Igual que `search` | — |
| `play(track_id)` | Una sesión de reproducción, con sus métricas de cortes | `NotFound`, `Unavailable` |
| `withdraw(track_id, reason)` | — | `NotFound`, `Unavailable` |

**Resultado completo:** una búsqueda es completa si los almacenes que no respondieron son menos de N. Así, cada manifiesto tiene al menos una réplica entre los que sí respondieron, y el cliente puede afirmarlo sin consultar nada más. El comportamiento con almacenes inaccesibles queda así expresado en la propia interfaz.

Interfaz interna de cada almacén: `put(id, data)`, `get(id)`, `inventory()`, `search(query)`, `tombstones(track_id, original)`, `delete_if_older(id, seconds)` y `ping()`. Las dos penúltimas salen de «Cómo encuentra el cliente la lápida de una pista» y «Cómo borra un almacén un objeto sin uso». La especificación de cada una está en [`contract.md`](contract.md).

La validación de metadatos y la función de búsqueda `matches(metadata, query)` son funciones puras. Cada almacén ejecuta `matches` sobre sus manifiestos, y ambas se desarrollan sin depender de cómo se guardan ni se replican los objetos.

---

## Cuándo se responde «no existe» y cuándo «no disponible»

### El problema

Reproducir y retirar empiezan buscando el manifiesto de una pista. Si ningún almacén de los que respondieron lo tiene, puede ser que la pista no exista o que sus réplicas estén justo en los almacenes que no respondieron. `NotFound` le dice a quien llama que no vuelva a intentarlo, y `Unavailable` que lo intente más tarde (principio P5). Confundirlos tiene coste en los dos sentidos.

### Posibles soluciones

**`NotFound` si no lo tiene ninguno de los que respondieron.** Con almacenes caídos, afirmaría que no existe una pista que sí existe.

**`Unavailable` si falta cualquier almacén.** Nunca se equivoca, pero con un solo almacén caído no se podría afirmar nunca que algo no existe.

**`NotFound` solo si no respondieron menos de N almacenes.** Todo manifiesto confirmado tiene N réplicas. Si faltan menos de N almacenes, alguna réplica estaba entre los que respondieron, así que la ausencia es real. Es la misma regla que decide si una búsqueda está completa (ver «Qué interfaz se ofrece»).

### Decisión

**`NotFound` solo si no respondieron menos de N almacenes. En otro caso, `Unavailable`.** Una pista retirada también da `NotFound`, indicando el motivo.

| Tipo | Declaración |
|---|---|
| Corrección | Con como mucho 2 almacenes caídos, nunca se responde `NotFound` para una pista confirmada y no retirada. |

---

## Cómo se usa el sistema desde fuera

### El problema

La biblioteca del cliente es la interfaz para otro programa. Para usar el sistema a mano y, sobre todo, para lanzar los experimentos desde guiones, hace falta una forma de invocar cada operación y de saber cómo terminó sin leer texto.

### Posibles soluciones

**Solo la biblioteca.** Cada experimento sería un programa propio.

**Una interfaz web.** Cómoda para una persona, pero difícil de automatizar y mucho código que no aporta nada a lo distribuido.

**Una línea de comandos, con códigos de salida por clase de error y salida en JSON.** Cualquier guion la lanza, distingue los resultados por el código de salida y procesa la salida.

### Decisión

**Una línea de comandos, `dtn`, con una orden por operación.**

| Orden | Resultado |
|---|---|
| `dtn upload FILE --title T --artist A --license L` | Imprime el identificador de la pista. |
| `dtn search TEXTO`, `dtn list` | Una línea por pista e indicación de si el resultado es completo. |
| `dtn play TRACK_ID [--headless]` | Los eventos de la sesión, uno por línea, y un resumen al final. |
| `dtn withdraw TRACK_ID --reason MOTIVO` | Nada si tiene éxito. |

Códigos de salida: 0 éxito (en las búsquedas, también si el resultado es incompleto, que se indica en la salida); 2 `Invalid`; 3 `NotFound`; 4 `Unavailable`; 5 `Conflict`; 1 cualquier otro final (una operación que todavía no existe o un error del propio programa), para que un guion no lo confunda con un resultado del contrato. Toda orden admite `--json`.

**Eventos de una reproducción:** `started` (con el tiempo hasta empezar a sonar), `source_switch` (almacén abandonado, almacén nuevo y motivo), `stall_start` y `stall_end` (un corte y su duración), `finished` y `failed`. El resumen final da el número de cortes, su duración total, el número de cambios de fuente y los segmentos obtenidos de cada almacén. Son los datos que necesitan los experimentos.

---

## Cuánto puede durar una operación

### El problema

Una operación que nunca termina no es «lenta»: es un fallo que no se ve. Además, la limpieza de objetos sin uso supone que toda subida termina antes de `DTN_GC_GRACE_SECONDS` (ver «Qué se hace con los objetos que nadie usa»), y eso solo se puede asegurar si las subidas tienen un tamaño y una duración acotados.

### Posibles soluciones

**Sin plazo total.** Solo cuentan los plazos de cada petición a un almacén, pero una operación con muchos reintentos puede durar sin límite.

**Un plazo total por operación, configurable, y un tamaño máximo de subida.** Toda operación termina, y el supuesto de la limpieza se puede justificar.

### Decisión

**Plazo total configurable por operación y límites de subida.**

- Al agotarse el plazo total, la operación termina con `Unavailable`, que en las escrituras significa resultado incierto.
- Una subida admite como mucho `DTN_MAX_UPLOAD_SECONDS` de audio y `DTN_MAX_UPLOAD_BYTES` de archivo. Si los supera, se rechaza con `Invalid`.
- `title` y `artist` tienen como mucho 200 caracteres.

| Tipo | Declaración |
|---|---|
| Objetivo | Toda operación termina en como mucho su plazo total. |
| Supuesto | `DTN_UPLOAD_TIMEOUT_SECONDS` es mucho menor que `DTN_GC_GRACE_SECONDS`. |

---

## Cómo se escribe durante una subida

### El problema

Una pista larga tiene cientos de segmentos. Escribirlos uno detrás de otro hace la subida lenta, y escribirlos todos a la vez satura a los almacenes.

### Posibles soluciones

**Uno detrás de otro:** simple, pero la subida tarda la suma de todas las escrituras.

**Todos a la vez, sin límite:** lo más rápido con pocos segmentos, pero satura a los almacenes con pistas largas.

**En paralelo, con un máximo de escrituras simultáneas:** rápido sin saturar.

### Decisión

**En paralelo, con un máximo de `DTN_UPLOAD_PARALLELISM` escrituras simultáneas.** Cada escritura usa el plazo adaptativo de la lectura. El manifiesto se escribe solo cuando todo lo demás está confirmado.

---

## Qué se hace con los objetos que nadie usa

### El problema

Los objetos de una pista retirada, o de una subida abandonada, ocupan espacio indefinidamente. Pero borrarlos no es trivial: como los objetos se identifican por su contenido, dos pistas pueden compartir segmentos (por ejemplo, el mismo audio subido con metadatos distintos), y durante una subida hay segmentos que todavía no tienen manifiesto.

### Posibles soluciones

**Conservarlo todo:** nunca se borra nada por error, pero el espacio crece sin límite.

**Borrar los segmentos al retirar la pista:** inmediato, pero rompe las pistas que compartan segmentos.

**Contar referencias:** borra en cuanto un objeto deja de usarse, pero el contador es estado que cambia y tendría que estar replicado.

**Marcar y barrer, con periodo de gracia:** solo borra lo que ninguna pista vigente usa, sin estado replicado. El periodo de gracia protege las subidas en curso. Un objeto sin uso tarda en borrarse al menos ese periodo.

### Decisión

**Marcar y barrer, con periodo de gracia.**

1. El agente de reparación marca todo objeto referenciado por algún manifiesto no retirado: sus segmentos, su segmento inicial y su original.
2. Borra los objetos no marcados con más de `DTN_GC_GRACE_SECONDS` de antigüedad (ver «Cómo borra un almacén un objeto sin uso»).
3. Escribir un objeto que ya existe renueva su antigüedad. Sin esto, una subida que reutiliza segmentos antiguos sin uso podría perderlos entre su escritura y la del manifiesto.
4. La antigüedad se cuenta desde la última escritura o desde que arrancó el almacén, lo que sea más reciente, con un reloj monótono. No depende de la hora del sistema, que puede saltar hacia delante al corregirse y hacer que un objeto recién escrito parezca viejo, y no hay que guardarla en disco: tras un reinicio, todo objeto vuelve a empezar con antigüedad 0, que es la dirección segura.
5. Los manifiestos y las lápidas de las pistas retiradas se conservan, para que la retirada siga siendo visible.

| Tipo | Declaración |
|---|---|
| Corrección | Un objeto referenciado por una pista vigente nunca se borra. |
| Supuesto | Toda subida termina antes de `DTN_GC_GRACE_SECONDS`. |
| Límite | Un almacén que se reinicia más a menudo que `DTN_GC_GRACE_SECONDS` no borra nada. |

---

## Cómo borra un almacén un objeto sin uso

### El problema

La limpieza decide qué objetos no usa nadie y los borra (ver «Qué se hace con los objetos que nadie usa»). Entre la decisión del agente y el borrado pasa tiempo, y en ese intervalo una subida puede volver a escribir el objeto, renovando su antigüedad, porque lo necesita. Si el borrado no lo tiene en cuenta, borra un objeto que una subida en curso acaba de confirmar.

### Posibles soluciones

**Borrado incondicional, `delete(id)`.** Simple, pero sufre exactamente la carrera descrita.

**Bloquear las subidas durante la limpieza.** Evita la carrera, pero detiene el servicio y exige coordinar a todos los clientes con el agente.

**Borrado condicional, `delete_if_older(id, seconds)`.** El almacén borra el objeto solo si, en el momento de borrar, su antigüedad supera `seconds`. La comprobación y el borrado se hacen bajo el cerrojo del índice, así que ninguna escritura del mismo objeto puede colarse entre ambos. La antigüedad la mide el propio almacén con su reloj monótono, así que no hay que comparar relojes de procesos distintos ni depender de la hora del sistema.

### Decisión

**Borrado condicional, `delete_if_older(id, seconds)`, con `seconds = DTN_GC_GRACE_SECONDS`.** Si el objeto fue reescrito después de que el agente decidiera borrarlo, el almacén no lo borra y responde que se conservó.

| Tipo | Declaración |
|---|---|
| Corrección | Un objeto escrito hace menos de `DTN_GC_GRACE_SECONDS` nunca se borra, aunque el almacén se reinicie entre medias o cambie la hora del sistema. |

---

## Parámetros

| Variable | Valor por defecto | Significado |
|---|---|---|
| `DTN_NUM_STORES` | 5 | Número de almacenes (S). |
| `DTN_REPLICAS` | 3 | Réplicas de cada objeto (N). |
| `DTN_SEGMENT_SECONDS` | 2 | Duración objetivo de cada segmento de audio. |
| `DTN_AUDIO_BITRATE` | 128k | Tasa objetivo de la recodificación. |
| `DTN_BUFFER_SECONDS` | 16 | Audio pedido o recibido por delante del punto de reproducción. |
| `DTN_PREROLL_SECONDS` | 4 | Audio recibido necesario para empezar a sonar. |
| `DTN_HEDGE_DELAY_MS` | sin fijar | Si se fija, sustituye al retraso adaptativo; `0` pide siempre a dos réplicas. |
| `DTN_TIMEOUT_MIN_MS` | 200 | Mínimo del plazo de sospecha. |
| `DTN_TIMEOUT_MAX_MS` | 2000 | Máximo del plazo de sospecha. |
| `DTN_GIVE_UP_SECONDS` | 30 | Tiempo máximo sin conseguir un fragmento antes de terminar con `Unavailable`. |
| `DTN_DATA_DIR` | `./data` | Directorio donde cada almacén tiene su subdirectorio. |
| `DTN_STORE_CONCURRENCY` | 8 | Peticiones simultáneas por almacén. |
| `DTN_UPLOAD_PARALLELISM` | 8 | Escrituras simultáneas durante una subida. |
| `DTN_REPAIR_INTERVAL_SECONDS` | 30 | Periodo del agente de reparación. |
| `DTN_SCRUB_INTERVAL_SECONDS` | 3600 | Periodo de la comprobación de integridad en segundo plano. |
| `DTN_GC_GRACE_SECONDS` | 86400 | Antigüedad mínima de un objeto sin uso para borrarlo. |
| `DTN_UPLOAD_TIMEOUT_SECONDS` | 600 | Plazo total de una subida. |
| `DTN_SEARCH_TIMEOUT_MS` | 2000 | Plazo total de una búsqueda; los almacenes que no responden antes quedan como no respondidos. |
| `DTN_WITHDRAW_TIMEOUT_SECONDS` | 30 | Plazo total de una retirada. |
| `DTN_MAX_UPLOAD_SECONDS` | 1200 | Duración máxima del audio de una subida. |
| `DTN_MAX_UPLOAD_BYTES` | 209715200 | Tamaño máximo del archivo subido (200 MB). |

Los valores por defecto son un punto de partida y se ajustarán con mediciones.

---

## Cuestiones abiertas

- **Una máquina o varios nodos.** Lo que se promete ante almacenes inaccesibles ya está fijado (ver «Modelo de fallos»); queda decidir cómo se prueba. Con todos los almacenes en una máquina solo se prueban caídas de proceso. Repartirlos en varios nodos acercaría las caídas a fallos independientes y permitiría inyectar fallos de red reales (nodo lento, pérdida de paquetes), a cambio de una puesta en marcha más compleja.
