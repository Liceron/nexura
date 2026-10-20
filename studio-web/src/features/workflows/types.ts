// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { WorkflowSpec, WorkflowRecord } from '@liceron/nexura-sdk';

export interface WorkflowTemplate {
  id: string;
  name: string;
  title: string;
  description: string;
  category: 'support' | 'nlp' | 'routing' | 'testing';
  spec: WorkflowSpec;
  is_synthetic: boolean;
  required_modules: string[];
}

export type { WorkflowRecord };
