// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/** One skill in the list: the newest version's description plus label to version pointers. */
export interface SkillSummary {
  name: string;
  description: string;
  latest_version: number;
  version_count: number;
  labels: Record<string, number>;
}

/** One stored version. Every field is plain text; show it as text, never as HTML. */
export interface SkillVersionView {
  name: string;
  version: number;
  labels: string[];
  description: string;
  instructions: string;
  tools: string[];
  resources: Record<string, string>;
  sha256: string;
}

export interface SkillList {
  items: SkillSummary[];
  total: number;
}

export interface SkillVersions extends SkillSummary {
  versions: SkillVersionView[];
}
