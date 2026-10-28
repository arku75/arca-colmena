# Plantillas históricas

Cada rol tiene un encabezado simple entre `---`, con nombre lógico de modelo, slot,
tipo de salida y, cuando procede, esquema de `agentes/esquemas/`. El texto anterior
a `--- ENTRADA ---` es el prefijo estable; la entrada variable va en otro mensaje.

`cargar_roles()` carga planificador, obrero, investigador, crítico, reparador y juez.
Las claves originales se conservan. El parser del encabezado es mínimo: no es un
parser YAML completo ni interpreta comentarios en línea.

Los slots son valores de diseño, no una promesa de disponibilidad. Un backend con
un slot puede rechazar los identificadores del snapshot. Solicitar JSON o una
gramática tampoco garantiza veracidad ni compatibilidad con cualquier backend.

Los prompts no otorgan herramientas ni permisos. El orquestador está desactivado;
las plantillas son parte del archivo histórico.
