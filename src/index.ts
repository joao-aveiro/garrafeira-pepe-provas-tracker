/**
 * Polls Garrafeira Pepe's Amelia booking API on a Cloudflare Workers cron
 * schedule and posts a Telegram message for every newly published wine
 * tasting whose first period is in the future.
 *
 * State (the set of event IDs already notified) lives in Workers KV under
 * the "state" key. Failed Telegram sends are NOT marked seen, so the next
 * cron tick retries them.
 */

export interface Env {
  STATE: KVNamespace;
  AMELIA_URL: string;
  PROVAS_URL: string;
  USER_AGENT: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID: string;
}

interface AmeliaPeriod {
  periodStart: string;
  periodEnd: string;
}

export interface AmeliaEvent {
  id: number;
  name: string;
  price: number;
  description: string;
  periods: AmeliaPeriod[];
}

interface State {
  seen_ids: number[];
  last_check?: string;
}

const HTTP_TIMEOUT_MS = 30_000;
const MAX_PAGES = 100;
const TELEGRAM_MAX_RETRIES = 3;
const STATE_KEY = "state";
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
// Intro extraction: stop collecting paragraphs once we have this many chars
// of meaningful prose, then hard-truncate at the max.
const INTRO_LENGTH_TARGET = 200;
const INTRO_MAX_CHARS = 500;

export async function fetchAmeliaEvents(env: Env): Promise<AmeliaEvent[]> {
  const events: AmeliaEvent[] = [];
  for (let page = 1; page <= MAX_PAGES; page++) {
    const resp = await fetch(`${env.AMELIA_URL}&page=${page}`, {
      headers: { "User-Agent": env.USER_AGENT },
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    });
    if (!resp.ok) {
      throw new Error(`Amelia HTTP ${resp.status} on page ${page}`);
    }
    const payload = (await resp.json()) as { data?: { events?: AmeliaEvent[] } };
    const pageEvents = payload?.data?.events ?? [];
    if (pageEvents.length === 0) break;
    events.push(...pageEvents);
  }
  return events;
}

async function loadState(env: Env): Promise<State> {
  const raw = await env.STATE.get(STATE_KEY);
  if (!raw) return { seen_ids: [] };
  try {
    return JSON.parse(raw) as State;
  } catch {
    return { seen_ids: [] };
  }
}

async function saveState(env: Env, state: State): Promise<void> {
  state.seen_ids = [...new Set(state.seen_ids)].sort((a, b) => a - b);
  await env.STATE.put(STATE_KEY, JSON.stringify(state));
}

/** "YYYY-MM-DD HH:MM:SS" formatted in Europe/Lisbon. */
function nowLisbonString(): string {
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Europe/Lisbon",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
  return parts.replace("T", " ");
}

function isFuture(event: AmeliaEvent, nowLisbon: string): boolean {
  for (const p of event.periods ?? []) {
    if (p.periodStart && p.periodStart >= nowLisbon) return true;
  }
  return false;
}

function stripHtml(text: string): string {
  return text
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/\s+/g, " ")
    .trim();
}

function extractWines(description: string): string[] {
  const items = [...(description ?? "").matchAll(/<li[^>]*>([\s\S]*?)<\/li>/gi)].map(
    (m) => m[1],
  );
  const cleaned: string[] = [];
  for (const raw of items) {
    // Cut at first internal paragraph break so trailing prose nested in
    // the last <li> doesn't leak into the wine name.
    const cut = raw.split(/(?:<br\s*\/?>\s*){2,}|<\/p>\s*<p/i)[0] ?? "";
    const text = stripHtml(cut);
    if (text) cleaned.push(text);
  }
  return cleaned;
}

function extractIntro(description: string, maxChars = INTRO_MAX_CHARS): string {
  const paragraphs = [
    ...(description ?? "").matchAll(/<p[^>]*>([\s\S]*?)<\/p>/gi),
  ].map((m) => m[1]);
  const pieces: string[] = [];
  for (const raw of paragraphs) {
    const text = stripHtml(raw);
    if (!text) continue;
    if (/\b(Local|Data|Hor[áa]rio|Valor|Vagas|Inscri[çc][ãa]o)\b\s*:/i.test(text)) {
      continue;
    }
    pieces.push(text);
    if (pieces.reduce((acc, p) => acc + p.length, 0) >= INTRO_LENGTH_TARGET) break;
  }
  let text = pieces.join(" ");
  if (text.length > maxChars) {
    text = text.slice(0, maxChars).replace(/\s+\S*$/, "") + "...";
  }
  return text;
}

function htmlEscape(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function formatWhen(startStr: string, endStr: string | null | undefined): string {
  // "YYYY-MM-DD HH:MM:SS" -> "Mon, DD/MM/YYYY HH:MM-HH:MM"
  const m = startStr?.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):\d{2}$/);
  if (!m) return "?";
  const [, y, mo, d, h, mi] = m;
  const calDate = new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d)));
  const wd = WEEKDAYS[(calDate.getUTCDay() + 6) % 7];
  let out = `${wd}, ${d}/${mo}/${y} ${h}:${mi}`;
  const e = endStr?.match(/(\d{2}):(\d{2}):\d{2}$/);
  if (e) out += `-${e[1]}:${e[2]}`;
  return out;
}

export function formatMessage(event: AmeliaEvent, env: Env): string {
  const name = htmlEscape(event.name ?? "(untitled)");
  const start = event.periods?.[0]?.periodStart;
  const end = event.periods?.[0]?.periodEnd;
  const lines = [`🍷 <b>New tasting: ${name}</b>`, `📅 ${formatWhen(start, end)}`];
  if (event.price != null) lines.push(`💶 ${event.price}€`);

  const intro = extractIntro(event.description ?? "");
  if (intro) {
    lines.push("");
    lines.push(htmlEscape(intro));
  }

  const wines = extractWines(event.description ?? "");
  if (wines.length > 0) {
    lines.push("");
    lines.push("<b>Wines:</b>");
    for (const w of wines) lines.push(`• ${htmlEscape(w)}`);
  }

  lines.push("");
  lines.push(`<a href="${env.PROVAS_URL}">Book →</a>`);
  return lines.join("\n");
}

export async function sendTelegram(env: Env, text: string): Promise<void> {
  const body = new URLSearchParams({
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: "true",
  });

  for (let attempt = 0; attempt <= TELEGRAM_MAX_RETRIES; attempt++) {
    const resp = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
        signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
      },
    );

    if (resp.ok) {
      const json = (await resp.json()) as { ok?: boolean };
      if (!json.ok) throw new Error(`Telegram rejected: ${JSON.stringify(json)}`);
      return;
    }

    if (resp.status === 429 && attempt < TELEGRAM_MAX_RETRIES) {
      let retryAfter = 2 ** attempt;
      try {
        const json = (await resp.json()) as {
          parameters?: { retry_after?: number };
        };
        if (json.parameters?.retry_after) retryAfter = json.parameters.retry_after;
      } catch {
        /* fall back to exponential backoff */
      }
      console.warn(
        `Telegram 429; sleeping ${retryAfter}s before retry ${attempt + 1}/${TELEGRAM_MAX_RETRIES}`,
      );
      await new Promise((r) => setTimeout(r, retryAfter * 1000));
      continue;
    }

    throw new Error(`Telegram HTTP ${resp.status}`);
  }
}

async function runOnce(env: Env): Promise<void> {
  const events = await fetchAmeliaEvents(env);
  const state = await loadState(env);
  const seenBefore = new Set(state.seen_ids);
  const now = nowLisbonString();

  const apiIds = new Set(events.map((e) => e.id).filter((id) => id != null));
  const newIds = new Set([...apiIds].filter((id) => !seenBefore.has(id)));
  const newEvents = events.filter((e) => newIds.has(e.id));

  const notified = new Set<number>();
  const failed = new Set<number>();
  const skippedPast = new Set<number>();

  for (const event of newEvents) {
    if (!isFuture(event, now)) {
      skippedPast.add(event.id);
      continue;
    }
    try {
      await sendTelegram(env, formatMessage(event, env));
      notified.add(event.id);
    } catch (exc) {
      console.error(`Notification failed for id=${event.id}: ${exc}`);
      failed.add(event.id);
    }
  }

  // Mark seen only what we actually handled. Failed events stay out so the
  // next tick retries them.
  state.seen_ids = [...new Set([...seenBefore, ...notified, ...skippedPast])];
  state.last_check = new Date().toISOString();
  await saveState(env, state);

  console.log(
    `events=${events.length} new=${newEvents.length} notified=${notified.size} ` +
      `failed=${failed.size} past=${skippedPast.size}`,
  );
}

export default {
  async scheduled(_event: ScheduledController, env: Env, _ctx: ExecutionContext) {
    // Awaiting directly (rather than ctx.waitUntil) makes the scheduled
    // invocation surface as failed in the dashboard if runOnce throws,
    // instead of the error only ending up in logs.
    await runOnce(env);
  },
};
