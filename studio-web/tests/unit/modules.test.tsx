import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SchemaForm } from '../../src/features/modules/SchemaForm';
import { InputMappingForm } from '../../src/features/modules/InputMappingForm';
import {
  isPotentialSecret,
  formatConfigSummary,
} from '../../src/features/modules/types';
import type { JsonSchema } from '../../src/features/modules/types';

describe('Schema-Driven Module Configuration Engine (F05)', () => {
  const sampleConfigSchema: JsonSchema = {
    type: 'object',
    properties: {
      model_name: {
        type: 'string',
        title: 'Model Identifier',
        enum: ['fake-model-v1', 'gpt-4o-mini', 'claude-3-5-sonnet'],
        default: 'fake-model-v1',
      },
      temperature: {
        type: 'number',
        title: 'Temperature',
        default: 0.7,
        minimum: 0,
        maximum: 2,
      },
      max_tokens: {
        type: 'integer',
        title: 'Max Tokens',
        default: 2048,
      },
      enable_cache: {
        type: 'boolean',
        title: 'Enable Prompt Caching',
        default: true,
      },
      default_headers: {
        type: 'object',
        title: 'Default Headers',
        properties: {
          organization_id: { type: 'string', title: 'Organization ID' },
          client_tag: { type: 'string', title: 'Client Tag' },
        },
      },
      allowed_hosts: {
        type: 'array',
        title: 'Allowed Hosts',
        items: { type: 'string' },
      },
    },
    required: ['model_name'],
  };

  const sampleInputSchema: JsonSchema = {
    type: 'object',
    properties: {
      prompt: {
        type: 'string',
        description: 'Input user prompt for model generation',
      },
      context_docs: {
        type: 'array',
        description: 'Retrieved context chunks',
      },
    },
    required: ['prompt'],
  };

  it('renders schema form fields and applies default values', () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={sampleConfigSchema}
        value={{}}
        onChange={onChange}
        title="Model Settings"
      />
    );

    // Header and field count
    expect(screen.getByText('Model Settings')).toBeInTheDocument();
    expect(screen.getByText('6 fields')).toBeInTheDocument();

    // Verify fields rendered
    expect(screen.getByLabelText('Model Identifier')).toBeInTheDocument();
    expect(screen.getByLabelText('Temperature')).toBeInTheDocument();
    expect(screen.getByLabelText('Enable Prompt Caching')).toBeInTheDocument();

    // Apply defaults button
    const applyBtn = screen.getByRole('button', { name: /Apply Defaults/i });
    fireEvent.click(applyBtn);

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        model_name: 'fake-model-v1',
        temperature: 0.7,
        max_tokens: 2048,
        enable_cache: true,
      })
    );
  });

  it('updates form values on input change and preserves unknown custom keys', () => {
    const onChange = vi.fn();
    const initialConfig = {
      model_name: 'fake-model-v1',
      custom_plugin_flag: 'preserved-custom-val',
    };

    render(
      <SchemaForm
        schema={sampleConfigSchema}
        value={initialConfig}
        onChange={onChange}
      />
    );

    // Verify unknown custom key displayed in preserved custom fields badge
    expect(screen.getByText(/Preserved Custom Fields/i)).toBeInTheDocument();
    expect(screen.getByText('custom_plugin_flag: preserved-custom-val')).toBeInTheDocument();

    // Modify temperature
    const tempInput = screen.getByLabelText('Temperature');
    fireEvent.change(tempInput, { target: { value: '1.2' } });

    // Preserved custom key must remain in the emitted object
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        model_name: 'fake-model-v1',
        custom_plugin_flag: 'preserved-custom-val',
        temperature: 1.2,
      })
    );
  });

  it('toggles Advanced JSON mode and reports syntax errors', () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={sampleConfigSchema}
        value={{ model_name: 'gpt-4o-mini' }}
        onChange={onChange}
      />
    );

    const toggleBtn = screen.getByRole('button', { name: /Toggle Advanced JSON editor/i });
    fireEvent.click(toggleBtn);

    const textarea = screen.getByLabelText('Advanced JSON configuration');
    expect(textarea).toBeInTheDocument();

    // Type invalid JSON
    fireEvent.change(textarea, { target: { value: '{ invalid: ' } });
    expect(screen.getByText(/Syntax Error:/i)).toBeInTheDocument();

    // Type valid JSON and blur
    fireEvent.change(textarea, { target: { value: JSON.stringify({ model_name: 'claude-3-5-sonnet', budget: 10 }) } });
    fireEvent.blur(textarea);

    expect(onChange).toHaveBeenCalledWith({
      model_name: 'claude-3-5-sonnet',
      budget: 10,
    });
  });

  it('InputMappingForm lists ports and handles port binding additions/removals', () => {
    const onChange = vi.fn();
    render(
      <InputMappingForm
        inputSchema={sampleInputSchema}
        mapping={{ prompt: 'user_query' }}
        onChange={onChange}
      />
    );

    expect(screen.getByText('Input Mappings')).toBeInTheDocument();
    expect(screen.getByText('prompt')).toBeInTheDocument();
    expect(screen.getByText('* Required')).toBeInTheDocument();

    // Update existing port mapping
    const promptInput = screen.getByLabelText('Input mapping for prompt');
    fireEvent.change(promptInput, { target: { value: 'custom_state_prompt' } });

    expect(onChange).toHaveBeenCalledWith({
      prompt: 'custom_state_prompt',
    });

    // Add custom port mapping
    const portNameInput = screen.getByLabelText('Custom port name');
    const stateKeyInput = screen.getByLabelText('Custom state key');
    const addBtn = screen.getByRole('button', { name: /\+ Map/i });

    fireEvent.change(portNameInput, { target: { value: 'extra_doc' } });
    fireEvent.change(stateKeyInput, { target: { value: 'state.docs' } });
    fireEvent.click(addBtn);

    expect(onChange).toHaveBeenCalledWith({
      prompt: 'user_query',
      extra_doc: 'state.docs',
    });
  });

  it('detects potential raw secrets and displays security warnings', () => {
    // isPotentialSecret regex unit assertions
    expect(isPotentialSecret('sk-proj-123456789012345678901234567890')).toBe(true);
    expect(isPotentialSecret('Bearer secret_token_12345678901234567890')).toBe(true);
    expect(isPotentialSecret('ghp_abcdefghijklmnopqrstuvwxyz0123456789')).toBe(true);
    expect(isPotentialSecret('normal-model-name')).toBe(false);
    expect(isPotentialSecret('ORD-DELHI-501')).toBe(false);

    // SchemaField visual rendering with raw secret warning
    const schemaWithSecret: JsonSchema = {
      type: 'object',
      properties: {
        api_token: { type: 'string', title: 'API Key Token' },
      },
    };

    render(
      <SchemaForm
        schema={schemaWithSecret}
        value={{ api_token: 'sk-proj-123456789012345678901234567890' }}
        onChange={vi.fn()}
      />
    );

    expect(screen.getByText(/Security Notice:/i)).toBeInTheDocument();
    expect(screen.getByText(/This field looks like a raw secret or API key/i)).toBeInTheDocument();
  });

  it('formatConfigSummary produces concise summary for visual canvas node badges', () => {
    expect(formatConfigSummary(undefined)).toBeNull();
    expect(formatConfigSummary({})).toBeNull();

    const summary = formatConfigSummary({
      model: 'fake-model-v1',
      max_tokens: 2048,
      temperature: 0.7,
    });

    expect(summary).toBe('model: fake-model-v1, max_tokens: 2048');

    const nestedSummary = formatConfigSummary({
      headers: { 'X-Auth': 'token', 'X-Env': 'test' },
      timeout: 30,
    });

    expect(nestedSummary).toBe('headers: {2}, timeout: 30');
  });
});
