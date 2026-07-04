import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";

import { MX_DISCLAIMER } from "../../src/ingestion/content-normalizer.mjs";

// Keep this SQL synchronized with the operations manual. The assertion below
// is an intentional drift guard for this executable copy of operator guidance.
const verificationSql = `
SELECT
  coalesce((
    SELECT count(*)
    FROM events AS e
    JOIN json_tree(e.parsed_content_json, '$.parsed') AS node
    WHERE node.type = 'text'
      AND trim(node.atom, char(9) || char(10) || char(11) || char(12) || char(13) || char(32)) = '${MX_DISCLAIMER}'
      AND (node.parent IS NULL OR typeof(node.key) = 'integer' OR node.key = 'msg')
  ), 0) AS parsed_removable_matches,
  coalesce((
    SELECT count(*)
    FROM events AS e
    JOIN json_each(e.parsed_content_json, '$.texts') AS text
    WHERE text.type = 'text'
      AND trim(text.atom, char(9) || char(10) || char(11) || char(12) || char(13) || char(32)) = '${MX_DISCLAIMER}'
  ), 0) AS extracted_text_matches;
`;

function normalizedSql(value) {
  return value.replaceAll(/\s+/g, " ").trim();
}

test("documented disclaimer verification counts only exact removable JSON nodes", async () => {
  const manual = await readFile("docs/mx-listener-operations-manual.md", "utf8");
  assert.ok(
    normalizedSql(manual).includes(normalizedSql(verificationSql)),
    "operations manual must contain the executable verification SQL",
  );

  const database = new DatabaseSync(":memory:");
  database.exec("CREATE TABLE events(parsed_content_json TEXT NOT NULL)");
  const insert = database.prepare("INSERT INTO events(parsed_content_json) VALUES (?)");
  const quote = `正文引用：${MX_DISCLAIMER}`;
  insert.run(JSON.stringify({ parsed: quote, texts: [quote] }));
  insert.run(JSON.stringify({ parsed: { attribution: MX_DISCLAIMER }, texts: [] }));
  insert.run(JSON.stringify({ parsed: [MX_DISCLAIMER], texts: [MX_DISCLAIMER] }));
  insert.run(JSON.stringify({ parsed: { type: "text", msg: `  ${MX_DISCLAIMER}\n` }, texts: [] }));
  insert.run(JSON.stringify({ parsed: MX_DISCLAIMER, texts: [] }));

  assert.deepEqual({ ...database.prepare(verificationSql).get() }, {
    parsed_removable_matches: 3,
    extracted_text_matches: 1,
  });
});

test("documented disclaimer verification returns numeric zero for no events", () => {
  const database = new DatabaseSync(":memory:");
  database.exec("CREATE TABLE events(parsed_content_json TEXT NOT NULL)");

  assert.deepEqual({ ...database.prepare(verificationSql).get() }, {
    parsed_removable_matches: 0,
    extracted_text_matches: 0,
  });
});
