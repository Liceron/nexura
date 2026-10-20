// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
export interface TemplateSummary {
  id: string;
  template_version: number;
  title: string;
  description: string;
  category: string;
  difficulty: 'beginner' | 'intermediate' | 'advanced';
  tags: string[];
  required_modules: string[];
  required_credentials: string[];
  node_count: number;
  has_approval: boolean;
  has_trigger: boolean;
}

export interface TemplateList {
  items: TemplateSummary[];
  total: number;
  categories: string[];
  tags: string[];
}

export interface TemplateNode {
  id: string;
  handler?: string;
  transitions?: Record<string, string>;
  default_transition?: string | null;
  requires_approval?: boolean;
}

export interface TemplateDetail extends TemplateSummary {
  spec: {
    name: string;
    entry_node: string;
    nodes: Record<string, TemplateNode>;
    terminal_nodes?: string[];
  };
  mermaid: string;
  sample_input: Record<string, unknown>;
  suggested_trigger: Record<string, unknown> | null;
}

export interface InstantiateResult {
  workflow_id: string;
  template_id: string;
  missing_credentials: string[];
  runs_offline: boolean;
  suggested_trigger: Record<string, unknown> | null;
}
