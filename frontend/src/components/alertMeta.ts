/**
 * How each alert category is drawn.
 *
 * The words - label, owner, description, examples - come from the server, so
 * they cannot drift from the classifier. What lives here is presentation only:
 * a glyph, a column abbreviation, and a tone class.
 *
 * Tone is the STRIP GROUP, not the category. Five hues for eight categories,
 * so a colour on this page always means "who owns the first five minutes" and
 * the two categories sharing an owner are told apart by their glyph. Eight
 * hues would be eight things to learn and several of them indistinguishable
 * on a wall display at four metres.
 */

import type { AlarmCategory } from '../api/client';
import type { GlyphKind } from './CategoryGlyph';

export interface CategoryMeta {
  glyph: GlyphKind;
  /** Column head. Three or four characters: the row is eight columns wide. */
  head: string;
  /** Tone class, shared within a strip group. See home.css. */
  tone: string;
}

export const CATEGORY_META: Record<AlarmCategory, CategoryMeta> = {
  power:         { glyph: 'power',         head: 'PWR',  tone: 'pwr' },
  cooling:       { glyph: 'cooling',       head: 'COOL', tone: 'cool' },
  environmental: { glyph: 'environmental', head: 'ENV',  tone: 'cool' },
  it_equipment:  { glyph: 'it_equipment',  head: 'IT',   tone: 'it' },
  network:       { glyph: 'network',       head: 'NET',  tone: 'it' },
  visibility:    { glyph: 'visibility',    head: 'VIS',  tone: 'vis' },
  capacity:      { glyph: 'capacity',      head: 'CAP',  tone: 'cap' },
};

/** Tone per strip group, for the counters themselves. */
export const GROUP_TONE: Record<string, string> = {
  power: 'pwr',
  cooling_env: 'cool',
  it_network: 'it',
  visibility: 'vis',
  capacity: 'cap',
};

/** A category the UI has never heard of still has to render.
 *
 *  The server owns the taxonomy; if it grows an eighth category before this
 *  file learns about it, the counter must appear as an unknown rather than
 *  vanish. A category that is countable server-side and invisible here is a
 *  number nobody can reconcile. */
export const UNKNOWN_META: CategoryMeta = {
  glyph: 'alarms', head: '???', tone: 'unc',
};

export function metaFor(key: string): CategoryMeta {
  return CATEGORY_META[key as AlarmCategory] ?? UNKNOWN_META;
}

/** Column order for the table: by owner, power first.
 *
 *  Reads down the failure chain an operator thinks in - the electrical supply,
 *  then the heat, then what is being powered and cooled, then whether we can
 *  see any of it - rather than alphabetically. */
export const COLUMN_ORDER: AlarmCategory[] = [
  'power', 'cooling', 'environmental', 'it_equipment', 'network',
  'visibility', 'capacity',
];
