// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { WorkflowTemplate } from './types';

export const REGISTRY_TEMPLATES: WorkflowTemplate[] = [
  {
    id: 'order-triage-support',
    name: 'OrderSupportWorkflow',
    title: 'Customer Order Triage & Support',
    description:
      'Full 4-node support lifecycle: order lookup, value-based branching, human-approval refund gate, and synthetic agent response.',
    category: 'support',
    is_synthetic: true,
    required_modules: [
      'synthetic_order_lookup',
      'synthetic_router',
      'synthetic_ticket_creator',
      'synthetic_fake_agent',
    ],
    spec: {
      name: 'OrderSupportWorkflow',
      entry_node: 'lookup',
      nodes: {
        lookup: {
          id: 'lookup',
          handler: 'synthetic_order_lookup',
          default_transition: 'route',
          transitions: {},
          requires_approval: false,
        },
        route: {
          id: 'route',
          handler: 'synthetic_router',
          default_transition: 'approval_gate',
          transitions: {
            refund: 'approval_gate',
            standard_support: 'agent_assist',
          },
          requires_approval: false,
        },
        approval_gate: {
          id: 'approval_gate',
          handler: 'synthetic_ticket_creator',
          default_transition: 'agent_assist',
          transitions: {},
          requires_approval: true,
        },
        agent_assist: {
          id: 'agent_assist',
          handler: 'synthetic_fake_agent',
          default_transition: undefined,
          transitions: {},
          requires_approval: false,
        },
      },
    },
  },
  {
    id: 'bilingual-greeting',
    name: 'BilingualGreetingWorkflow',
    title: 'Bilingual Customer Greeting',
    description:
      '2-node localized workflow: retrieves synthetic customer order and formulates polite bilingual Devanagari Hindi support message.',
    category: 'nlp',
    is_synthetic: true,
    required_modules: ['synthetic_order_lookup', 'synthetic_fake_agent'],
    spec: {
      name: 'BilingualGreetingWorkflow',
      entry_node: 'lookup_order',
      nodes: {
        lookup_order: {
          id: 'lookup_order',
          handler: 'synthetic_order_lookup',
          default_transition: 'agent_assist',
          transitions: {},
          requires_approval: false,
        },
        agent_assist: {
          id: 'agent_assist',
          handler: 'synthetic_fake_agent',
          default_transition: undefined,
          transitions: {},
          requires_approval: false,
        },
      },
    },
  },
  {
    id: 'order-router',
    name: 'OrderRoutingWorkflow',
    title: 'High-Value Order Decision Router',
    description:
      'Fast 2-node decision graph: retrieves synthetic order and routes execution based on the ₹2,000 refund threshold.',
    category: 'routing',
    is_synthetic: true,
    required_modules: ['synthetic_order_lookup', 'synthetic_router'],
    spec: {
      name: 'OrderRoutingWorkflow',
      entry_node: 'lookup',
      nodes: {
        lookup: {
          id: 'lookup',
          handler: 'synthetic_order_lookup',
          default_transition: 'route',
          transitions: {},
          requires_approval: false,
        },
        route: {
          id: 'route',
          handler: 'synthetic_router',
          default_transition: undefined,
          transitions: {},
          requires_approval: false,
        },
      },
    },
  },
];

/**
 * Fixture with an unsupported/incompatible module used for negative testing.
 * Guarantees that templates are strictly validated and unsupported versions fail closed.
 */
export const INCOMPATIBLE_TEST_TEMPLATE: WorkflowTemplate = {
  id: 'incompatible-module-template',
  name: 'IncompatibleBillingWorkflow',
  title: 'Incompatible Billing Workflow (Test Fixture)',
  description:
    'Test fixture referencing unregistered module handler; must be rejected by server validation.',
  category: 'testing',
  is_synthetic: false,
  required_modules: ['nonexistent.billing.v99'],
  spec: {
    name: 'IncompatibleBillingWorkflow',
    entry_node: 'unsupported_step',
    nodes: {
      unsupported_step: {
        id: 'unsupported_step',
        handler: 'nonexistent.billing.v99',
        default_transition: undefined,
        transitions: {},
        requires_approval: false,
      },
    },
  },
};
