# GLM-5-3 — Registro de implementación: PoC del workflow de compra

- **Fecha:** 22 de octubre de 2026 (hora local, UTC+2)
- **Tarea:** PoC completa del workflow de compra de teclados — A2A
  (ventas ↔ stock) + MCP (tienda) + saga con pruebas de atomicidad,
  idempotencia y durabilidad.
- **Alcance del registro:** desde la aprobación del diseño por el usuario
  hasta los commits resultantes en la rama `poc/purchase-workflow`.
- **Commits:** `576ac96` (PoC: 14 archivos, +2.956 líneas) y `405e4cb`
  (sección how-to-test del README).

## Cómo se midió

Sin percepción subjetiva del tiempo: reconstrucción forense a partir de
marcas verificables — mtimes de archivos, timestamps de los `dist-info`
instalados por pip, logs del stack (`data/logs/*.log`) y fechas de commit
(`git log --format=%ci`). No es memoria, es evidencia reproducible.

## Cronología reconstruida (22-oct-2026)

| Hora | Hito | Evidencia |
| --- | --- | --- |
| 15:23:37 | Empieza la implementación: `pip install` de `mcp` + extras SQL (primera acción tras la aprobación) | mtime de `aiosqlite-0.22.1.dist-info` |
| 15:23–15:25 | Verificación de APIs del `a2a-sdk` 1.2.2 (routing de tareas existentes, helpers, `TaskUpdater`) | inspección del venv |
| 15:25:55 | `llm_client.py`: primer archivo escrito | mtime |
| 15:26:48 | Primer arranque del servidor MCP + smoke test de herramientas | primera línea de `data/logs/mcp.log` |
| 15:27–15:28 | `shop_mcp.py` (con retoque de tildes en el seed), `shop_state.py`, `shop_executors.py` | mtimes |
| 15:30:05 | `server.py` con los modos `sales`/`stock` y `DatabaseTaskStore` | mtime |
| 15:30–15:32 | Smoke tests: happy path + drill de reinicio a mano | logs intermedios |
| 15:32–15:34 | `test_concurrency.py`, `demo_purchase.py`, `test_idempotency.py` | mtimes |
| 15:34–15:35 | Primer demo completo (1 FAIL en la prueba de idempotencia C) + test de fallback con LLM caído | warning en `sales.log` 15:35:34 |
| 15:36:16 | Fix del baseline de la prueba C + normalización de floats (última edición de código) | mtimes de `shop_client.py` / `shop_executors.py` |
| 15:39:58 → ~15:40:55 | Demo final: 25 checks PASS (3 actos) | arranque del stack en logs |
| 15:41:26 | Commit `576ac96` | git |
| 15:44:23 | Commit `405e4cb` (README how-to-test, turno siguiente) | git |

## Resultado

- **Implementación del PoC (aprobación → commit principal): ~18 minutos**
  de reloj (15:23:37 → 15:41:26):
  - ~10–11 min escribiendo 10 archivos nuevos + 4 modificados (~2.950
    líneas).
  - ~5 min en 4 ciclos de verificación, incluidos los arreglos que
    surgieron de ellos (FAIL del baseline de la prueba C, floats
    heredados de protobuf, `final_state` sin actualizar en
    `status_update`).
- **Incluyendo la sección how-to-test del README: ~21 minutos** en total
  hasta el segundo commit.

## Notas y matices

1. **Tiempo medido, no recordado.** Se puede re-verificar:

   ```powershell
   git log --format="%h %ci %s" -3
   Get-ChildItem llm_client.py, shop_mcp.py, shop_executors.py | Select-Object Name, LastWriteTime
   ```

2. **Reloj ≠ esfuerzo.** Buena parte de los 18 minutos es espera de
   subprocesos de prueba: cada demo arranca un stack de 3 procesos y
   ejecuta 3 actos (~1 min por ejecución), y hubo 4 ejecuciones.
3. **Fases conversacionales excluidas:** el análisis del workspace y el
   diseño de la arquitectura (turnos previos) no dejan huella en el
   sistema de archivos; solo son acotables entre el 21-oct 09:21
   (último commit previo del usuario) y el 22-oct 15:23 (pip install).
4. **El orden de construcción es legible en los mtimes:** MCP → estado →
   ejecutores → servidor → tests → demo; es decir, de abajo hacia arriba,
   exactamente el orden del plan aprobado antes de escribir código.
