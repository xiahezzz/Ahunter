export type LineChange = {
  kind: "same" | "removed" | "added";
  text: string;
  oldLine: number | null;
  newLine: number | null;
};

const MAX_MATRIX_CELLS = 100_000;

function fallbackDiff(before: string[], after: string[]): LineChange[] {
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < before.length - prefix &&
    suffix < after.length - prefix &&
    before[before.length - suffix - 1] === after[after.length - suffix - 1]
  ) suffix += 1;

  const changes: LineChange[] = [];
  for (let index = 0; index < prefix; index += 1) {
    changes.push({ kind: "same", text: before[index], oldLine: index + 1, newLine: index + 1 });
  }
  for (let index = prefix; index < before.length - suffix; index += 1) {
    changes.push({ kind: "removed", text: before[index], oldLine: index + 1, newLine: null });
  }
  for (let index = prefix; index < after.length - suffix; index += 1) {
    changes.push({ kind: "added", text: after[index], oldLine: null, newLine: index + 1 });
  }
  for (let offset = suffix; offset > 0; offset -= 1) {
    const oldIndex = before.length - offset;
    const newIndex = after.length - offset;
    changes.push({ kind: "same", text: before[oldIndex], oldLine: oldIndex + 1, newLine: newIndex + 1 });
  }
  return changes;
}

export function diffLines(beforeText: string, afterText: string): LineChange[] {
  const before = beforeText.split("\n");
  const after = afterText.split("\n");
  if (before.length * after.length > MAX_MATRIX_CELLS) return fallbackDiff(before, after);

  const lengths = Array.from({ length: before.length + 1 }, () => new Uint32Array(after.length + 1));
  for (let left = before.length - 1; left >= 0; left -= 1) {
    for (let right = after.length - 1; right >= 0; right -= 1) {
      lengths[left][right] = before[left] === after[right]
        ? lengths[left + 1][right + 1] + 1
        : Math.max(lengths[left + 1][right], lengths[left][right + 1]);
    }
  }

  const changes: LineChange[] = [];
  let left = 0;
  let right = 0;
  while (left < before.length && right < after.length) {
    if (before[left] === after[right]) {
      changes.push({ kind: "same", text: before[left], oldLine: left + 1, newLine: right + 1 });
      left += 1;
      right += 1;
    } else if (lengths[left + 1][right] >= lengths[left][right + 1]) {
      changes.push({ kind: "removed", text: before[left], oldLine: left + 1, newLine: null });
      left += 1;
    } else {
      changes.push({ kind: "added", text: after[right], oldLine: null, newLine: right + 1 });
      right += 1;
    }
  }
  while (left < before.length) {
    changes.push({ kind: "removed", text: before[left], oldLine: left + 1, newLine: null });
    left += 1;
  }
  while (right < after.length) {
    changes.push({ kind: "added", text: after[right], oldLine: null, newLine: right + 1 });
    right += 1;
  }
  return changes;
}
