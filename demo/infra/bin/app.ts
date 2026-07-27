#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { GuardrailsStack } from '../lib/guardrails-stack.js';

const app = new cdk.App();

// Account is never hardcoded: this repo is public.
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

const notifyEmail = app.node.tryGetContext('notifyEmail') ?? process.env.BUDGET_EMAIL;
if (!notifyEmail) {
  throw new Error(
    'Set BUDGET_EMAIL (or -c notifyEmail=...) so cost alarms reach a human. ' +
      'Refusing to deploy spendable infrastructure without it.',
  );
}

new GuardrailsStack(app, 'MlsAgentGuardrails', {
  env,
  notifyEmail,
  monthlyLimitUsd: Number(app.node.tryGetContext('monthlyLimitUsd') ?? 40),
  description: 'Cost guardrails. Deploy before anything that can spend.',
});

cdk.Tags.of(app).add('project', 'mls-agent-demo');
cdk.Tags.of(app).add('owner', 'take-home');
