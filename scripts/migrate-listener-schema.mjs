import {
  ListenerSchemaMigrationError,
  auditListenerDatabase,
  migrateListenerDatabase,
} from "../src/events/migrate-listener-schema.mjs";

function usageError() {
  return new ListenerSchemaMigrationError("migration_usage");
}

function parseArguments(args) {
  let mode = null;
  let database = null;
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "--check" || argument === "--apply") {
      if (mode !== null) throw usageError();
      mode = argument.slice(2);
      continue;
    }
    if (argument === "--database") {
      if (database !== null) throw usageError();
      const value = args[index + 1];
      if (!value || value.startsWith("--")) throw usageError();
      database = value;
      index += 1;
      continue;
    }
    throw usageError();
  }
  if (mode === null || database === null) throw usageError();
  return { mode, database };
}

try {
  const { mode, database } = parseArguments(process.argv.slice(2));
  const report = mode === "check"
    ? auditListenerDatabase(database)
    : migrateListenerDatabase(database);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} catch (error) {
  const code = error instanceof ListenerSchemaMigrationError
    ? error.code
    : "listener_migration_failed";
  if (code === "migration_usage") {
    process.stderr.write(
      "Usage: migrate-listener-schema.mjs (--check|--apply) --database <path>\n",
    );
  } else {
    process.stderr.write(`Listener schema operation failed (${code})\n`);
  }
  process.exitCode = 1;
}
