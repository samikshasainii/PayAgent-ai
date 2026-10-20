---
title: Best practices for prompts
---

# Best practices for prompts (/ai-tools/prompts)



Build effective prompts that unlock the full potential of PayPal AI agents with these tips. To start building AI tools using PayPal'a agent toolkit, see the [agent toolkit quickstart guide](/ai-tools/toolkit/).

| Principle                                          | Usage                                                                                                                                                                                                                                                             |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Be clear about what to do and how to do it.        | Put the action first ("Create an invoice…"), then supply parameter values in the order the tool expects.                                                                                                                                                          |
| Be specific.                                       | Include every required field, such as `currency` and `invoice_id`. Unclear input can cause failure.                                                                                                                                                               |
| Separate complex tasks into simple ones.           | If you want to create a checkout sequence, you might have 3 subtasks: <br /> - Create order. <br /> - Pay order. <br /> - Create shipment.                                                                                                                        |
| Encourage the agent to "think."                    | Begin an analytic request with a statement like "Think step-by-step before calling the tool…". This can help the agent to avoid making bad calls.                                                                                                                 |
| Protect personally identifiable information (PII). | Never embed card numbers, personal financial information like social security numbers (SSNs), passwords, or secrets in prompts. The agent toolkit will reject this information, and you might violate the Payment Card Industry Data Security Standard (PCI DSS). |

## Multistep agent prompt examples [#multistep-agent-prompt-examples]

The following examples provide ideas for how to form multistep prompts for some standard use cases.

### Invoice a client [#invoice-a-client]

To invoice a client, you might have steps like these:

1. `create_invoice`: Create an invoice for [jane@example.com](mailto:jane@example.com) for 3 hours of CAD design at $100 per hour.
2. `send_invoice`: Send invoice `{invoice_id}` to the client.
3. `send_invoice_reminder`: Send a reminder for invoice `{invoice_id}`.

### Create an online order [#create-an-online-order]

For an online order with shipping, you might use these steps in your prompts:

1. `create_order`: Place an order for 2 "Custom Gold Ring" at $450 each (currency USD).
2. `pay_order`: Capture payment for order `{order_id}`.
3. `create_shipment_tracking`: Add tracking 1Z999AA10123456784 with carrier UPS to order `{order_id}`.

### Sell a subscription [#sell-a-subscription]

Subscriptions are a type of product, which come with specific parameters. To sell subscriptions, you might complete these tasks using your agent tools:

1. `create_product`: Create a new product named "Coffee-Club Subscription" with the type of `DIGITAL`.
2. `create_subscription_plan`: Create a `MONTH` subscription plan for product `{product_id}` named "Monthly Roast Plan" at the price of 20 USD per month.
3. `create_subscription`: Create a subscription to plan `{plan_id}` for subscriber Alice Brown whose email address is [alice@example.com](mailto:alice@example.com).

### Evaluate and resolve disputes [#evaluate-and-resolve-disputes]

To manage disputes, you might use prompts like these:

1. `list_disputes`: List all `OPEN` disputes.
2. `get_dispute`: Get details for dispute `{dispute_id}`.
3. `accept_dispute_claim`: Accept the dispute with ID `{dispute_id}`.

### Add and confirm the details for a new product [#add-and-confirm-the-details-for-a-new-product]

When you add a new product, you'll want to confirm that the product appears where and how it should. These prompts could do that:

1. `create_product`: Create a new product called "Limited-Edition Walnut Serving Tray" of type `PHYSICAL`.
2. `list_product`: List all products (`page` 1, `page_size` 25).
3. `show_product_details`: Show the details for product `{product_id}`.

### Create an invoice with a QR code [#create-an-invoice-with-a-qr-code]

This example shows how to invoice a client and use a QR code for on-site payment. To complete this sort of process, you might use the following tools and steps:

1. `create_invoice`: Create an invoice for [bob@example.com](mailto:bob@example.com) for one "Pop-up Booth Entry Fee" at 20 USD.
2. `send_invoice`: Send invoice `{invoice_id}`.
3. `generate_invoice_qr_code`: Generate a QR code for invoice `{invoice_id}`.

### Fix an invoice error [#fix-an-invoice-error]

Correcting an invoice error might require steps like these:

1. `cancel_sent_invoice`: Cancel the `SENT` invoice with ID `{invoice_id}`.
2. `create_invoice`: Create a corrected invoice for [dana@example.com](mailto:dana@example.com): 4 h consulting at $90 USD/h.
3. `send_invoice`: Send invoice `{new_invoice_id}`.

### Reconcile a transaction [#reconcile-a-transaction]

To reconcile a transaction, compare the order details to the transaction itself:

1. `list_transaction`: List of my transactions for the last 14 days.
2. `get_order`: Get the details for order `{order_id}`.

### Cancel a subscription [#cancel-a-subscription]

If a user wants to cancel a subscription, you might need to check the terms of their subscription. To do that, you could use these steps:

1. `show_subscription_details`: Show the details for subscription ID `{subscription_id}`.
2. `cancel_subscription`: Cancel the subscription ID `{subscription_id}`.

## See also [#see-also]

For more information about using AI with PayPal, see [PayPal's AI resources for developers](/ai-tools/get-started/).
