---
id: ADR-0010
title: "WhatsApp is reached only through AWS End User Messaging Social"
status: Accepted
date: 2026-10-29
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [provider, security, whatsapp]
---

# ADR-0010: WhatsApp is reached only through AWS End User Messaging Social

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-29 | SMART maintainers |

## Context

Everything the toolkit does on WhatsApp has two possible paths: Meta's WhatsApp Business Platform (the Cloud API, called with a Meta access token held by the deployer) or [AWS End User Messaging Social](https://docs.aws.amazon.com/social-messaging/latest/userguide/what-is-service.html) (EUM Social), the AWS service that fronts the same platform behind the `socialmessaging` API, IAM and SigV4. The choice decides where credentials live, what the stack must store and rotate, which calls the threat model and the audit trail can see, and whether a deployer needs anything from Meta beyond the WhatsApp Business Account itself. Sending, media, templates and phone-number management each have both paths; a toolkit that used Meta's API for some of them and EUM Social for the rest would carry two credential models and two audit surfaces.

## Decision

Every call that reaches WhatsApp goes through EUM Social: sending text, templates and interactive messages, uploading and downloading media, creating, listing and deleting message templates, and reading WhatsApp Business Account and phone-number details. The toolkit holds no Meta access token and makes no HTTP request to a Meta endpoint; the only WhatsApp-facing network client in the Lambda layer is the `socialmessaging` client that `src/socialagent/providers/eum_social.py` builds, and the admin console's backend uses the same service for phone numbers and templates. Inbound messages arrive from EUM Social over Amazon SNS. A change that adds a Meta Graph API call, a Meta token or a Meta webhook endpoint supersedes this record.

## Alternatives considered

- **Meta's Cloud API directly.** One fewer AWS service in the path and the newest WhatsApp features on the day Meta ships them. It costs a long-lived Meta token in Secrets Manager with its own rotation, a Meta app and webhook registration per deployment, outbound HTTP from the Lambda functions to an endpoint outside AWS, and a second trust boundary in the threat model. Rejected.
- **Both, behind a provider interface.** `src/socialagent/providers/` is already an interface, so a second provider is possible. Shipping one would reintroduce every cost above for the deployers who pick it, so the interface stays, and the only implementation is EUM Social. A feature that EUM Social does not expose waits for the service rather than bypassing it.

## Consequences

- Deployers need an AWS account with EUM Social and a WhatsApp Business Account linked to it, and nothing else from Meta: no app, no token, no webhook URL. The Terraform creates the SNS topic and lets EUM Social publish to it; the console's Bind, or the `aws socialmessaging put-whatsapp-business-account-event-destinations` command in [docs/getting-started.md](../getting-started.md), points the WABA at it.
- Credentials are IAM roles; the only secrets the stack keeps are the event API's webhook secret and API key, and the secrets the deployer supplies.
- Features reach the toolkit when EUM Social supports them, which can lag Meta's platform. `SendWhatsAppMessage` passes Meta's message object through, so many message types need no service change; template management follows the service's own API.
- Template, WABA, phone-number and event-destination calls are CloudTrail management events from `social-messaging.amazonaws.com`. `SendWhatsAppMessage` and the media calls are data events: CloudTrail does not log them by default, and the stack's trail does not select them, on purpose, because their records carry the message content. A deployer who needs them adds a data-event selector. The data-flow description ([docs/data-flows.md](../data-flows.md), "WhatsApp delivery through Meta") names one path.

## Open questions

- A WhatsApp capability that EUM Social does not offer is a request against the service, not a reason to add a second path.

## Enforced by

- `tests/test_architecture.py::test_socialmessaging_client_only_in_provider`: the Lambda layer, the agent package, the recipes and the blueprints build a `socialmessaging` client in `src/socialagent/providers/eum_social.py` and nowhere else, checked on the parsed source for a literal service name.
- `tests/test_architecture.py::test_recipes_do_not_import_control_layer` and `tests/test_architecture.py::test_agent_package_does_not_import_control_layer`: agent-side toolkit code never imports the provider; it sends through the notification Lambda.
- `tests/test_terraform_hardening.py::test_runtime_role_has_no_social_messaging_actions`: the AgentCore runtime role grants no `social-messaging:` action, so agent-side code cannot reach EUM Social even with its own client; it sends through the notification Lambda.
- No module calls a messaging endpoint over HTTP. The outbound HTTP the code makes is the SNS signing-certificate fetch, the Transcribe transcript fetch and the console's call to the stack's own event API.
