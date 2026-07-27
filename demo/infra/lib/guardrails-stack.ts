import * as cdk from 'aws-cdk-lib';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import type { Construct } from 'constructs';

export interface GuardrailsStackProps extends cdk.StackProps {
  /** Where budget alarms are sent. */
  readonly notifyEmail: string;
  /** Monthly ceiling in USD. */
  readonly monthlyLimitUsd: number;
}

/**
 * Cost guardrails. Deployed FIRST, before anything that can spend money.
 *
 * Two notifications rather than one: ACTUAL tells you what already happened,
 * FORECASTED tells you in time to do something about it. Only the second one
 * is actually useful, which is why it fires at a lower threshold.
 */
export class GuardrailsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: GuardrailsStackProps) {
    super(scope, id, props);

    const subscriber: budgets.CfnBudget.SubscriberProperty = {
      subscriptionType: 'EMAIL',
      address: props.notifyEmail,
    };

    new budgets.CfnBudget(this, 'MonthlyBudget', {
      budget: {
        budgetName: 'mls-agent-demo',
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: props.monthlyLimitUsd, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        {
          // Early warning, while it can still be acted on.
          notification: {
            notificationType: 'FORECASTED',
            comparisonOperator: 'GREATER_THAN',
            threshold: 50,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [subscriber],
        },
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 80,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [subscriber],
        },
      ],
    });

    new cdk.CfnOutput(this, 'BudgetLimit', {
      value: `$${props.monthlyLimitUsd}/month, alerting ${props.notifyEmail}`,
    });
  }
}
