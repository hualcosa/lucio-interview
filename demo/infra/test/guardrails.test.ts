import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { describe, expect, it } from 'vitest';
import { GuardrailsStack } from '../lib/guardrails-stack.js';

/**
 * Guardrails asserted, not assumed. A control nobody verifies quietly
 * disappears by the third sprint.
 */
function synth() {
  const app = new cdk.App();
  const stack = new GuardrailsStack(app, 'Test', {
    notifyEmail: 'nobody@example.com',
    monthlyLimitUsd: 40,
  });
  return Template.fromStack(stack);
}

describe('GuardrailsStack', () => {
  it('caps monthly spend', () => {
    synth().hasResourceProperties('AWS::Budgets::Budget', {
      Budget: {
        BudgetType: 'COST',
        TimeUnit: 'MONTHLY',
        BudgetLimit: { Amount: 40, Unit: 'USD' },
      },
    });
  });

  it('warns on forecast, not only after the money is gone', () => {
    const budgets = synth().findResources('AWS::Budgets::Budget');
    const notifications = Object.values(budgets)[0].Properties.NotificationsWithSubscribers;
    const types = notifications.map((n: any) => n.Notification.NotificationType);

    expect(types).toContain('FORECASTED');
    expect(types).toContain('ACTUAL');
  });

  it('routes every alarm to a human', () => {
    const budgets = synth().findResources('AWS::Budgets::Budget');
    const notifications = Object.values(budgets)[0].Properties.NotificationsWithSubscribers;

    for (const n of notifications) {
      expect(n.Subscribers.length).toBeGreaterThan(0);
      expect(n.Subscribers[0].Address).toBe('nobody@example.com');
    }
  });
});
