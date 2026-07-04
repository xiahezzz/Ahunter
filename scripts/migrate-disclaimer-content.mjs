import { openEventStore } from "../src/events/event-store.mjs";
import { migrateDisclaimerContent } from "../src/events/migrate-disclaimer-content.mjs";

function usageError(message) {
  const error = new Error(message);
  error.code = "migration_usage";
  return error;
}

function parseDatabasePath(args) {
  let databasePath = "data/state/events.sqlite";
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] !== "--database") throw usageError(`Unexpected argument: ${args[index]}`);
    const value = args[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw usageError("--database requires a value");
    }
    databasePath = value;
    index += 1;
  }
  return databasePath;
}

let store;
try {
  const databasePath = parseDatabasePath(process.argv.slice(2));
  store = openEventStore(databasePath);
  const report = migrateDisclaimerContent(store.database);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} catch (error) {
  const message = error?.code === "migration_usage" ? error.message : "Disclaimer migration failed";
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
} finally {
  store?.close();
}
