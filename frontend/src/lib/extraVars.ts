/** Extra vars as typed: a JSON object, or `key=value` pairs (one per line or space-separated). */
export function parseExtraVars(text: string): { vars: Record<string, unknown> } | { error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { vars: {} };
  if (trimmed.startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed))
        return { vars: parsed as Record<string, unknown> };
      return { error: "Extra vars JSON must be an object" };
    } catch (e) {
      return { error: `Extra vars are not valid JSON: ${(e as Error).message}` };
    }
  }
  const vars: Record<string, unknown> = {};
  for (const pair of trimmed.split(/\s+/)) {
    const eq = pair.indexOf("=");
    if (eq <= 0) return { error: `Extra vars: expected key=value, got "${pair}"` };
    vars[pair.slice(0, eq)] = pair.slice(eq + 1);
  }
  return { vars };
}
