# Jira Project Sync

Herramienta de volcado incremental de un proyecto de Jira Cloud. Conserva el modelo de Jira, las relaciones entre issues y sus tipos, y solo descarga lo que es nuevo o ha cambiado desde la última ejecución.

## Requisitos

- [uv](https://docs.astral.sh/uv/). El script declara sus dependencias (httpx) en la cabecera y `uv` las instala solo.
- Un API token de Atlassian: se genera en [la página de tokens de Atlassian](https://id.atlassian.com/manage-api-tokens).

## Configuración

Copia `.env.example` a `.env` y rellena los valores. Las variables de entorno tienen prioridad sobre `.env`.

| Variable         | Obligatoria | Por defecto | Descripción                       |
| :--------------- | :---------: | :---------- | :-------------------------------- |
| `JIRA_URL`       |     Sí      | —           | URL de la instancia de Jira Cloud |
| `JIRA_EMAIL`     |     Sí      | —           | Email de la cuenta                |
| `JIRA_API_TOKEN` |     Sí      | —           | API token                         |
| `JIRA_PROJECT`   |     No      | `PROJ`       | Clave del proyecto                |
| `OUTPUT_DIR`     |     No      | `./backup`  | Carpeta de destino                |
| `CONCURRENCY`    |     No      | `5`         | Número de peticiones en paralelo  |

## Uso

```bash
uv run jira_sync.py            # sincronización incremental
uv run jira_sync.py --full     # ignora el estado local y vuelve a descargar todo
uv run jira_sync.py --env ruta/al/.env
```

Al terminar muestra un resumen: issues nuevos, actualizados, sin cambios, borrados en Jira y adjuntos descargados.

## Qué se guarda

Todo queda en `backup/<PROYECTO>/`:

| Ruta                                  | Contenido                                                                                            |
| :------------------------------------ | :--------------------------------------------------------------------------------------------------- |
| `issues/PROJ-n.json`                   | Issue con todos sus campos, changelog completo, comentarios, worklogs y enlaces remotos              |
| `links.json`                          | Enlaces entre issues con su tipo (`Blocks`, `Relates`...) y jerarquía padre/hijo (epics y subtareas) |
| `model/project.json`                  | Definición del proyecto, con sus tipos de issue                                                      |
| `model/statuses.json`                 | Estados por tipo de issue                                                                            |
| `model/fields.json`                   | Campos, incluidos los personalizados                                                                 |
| `model/link_types.json`               | Tipos de enlace disponibles                                                                          |
| `model/priorities.json`               | Prioridades                                                                                          |
| `model/components.json`               | Componentes del proyecto                                                                             |
| `model/versions.json`                 | Versiones del proyecto                                                                               |
| `model/boards.json`                   | Boards del proyecto                                                                                  |
| `model/board_<id>_configuration.json` | Configuración de cada board                                                                          |
| `model/board_<id>_sprints.json`       | Sprints de cada board                                                                                |
| `attachments/<issue>/<id>_<nombre>`   | Adjuntos                                                                                             |
| `state.json`                          | Estado de sincronización (no editar a mano)                                                          |

Los ficheros JSON se escriben con claves ordenadas y sangría fija, de modo que son comparables con `diff` o con git.

## Cómo evita trabajo innecesario

1. Pide a Jira un listado ligero de todas las claves con `updated`, enlaces, adjuntos y padre.
2. Calcula una huella por issue y la compara con la de `state.json`. Si coincide, no hace nada más con ese issue.
3. Descarga el issue completo solo si es nuevo o su huella ha cambiado. Los enlaces y adjuntos entran en la huella por si Jira no actualiza `updated` en los dos extremos de un enlace.
4. Descarga solo los adjuntos que faltan, comparando por id y tamaño.
5. Si un issue desaparece de Jira, lo marca como borrado en `state.json` y conserva su fichero local.
6. Si el issue cambia de clave (movido de proyecto), renombra su fichero. La identidad de un issue es su id, no su clave.
7. Los ficheros de `model/` se reescriben solo si su contenido cambia.

El estado se guarda tras cada issue, así que una ejecución interrumpida continúa donde se quedó.

## Tests

Los tests usan un Jira simulado y no necesitan credenciales:

```bash
uv run --with httpx python -m unittest discover -s tests -v
```

## Limitaciones

- Solo es una herramienta de volcado: no restaura datos en Jira.
- Los comentarios, worklogs y changelog se descargan completos cada vez que cambia el issue.
- Los endpoints se han probado contra un Jira simulado; la primera ejecución contra Jira real puede requerir ajustes.
