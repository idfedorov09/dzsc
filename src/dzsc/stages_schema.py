from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shlex
import shutil
import subprocess
import textwrap
import uuid

from dzsc.common import ensure_project_dir, rmdir_if_empty
from dzsc.sdk import StageRunContext, stage


DOCZILLA_EMPTY_DATE_MS = -62135769600000
FIELD_SEPARATOR = "|"


@dataclass(frozen=True, slots=True)
class ComposeContext:
    compose_file: Path
    compose_dir: Path
    doczilla_service: str
    postgres_service: str


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    name: str
    table_count: int
    has_settings: bool
    has_users: bool
    version: str
    user_count: str
    last_data_ms: int | None
    security_log_mtime: float | None

    @property
    def is_doczilla(self) -> bool:
        return self.has_settings and self.has_users

    @property
    def last_activity_time(self) -> float | None:
        values: list[float] = []
        if self.last_data_ms is not None:
            values.append(self.last_data_ms / 1000.0)
        if self.security_log_mtime is not None:
            values.append(self.security_log_mtime)
        return max(values) if values else None


def _compose_context(ctx: StageRunContext) -> ComposeContext:
    ensure_project_dir(ctx.project_dir)
    compose_file = _find_compose_file(ctx.project_dir, ctx.compose_file)
    return ComposeContext(
        compose_file=compose_file,
        compose_dir=compose_file.parent,
        doczilla_service=ctx.doczilla_service,
        postgres_service=ctx.postgres_service,
    )


def _find_compose_file(project_dir: Path, configured: Path | None) -> Path:
    if configured is not None:
        candidate = configured.expanduser()
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        if not candidate.is_file():
            raise SystemExit(f"compose file not found: {candidate}")
        return candidate.resolve()

    for directory in (project_dir, *project_dir.parents):
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            candidate = directory / name
            if candidate.is_file():
                return candidate.resolve()

    raise SystemExit(f"docker compose file not found from {project_dir} upward")


def _compose_base(compose: ComposeContext) -> list[str]:
    return ["docker", "compose", "-f", str(compose.compose_file)]


def _run_compose(
    compose: ComposeContext,
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = [*_compose_base(compose), *args]
    if not capture:
        print("running:", " ".join(shlex.quote(part) for part in cmd))
    result = subprocess.run(
        cmd,
        cwd=compose.compose_dir,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise SystemExit(details or f"command failed: {' '.join(cmd)}")
    return result


def _psql(compose: ComposeContext, sql: str) -> str:
    command = (
        'psql -U "${POSTGRES_USER:-dz}" '
        '-d "${POSTGRES_DB:-doczilla}" '
        f"-v ON_ERROR_STOP=1 -At -F {shlex.quote(FIELD_SEPARATOR)} -c {shlex.quote(sql)}"
    )
    return _run_compose(
        compose,
        ["exec", "-T", compose.postgres_service, "sh", "-lc", command],
    ).stdout.strip()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified(schema: str, table: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(table)}"


def _parse_bool(value: str) -> bool:
    return value.lower() in {"t", "true", "1", "yes"}


def _rows(output: str) -> list[list[str]]:
    if not output:
        return []
    return [line.split(FIELD_SEPARATOR) for line in output.splitlines()]


def _read_schemas(compose: ComposeContext) -> list[SchemaInfo]:
    sql = """
        select
            n.nspname,
            count(c.oid) filter (where c.relkind in ('r', 'p')),
            exists (
                select 1 from information_schema.tables t
                where t.table_schema = n.nspname and t.table_name = 'System Settings'
            ),
            exists (
                select 1 from information_schema.tables t
                where t.table_schema = n.nspname and t.table_name = 'System Users'
            )
        from pg_namespace n
        left join pg_class c on c.relnamespace = n.oid
        where n.nspname <> 'information_schema' and n.nspname not like 'pg_%'
        group by n.nspname
        order by n.nspname
    """
    result: list[SchemaInfo] = []
    for row in _rows(_psql(compose, sql)):
        if len(row) < 4:
            continue
        name = row[0]
        has_settings = _parse_bool(row[2])
        has_users = _parse_bool(row[3])
        result.append(
            SchemaInfo(
                name=name,
                table_count=int(row[1] or "0"),
                has_settings=has_settings,
                has_users=has_users,
                version=_schema_version(compose, name) if has_settings else "",
                user_count=_schema_user_count(compose, name) if has_users else "",
                last_data_ms=_schema_last_data_ms(compose, name),
                security_log_mtime=_schema_security_log_mtime(compose.compose_dir, name),
            )
        )
    return result


def _schema_version(compose: ComposeContext, schema: str) -> str:
    sql = (
        'select encode("Value", \'escape\') '
        f"from {_qualified(schema, 'System Settings')} "
        "where \"Name\" = 'Version' and \"Comment\" = 'Schema version' "
        "limit 1"
    )
    output = _psql(compose, sql)
    return output.splitlines()[0] if output else ""


def _schema_user_count(compose: ComposeContext, schema: str) -> str:
    sql = f"select count(*) from {_qualified(schema, 'System Users')}"
    return _psql(compose, sql).strip()


def _schema_last_data_ms(compose: ComposeContext, schema: str) -> int | None:
    table_rows = _rows(
        _psql(
            compose,
            """
            select
                table_name,
                bool_or(column_name = 'CreatedAt'),
                bool_or(column_name = 'ModifiedAt')
            from information_schema.columns
            where table_schema = {schema}
              and column_name in ('CreatedAt', 'ModifiedAt')
            group by table_name
            order by table_name
            """.format(schema=_sql_literal(schema)),
        )
    )
    selects: list[str] = []
    for table_name, has_created, has_modified in table_rows:
        parts: list[str] = []
        if _parse_bool(has_created):
            parts.append(f'coalesce(nullif("CreatedAt", {DOCZILLA_EMPTY_DATE_MS}), {DOCZILLA_EMPTY_DATE_MS})')
        if _parse_bool(has_modified):
            parts.append(f'coalesce(nullif("ModifiedAt", {DOCZILLA_EMPTY_DATE_MS}), {DOCZILLA_EMPTY_DATE_MS})')
        if not parts:
            continue
        expression = parts[0] if len(parts) == 1 else f"greatest({', '.join(parts)})"
        selects.append(f"select max({expression}) as value from {_qualified(schema, table_name)}")
    if not selects:
        return None

    sql = "select nullif(max(value), {empty}) from ({union}) s".format(
        empty=DOCZILLA_EMPTY_DATE_MS,
        union=" union all ".join(selects),
    )
    output = _psql(compose, sql).strip()
    return int(output) if output else None


def _schema_security_log_mtime(compose_dir: Path, schema: str) -> float | None:
    log_dir = compose_dir / "data" / "dz_opt_work" / "security-log" / schema
    if not log_dir.is_dir():
        return None

    latest: float | None = None
    for path in log_dir.glob("security.log*"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        latest = mtime if latest is None else max(latest, mtime)
    return latest


def _format_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_ms(value: int | None) -> str:
    return _format_time(value / 1000.0 if value is not None else None)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    print("  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))


def _java_pid(compose: ComposeContext) -> str:
    command = (
        "ps -eo pid,args | "
        "grep '[o]rg.zenframework.z8.server.engine.ServerMain' | "
        "awk '{print $1; exit}'"
    )
    pid = _run_compose(
        compose,
        ["exec", "-T", compose.doczilla_service, "sh", "-lc", command],
    ).stdout.strip()
    if not pid:
        raise SystemExit(f"Doczilla java process not found in service '{compose.doczilla_service}'")
    return pid


def _runtime_schema(compose: ComposeContext, pid: str | None = None) -> str:
    pid = pid or _java_pid(compose)
    result = _run_compose(
        compose,
        ["exec", "-T", compose.doczilla_service, "jcmd", pid, "VM.system_properties"],
    )
    for line in result.stdout.splitlines():
        if line.startswith("z8.application.database.schema="):
            return line.split("=", 1)[1].strip()
    return ""


def _env_schema(compose: ComposeContext) -> str:
    result = _run_compose(
        compose,
        ["exec", "-T", compose.doczilla_service, "sh", "-lc", 'printf "%s" "$Z8_DB_SCHEMA"'],
    )
    return result.stdout.strip()


def _container_id(compose: ComposeContext) -> str:
    container_id = _run_compose(compose, ["ps", "-q", compose.doczilla_service]).stdout.strip()
    if not container_id:
        raise SystemExit(f"container for service '{compose.doczilla_service}' not found")
    return container_id


def _docker_cp(source: str | Path, container_id: str, target: str) -> None:
    cmd = ["docker", "cp", str(source), f"{container_id}:{target}"]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit((result.stderr or result.stdout).strip() or f"command failed: {' '.join(cmd)}")


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _write_hook_sources(source_dir: Path) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "AttachMain.java").write_text(ATTACH_MAIN_JAVA, encoding="utf-8")
    (source_dir / "DzSchemaSwitchAgent.java").write_text(DZ_SCHEMA_SWITCH_AGENT_JAVA, encoding="utf-8")
    (source_dir / "MANIFEST.MF").write_text(
        "Manifest-Version: 1.0\n"
        "Agent-Class: DzSchemaSwitchAgent\n"
        "Can-Redefine-Classes: false\n"
        "Can-Retransform-Classes: false\n\n",
        encoding="utf-8",
    )


def _parse_agent_result(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _run_switch_hook(ctx: StageRunContext, compose: ComposeContext, schema: str, pid: str) -> dict[str, str]:
    run_id = f"schema-switch-{uuid.uuid4().hex[:8]}"
    run_dir = ctx.project_dir / ".dzsc" / "run" / run_id
    source_dir = run_dir / "java"
    container_dir = f"/tmp/dzsc-{run_id}"
    result_path = f"{container_dir}/result.properties"

    try:
        _write_hook_sources(source_dir)
        _run_compose(
            compose,
            ["exec", "-T", compose.doczilla_service, "sh", "-lc", f"rm -rf {shlex.quote(container_dir)} && mkdir -p {shlex.quote(container_dir)}"],
        )
        _docker_cp(f"{source_dir}/.", _container_id(compose), f"{container_dir}/")

        compile_cmd = textwrap.dedent(
            f"""
            set -eu
            cd {shlex.quote(container_dir)}
            javac -cp '/opt/java/openjdk/lib/tools.jar:/opt/doczilla/lib/*' AttachMain.java DzSchemaSwitchAgent.java
            jar cfm dz-schema-switch-agent.jar MANIFEST.MF DzSchemaSwitchAgent.class AttachMain.class
            java -cp 'dz-schema-switch-agent.jar:/opt/java/openjdk/lib/tools.jar' AttachMain \
              {shlex.quote(pid)} \
              {shlex.quote(container_dir + '/dz-schema-switch-agent.jar')} \
              {shlex.quote(_b64(schema) + ':' + _b64(result_path))}
            cat {shlex.quote(result_path)}
            """
        )
        result = _run_compose(
            compose,
            ["exec", "-T", compose.doczilla_service, "sh", "-lc", compile_cmd],
        )
        return _parse_agent_result(result.stdout)
    finally:
        _run_compose(
            compose,
            ["exec", "-T", compose.doczilla_service, "sh", "-lc", f"rm -rf {shlex.quote(container_dir)}"],
            check=False,
        )
        shutil.rmtree(run_dir, ignore_errors=True)
        rmdir_if_empty(ctx.project_dir / ".dzsc" / "run")
        rmdir_if_empty(ctx.project_dir / ".dzsc")


def _require_doczilla_schema(compose: ComposeContext, schema: str) -> None:
    sql = """
        select
            exists (select 1 from pg_namespace where nspname = {schema}),
            exists (
                select 1 from information_schema.tables
                where table_schema = {schema} and table_name = 'System Settings'
            ),
            exists (
                select 1 from information_schema.tables
                where table_schema = {schema} and table_name = 'System Users'
            )
    """.format(schema=_sql_literal(schema))
    rows = _rows(_psql(compose, sql))
    if not rows or len(rows[0]) < 3 or not _parse_bool(rows[0][0]):
        raise SystemExit(f"schema not found: {schema}")
    if not _parse_bool(rows[0][1]) or not _parse_bool(rows[0][2]):
        raise SystemExit(f"schema '{schema}' does not look like a Doczilla schema")


def run_schema_list(ctx: StageRunContext) -> int:
    compose = _compose_context(ctx)
    current = ""
    try:
        current = _runtime_schema(compose)
    except SystemExit:
        current = ""

    rows: list[list[str]] = []
    for info in _read_schemas(compose):
        rows.append(
            [
                "*" if info.name == current else "",
                info.name,
                "yes" if info.is_doczilla else "no",
                info.version or "-",
                str(info.table_count),
                info.user_count or "-",
                _format_ms(info.last_data_ms),
                _format_time(info.security_log_mtime),
                _format_time(info.last_activity_time),
            ]
        )
    _print_table(
        ["cur", "schema", "doczilla", "version", "tables", "users", "last data change", "security log", "last activity"],
        rows,
    )
    print("note: last activity is max(last data change, security log mtime); read-only usage is not visible in Postgres")
    return 0


def run_schema_current(ctx: StageRunContext) -> int:
    compose = _compose_context(ctx)
    pid = _java_pid(compose)
    print(f"java pid: {pid}")
    print(f"runtime schema: {_runtime_schema(compose, pid)}")
    print(f"compose env Z8_DB_SCHEMA: {_env_schema(compose) or '-'}")
    return 0


def run_schema_switch(ctx: StageRunContext) -> int:
    compose = _compose_context(ctx)
    schema = (ctx.schema_name or "").strip()
    if not schema:
        raise SystemExit("schema_switch requires --schema <name>")
    if not ctx.yes:
        raise SystemExit("schema_switch drops active sessions and connections; pass --yes to confirm")

    _require_doczilla_schema(compose, schema)
    pid = _java_pid(compose)
    current = _runtime_schema(compose, pid)
    if current == schema:
        print(f"runtime schema is already '{schema}'")
        return 0

    result = _run_switch_hook(ctx, compose, schema, pid)
    if result.get("ok") != "true":
        raise SystemExit(result.get("error") or "schema switch agent failed")

    print(f"switched schema: {result.get('previousSchema', current)} -> {result.get('schema', schema)}")
    print(f"sessions dropped: {result.get('sessionsDropped', '0')}")
    print(f"sockets dropped: {result.get('socketsDropped', '0')}")
    print(f"connections closed: {result.get('connectionsClosed', '0')}")
    print(f"runtime schema: {_runtime_schema(compose, pid)}")
    return 0


ATTACH_MAIN_JAVA = r"""
import com.sun.tools.attach.VirtualMachine;

public class AttachMain {
    public static void main(String[] args) throws Exception {
        if (args.length != 3) {
            throw new IllegalArgumentException("usage: AttachMain <pid> <agent-jar> <agent-args>");
        }

        VirtualMachine vm = VirtualMachine.attach(args[0]);
        try {
            vm.loadAgent(args[1], args[2]);
        } finally {
            vm.detach();
        }
    }
}
""".strip() + "\n"


DZ_SCHEMA_SWITCH_AGENT_JAVA = r"""
import java.io.FileOutputStream;
import java.io.PrintWriter;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;

public class DzSchemaSwitchAgent {
    public static void agentmain(String args, Instrumentation instrumentation) throws Exception {
        run(args);
    }

    public static void premain(String args, Instrumentation instrumentation) throws Exception {
        run(args);
    }

    private static void run(String args) throws Exception {
        String[] parsed = parseArgs(args);
        String schema = parsed[0];
        String resultPath = parsed[1];
        PrintWriter result = new PrintWriter(new FileOutputStream(resultPath));

        String previousSchema = "";
        try {
            Class<?> serverConfigClass = Class.forName("org.zenframework.z8.server.config.ServerConfig");
            Field databaseSchemaField = field(serverConfigClass, "databaseSchema");
            previousSchema = (String)databaseSchemaField.get(null);

            Class<?> databaseClass = Class.forName("org.zenframework.z8.server.db.Database");
            Method databaseGet = databaseClass.getMethod("get");
            Object previousDatabase = databaseGet.invoke(null);

            stopScheduler(previousDatabase, previousSchema);
            int sessionsDropped = dropSessions();
            int socketsDropped = dropSockets();
            int connectionsClosed = closeConnections();

            databaseSchemaField.set(null, schema);
            System.setProperty("z8.application.database.schema", schema);

            Object newDatabase = databaseGet.invoke(null);
            startScheduler(newDatabase);

            result.println("ok=true");
            result.println("previousSchema=" + safe(previousSchema));
            result.println("schema=" + safe(schema));
            result.println("sessionsDropped=" + sessionsDropped);
            result.println("socketsDropped=" + socketsDropped);
            result.println("connectionsClosed=" + connectionsClosed);
        } catch (Throwable throwable) {
            result.println("ok=false");
            result.println("previousSchema=" + safe(previousSchema));
            result.println("error=" + safe(throwable.getClass().getName() + ": " + throwable.getMessage()));
            throw throwable;
        } finally {
            result.close();
        }
    }

    private static String[] parseArgs(String args) {
        int separator = args.indexOf(':');
        if (separator <= 0 || separator >= args.length() - 1) {
            throw new IllegalArgumentException("invalid agent args");
        }
        String schema = decode(args.substring(0, separator));
        String resultPath = decode(args.substring(separator + 1));
        return new String[] { schema, resultPath };
    }

    private static String decode(String value) {
        return new String(Base64.getDecoder().decode(value), StandardCharsets.UTF_8);
    }

    private static String safe(String value) {
        return value == null ? "" : value.replace('\n', ' ').replace('\r', ' ');
    }

    private static Field field(Class<?> cls, String name) throws Exception {
        Class<?> current = cls;
        while (current != null) {
            try {
                Field field = current.getDeclaredField(name);
                field.setAccessible(true);
                return field;
            } catch (NoSuchFieldException ignored) {
                current = current.getSuperclass();
            }
        }
        throw new NoSuchFieldException(cls.getName() + "." + name);
    }

    private static void stopScheduler(Object database, String schema) throws Exception {
        Class<?> schedulerClass = Class.forName("org.zenframework.z8.server.base.job.scheduler.Scheduler");
        schedulerClass.getMethod("stop", database.getClass()).invoke(null, database);

        Field schedulersField = field(schedulerClass, "schedulers");
        Field mutexField = field(schedulerClass, "mutex");
        Object mutex = mutexField.get(null);
        synchronized (mutex) {
            Map<?, ?> schedulers = (Map<?, ?>)schedulersField.get(null);
            schedulers.remove(schema);
        }
    }

    private static void startScheduler(Object database) throws Exception {
        Class<?> schedulerClass = Class.forName("org.zenframework.z8.server.base.job.scheduler.Scheduler");
        schedulerClass.getMethod("start", database.getClass()).invoke(null, database);
    }

    private static int dropSessions() throws Exception {
        Object authorityCenter = authorityCenter();
        if (authorityCenter == null) {
            return 0;
        }

        Object sessionManager = field(authorityCenter.getClass(), "sessionManager").get(authorityCenter);
        if (sessionManager == null) {
            return 0;
        }

        synchronized (sessionManager) {
            Map<?, ?> sessions = (Map<?, ?>)field(sessionManager.getClass(), "sessions").get(sessionManager);
            Map<?, ?> userSessions = (Map<?, ?>)field(sessionManager.getClass(), "userSessions").get(sessionManager);
            int count = sessions.size();
            sessions.clear();
            userSessions.clear();
            return count;
        }
    }

    private static int dropSockets() throws Exception {
        Object authorityCenter = authorityCenter();
        if (authorityCenter == null) {
            return 0;
        }

        Object socketManager = field(authorityCenter.getClass(), "socketManager").get(authorityCenter);
        if (socketManager == null) {
            return 0;
        }

        Map<?, ?> sockets = (Map<?, ?>)field(socketManager.getClass(), "sockets").get(socketManager);
        int count = sockets.size();
        sockets.clear();
        return count;
    }

    private static Object authorityCenter() throws Exception {
        Class<?> authorityCenterClass = Class.forName("org.zenframework.z8.auth.AuthorityCenter");
        return field(authorityCenterClass, "instance").get(null);
    }

    private static int closeConnections() throws Exception {
        Class<?> connectionManagerClass = Class.forName("org.zenframework.z8.server.db.ConnectionManager");
        Field mutexField = field(connectionManagerClass, "mutex");
        Field connectionsField = field(connectionManagerClass, "connections");
        Object mutex = mutexField.get(null);

        synchronized (mutex) {
            List<?> connections = (List<?>)connectionsField.get(null);
            int count = connections.size();
            for (Object connection : new ArrayList<Object>(connections)) {
                Method close = connection.getClass().getMethod("close");
                close.invoke(connection);
            }
            connections.clear();
            return count;
        }
    }
}
""".strip() + "\n"


SCHEMA_LIST_STAGE = stage(
    "schema_list",
    "List local docker-compose Postgres schemas with Doczilla metadata",
    aliases=("schema-list", "schemas"),
)(run_schema_list)

SCHEMA_CURRENT_STAGE = stage(
    "schema_current",
    "Show current runtime Doczilla schema from the running JVM",
    aliases=("schema-current",),
)(run_schema_current)

SCHEMA_SWITCH_STAGE = stage(
    "schema_switch",
    "Switch running local docker-compose Doczilla JVM to another Postgres schema",
    aliases=("schema-switch",),
)(run_schema_switch)
